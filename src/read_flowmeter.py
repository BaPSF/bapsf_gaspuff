from flow_meter import FlowMeter
from RPi import GPIO
import datetime
import numpy as np

from sensirion_shdlc_driver import ShdlcSerialPort, ShdlcConnection
from sensirion_shdlc_sfc5xxx import Sfc5xxxShdlcDevice, Sfc5xxxScaling, \
    Sfc5xxxValveInputSource, Sfc5xxxUnitPrefix, Sfc5xxxUnit, \
    Sfc5xxxUnitTimeBase, Sfc5xxxMediumUnit

if __name__ == '__main__':
    trigger_pin = 25
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(trigger_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    print("GPIO setup - complete")

    portEast = '/dev/serial/by-id/usb-FTDI_USB-RS485_Cable_AU05D9B7-if00-port0'
    portWest = '/dev/serial/by-id/usb-FTDI_USB-RS485_Cable_AU050ZDN-if00-port0'
    # Assumes Helium gas
    fm_east = FlowMeter(port=portEast, slave_address=2)
    fm_west = FlowMeter(port=portWest, slave_address=0)

    print("Flow meters - setup complete")

    print("Acquiring samples...")

    pretrigger_samples = 0
    posttrigger_samples = 20

    np_samples = np.empty((0, 2, pretrigger_samples + posttrigger_samples), dtype=np.float32)
    for i in range(20):
        # Keep this value shorter than the shortest possible shot time so that these processes
        #   can be terminated easily.
        trigger = GPIO.wait_for_edge(trigger_pin, GPIO.RISING, timeout=500)
        if trigger is None:
            print('timeout')  # check for quit signal
        else:
            # Get 10 samples before and 91 samples after the trigger (101 samples). This should be
            #   about 101 ms to make sure we get all the samples.
            # samples_east = fm_east.get_pre_and_post_trigger_samples(pretrigger_samples,
            #                                                         posttrigger_samples)
            # samples_west = fm_west.get_pre_and_post_trigger_samples(pretrigger_samples,
            #                                                         posttrigger_samples)
            samples_east = []
            samples_west = []

            for s in range(posttrigger_samples):
                samples_east.append(fm_east.device.read_measured_value(Sfc5xxxScaling.USER_DEFINED))
                samples_west.append(fm_west.device.read_measured_value(Sfc5xxxScaling.USER_DEFINED))

            np_samples = np.append(np_samples, np.vstack([np.array(samples_east),
                                                          np.array(samples_west)])[np.newaxis, :, :],
                                   axis=0)

            print(datetime.datetime.now(), end='')
            print("-- flow meter samples acquired")

    np.savez('saved_flowmeter', samples=np_samples)

    print('Done')
    GPIO.cleanup()
