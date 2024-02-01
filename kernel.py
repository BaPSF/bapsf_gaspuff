import numpy as np 
from wavegen_control import wavegen_control
from flow_meter import FlowMeter
from input import generate_pulse

class GasPuffController(object):
    def __init__(self) -> None:
        self.wavegen = wavegen_control(server_ip_addr='192.168.1.13')
        self.flow_meter = FlowMeter()

    def set_waveform(self, function, voltages, freq=None, offset=None):
        if function == 'USER':
            self.wavegen.send_dac_data(generate_pulse().astype('>i2'))
            self.wavegen.send_dac_data(generate_pulse(freq, offset).astype('>i2'))
        self.wavegen.function = function
        self.wavegen.voltage_level = voltages[0], voltages[1]
        if freq is not None:
            self.wavegen.frequency = freq
        if offset is not None:
            self.wavegen.DCoffset = offset

    def burst_mode(self, ncycles):
        self.wavegen.burst(enable=True, ncycles=ncycles, phase=0)