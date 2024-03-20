# -*- coding: utf-8 -*-
'''
Author: Jia Han
Creation date: Mar-16-2024
'''

import numpy as np
import time
from wavegen_control import wavegen_control


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


#-------------------------------------------------------#

if __name__ == '__main__':

	wavegen = wavegen_control(server_ip_addr = '192.168.0.106')
	
#	init(wavegen, 1.5, -1, 20)
