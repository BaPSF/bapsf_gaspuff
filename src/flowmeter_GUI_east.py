#!/usr/bin/env python3
"""
flowmeter_gui_east.py

PyQt5 GUI that polls the East-port HDF5 file every 200 ms,
plots only the newest packet (170/171 points) on a dynamic window,
with 10 ms ticks and X-axis spanning exactly the packet duration.
"""
import os
os.environ['HDF5_USE_FILE_LOCKING'] = 'FALSE'

import sys, datetime
import h5py
import numpy as np
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout
from PyQt5.QtCore import QTimer
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.ticker import MultipleLocator

# Configuration
HDF5_FILE = os.path.join(
    "/home/gaspuffpi/flow_meter/data",
    f"flow_data_{datetime.date.today().isoformat()}.hdf5"
)
SAMPLE_DT        = 0.0003   # sample interval seconds (0.3 ms)
POLL_INTERVAL_MS = 200    # polling interval for checking HDF5
TICK_INTERVAL_MS = 10     # x-axis tick interval (ms)

class FlowMeterEastViewer(QWidget):
    def __init__(self):
        super().__init__()
        self.prev_count  = 0
        self.last_packet = None

        # Matplotlib canvas
        self.fig    = Figure()
        self.canvas = FigureCanvas(self.fig)
        self.ax     = self.fig.add_subplot(111)
        layout      = QVBoxLayout(self)
        layout.addWidget(self.canvas)

        # Polling timer
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_INTERVAL_MS)
        self.timer.timeout.connect(self.check_for_new_data)
        self.timer.start()

    def check_for_new_data(self):
        try:
            with h5py.File(HDF5_FILE, 'r', libver='latest', swmr=True) as f:
                grp   = f['FlowMeter_East']
                count = grp['flow_data'].shape[0]
                if count > self.prev_count:
                    self.prev_count  = count
                    self.last_packet = grp['flow_data'][count-1, :]
                    ts               = grp['timestamp'][count-1]
                    ts_str           = datetime.datetime.fromtimestamp(ts).isoformat()
                    print(f"[GUI] Detected packet #{count} at {ts_str} with {self.last_packet.size} points")
                    self.update_plot()
        except Exception as e:
            print(f"[GUI] read error: {e}")

    def update_plot(self):
        self.ax.clear()
        if self.last_packet is not None:
            n  = self.last_packet.size
            # build x from first sample = 0 to last sample = (n-1)*dt
            x  = np.arange(n) * SAMPLE_DT * 1000  # convert to ms
            y  = self.last_packet
            self.ax.plot(x, y, '-')  
            # set axis to packet span
            max_ms = x[-1]
        else:
            # no data yet: default span
            x = np.array([0])
            y = np.array([0])
            max_ms = SAMPLE_DT * 1000 * 10
            self.ax.plot(x, y, '-')

        # dynamic axis limits
        self.ax.set_xlim(0, max_ms)
        # ticks every 10ms
        self.ax.xaxis.set_major_locator(MultipleLocator(TICK_INTERVAL_MS))
        self.ax.set_xlabel('Time (ms)')
        self.ax.set_ylabel('Flow (slm)')
        self.ax.set_title('East Port Latest Packet')
        self.fig.tight_layout()
        self.ax.grid(True, which='major', linestyle='--', linewidth=0.5)
        self.canvas.draw()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('East Flow Meter Packet Viewer')
        self.setCentralWidget(FlowMeterEastViewer())
        self.resize(800, 400)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
