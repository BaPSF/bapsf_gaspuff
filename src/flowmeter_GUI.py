#!/usr/bin/env python3
"""
flowmeter_gui_dual.py

PyQt5 GUI that polls the HDF5 file every 200 ms,
plots the newest packet for both East and West devices
in different colors, with a legend and grid.
"""
import os
# Disable HDF5 file locking for SWMR
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
HDF5_FILE        = os.path.join(
    "/home/gaspuffpi/flow_meter/data",
    f"flow_data_{datetime.date.today().isoformat()}.hdf5"
)
SAMPLE_DT        = 0.0003   # sample interval seconds (0.3 ms)
POLL_INTERVAL_MS = 200    # polling interval for checking HDF5
TICK_INTERVAL_MS = 10     # x-axis tick interval (ms)

class FlowMeterDualViewer(QWidget):
    def __init__(self):
        super().__init__()
        # Track previous counts and latest packets
        self.prev_counts = {
            'FlowMeter_East': 0,
            'FlowMeter_West': 0
        }
        self.last_packets = {
            'FlowMeter_East': None,
            'FlowMeter_West': None
        }

        # Setup plot canvas
        self.fig    = Figure()
        self.canvas = FigureCanvas(self.fig)
        self.ax     = self.fig.add_subplot(111)
        layout      = QVBoxLayout(self)
        layout.addWidget(self.canvas)

        # Start polling timer
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_INTERVAL_MS)
        self.timer.timeout.connect(self.check_for_new_data)
        self.timer.start()

    def check_for_new_data(self):
        updated = False
        try:
            with h5py.File(HDF5_FILE, 'r', libver='latest', swmr=True) as f:
                for grp_name in ('FlowMeter_East', 'FlowMeter_West'):
                    grp   = f[grp_name]
                    count = grp['flow_data'].shape[0]
                    if count > self.prev_counts[grp_name]:
                        self.prev_counts[grp_name]       = count
                        self.last_packets[grp_name]     = grp['flow_data'][count-1, :]
                        updated = True
        except Exception as e:
            print(f"[GUI] read error: {e}")

        if updated:
            self.update_plot()

    def update_plot(self):
        self.ax.clear()
        max_ms = 0
        # Plot each meter
        for grp_name, label, color in [
            ('FlowMeter_East', 'East', 'tab:blue'),
            ('FlowMeter_West', 'West', 'tab:orange')
        ]:
            packet = self.last_packets.get(grp_name)
            if packet is not None:
                n   = packet.size
                x   = np.arange(n) * SAMPLE_DT * 1000  # ms
                y   = packet
                self.ax.plot(x, y, '-', label=label, color=color)
                max_ms = max(max_ms, x[-1])

        # Configure axes
        if max_ms <= 0:
            max_ms = SAMPLE_DT * 1000 * 10
        self.ax.set_xlim(0, max_ms)
        self.ax.xaxis.set_major_locator(MultipleLocator(TICK_INTERVAL_MS))
        self.ax.set_xlabel('Time (ms)')
        self.ax.set_ylabel('Flow (slm)')
        self.ax.set_title('Latest Flow Packets (East & West)')
        self.ax.grid(True, which='major', linestyle='--', linewidth=0.5)
        self.ax.legend()
        self.fig.tight_layout()
        self.canvas.draw()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Flow Meter Live Viewer')
        self.setCentralWidget(FlowMeterDualViewer())
        self.resize(800, 400)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
