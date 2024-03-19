# -*- coding: utf-8 -*-
'''
Author: Jia Han
Creation date: Mar-16-2024
'''

import numpy as np
from wavegen_control import wavegen_control
import time

def generate_pulse_waveform():
	cycle_length = 16384  # Number of points in one cycle

	# Create a square wave with half number of zeros and half number of ones
	data = np.zeros(cycle_length)
	data[:int(0.5 * cycle_length)] = 1  # Set 1 for the first half of data

	# Normalize data to be within -1 to +1, ensuring low voltage level doesn't go negative after normalization
	min_data, max_data = np.min(data), np.max(data)
	range_data = max_data - min_data
	if range_data == 0:
		data_normalized = data - min_data  # Avoid division by zero if all data points are the same
	else:
		data_normalized = (data - min_data) * 2 / range_data - 1

	# Ensure the normalized low voltage level does not go negative
	# This might not be necessary if your normalization logic already ensures this, but it's a safeguard
	data_normalized = np.clip(data_normalized, -1, 1)

	return data_normalized

def init(wavegen, high_voltage, low_voltage, puff_time):
    wavegen.output = 0
    data = generate_pulse_waveform() # Define Arbitrary waveform shape
    wavegen.send_dac_data(data)

    wavegen.frequency = 1 / (2 * puff_time * 1e-3) # factor of 2 due to the way waveform shape is written; check generate_pulse_waveform()
    wavegen.burst(True, 1, 180)
    wavegen.voltage_level = (high_voltage, low_voltage)
    
    # User prompt for confirmation
    confirm = input("Proceed with enabling output? (Y/N): ").strip().upper()
    if confirm == 'Y':
        wavegen.output = 1
        print("Output enabled.")
    else:
        print("Operation canceled, output remains disabled.")


#-------------------------------------------------------#

if __name__ == '__main__':

	wavegen = wavegen_control(server_ip_addr = '192.168.0.106')
	
#	init(wavegen, 1.5, -1, 20)
	# wavegen.output = 0
	# wavegen.voltage_level = (1, 0.25)
	# wavegen.output = 1

	# puff_time = 10
	# wavegen.frequency = 1 / (2 * puff_time * 1e-3)