from sensirion_shdlc_driver import ShdlcSerialPort, ShdlcConnection
from sensirion_shdlc_sfc5xxx import Sfc5xxxShdlcDevice, Sfc5xxxScaling, \
    Sfc5xxxValveInputSource, Sfc5xxxUnitPrefix, Sfc5xxxUnit, \
    Sfc5xxxUnitTimeBase, Sfc5xxxMediumUnit
import time
import RPi.GPIO as GPIO
import numpy as np

class FlowMeter(object):
    """
    This class represents the flowmeter device and handles its I/O with the APIs provided by manufacturer
    Sensirion."""
    def __init__(self, port='/dev/ttyUSB0', baudrate=460800, slave_address=2):
        """
        Initialize connection to the flow meter and other configurations.
        
        Parameters
        ----------
        port : Address of the serial port on local device.
        baudrate : Baud rate for serial communication. Default is the current baud rate setup for the device.
        slave_address : Address of the device in the master-slave model of the device control model.
            Typically no need to change.
        """
        self.port = ShdlcSerialPort(port=port, baudrate=baudrate) # setup serial port
        self.device = Sfc5xxxShdlcDevice(ShdlcConnection(self.port), slave_address=slave_address)
        self.device.activate_calibration(3) # specify calibration file index in list; default now on Helium (3)
        # set units
        self.unit = Sfc5xxxMediumUnit(
            Sfc5xxxUnitPrefix.ONE,
            Sfc5xxxUnit.STANDARD_LITER,
            Sfc5xxxUnitTimeBase.MINUTE
        )
        self.device.set_user_defined_medium_unit(self.unit)


    def set_baudrate(self, baudrate):
        """
        Set baudrate for serial communication.
        """
        self.device.set_baudrate(baudrate)


    def set_slave_address(self, slave_address):
        """
        Set slave address for the flow meter. Do not change unless necessary.
        """
        self.device.set_slave_address(slave_address)


    def get_reading(self, duration):
        """
        Retrieve flow readings from the flow meter for a fixed duration
        by repeatedly reading from its buffer. Duration in unit of seconds.
        """
        reading = []
        # dump what's already inside the buffer. this is only useful for low overhead
        #buffer = self.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED)
        while len(reading) <= duration * 1000: # flow meter reads at 1kHz
            buffer = self.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED)
            #print(buffer.sampling_time) # the only "time" returned from read buffer command
            reading.extend(buffer.values)
        return reading
    
    def get_single_buffer(self):
        #dump = self.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED)
        buffer = self.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED, max_reads=1)
        return buffer.values
    
    def get_reading_single_cycle(self, duration):
        reading = []
        n = int(duration * 1000)
        for i in range(n):
            val = self.device.read_measured_value(Sfc5xxxScaling.USER_DEFINED)
            reading.append(val)
        return reading

    def get_pre_and_post_trigger_samples(self, pretrigger_samples=10, posttrigger_samples=90):
        """
        Retrieve pre-trigger and post-trigger samples from the flow meter buffer.
        """
        samples = []
        buffer = self.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED)
        # Get the last samples of the buffer (is this the correct side of the array? Not sure)
        samples.extend(buffer.values[-pretrigger_samples:])
        # Loop over the next values to get the post-trigger samples
        for i in range(posttrigger_samples):
            samples.append(self.device.read_measured_value(Sfc5xxxScaling.USER_DEFINED))
        
        return samples


"""
This class represents a high-level interface for controlling the flow reading system,
including triggering and data acquisition.
"""
def initialize(self, gpio_channel) -> None:
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


# below is the standalone implementation of flow meter reading for reference
'''
with ShdlcSerialPort(port='/dev/ttyUSB0', baudrate=460800) as port:   
    device = Sfc5xxxShdlcDevice(ShdlcConnection(port), slave_address=2)
    
    # select calibration
    print('activate calibration...')
    device.activate_calibration(3)
    
    # set units
    unit = Sfc5xxxMediumUnit(
        Sfc5xxxUnitPrefix.ONE,
        Sfc5xxxUnit.STANDARD_LITER,
        Sfc5xxxUnitTimeBase.MINUTE
    )

    device.set_user_defined_medium_unit(unit)

    # read flow value for 10s
    # try with single value reading
    print('start acquiring...')
    
    # an implementation with buffer reading
    read_time = []
    reading = []
    t = time.time()
    buffer = device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED) # dump what's already inside the buffer
    while len(reading) <= 3000:
        buffer = device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED)
        reading.extend(buffer.values)
        if buffer.lost_values != 0:
            print('lost values detected!')
        #read_time.extend([t * 0.001 for t in range(counter, counter + len(buffer.values))])
        #print(buffer.lost_values)
    print('execution time:', time.time()-t)
    
    
    output = open('flow_reading.txt', 'w', encoding='utf-8')
    #output_time = open('sampling_time.txt', 'w', encoding='utf-8')
    for r in reading:
        output.write(str(r))
        output.write("\n")
    #for t in read_time:
        #output_time.write(str(t))
        #output_time.write("\n")
    print('success')
    output.close()
    #output_time.close()
'''
