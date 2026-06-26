# coding: utf-8

'''
This module contains functions for plotting pressure reading in real time
The plotted data reads hdf5 files saved using Pfeiffer_control.py

Author: Jia Han
Ver1.0 created on: 2021-07-23
'''

import os
os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"  # noqa

import datetime
import h5py
import numpy as np

from PyQt5.QtCore import QThread, pyqtSignal, QObject, Qt
from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QLabel, QPushButton, QWidget
from PyQt5.QtGui import QFont

# the matplotlib backend imports must happen after import matplotlib and PyQt5
import matplotlib as mpl
mpl.use("qtagg")
from matplotlib import pyplot as plt
from matplotlib import ticker
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar


#===============================================================================================================================================
sensor_number = 1
n_points = 10000
#===============================================================================================================================================

def get_latest_file(dir_path=r"Z:\gauge"):
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

    def run(self):
        '''
        Find the latest file and read the last indexed data from it
        '''
        last_file = ""
        while True:
            try:
                ifn = get_latest_file()
                if ifn != last_file:
                    print("Latest HDF5 file selected:", ifn)
                    last_file = ifn
                tarr, parr, gauge_id = get_data(ifn)

                if (
                    isinstance(tarr, np.ndarray)
                    and isinstance(parr, np.ndarray)
                    and isinstance(gauge_id, str)
                ):
                    self.data_updated.emit(tarr, parr, gauge_id)
                else:
                    print("Skipping emit due to invalid data types.") 
                
                QThread.msleep(500)
            except OSError as e:
                if "unable to lock file" in str(e):
                    print("File temporarily locked by writer. Retry in 1s...")
                else:
                    print(f"HDF5 read error: {e}")
                QThread.msleep(1000)


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__() # Call the parent class constructor

        self.avg_ts = []               # Cached 5-min bin start times
        self.avg_ps = []               # Cached pressure averages
        self.last_bin_timestamp = None  # timestamp of last completed bin
        self.cache_initialized = False
        self._gui_day = None

        #======================== GUI setup ========================
        central_widget = QWidget() # Create a central widget
        self.setCentralWidget(central_widget) # Set the central widget
        self.setGeometry(100,100,500,500)

        _title = QLabel("Real time pressure reading", parent=self)
        _title.setFixedHeight(36)
        font = _title.font()
        font.setPointSize(16)
        font.setBold(True)
        _title.setFont(font)

        # Create a button to start the plot
        button = QPushButton("Start Plot")
        button.setFont(QFont("Arial", 24))

        self.canvas = FigureCanvas()
        self.toolbar = NavigationToolbar(self.canvas, parent=self)

        # Create a figure and a canvas for the figure
        # self.fig = Figure(figsize=(15,15))
        plt.rcParams['font.size'] = 16
        self.ax_short = self.fig.add_subplot(211)
        self.ax_day = self.fig.add_subplot(212)

        # Create the plot lines
        self.line_short, = self.ax_short.plot([], [], "-o")
        self.line_day, = self.ax_day.plot([], [])

        # Setup plots
        self._setup_short_plot()  # trailing 30 sec. plot
        self._setup_day_plot()  # 1-day of 5 min. ave.

        self.fig.tight_layout()

        # Build Layout
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(_title, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(button)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas, stretch=1)

        # Connect Signals
        button.clicked.connect(self.start_plot)

        #======================== END GUI setup ========================

        # Updating the plot by reading data from hdf5; use thread to avoid blocking the GUI
        self.thread = QThread()  # Thread for running the worker
        self.worker = Worker()  # Worker object
        self.worker.moveToThread(self.thread)  # Move worker to the thread
        self.worker.data_updated.connect(self.update_plot)  # Connect signal
        self.thread.started.connect(self.worker.run)  # Start worker.run when the thread starts

        self.update_count = 0  # Counter for testing

        #======================== END INIT FUNC ==========================
    
    @property
    def fig(self):
        return self.canvas.figure

    def _setup_short_plot(self):
        self.ax_short.set_title("Trailing 30 sec.")
        self.ax_short.set_xlabel("Time")
        self.ax_short.set_ylabel("Pressure (Torr)")
        self.ax_short.grid(True)

        # y-axis formatter
        formatter = ticker.ScalarFormatter(useMathText=True)
        formatter.set_powerlimits((0, 0))
        self.ax_short.yaxis.set_major_formatter(formatter)

    def _setup_day_plot(self):
        self.ax_day.set_title(self._generate_day_title())
        self.ax_day.set_xlabel("Time")
        self.ax_day.set_ylabel("Pressure (Torr)")
        self.ax_day.grid(True)

        formatter = ticker.ScalarFormatter(useMathText=True)
        formatter.set_powerlimits((0, 0))
        self.ax_day.yaxis.set_major_formatter(formatter)

    @staticmethod
    def _generate_day_title(day: str | None = None):
        title = "5 min. Ave."
        if isinstance(day, str):
            title = f"{title} [{day}]"

        return title

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
            padding = 0.1 * max_val if max_val == min_val else 0.1 * (max_val - min_val)

            self.ax_short.set_ylim(min_val - padding, max_val + padding)
            self.ax_short.relim()
            self.ax_short.autoscale_view(True, True, True)

        # ==================== Plot 2: Full day, 5-minute average ====================
        start_day = datetime.datetime(now.year, now.month, now.day)
        end_day   = start_day + datetime.timedelta(days=1)
        bin_sec   = 5*60

        today = start_day.date()
        if self._gui_day is None or self._gui_day != today:
            self._gui_day = today
            self.avg_ts.clear()
            self.avg_ps.clear()
            self.last_bin_timestamp = None
            self.cache_initialized = False

        ts_day  = [ts for ts in timestamps if ts >= start_day]
        ps_day  = pressures[-len(ts_day):]  # same indexing
        ts_unix = np.array([ts.timestamp() for ts in ts_day])
        edges   = np.arange(start_day.timestamp(), end_day.timestamp(), bin_sec)
        bins    = np.digitize(ts_unix, edges)

        if not self.cache_initialized:
            for b in range(1, len(edges)):
                vals = [ps_day[i] for i in range(len(bins)) if bins[i] == b]
                if vals:
                    self.avg_ts.append(datetime.datetime.fromtimestamp(edges[b-1]))
                    self.avg_ps.append(np.mean(vals))
                    self.last_bin_timestamp = edges[b-1]
            self.cache_initialized = True
        else:
            latest_bin = (ts_unix[-1] // bin_sec) * bin_sec
            if self.last_bin_timestamp is None or latest_bin > self.last_bin_timestamp:
                start_b = datetime.datetime.fromtimestamp(latest_bin)
                end_b   = start_b + datetime.timedelta(seconds=bin_sec)
                vals = [p for (p, ts) in zip(ps_day, ts_day) if start_b <= ts < end_b]
                if vals:
                    self.avg_ts.append(start_b)
                    self.avg_ps.append(np.mean(vals))
                    self.last_bin_timestamp = latest_bin

        date_str = start_day.strftime('%Y-%m-%d')
        self.ax_day.set_title(self._generate_day_title(date_str))
        self.line_day.set_data(self.avg_ts, self.avg_ps)
        self.ax_day.set_xlim(start_day, end_day)
        ticks = [start_day + datetime.timedelta(hours=2*i) for i in range(13)]
        self.ax_day.set_xticks(ticks)
        labels = [t.strftime('%H:%M') for t in ticks]
        self.ax_day.set_xticklabels(labels, rotation=45, ha='right')

        if self.avg_ts:
            ts_arr = np.array(self.avg_ts)
            ps_arr = np.array(self.avg_ps)
            mask   = ts_arr >= start_day
            if mask.any():
                mn, mx = ps_arr[mask].min(), ps_arr[mask].max()
                pad = 0.1*(mx-mn) if mx!=mn else 0.1*mx
                self.ax_day.set_ylim(mn-pad, mx+pad)

        self.canvas.draw_idle()
        self.fig.tight_layout()
    
    def resizeEvent(self, a0):
        super().resizeEvent(a0)
        self.fig.tight_layout()

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================


if __name__ == "__main__":
    app = QApplication([])
    window = MainWindow()
    window.show()
    app.exec_()
