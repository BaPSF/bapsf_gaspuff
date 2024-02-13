import numpy as np
import matplotlib.pyplot as plt
import RPi.GPIO as GPIO
import time, os, shutil
from datetime import datetime
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

    def check_disk_space(required_space_mb, path="/"):
        """
        Check if there is enough disk space available.

        Parameters
        ----------
        required_space_mb : float
            The required space in megabytes.
        path : str, optional
            The path to check the disk space of. Default is root.
        
        Returns
        -------
        bool
            True if there is enough space, False otherwise.
        """
        total, used, free = shutil.disk_usage(path)
        free_mb = free / 1024**2  # Convert bytes to MB
        return free_mb >= required_space_mb

    def acquire(self, path, duration):
        """
        Acquire flow rate measurements from the flow meter for a fixed duration at trigger.
        Currently an upper limit of the number of acquisitions is in place.
        
        Parameters
        ----------
        path : str
            Path to the diectory that stores acquired data.
        duration : float
            Duration of a single acquisition in seconds. This should not exceed the period of
            plasma discharge to avoid malfunctioning.
        """
        shot_counts = 0
        data_folder = path

        plt.ion()
        fig, ax = plt.subplots()
        line, = ax.plot(np.zeros(int(duration*1000)))
        ax.set_title('real time flow rate')
        ax.set_ylabel('flow rate ('+self.flow_meter._unit.__str__()+')')
        t = time.time()
        try:
            while True:
                if not self.check_disk_space(5, data_folder):  # Check for at least 5 MB of free space
                    print("Insufficient disk space. Please free up space to continue.")
                    break

                file_path = os.path.join(data_folder, f'output_{shot_counts}.csv')

                print('waiting for signals...')
                try:
                    GPIO.wait_for_edge(self.gpio_channel, GPIO.RISING) # stop the code until receiving a trigger
                    readings = np.array(self.flow_meter.get_reading(duration))
                except:
                    print(f'Connection interrupted at {datetime.now().time()}. Trying to reconnect...')
                    max_attempts = 3
                    reconnection_attempt = 0
                    connection = False
                    while reconnection_attempt < max_attempts:
                        try:
                            time.sleep(10) # sleep for 10s before restarting connection
                            self.flow_meter = FlowMeter()
                            GPIO.wait_for_edge(self.gpio_channel, GPIO.RISING) # stop the code until receiving a trigger
                            readings = np.array(self.flow_meter.get_reading(duration))
                            connection = True
                            break
                        except Exception as e:
                            print(f'Reconnection attempt {reconnection_attempt+1} failed. Reconnecting...')
                            reconnection_attempt += 1
                            pass
                    if connection:
                        print('Cnnection resumed')
                        pass
                    else:
                        print('Maximum reconnection attempt reached. Program terminate with error:\n', e)
                
                np.savetxt(file_path, readings)
                print('shot count {}'.format(shot_counts))
                print(f'shot interval {time.time()-t}')
                t = time.time()

                line.set_ydata(readings[:int(duration*1000)])
                ax.set_ylim(0,max(readings)*1.2)
                fig.canvas.draw()
                fig.canvas.flush_events()

                shot_counts += 1

        except KeyboardInterrupt:
            print('exit on Ctrl-C keyboard interrupt')
        except Exception as e:
            print('An error occured:\n', e)
        