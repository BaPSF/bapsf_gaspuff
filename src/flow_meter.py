from sensirion_shdlc_driver import ShdlcSerialPort, ShdlcConnection
from sensirion_shdlc_sfc5xxx import Sfc5xxxShdlcDevice, Sfc5xxxScaling, \
    Sfc5xxxValveInputSource, Sfc5xxxUnitPrefix, Sfc5xxxUnit, \
    Sfc5xxxUnitTimeBase, Sfc5xxxMediumUnit
import time

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
        self._unit = Sfc5xxxMediumUnit(
            Sfc5xxxUnitPrefix.ONE,
            Sfc5xxxUnit.STANDARD_LITER,
            Sfc5xxxUnitTimeBase.MINUTE
        )
        self.device.set_user_defined_medium_unit(self._unit)


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
