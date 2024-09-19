import time
import socket
import struct
import multiprocessing as mp
import numpy as np
import datetime
# import tables
import datetime
import pickle
from dateutil.relativedelta import relativedelta

# For GUI
import tkinter as tk
from tkinter.constants import BOTTOM, CENTER, LEFT, TOP, RIGHT, HORIZONTAL
from tkinter.ttk import Progressbar

# For plotting in GUI
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.backends.backend_tkagg as tkagg
from matplotlib.animation import FuncAnimation
import matplotlib.pyplot as plt

# uhh Other stuff
import gc
import queue


def receive_data(q, MCAST_GRP, MCAST_PORT):
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                # on this port, listen ONLY to MCAST_GRP
                # sock.bind((MCAST_GRP, MCAST_PORT))
                # mreq = struct.pack("4sl", , socket.INADDR_ANY)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                socket.inet_aton(MCAST_GRP) + socket.inet_aton('192.168.7.38'))

                sock.bind((MCAST_GRP, MCAST_PORT))

                while True:
                    data_length = struct.unpack(">i", sock.recv(4))[0]
                    data = b''
                    data_remaining = data_length
                    print("Getting data length: " + str(data_length))

                    BUFF_LEN = 65535
                    while data_remaining > 0 and data_length < 1000000:
                        part = sock.recv(BUFF_LEN if data_remaining > BUFF_LEN else data_remaining)
                        data += part
                        data_remaining -= len(part)

                    print("Received data length: " + str(len(data)))

                    if data_length != len(data):
                        raise Exception("Data length does not match data received")

                    if data != b'':
                        q.put(data)

        except Exception as e:
            print("Recieve data exception: ")
            print(repr(e))
            time.sleep(0.5)


class App(tk.Frame):
    def __init__(self, root):
        tk.Frame.__init__(self, root)
        self.root = root
        self.pack()

        # Multicast group settings
        # '224.0.0.36'
        MCAST_GRP = '224.1.1.1'
        MCAST_PORT = 10004

        self.data_q = mp.Queue()
        self.multicast_process = mp.Process(target=receive_data, args=(self.data_q, MCAST_GRP, MCAST_PORT))
        self.multicast_process.start()

        self.create_plot()
        self.update_plot()
        self.create_button("Quit", self._quit, location=BOTTOM, pady=20)
        self.create_button("Save trace", self.save_trace, location=TOP, pady=20)
        self.create_button("Clear traces", self.clear_trace, location=TOP, pady=20)

        self.data_list = []

    def _quit(self):
        self.multicast_process.terminate()
        self.multicast_process.join()
        self.root.quit()     # stops mainloop
        self.root.destroy()

    def create_plot(self):
        temp = tk.LabelFrame(self.root)

        self.fig = plt.Figure()
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("Time (ms)")
        self.ax.set_ylabel("Flow (L/min)")
        self.ax.set_xlim(-5, 185)
        self.ax.set_ylim(-1.5, 6.5)
        # self.ax.set_yscale('log')
        # self.ax.set_title("Spectrum")
        self.ax.set_title("Flow meters (T = ?)")
        self.canvas = FigureCanvasTkAgg(self.fig, master=temp)
        self.linesEast, = self.ax.plot(np.zeros(180), np.zeros(180), label='East')
        self.linesWest, = self.ax.plot(np.zeros(180), np.zeros(180), label='West')
        self.ax.plot([], [], label='Saved', linestyle='dashed', color='black')
        self.saved_linesEast = self.ax.plot(np.zeros(180), np.zeros(180), color='tab:blue', linestyle='dashed', alpha=0.7)[0]
        self.saved_linesWest = self.ax.plot(np.zeros(180), np.zeros(180), color='tab:orange', linestyle='dashed', alpha=0.7)[0]
        self.saved_text = self.ax.text(5, -1, '')

        self.ax.legend()
        self.navi = tkagg.NavigationToolbar2Tk(self.canvas, temp)
        self.navi.update()
        temp.pack(side=tk.RIGHT, expand=tk.TRUE, fill=tk.BOTH)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=1, pady=20, padx=(0, 10))
        self.navi.pack(side=tk.BOTTOM)

    def update_plot(self):
        # Update after every few seconds or so
        # print("==== updating plot")
        try:
            if self.data_q.empty() is False:
                # print("==== queue not empty")
                while not self.data_q.empty():
                    print('getting data')
                    self.msi = pickle.loads(self.data_q.get())
                    self.flows = self.msi['flowmeter']
                    self.timestamp = datetime.datetime.fromtimestamp(self.msi['diode_t0_seconds'][0] +
                                                                     self.msi['diode_t0_fraction'][0].astype('u8') / 2 ** 64)
                    self.timestamp = self.timestamp - relativedelta(years=66, leapdays=-1)
                    
                #     self.data_list.append(self.flows)
                #     while len(self.data_list) > 1024:
                #         self.data_list.pop(0)
                # self.data_array = np.array(self.data_list)
                # num_shots = int(self.shot_avg.get())
                # num_shots = num_shots if num_shots <= self.data_array.shape[0] else self.data_array.shape[0]
                # avg_spectrum = np.mean(self.data_array[-num_shots:], axis=0)

                self.linesEast.set_xdata(np.arange(len(self.flows[0])))
                self.linesEast.set_ydata(self.flows[0])
                self.linesWest.set_xdata(np.arange(len(self.flows[1])))
                self.linesWest.set_ydata(self.flows[1])

                # self.ax.set_ylim(np.min(flows) * 1.1, np.max(flows) * 1.1)
                # self.ax.set_xlim(-5, 185)

                # plt.draw()
                self.ax.set_title("Flow meters (T = {})".format(self.timestamp.strftime("%Y-%m-%d %H:%M:%S")))
                self.canvas.draw()
                gc.collect()  # to prevent canvas.draw from leaking memory
        except Exception as e:
            print("Exception: ")
            print(repr(e))
        self.after(100, self.update_plot)

    def save_trace(self):
        self.saved_flows = self.flows

        self.saved_linesEast.set_xdata(np.arange(len(self.saved_flows[0])))
        self.saved_linesEast.set_ydata(self.saved_flows[0])
        self.saved_linesWest.set_xdata(np.arange(len(self.saved_flows[1])))
        self.saved_linesWest.set_ydata(self.saved_flows[1])
        self.saved_text.set_text("Saved: T = {}".format(self.timestamp.strftime("%Y-%m-%d %H:%M:%S")))

    def clear_trace(self):
        self.saved_linesEast.set_xdata([])
        self.saved_linesEast.set_ydata([])
        self.saved_linesWest.set_xdata([])
        self.saved_linesWest.set_ydata([])
        self.saved_text.set_text('')

    def create_button(self, text, command, width=None, location=None, pady=0, size=9):
        '''Creates and packs a button.'''
        button = tk.Button(master=self.root, text=text, command=command,
                           font=('Helvetica', size), width=width)
        button.pack(side=location, pady=pady, padx=10, ipadx=10)

if __name__ == '__main__':

    root = tk.Tk()
    myapp = App(root)
    root.geometry("1024x640")
    root.title("Flow meter plots")
    root.config(bg='#345')
    myapp.mainloop()


    # try:
    #     while True:
    #         time.sleep(1)
    # except KeyboardInterrupt:
    #     print("Interrupting processes...")
    # finally:
    #     multicast_process.terminate()
    #     multicast_process.join()
    #     # ffc_process.terminate()
    #     # ffc_process.join()
    #     multicast_process.close()
        # ffc_process.close()
