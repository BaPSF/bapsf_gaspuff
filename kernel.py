import numpy as np
import RPi.GPIO as GPIO
from wavegen_control import wavegen_control
from flow_meter import FlowMeter
from input import generate_pulse

class GasPuffController(object):
    def __init__(self, gpio_channel) -> None:
        # self.wavegen = wavegen_control(server_ip_addr='192.168.1.13')
        self.flow_meter = FlowMeter()
        GPIO.setup(gpio_channel, GPIO.IN)
        self.gpio_channel = gpio_channel

    def set_waveform(self, function, voltages, freq=None, offset=None):
        if function == 'USER':
            self.wavegen.send_dac_data(generate_pulse().astype('>i2'))
            # self.wavegen.send_dac_data(generate_pulse(freq, offset).astype('>i2'))
        self.wavegen.function = function
        self.wavegen.voltage_level = voltages[0], voltages[1]
        if freq is not None:
            self.wavegen.frequency = freq
        if offset is not None:
            self.wavegen.DCoffset = offset

    def burst_mode(self, ncycles):
        self.wavegen.burst(enable=True, ncycles=ncycles, phase=0)
        

    def acquire(self, duration, acquisition_limit=1000):
        shot_counts = 0
        try:
            while shot_counts <= acquisition_limit:
                GPIO.wait_for_edge(self.gpio_channel, GPIO.RISING)
                readings = np.array(self.flow_meter.get_reading(duration))
                np.savetxt(f'output_{shot_counts}.csv', readings)
                shot_counts += 1
        except KeyboardInterrupt():
            GPIO.cleanup()
        print('Maximum number of shot records reached!')
        GPIO.cleanup()