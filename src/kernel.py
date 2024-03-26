import numpy as np
import time
from flow_meter import FlowMeter
from wavegen_control import wavegen_control
from input import generate_pulse_waveform

#-------------------------------------------------------#

class GasPuffValve(object):

	def __init__(self, ip_address) -> None:
		if ip_address is None:
			raise ValueError('IP address must be provided.')
		# Connect to waveform generator
		self.wavegen = wavegen_control(server_ip_addr='192.168.0.106')
		self._puff_time = 10
		self._high_voltage = 0
		self._low_voltage = 0

	def program_waveform(self):
		# Turn off output before applying initial settings
		self.wavegen.output = 0
		data = generate_pulse_waveform() # Define Arbitrary waveform shape
		self.wavegen.send_dac_data(data) # Send waveform shape to the device
		self.wavegen.burst(True, 1, 0) # Enable burst mode
		self.wavegen.voltage_range('ON')
	
	@property
	def high_voltage(self):
		hi, lo = self.wavegen.voltage_level()
		self._high_voltage = hi
		return self._high_voltage

	@high_voltage.setter
	def high_voltage(self, value):
		if value < 0:
			value = 0
		if value < self._low_voltage:
			print("High voltage is lower than low voltage.")

		self.wavegen.output = 0
		self.wavegen.set_high_level(value)
		self.wavegen.output = 1

		self._high_voltage = value

	@property
	def low_voltage(self):
		hi, lo = self.wavegen.voltage_level()
		self._low_voltage = lo
		return self._low_voltage

	@low_voltage.setter
	def low_voltage(self, value):
		if value < 0:
			value = 0
		if value > self._high_voltage:
			print("Low voltage is higher than high voltage.")

		self.wavegen.output = 0
		self.wavegen.set_low_level(value)
		self.wavegen.output = 1

		self._low_voltage = value

	@property
	def puff_time(self):
		return self._puff_time

	@puff_time.setter
	def puff_time(self, value):
		# factor of 2 due to the way waveform shape is written; check generate_pulse_waveform()
		self.wavegen.frequency = 1 / (2 * value * 1e-3)
		self._puff_time = value

	def set_output(self,i):
		self.wavegen.output = i

#-------------------------------------------------------#

if __name__ == '__main__':
	gpc = FlowMeter(gpio_channel=5)
	gpc.acquire(0.3)
