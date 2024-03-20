from kernel import GasPuffValve
import numpy as np
import tkinter as tk
from tkinter import messagebox

def connect_wavegen():
    global gas_puff_valve
    ip_address = ip_address_entry.get()
    gas_puff_valve = GasPuffValve(ip_address=ip_address)

def init_waveform():
    gas_puff_valve.program_waveform()

def enable_output():
    gas_puff_valve.set_output(1)

def disable_output():
    gas_puff_valve.set_output(0)    

def update_high_voltage():
    gas_puff_valve.high_voltage = float(high_voltage_entry.get())

def update_low_voltage():
    gas_puff_valve.low_voltage = float(low_voltage_entry.get())
    
def update_puff_time():
    gas_puff_valve.puff_time = float(puff_time_entry.get())


# Create the main window
window = tk.Tk()
window.title("Waveform Control")
window.geometry("400x500")

# Create the widgets
ip_address_label = tk.Label(window, text="IP Address:")
ip_address_entry = tk.Entry(window)
ip_address_label.pack()
ip_address_entry.pack()

init_button = tk.Button(window, text="Connect", command=connect_wavegen)
init_button.pack(pady=10)

init_button = tk.Button(window, text="Send program waveform", command=init_waveform)
init_button.pack(pady=10)

enable_button = tk.Button(window, text="Enable Output", command=enable_output)
enable_button.pack(pady=10)

disable_button = tk.Button(window, text="Disable Output", command=disable_output)
disable_button.pack(pady=10)

high_voltage_label = tk.Label(window, text="High Voltage(volts):")
high_voltage_entry = tk.Entry(window)
high_voltage_label.pack()
high_voltage_entry.pack()

update_high_voltage_button = tk.Button(window, text="Update", command=update_high_voltage)
update_high_voltage_button.pack(pady=5)

low_voltage_label = tk.Label(window, text="Low Voltage(volts):")
low_voltage_entry = tk.Entry(window)
low_voltage_label.pack()
low_voltage_entry.pack()

update_low_voltage_button = tk.Button(window, text="Update", command=update_low_voltage)
update_low_voltage_button.pack(pady=5)

puff_time_label = tk.Label(window, text="Puff Time(ms):")
puff_time_entry = tk.Entry(window)
puff_time_label.pack()
puff_time_entry.pack()

update_puff_time_button = tk.Button(window, text="Update", command=update_puff_time)
update_puff_time_button.pack(pady=5)


# Run the main loop
window.mainloop()