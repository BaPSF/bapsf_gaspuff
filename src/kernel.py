import numpy as np
#import RPi.GPIO as GPIO
import time
#from flow_meter import FlowMeter
from wavegen_control import wavegen_control

from input import generate_pulse_waveform
'''
class FlowMeter(object):
    """
    This class represents a high-level interface for controlling the flow reading system,
    including triggering and data acquisition.
    """
    def __init__(self, gpio_channel) -> None:
        """
        Initialize the flow reader system, including trigger box and flow meter connection.
        
        Parameters
        ----------
        gpio_channel : GPIO pin number for input of the trigger signal.
        """
        # self.wavegen = wavegen_control(server_ip_addr='192.168.1.13')
        self.flow_meter = FlowMeter()
        GPIO.setmode(GPIO.BCM) # set GPIO indexing convention to BCM
        GPIO.setup(gpio_channel, GPIO.IN)
        self.gpio_channel = gpio_channel

    # def set_waveform(self, function, voltages, freq=None, offset=None):
       # if function == 'USER':
           # self.wavegen.send_dac_data(generate_pulse().astype('>i2'))
            # self.wavegen.send_dac_data(generate_pulse(freq, offset).astype('>i2'))
       # self.wavegen.function = function
       # self.wavegen.voltage_level = voltages[0], voltages[1]
       # if freq is not None:
         #   self.wavegen.frequency = freq
       # if offset is not None:
           # self.wavegen.DCoffset = offset

   # def burst_mode(self, ncycles):
       # self.wavegen.burst(enable=True, ncycles=ncycles, phase=0)
        

    def acquire(self, duration, acquisition_limit=100):
        """
        Acquire flow rate measurements from the flow meter for a fixed duration at trigger.
        Currently an upper limit of the number of acquisitions is in place.
        
        Parameters
        ----------
        duration : Duration of a single acquisition in seconds. This should not exceed the period of
        plasma discharge to avoid malfunctioning.
        acquisition_limit : Maximum number of acquisition the command can perform.
        """
        shot_counts = 0
        try:
            while shot_counts <= acquisition_limit:
                print('waiting for signals...')
                GPIO.wait_for_edge(self.gpio_channel, GPIO.RISING) # stop the code until receiving a trigger
                #time.sleep(.1)
                t = time.time()
                #readings = np.array(self.flow_meter.get_reading(duration))
                readings = np.array(self.flow_meter.get_reading_single_cycle(duration))
                #readings = np.array(self.flow_meter.get_single_buffer())
                np.savetxt(f'/home/pi/flow_meter/data/output_single_cycle_{shot_counts}.csv', readings)
                print('shot count {}'.format(shot_counts))
                print(f'shot interval {time.time()-t}')
                shot_counts += 1
        except KeyboardInterrupt:
            GPIO.cleanup()
            print('exit on ctrl-C keyboard interrupt')
        except:
            GPIO.cleanup()
            print('an error occured')
        print('Maximum number of shot records reached!')
        GPIO.cleanup()
'''

class GasPuffValve(object):

    def __init__(self, ip_address) -> None:
        if ip_address is None:
            raise ValueError('IP address must be provided.')
        # Connect to waveform generator
        self.wavegen = wavegen_control(server_ip_addr='192.168.0.106')
        self.puff_time = 10
        self.high_voltage = 0
        self.low_voltage = 0

    def program_waveform(self):
        # Turn off output before applying initial settings
        self.wavegen.output = 0
        data = generate_pulse_waveform() # Define Arbitrary waveform shape
        self.wavegen.send_dac_data(data)

        self.wavegen.frequency = 1 / (2 * self.puff_time * 1e-3) # factor of 2 due to the way waveform shape is written; check generate_pulse_waveform()
        self.wavegen.burst(True, 1, 180)
        self.wavegen.voltage_level = (self.high_voltage, self.low_voltage)
    
    @property
    def high_voltage(self):
        return self._high_voltage

    @high_voltage.setter
    def high_voltage(self, value):
        if value < 0:
            high_voltage = 0
        if value < self.low_voltage:
            print("High voltage is lower than low voltage.")

        self.wavegen.output = 0
        self.wavegen.voltage_level = (value, self.low_voltage)
        self.wavegen.output = 1

    @property
    def low_voltage(self):
        return self._low_voltage

    @low_voltage.setter
    def low_voltage(self, value):
        if value < 0:
            low_voltage = 0
        if value > self.high_voltage:
            print("Low voltage is higher than high voltage.")

        self.wavegen.output = 0
        self.wavegen.voltage_level = (self.high_voltage, value)
        self.wavegen.output = 1

    @property
    def puff_time(self):
        return self._puff_time

    @puff_time.setter
    def puff_time(self, value):
    	# factor of 2 due to the way waveform shape is written; check generate_pulse_waveform()
    	self.wavegen.frequency = 1 / (2 * value * 1e-3)



#-------------------------------------------------------#

if __name__ == '__main__':
    gpc = FlowMeter(gpio_channel=5)
    gpc.acquire(0.3)