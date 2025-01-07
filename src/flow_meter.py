from sensirion_shdlc_driver import ShdlcSerialPort, ShdlcConnection
from sensirion_shdlc_sfc5xxx import Sfc5xxxShdlcDevice, Sfc5xxxScaling, \
    Sfc5xxxValveInputSource, Sfc5xxxUnitPrefix, Sfc5xxxUnit, \
    Sfc5xxxUnitTimeBase, Sfc5xxxMediumUnit
import time
import numpy as np

class FlowMeter(object):
    """
    This class represents the flowmeter device and handles its I/O with the APIs provided by manufacturer
    Sensirion.
    """
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
        
        # Print device information upon initialization
        self._print_device_info()
        
        self.device.activate_calibration(3) # specify calibration file index in list; default now on Helium (3)
        # set units
        self.unit = Sfc5xxxMediumUnit(
            Sfc5xxxUnitPrefix.ONE,
            Sfc5xxxUnit.STANDARD_LITER,
            Sfc5xxxUnitTimeBase.MINUTE
        )
        self.device.set_user_defined_medium_unit(self.unit)

    def _print_device_info(self):
        """Print device information and available calibration blocks."""
        print(f"\nFlow meter at {self.port.port} (slave address: {self.device.slave_address})")
        print("Version:", self.device.get_version())
        print("Product Name:", self.device.get_product_name())
        print("Article Code:", self.device.get_article_code())
        print("Serial Number:", self.device.get_serial_number())
        
        print("\nAvailable gas calibration blocks:")
        for i in range(self.device.get_number_of_calibrations()):
            if self.device.get_calibration_validity(i):
                gas = self.device.get_calibration_gas_description(i)
                fullscale = self.device.get_calibration_fullscale(i)
                unit = self.device.get_calibration_gas_unit(i)
                print(f" - {i}: {fullscale:.2f} {unit} {gas}")
        print()

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
