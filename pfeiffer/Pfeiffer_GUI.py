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
import os
import time
import datetime

#===============================================================================================================================================
sensor_number = 1
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

def get_data(ifn):
    '''
    read the data from the hdf5 file
    '''
    try:
        with h5py.File(ifn, 'r', swmr=True) as f:
            parr =  f['PfeifferVacuum'][str(sensor_number)][::10]
            tarr = f['PfeifferVacuum']['timestamp'][::10]
            gauge_id = f['PfeifferVacuum'][str(sensor_number)].attrs['Model'][-1]

            if isinstance(gauge_id, (list, np.ndarray)):
                gauge_id = gauge_id[0]
            if isinstance(gauge_id, bytes):
                gauge_id = gauge_id.decode()

            if len(parr) < n_points:
                return tarr, parr, gauge_id
            else:
                return tarr[-n_points:], parr[-n_points:], gauge_id
    except Exception as e:
        print("GUI read failed:", e)
        return None, None, None
#===============================================================================================================================================

class Worker(QObject):
    '''
    Worker function that emits the data to the plotting GUI
    Runs in a separate thread to avoid blocking the GUI
    '''
    data_updated = pyqtSignal(np.ndarray, np.ndarray, str)  # Signal to emit the data

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
                tarr, parr, gauge_id = get_data(ifn)

                if isinstance(tarr, np.ndarray) and isinstance(parr, np.ndarray) and isinstance(gauge_id, str):
                    self.data_updated.emit(tarr, parr, gauge_id)
                else:
                    print("Skipping emit due to invalid data types.") 
                
                QThread.msleep(500)  # Sleep for 1 second
            except OSError as e:
                if "unable to lock file" in str(e):
                    print("File temporarily locked by writer. Retry in 1s...")
                else:
                    print(f"HDF5 read error: {e}")
                QThread.msleep(20)




class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__() # Call the parent class constructor

        self.avg_ts = []               # Cached 5-min bin start times
        self.avg_ps = []               # Cached pressure averages
        self.last_bin_timestamp = None  # timestamp of last completed bin
        self.cache_initialized = False

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
        plt.rcParams['font.size'] = 12
        self.ax_short = self.fig.add_subplot(211)  
        self.ax_day = self.fig.add_subplot(212)
        self.canvas = FigureCanvas(self.fig)  # Create a canvas for the figure
        # Add the navigation toolbar for interacting with plot
        self.toolbar = NavigationToolbar(self.canvas, self)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)  # Add the canvas to the layout
        # Create the plot lines
        self.line_short, = self.ax_short.plot([], [])
        self.line_day, = self.ax_day.plot([], [])
        # Plot label and title
        self.ax_short.set_title("Pressure (30 Seconds)")
        self.ax_short.set_xlabel("Time")
        self.ax_short.set_ylabel("Pressure (Torr)")
        self.ax_short.grid(True)

        self.ax_day.set_title("Pressure (Full Day, 5-min Average)")
        self.ax_day.set_xlabel("Time")
        self.ax_day.set_ylabel("Pressure (Torr)")
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

    def update_plot(self, tarr, parr, gauge_id): # Update the plot with new data
        if len(tarr) == 0 or len(parr) == 0: # Update: prevent crashes because of conflicts mid-write
            return  

        if len(tarr) != len(parr):
            min_len = min(len(tarr), len(parr))
            tarr = tarr[:min_len]
            parr = parr[:min_len]

        # Convert the timestamp to datetime objects
        timestamps = [datetime.datetime.fromtimestamp(float(ts)) for ts in tarr]
        pressures = np.asarray(parr, dtype=float)
        now = timestamps[-1]

        # ==================== Plot 1: 30 seconds ====================
        time_30s = now - datetime.timedelta(seconds=30)
        indices_short = [i for i, ts in enumerate(timestamps) if ts >= time_30s]

        if indices_short:
            ts_short = [timestamps[i] for i in indices_short]
            ps_short = pressures[indices_short]
            self.line_short.set_data(ts_short, ps_short)
            self.ax_short.set_xlim(ts_short[0], ts_short[-1])
            min_val = np.min(ps_short)
            max_val = np.max(ps_short)
            if max_val == min_val:
                padding = 0.1 * max_val
            else:
                padding = 0.1 * (max_val - min_val)
            self.ax_short.set_ylim(min_val - padding, max_val + padding)
            self.ax_short.relim()
            self.ax_short.autoscale_view(True, True, True)

        # ==================== Plot 2: Full day, 5-minute average ====================
        start_of_day = datetime.datetime(now.year, now.month, now.day)
        end_of_day = start_of_day + datetime.timedelta(days=1)
        bin_minutes = 5
        bin_width_sec = bin_minutes * 60

        # Filter today's data
        indices_day = [i for i, ts in enumerate(timestamps) if ts >= start_of_day]
        if indices_day:
            ts_day = [timestamps[i] for i in indices_day]
            ps_day = pressures[indices_day]
            ts_unix = np.array([ts.timestamp() for ts in ts_day])

            # Determine which bins each point falls into
            bin_edges = np.arange(start_of_day.timestamp(), end_of_day.timestamp(), bin_width_sec)
            bin_indices = np.digitize(ts_unix, bin_edges)

            if not self.cache_initialized:
                # INITIALIZE FULL CACHE ON FIRST RUN
                print("Initializing full-day bin cache...")
                for b in range(1, len(bin_edges)):
                    bin_vals = [ps_day[i] for i in range(len(bin_indices)) if bin_indices[i] == b]
                    if bin_vals:
                        avg_time = datetime.datetime.fromtimestamp(bin_edges[b - 1])
                        avg_val = np.mean(bin_vals)
                        self.avg_ts.append(avg_time)
                        self.avg_ps.append(avg_val)
                        self.last_bin_timestamp = bin_edges[b - 1]
                self.cache_initialized = True

            else:
                # ONLY UPDATE NEWEST BIN
                latest_ts = ts_unix[-1]
                current_bin_start = (latest_ts // bin_width_sec) * bin_width_sec
                if self.last_bin_timestamp is None or current_bin_start > self.last_bin_timestamp:
                    bin_start_time = datetime.datetime.fromtimestamp(current_bin_start)
                    bin_end_time = bin_start_time + datetime.timedelta(seconds=bin_width_sec)
                    bin_vals = [ps_day[i] for i, ts in enumerate(ts_day) if bin_start_time <= ts < bin_end_time]

                    if bin_vals:
                        avg_val = np.mean(bin_vals)
                        self.avg_ts.append(bin_start_time)
                        self.avg_ps.append(avg_val)
                        self.last_bin_timestamp = current_bin_start
                        print(f"Appended new bin: {bin_start_time.strftime('%H:%M')} with {len(bin_vals)} points")

            # Plot cached averages
            self.line_day.set_data(self.avg_ts, self.avg_ps)
            self.ax_day.set_xlim(start_of_day, end_of_day)
            if self.avg_ps:
                min_p, max_p = np.min(self.avg_ps), np.max(self.avg_ps)
                pad = 0.1*(max_p-min_p) if max_p!=min_p else 0.1*max_p
                self.ax_day.set_ylim(min_p - pad, max_p + pad)
            self.ax_day.relim()
            self.ax_day.autoscale_view(True, True, True)

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
