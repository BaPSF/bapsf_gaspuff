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
                
                QThread.sleep(1)  # Sleep for 1 second
            except OSError as e:
                if "unable to lock file" in str(e):
                    print("File temporarily locked by writer. Retry in 1s...")
                else:
                    print(f"HDF5 read error: {e}")
                QThread.sleep(2)




class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__() # Call the parent class constructor

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
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvas(self.fig)  # Create a canvas for the figure
        # Add the navigation toolbar for interacting with plot
        self.toolbar = NavigationToolbar(self.canvas, self)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)  # Add the canvas to the layout
        # Plot label and title
        self.ax.set_xlabel('Time')
        self.ax.set_ylabel('Pressure (Torr)')
        self.ax.grid(True)
        # Create the plot lines
        self.line_A, = self.ax.plot([], [])
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
        # Convert the datetime objects to strings in the format hr:min:sec
        time_strings = [ts.strftime('%H:%M:%S') for ts in timestamps]
        # Set the x-axis tick labels
        x_ticks = np.linspace(0, len(time_strings)-1, 5, dtype=int)
        self.ax.set_xticks(x_ticks)
        self.ax.set_xticklabels([time_strings[i] for i in x_ticks])
        
        # Set the x-axis as the range of time_strings
        self.line_A.set_data(range(len(time_strings)), np.asarray(parr, dtype=float))
        
        if gauge_id == 'PKR':
            self.line_A.set_label("Pirani/Cold Cathode")
        else:
            self.line_A.set_label("Unknown gauge")
        self.ax.legend(loc='upper right', fontsize=18)

        self.ax.relim()
        self.ax.autoscale_view(True, True, True)
        # Set y-axis tick labels in scientific notation
        self.ax.ticklabel_format(style='sci', axis='y', scilimits=(0,0))  
        

        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

        self.update_count += 1  # Increment the update counter
        print(f"Plot updated: {self.update_count}")  # Print the update count

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================


if __name__ == "__main__":
    app = QApplication([])
    window = MainWindow()
    window.show()
    app.exec_()
