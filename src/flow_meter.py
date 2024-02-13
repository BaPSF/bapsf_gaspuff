from sensirion_shdlc_driver import ShdlcSerialPort, ShdlcConnection
from sensirion_shdlc_sfc5xxx import Sfc5xxxShdlcDevice, Sfc5xxxScaling, \
    Sfc5xxxValveInputSource, Sfc5xxxUnitPrefix, Sfc5xxxUnit, \
    Sfc5xxxUnitTimeBase, Sfc5xxxMediumUnit
import time

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
        self.device.activate_calibration(3) # specify calibration file index in list; default now on Helium (3)
        # set default units
        self._unit = Sfc5xxxMediumUnit(
            Sfc5xxxUnitPrefix.ONE,
            Sfc5xxxUnit.STANDARD_LITER,
            Sfc5xxxUnitTimeBase.MINUTE
        )
        self.device.set_user_defined_medium_unit(self._unit)


    def set_units(self, prefix, unit, time_base):
        """
        Set units of output for the flow rate readings. The Sensirion driver has a set of conventions for
        naming the units, and you can use the get_unit_convention() method to view the valid units.

        Parameters
        ----------
        prefix : str
            String literal of the unit prefix (MILLI, MICRO, KILO, etc.) Name has to match exactly with names of
            the Sfx5xxxUnitPrefix enumerator items.
        prefix : str
            String literal of the medium units (STANDARD_LITER, BAR, etc.) Name has to match exactly with names of
            the Sfx5xxxUnit enumerator items.
        prefix : str
            String literal of the unit time base (MILLISECOND, MINUTE, etc.) Name has to match exactly with names of
            the Sfx5xxxUnitTimeBase enumerator items.
        """
        _prefix = None
        _unit = None
        _time_base = None
        for item in Sfc5xxxUnitPrefix:
            if item.name == prefix : _prefix = item
        if _prefix == None : raise KeyError('Invalid unit prefix. Refer to get_unit_convention() for prefix names')
        for item in Sfc5xxxUnit:
            if item.name == unit : _unit = item
        if _unit == None : raise KeyError('Invalid unit. Refer to get_unit_convention() for unit names')
        for item in Sfc5xxxUnitTimeBase:
            if item.name == time_base : _time_base = item
        if _time_base == None : raise KeyError('Invalid time base. Refer to get_unit_convention() for time base names')

        units = Sfc5xxxMediumUnit(_prefix, _unit, _time_base)
        self.device.set_user_defined_medium_unit(units)
        self._unit = units


    def get_unit_convention(self, category):
        """
        A helper method for getting available units for flow meter readings. You can use the 'name' string literals
        to access and set units in the set_units() method.

        Parameters
        ----------
        category : str
            Category of units you want to view. Available options are 'prefix', 'unit', and 'time_base'.
        """
        if category == 'prefix':
            for item in Sfc5xxxUnitPrefix:
                print('name: ', item.name, ', description: ', item.description)
        elif category == 'unit':
            for item in Sfc5xxxUnit:
                print('name: ', item.name, ', description: ', item.description)
        elif category == 'time_base':
            for item in Sfc5xxxUnitTimeBase:
                print('name: ', item.name, ', description: ', item.description)
        else: raise KeyError('Invalid unit category. Choose from prefix, unit, and time_base to view available units')


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
            buffer = self.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED, max_reads=2)
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


