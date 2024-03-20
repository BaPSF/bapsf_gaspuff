from kernel import GasPuffController, 
import numpy as np
import tkinter as tk
from tkinter import messagebox

gpc = GasPuffController(gpio_channel=5)
#gpc.acquire(0.3)

def initialize_wavegen():


def enable_output():
    confirm = messagebox.askyesno("Confirmation", "Proceed with enabling output?")
    if confirm:
        wavegen.output = 1
        messagebox.showinfo("Output Enabled", "Output has been enabled.")
    else:
        messagebox.showinfo("Operation Canceled", "Output remains disabled.")

def disable_output():
    wavegen.output = 0
    messagebox.showinfo("Output Disabled", "Output has been disabled.")

def update_high_voltage():
    try:
        high_voltage = float(high_voltage_entry.get())
        change_high_voltage(wavegen, high_voltage)
        messagebox.showinfo("High Voltage Updated", "High voltage has been updated.")
    except ValueError:
        messagebox.showerror("Invalid Input", "Please enter a valid number for high voltage.")

def update_low_voltage():
    try:
        low_voltage = float(low_voltage_entry.get())
        change_low_voltage(wavegen, low_voltage)
        messagebox.showinfo("Low Voltage Updated", "Low voltage has been updated.")
    except ValueError:
        messagebox.showerror("Invalid Input", "Please enter a valid number for low voltage.")

def update_puff_time():
    try:
        puff_time = float(puff_time_entry.get())
        change_puff_time(wavegen, puff_time)
        messagebox.showinfo("Puff Time Updated", "Puff time has been updated.")
    except ValueError:
        messagebox.showerror("Invalid Input", "Please enter a valid number for puff time.")

# Create the main window
window = tk.Tk()
window.title("Waveform Control")
window.geometry("400x300")

# Create the widgets
init_button = tk.Button(window, text="Initialize", command=initialize_wavegen)
enable_button = tk.Button(window, text="Enable Output", command=enable_output)
disable_button = tk.Button(window, text="Disable Output", command=disable_output)
high_voltage_label = tk.Label(window, text="High Voltage:")
high_voltage_entry = tk.Entry(window)
update_high_voltage_button = tk.Button(window, text="Update", command=update_high_voltage)
low_voltage_label = tk.Label(window, text="Low Voltage:")
low_voltage_entry = tk.Entry(window)
update_low_voltage_button = tk.Button(window, text="Update", command=update_low_voltage)
puff_time_label = tk.Label(window, text="Puff Time:")
puff_time_entry = tk.Entry(window)
update_puff_time_button = tk.Button(window, text="Update", command=update_puff_time)

# Place the widgets in the window
enable_button.pack(pady=10)
disable_button.pack(pady=10)
high_voltage_label.pack()
high_voltage_entry.pack()
update_high_voltage_button.pack(pady=5)
low_voltage_label.pack()
low_voltage_entry.pack()
update_low_voltage_button.pack(pady=5)
puff_time_label.pack()
puff_time_entry.pack()
update_puff_time_button.pack(pady=5)

# Run the main loop
window.mainloop()