# coding: utf-8

'''
This module contains functions for plotting pressure reading in real time
The plotted data reads hdf5 files saved using Pfeiffer_control.py

Author: Jia Han
Ver1.0 created on: 2021-07-23
'''

from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QLabel, QPushButton, QWidget
from PyQt5.QtCore import QThread, pyqtSignal, QObject
from PyQt5.QtGui import QFont

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar

import os
os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"

import numpy as np
import h5py
import time
import datetime

#===============================================================================================================================================
sensor_numbers = None  # e.g. [1, 2] to show specific channels; None = auto-detect connected gauges
n_points = 10000
#===============================================================================================================================================

def get_latest_file(dir_path=r"C:\data\gauge"):
    """
    This function returns the latest file in a directory.

    Args:
        dir_path (str): The path to the directory.

    Returns:
        str: The path to the latest file.
    """
    file_list = [f for f in os.listdir(dir_path) if f.endswith(".hdf5")]
    full_path_file_list = [os.path.join(dir_path, file) for file in file_list]
    return max(full_path_file_list, key=os.path.getctime)

def read_gauge_model(dataset):
    '''
    gauge model string from the dataset attribute written at file init
    '''
    gauge_id = dataset.attrs['Model'][-1]
    if isinstance(gauge_id, (list, np.ndarray)):
        gauge_id = gauge_id[0]
    if isinstance(gauge_id, bytes):
        gauge_id = gauge_id.decode()
    return str(gauge_id)

def get_data(ifn):
    '''
    read the data from the hdf5 file
    Returns (tarr, sensors) where sensors maps sensor number -> (parr, gauge model)
    '''
    try:
        with h5py.File(ifn, 'r', swmr=True) as f:
            grp = f['PfeifferVacuum']
            tarr = grp['timestamp'][::10]
            if len(tarr) > n_points:
                tarr = tarr[-n_points:]

            channels = sensor_numbers or sorted(int(k) for k in grp.keys() if k.isdigit())
            sensors = {}
            for ch in channels:
                dset = grp[str(ch)]
                gauge_id = read_gauge_model(dset)
                # controller reports "noSen"-type model on channels without a gauge
                if gauge_id.lower().startswith('nosen'):
                    if sensor_numbers is not None:
                        print(f"Channel {ch} requested but controller reports no gauge; skipping")
                    continue
                parr = dset[::10]
                if len(parr) > n_points:
                    parr = parr[-n_points:]
                sensors[ch] = (parr, gauge_id)
            return tarr, sensors
    except Exception as e:
        print("GUI read failed:", e)
        return None, None
#===============================================================================================================================================

class Worker(QObject):
    '''
    Worker function that emits the data to the plotting GUI
    Runs in a separate thread to avoid blocking the GUI
    '''
    data_updated = pyqtSignal(np.ndarray, dict)  # Signal to emit the data

    def __init__(self):
        super().__init__()


    def run(self):
        '''
        Find the latest file and read the last indexed data from it
        '''
        while True:
            try:
                ifn = get_latest_file()
                print("Latest HDF5 file selected:", ifn)
                tarr, sensors = get_data(ifn)

                if isinstance(tarr, np.ndarray) and sensors:
                    self.data_updated.emit(tarr, sensors)
                else:
                    print("Skipping emit due to invalid data types.")
                
                QThread.msleep(500)  # Sleep for 1 second
            except OSError as e:
                if "unable to lock file" in str(e):
                    print("File temporarily locked by writer. Retry in 1s...")
                else:
                    print(f"HDF5 read error: {e}")
                QThread.msleep(1000)




