# -*- coding: utf-8 -*-
'''
Author: Jia Han
Creation date: Mar-16-2024
'''

import numpy as np
from wavegen_control import wavegen_control
import time

def send_waveform(wavegen):
	# Parameters for the square pulse

	low_voltage = 0.1  # Low voltage level (0.1V)
	high_voltage = 1.0  # High voltage level (1V)
	cycle_length = 16000  # Number of points in one cycle

	# Create the square pulse waveform
	data = np.ones(cycle_length) * low_voltage  # Initialize with low voltage
	data[:int(0.5 * cycle_length)] = high_voltage  # Set high voltage level for the high pulse duration

	# Normalize data to be within -1 to +1
	data_normalized = (data - np.min(data)) / (np.max(data) - np.min(data)) * 2 - 1

	wavegen.send_dac_data(data_normalized)

#-------------------------------------------------------#

if __name__ == '__main__':

	wavegen = wavegen_control(server_ip_addr = '192.168.0.106')
#	wavegen.burst(True, 1, 180)
	
#	wavegen.output = 0
#	send_waveform(wavegen)
#	wavegen.voltage_level = (1.5,0.5)
#	time.sleep(0.5) #after changing amplitude Agilent needs time to check output limit; see page 183 on user manual
	wavegen.frequency = 5
	print(wavegen.system_error())
#	wavegen.output = 1