from kernel import GasPuffController, GasPuffValve
import numpy as np
import tkinter as tk
from tkinter import messagebox

def initialize_wavegen():
    global gas_puff_valve
    ip_address = ip_address_entry.get()
    puff_time = float(puff_time_entry.get())
    high_voltage = float(high_voltage_entry.get())
    low_voltage = float(low_voltage_entry.get())
    gas_puff_valve = GasPuffValve(ip_address=ip_address, puff_time=puff_time, high_voltage=high_voltage, low_voltage=low_voltage)

def enable_output():
    gas_puff_valve.enable_output()

def disable_output():
    gas_puff_valve.disable_output()

def update_high_voltage():
    high_voltage = float(high_voltage_entry.get())
    gas_puff_valve.update_high_voltage(high_voltage)

def update_low_voltage():
    low_voltage = float(low_voltage_entry.get())
    gas_puff_valve.update_low_voltage(low_voltage)

def update_puff_time():
    puff_time = float(puff_time_entry.get())
    gas_puff_valve.update_puff_time(puff_time)



# Create the main window
window = tk.Tk()
window.title("Waveform Control")
window.geometry("400x300")

# Create the widgets
ip_address_label = tk.Label(window, text="IP Address:")
ip_address_entry = tk.Entry(window)
ip_address_label.pack()
ip_address_entry.pack()

init_button = tk.Button(window, text="Initialize", command=initialize_wavegen)
init_button.pack(pady=10)

enable_button = tk.Button(window, text="Enable Output", command=enable_output)
enable_button.pack(pady=10)

disable_button = tk.Button(window, text="Disable Output", command=disable_output)
disable_button.pack(pady=10)

high_voltage_label = tk.Label(window, text="High Voltage:")
high_voltage_entry = tk.Entry(window)
high_voltage_label.pack()
high_voltage_entry.pack()

update_high_voltage_button = tk.Button(window, text="Update", command=update_high_voltage)
update_high_voltage_button.pack(pady=5)

low_voltage_label = tk.Label(window, text="Low Voltage:")
low_voltage_entry = tk.Entry(window)
low_voltage_label.pack()
low_voltage_entry.pack()

update_low_voltage_button = tk.Button(window, text="Update", command=update_low_voltage)
update_low_voltage_button.pack(pady=5)

puff_time_label = tk.Label(window, text="Puff Time:")
puff_time_entry = tk.Entry(window)
puff_time_label.pack()
puff_time_entry.pack()

update_puff_time_button = tk.Button(window, text="Update", command=update_puff_time)
update_puff_time_button.pack(pady=5)


# Run the main loop
window.mainloop()