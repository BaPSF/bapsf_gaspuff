import numpy as np
import matplotlib.pyplot as plt
import RPi.GPIO as GPIO
import time
#from wavegen_control import wavegen_control
from flow_meter import FlowMeter
#from input import generate_pulse

class GasPuffController(object):
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
<<<<<<< HEAD
        plt.ion()
        fig, ax = plt.subplots()
        line, = ax.plot(np.zeros(int(duration*1000)))
        ax.set_title('real time flow rate')
        ax.set_ylabel('flow rate ('+self.flow_meter._unit.__str__()+')')
        t = time.time()
=======
        fig, ax = plt.subplots()
        line, = ax.plot(np.zeros(100))
>>>>>>> b958ecfa45898f915dbcd9bcc629f6a464d36aab
        try:
            while shot_counts <= acquisition_limit:
                print('waiting for signals...')
                GPIO.wait_for_edge(self.gpio_channel, GPIO.RISING) # stop the code until receiving a trigger
                #time.sleep(.1)
                readings = np.array(self.flow_meter.get_reading(duration))
                #readings = np.array(self.flow_meter.get_reading_single_cycle(duration))
                #readings = np.array(self.flow_meter.get_single_buffer())
                np.savetxt(f'/home/pi/flow_meter/data/0208/output_{shot_counts}.csv', readings)
                print('shot count {}'.format(shot_counts))
                print(f'shot interval {time.time()-t}')
<<<<<<< HEAD
                t = time.time()
                line.set_ydata(readings[:int(duration*1000)])
                ax.set_ylim(0,max(readings)*1.2)
=======
                line.set_ydata(readings)
>>>>>>> b958ecfa45898f915dbcd9bcc629f6a464d36aab
                fig.canvas.draw()
                fig.canvas.flush_events()
                shot_counts += 1
            print('Maximum number of shot records reached!')
        except KeyboardInterrupt:
            print('exit on ctrl-C keyboard interrupt')
        except Exception as error:
            print('an error occured:\n', error)
        