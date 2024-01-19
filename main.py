from sensirion_shdlc_driver import ShdlcSerialPort, ShdlcConnection
from sensirion_shdlc_sfc5xxx import Sfc5xxxShdlcDevice, Sfc5xxxScaling, \
    Sfc5xxxValveInputSource, Sfc5xxxUnitPrefix, Sfc5xxxUnit, \
    Sfc5xxxUnitTimeBase, Sfc5xxxMediumUnit
import time

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
    
    '''
    reading = []
    t = time.time()
    for i in range(300):
        t += 0.001
        reading.append(device.read_measured_value(Sfc5xxxScaling.USER_DEFINED))
    print('execution time:', time.time()-t)
    '''
    
    
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
