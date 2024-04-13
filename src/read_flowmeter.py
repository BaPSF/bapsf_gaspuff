from flow_meter import FlowMeter
from RPi import GPIO
import datetime
import numpy as np
import time
import multiprocessing as mp
import queue

from sensirion_shdlc_driver import ShdlcSerialPort, ShdlcConnection
from sensirion_shdlc_sfc5xxx import Sfc5xxxShdlcDevice, Sfc5xxxScaling, \
    Sfc5xxxValveInputSource, Sfc5xxxUnitPrefix, Sfc5xxxUnit, \
    Sfc5xxxUnitTimeBase, Sfc5xxxMediumUnit


def read_flowmeter(q_trigger, q_data, flow_meter_port, slave_address, wait_time=0.1):
    # Set up flow meter class that handles communication and whatnot. Assumes Helium gas.
    fm = FlowMeter(port=flow_meter_port, slave_address=slave_address)

    while True:
        trigger = q_trigger.get()
        if trigger == 'QUIT':
            return
        elif trigger == 'TRIG':
            # Wait so that both pre- and post-trigger samples are recorded
            time.sleep(wait_time)
            # Only use buffer readouts for maximum speed. 3 takes usually ~70 ms.
            t0 = time.time()
            buff = fm.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED, max_reads=3)
            t1 = time.time() - t0
            print(' - device: {}, '.format(slave_address) +
                  'read_count: {}, '.format(buff.read_count) +
                  'lost_values: {}, '.format(buff.lost_values) +
                  'remaining_values: {}, '.format(buff.remaining_values) +
                  'sampling_time: {:0.4} '.format(buff.sampling_time) +
                  'read time: {:0.4}'.format(t1))

            q_data.put((slave_address, buff.values))
        else:
            print('Unknown command: \'' + str(trigger) + '\'')
        # Wait a little bit so the other flow meter gets a chance to get triggered in case this one
        #   is exceptionally fast.
        time.sleep(0.1)


# Send data to the read-msi-tcp.py script on another PC on the network. That script stores all MSI
#   for each shot in an HDF5 file.
def send_data(q, HOST, PORT):
    pass


if __name__ == '__main__':
    trigger_pin = 25  # For the gas puff trigger
    # Flow meter serial port should be deterministic when referenced in this way instead of using
    #   /dev/ttyUSB{0,1}.
    portEast = '/dev/serial/by-id/usb-FTDI_USB-RS485_Cable_AU05D9B7-if00-port0'
    portWest = '/dev/serial/by-id/usb-FTDI_USB-RS485_Cable_AU050ZDN-if00-port0'
    # This slave address is important if you have more than one device on the same serial port
    #   (I think) which we do not do, but we'll use it as a unique identifier.
    addrEast = 2
    addrWest = 0

    # Size of numpy array buffer for holding the samples. The gas puff buffer seems to be ~174
    #   samples long, and 3 calls of reading the buffer is 3*60 = 180 samples. I'm not sure if it's
    #   guaranteed to be 60 samples long per call, but that has been the case so far.
    num_samples = 180
    # Time to wait before reading off the buffer. Reading 110 ms seems to get the full waveform
    wait_time = 0.110

    # Set up the queues for exchanging data with the subprocesses.
    # q_trigger tells the subprocess to trigger or quit for a clean exit.
    q_trigger = mp.Queue()
    # Data from the flow meters are placed in q_data to be processed by the main process.
    q_data = mp.Queue()

    print("Starting flow meter threads")
    # Launch the subprocesses. mp.Process is used because it allows us to get around the global
    #    interpreter lock and read in both flow meters simultaneously.
    eastProcess = mp.Process(target=read_flowmeter, args=(q_trigger, q_data, portEast, addrEast, wait_time))
    westProcess = mp.Process(target=read_flowmeter, args=(q_trigger, q_data, portWest, addrWest, wait_time))
    eastProcess.start()
    westProcess.start()

    n_loop = 200

    # List to store the sample numpy arrays
    samp_list = []

    try:
        # GPIO seems to need to be initilized inside of the try block. I am not sure why -- code
        #   fails when the following GPIO code is placed at the top of the program.
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(trigger_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        print("GPIO setup - complete")

        for i in range(n_loop):
            # Timeout after 500 ms so that we are not stuck waiting for a trigger forever.
            trigger = GPIO.wait_for_edge(trigger_pin, GPIO.RISING, timeout=500)
            if trigger is None:
                pass
                # print('trigger timeout')  # check for quit signal
            else:
                print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + ': ', end='')
                # Trigger the flow meter readout. Put two -- one for each flow meter.
                # Also check to see that both processes are alive so that we don't trigger one
                #    process twice if the other one died.
                if eastProcess.is_alive():
                    q_trigger.put('TRIG')
                else:
                    print("East process is dead. ", end='')

                if westProcess.is_alive():
                    q_trigger.put('TRIG')
                else:
                    print("West process is dead. ", end='')

                if not eastProcess.is_alive() and not westProcess.is_alive():
                    print("Both flow meter processes are dead. Exiting.")
                    break

                try:
                    # Default value of -1 so that it's obvious when data is not saved
                    temp_samples = -1 * np.ones((2, 180), dtype=np.float32)
                    # Note that data is put in the queue in any order. If one of the flow meters
                    #   goes offline, then it will timeout in the get(timeout=1) section, and the
                    #   data will remain as an numpy array of value -1.
                    # 6 seconds because shots may be 4s apart and it may take a while to read from
                    #   the flow meter.
                    qd = q_data.get(timeout=6)
                    # Figure out which flow meter the data came from and fill that part of the array
                    if qd[0] == addrEast:
                        temp_samples[0, :len(qd[1])] = qd[1]
                    elif qd[0] == addrWest:
                        temp_samples[1, :len(qd[1])] = qd[1]
                    print('{}, '.format('East' if qd[0] == 2 else 'West'), end='')

                    # Same thing, but for the second flow meter.
                    qd = q_data.get(timeout=1)
                    if qd[0] == addrEast:
                        temp_samples[0, :len(qd[1])] = qd[1]
                    elif qd[0] == addrWest:
                        temp_samples[1, :len(qd[1])] = qd[1]
                    print('{}'.format('East' if qd[0] == 2 else 'West'), end='')

                    samp_list.append(temp_samples)

                # If one of the flow meters is unresponsive, then throw an exception.
                except queue.Empty:
                    print('Exception: no data from flow meter(s)')
                print('')

    except KeyboardInterrupt:
        print("Interrupting processes...")

    finally:
        np.savez('saved_flowmeter', samples=np.array(samp_list))

        q_trigger.put('QUIT')
        q_trigger.put('QUIT')
        # eastProcess.terminate()
        # westProcess.terminate()
        eastProcess.join()
        westProcess.join()
        eastProcess.close()
        westProcess.close()
        GPIO.cleanup()

        print('Done')
