from sensirion_shdlc_driver import ShdlcSerialPort, ShdlcConnection
from sensirion_shdlc_sfc5xxx import Sfc5xxxShdlcDevice, Sfc5xxxScaling, \
    Sfc5xxxValveInputSource, Sfc5xxxUnitPrefix, Sfc5xxxUnit, \
    Sfc5xxxUnitTimeBase, Sfc5xxxMediumUnit
import time

class FlowMeter(object):
    def __init__(self, port='/dev/ttyUSB0', baudrate=460800, slave_address=2):
        with ShdlcSerialPort(port=port, baudrate=baudrate) as p:
            self.device = Sfc5xxxShdlcDevice(ShdlcConnection(p), slave_address=slave_address)
            self.device.activate_calibration(3)
            # set units
            self.unit = Sfc5xxxMediumUnit(
                Sfc5xxxUnitPrefix.ONE,
                Sfc5xxxUnit.STANDARD_LITER,
                Sfc5xxxUnitTimeBase.MINUTE
            )
            self.device.set_user_defined_medium_unit(self.unit)


    def set_baudrate(self, baudrate):
        self.device.set_baudrate(baudrate)


    def set_slave_address(self, slave_address):
        self.device.set_slave_address(slave_address)


    def get_reading(self, duration):
        reading = []
        buffer = self.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED) # dump what's already inside the buffer
        while len(reading) <= duration * 1000:
            buffer = self.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED)
            reading.extend(buffer.values)
        return reading
        
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
