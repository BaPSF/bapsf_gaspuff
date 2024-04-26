import time
from sensirion_shdlc_driver import ShdlcSerialPort, ShdlcConnection
from sensirion_shdlc_sfc5xxx import Sfc5xxxShdlcDevice, Sfc5xxxScaling, \
    Sfc5xxxValveInputSource, Sfc5xxxUnitPrefix, Sfc5xxxUnit, \
    Sfc5xxxUnitTimeBase, Sfc5xxxMediumUnit

print('Flow meter #2 (East/near)')

portWest = '/dev/serial/by-id/usb-FTDI_USB-RS485_Cable_AU050ZDN-if00-port0'
# portEast = '/dev/ttyUSB0'
portEast = '/dev/serial/by-id/usb-FTDI_USB-RS485_Cable_AU05D9B7-if00-port0'
# portEast = '/dev/ttyUSB1'

with ShdlcSerialPort(port=portEast, baudrate=460800) as port:
    device = Sfc5xxxShdlcDevice(ShdlcConnection(port), slave_address=2)

    # Print some device information
    print("Version: {}".format(device.get_version()))
    print("Product Name: {}".format(device.get_product_name()))
    print("Article Code: {}".format(device.get_article_code()))
    print("Serial Number: {}".format(device.get_serial_number()))

    # List all available calibration blocks
    print("Available gas calibration blocks:")
    for i in range(device.get_number_of_calibrations()):
        if device.get_calibration_validity(i):
            gas = device.get_calibration_gas_description(i)
            fullscale = device.get_calibration_fullscale(i)
            unit = device.get_calibration_gas_unit(i)
            print(" - {}: {:.2f} {} {}".format(i, fullscale, unit, gas))

print('====================')

print('Flow meter #1 (West/far)')
with ShdlcSerialPort(port=portWest, baudrate=460800) as port:
    device = Sfc5xxxShdlcDevice(ShdlcConnection(port), slave_address=0)

    # Print some device information
    print("Version: {}".format(device.get_version()))
    print("Product Name: {}".format(device.get_product_name()))
    print("Article Code: {}".format(device.get_article_code()))
    print("Serial Number: {}".format(device.get_serial_number()))

    # List all available calibration blocks
    print("Available gas calibration blocks:")
    for i in range(device.get_number_of_calibrations()):
        if device.get_calibration_validity(i):
            gas = device.get_calibration_gas_description(i)
            fullscale = device.get_calibration_fullscale(i)
            unit = device.get_calibration_gas_unit(i)
            print(" - {}: {:.2f} {} {}".format(i, fullscale, unit, gas))
