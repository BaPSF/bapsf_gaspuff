from sensirion_shdlc_driver import ShdlcSerialPort, ShdlcConnection
from sensirion_shdlc_sfc5xxx import Sfc5xxxShdlcDevice, Sfc5xxxScaling, \
    Sfc5xxxValveInputSource, Sfc5xxxUnitPrefix, Sfc5xxxUnit, \
    Sfc5xxxUnitTimeBase, Sfc5xxxMediumUnit
import time

with ShdlcSerialPort(port='***PORT NAME HERE***', baudrate=115200) as port:
    device = Sfc5xxxShdlcDevice(ShdlcConnection(port), slave_address=0)

    # select calibration
    device.activate_calibration(0)

    # set units
    unit = Sfc5xxxMediumUnit(
        Sfc5xxxUnitPrefix.MILLI,
        Sfc5xxxUnit.STANDARD_LITER,
        Sfc5xxxUnitTimeBase.MINUTE
    )

    device.set_user_defined_medium_unit(unit)

    # read flow value for 10s
    # try with single value reading
    reading = []
    t = time.time()
    for i in range(10000):
        t += 0.001
        reading.append(device.read_measured_value(Sfc5xxxScaling.USER_DEFINED))
        time.sleep(max(0, t-time.time()))

    # an implementation with buffer reading
    # read_time = []
    # reading = []
    # counter = 0
    # while counter <= 10000:
    #     buffer = device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED)
    #     reading.extend(buffer.values)
    #     read_time.extend([t * 0.001 for t in range(counter, counter + len(buffer.values))])
    #     counter = len(reading)