class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__() # Call the parent class constructor

        self.day_caches = {}  # per-sensor 5-min average caches, keyed by sensor number
        self._gui_day = None  # date the caches cover; cleared on day rollover

        #======================== GUI setup ========================
        central_widget = QWidget() # Create a central widget
        self.setCentralWidget(central_widget) # Set the central widget
        self.setGeometry(100,100,500,500)

        # Create a layout for the central widget
        layout = QVBoxLayout(central_widget)
        layout.addWidget(QLabel("Real time pressure reading"))
        # Create a button to start the plot
        button = QPushButton("Start Plot")
        button.setFont(QFont("Arial", 24)) 
        layout.addWidget(button)
        button.clicked.connect(self.start_plot)

        # Create a figure and a canvas for the figure
        self.fig = Figure(figsize=(15,15))
        plt.rcParams['font.size'] = 20
        self.ax_short = self.fig.add_subplot(211)  
        self.ax_day = self.fig.add_subplot(212)
        self.canvas = FigureCanvas(self.fig)  # Create a canvas for the figure
        # Add the navigation toolbar for interacting with plot
        self.toolbar = NavigationToolbar(self.canvas, self)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)  # Add the canvas to the layout
        # Plot lines are created per sensor when its data first arrives
        self.lines_short = {}
        self.lines_day = {}
        # Plot label and title
        self.ax_short.set_title("Pressure (30 Seconds)")
        self.ax_short.set_xlabel("Time")
        self.ax_short.set_ylabel("Pressure (Torr)")
        self.ax_short.set_yscale('log')  # gauges can sit decades apart in pressure
        self.ax_short.grid(True)

        self.ax_day.set_xlabel("Time")
        self.ax_day.set_ylabel("Pressure (Torr)")
        self.ax_day.set_yscale('log')
        self.ax_day.grid(True)
        #======================== END GUI setup ========================

        # Updating the plot by reading data from hdf5; use thread to avoid blocking the GUI
        self.thread = QThread()  # Thread for running the worker
        self.worker = Worker()  # Worker object
        self.worker.moveToThread(self.thread)  # Move worker to the thread
        self.worker.data_updated.connect(self.update_plot)  # Connect signal
        self.thread.started.connect(self.worker.run)  # Start worker.run when the thread starts

        self.update_count = 0  # Counter for testing

        #======================== END INIT FUNC ==========================
    
    def start_plot(self):
        self.thread.start()  # Start the thread, which starts worker.run

    def _get_lines(self, ch, gauge_id):
        '''
        Return (short, day) plot lines for a sensor, creating them with a legend entry on first use
        '''
        if ch not in self.lines_short:
            label = f"CH{ch}: {gauge_id}"
            self.lines_short[ch], = self.ax_short.plot([], [], label=label)
            self.lines_day[ch], = self.ax_day.plot([], [], label=label)
            self.ax_short.legend(loc='upper left', fontsize=12)
            self.ax_day.legend(loc='upper left', fontsize=12)
        return self.lines_short[ch], self.lines_day[ch]

    def _update_day_cache(self, ch, ts_unix, pressures, start_day, end_day, bin_sec):
        '''
        Maintain the 5-min average cache for one sensor; ts_unix/pressures cover today only
        '''
        cache = self.day_caches.setdefault(ch, {'ts': [], 'ps': [], 'last_bin': None, 'init': False})
        if len(ts_unix) == 0:
            return cache

        if not cache['init']:
            edges = np.arange(start_day.timestamp(), end_day.timestamp(), bin_sec)
            bins = np.digitize(ts_unix, edges)
            for b in range(1, len(edges)):
                vals = pressures[bins == b]
                if len(vals):
                    cache['ts'].append(datetime.datetime.fromtimestamp(edges[b-1]))
                    cache['ps'].append(np.mean(vals))
                    cache['last_bin'] = edges[b-1]
            cache['init'] = True
        else:
            latest_bin = (ts_unix[-1] // bin_sec) * bin_sec
            if cache['last_bin'] is None or latest_bin > cache['last_bin']:
                mask = (ts_unix >= latest_bin) & (ts_unix < latest_bin + bin_sec)
                vals = pressures[mask]
                if len(vals):
                    cache['ts'].append(datetime.datetime.fromtimestamp(latest_bin))
                    cache['ps'].append(np.mean(vals))
                    cache['last_bin'] = latest_bin
        return cache

    def _set_log_ylim(self, ax, val_arrays):
        '''
        Shared log-scale y-limits across all sensors (nonpositive readings excluded)
        '''
        vals = [np.asarray(v) for v in val_arrays if len(v)]
        if not vals:
            return
        allv = np.concatenate(vals)
        allv = allv[allv > 0]
        if len(allv):
            ax.set_ylim(allv.min() / 1.5, allv.max() * 1.5)

    def update_plot(self, tarr, sensors): # Update the plot with new data
        if len(tarr) == 0 or not sensors: # prevent crashes because of conflicts mid-write
            return

        tarr = np.asarray(tarr, dtype=float)
        now = datetime.datetime.fromtimestamp(tarr[-1])

        start_day = datetime.datetime(now.year, now.month, now.day)
        end_day   = start_day + datetime.timedelta(days=1)
        bin_sec   = 5*60

        # Reset per-sensor caches and redo the day-axis decoration when the day rolls over
        if self._gui_day != start_day.date():
            self._gui_day = start_day.date()
            self.day_caches.clear()
            date_str = start_day.strftime('%Y-%m-%d')
            self.ax_day.set_title(f"Pressure (Full Day, 5-min Average) [{date_str}]")
            self.ax_day.set_xlim(start_day, end_day)
            ticks = [start_day + datetime.timedelta(hours=2*i) for i in range(13)]
            self.ax_day.set_xticks(ticks)
            labels = [t.strftime('%H:%M') for t in ticks]
            self.ax_day.set_xticklabels(labels, rotation=45, ha='right')

        time_30s = now - datetime.timedelta(seconds=30)

        short_vals = []
        for ch in sorted(sensors):
            parr, gauge_id = sensors[ch]
            pressures = np.asarray(parr, dtype=float)
            n = min(len(tarr), len(pressures)) # sensor datasets may lead timestamp mid-write
            ts_unix = tarr[:n]
            pressures = pressures[:n]
            line_short, line_day = self._get_lines(ch, gauge_id)

            # ==================== Plot 1: 30 seconds ====================
            i0 = np.searchsorted(ts_unix, time_30s.timestamp())
            if i0 < n:
                xs = [datetime.datetime.fromtimestamp(t) for t in ts_unix[i0:]]
                line_short.set_data(xs, pressures[i0:])
                short_vals.append(pressures[i0:])

            # ==================== Plot 2: Full day, 5-minute average ====================
            d0 = np.searchsorted(ts_unix, start_day.timestamp())
            cache = self._update_day_cache(ch, ts_unix[d0:], pressures[d0:], start_day, end_day, bin_sec)
            line_day.set_data(cache['ts'], cache['ps'])

        self.ax_short.set_xlim(time_30s, now)
        self._set_log_ylim(self.ax_short, short_vals)
        self._set_log_ylim(self.ax_day, [c['ps'] for c in self.day_caches.values()])

        self.canvas.draw()
        self.canvas.flush_events()

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================


if __name__ == "__main__":
    app = QApplication([])
    window = MainWindow()
    window.show()
    app.exec_()
