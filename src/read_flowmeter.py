from flow_meter import FlowMeter
from RPi import GPIO
import datetime
import numpy as np
import time
import multiprocessing as mp
import queue
import socket
import struct
import errno

from sensirion_shdlc_sfc5xxx import Sfc5xxxScaling


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
            # t0 and t1 for timing, if wanted
            # t0 = time.time()
            # Only use buffer readouts for maximum speed. 3 takes usually ~70 ms.
            buff = fm.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED, max_reads=3)
            # t1 = time.time() - t0
            # print(' - device: {}, '.format(slave_address) +
            #       'read_count: {}, '.format(buff.read_count) +
            #       'lost_values: {}, '.format(buff.lost_values) +
            #       'remaining_values: {}, '.format(buff.remaining_values) +
            #       'sampling_time: {:0.4} '.format(buff.sampling_time) +
            #       'read time: {:0.4}'.format(t1))

            q_data.put((slave_address, buff.values))
        else:
            print('Unknown command: \'' + str(trigger) + '\'')
        # Wait a little bit so the other flow meter gets a chance to get triggered in case this one
        #   is exceptionally fast.
        time.sleep(0.1)


# Send data to the read-msi-tcp.py script on another PC on the network. That script stores all MSI
#   for each shot in an HDF5 file. This code is a server and read-msi-tcp.py is a client.
def send_data(q, q_quit, HOST, PORT):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, PORT))
    sock.settimeout(5)  # Wait at most 5 seconds before timing out and checking the quit queue
    sock.listen()

    # Only exit when it's told to quit
    while True:
        connected = False
        # Try to accept a connection, timeout and check the quit queue if there isn't one
        try:
            print('Waiting for connection...')
            connection, address = sock.accept()
            print('Connected')
            connected = True
        except socket.timeout:
            pass
            # print("Socket timed out")
        # So that the program can quit while waiting for a connection
        finally:
            if q_quit.empty():
                pass
            elif q_quit.get() == 'QUIT':
                connection.close()
                return

        # Only read from the queue if there's a connection. If there's nothing in the queue then
        #   timeout and check for a quit signal.
        while True and connected:
            try:
                gp_samples = q.get(timeout=1)
                gp_samples_size = gp_samples.size * gp_samples.itemsize
                # The socket code below shouldn't run if the queue is empty
                connection.send(struct.pack(">i", gp_samples_size))
                connection.sendall(gp_samples.tobytes())
                print(" --> msi ", end="", flush=True)
            except socket.error as e:
                if e.errno == errno.EPIPE:
                    print("Error or client disconnected")
                    connected = False
                    connection.close()
            except KeyboardInterrupt:
                print("Keyboard interrupt")
                # print("Closing data connection (keyboard interrupt)")
                # connection.close()
            except queue.Empty:
                pass  # no problem
            except socket.timeout:
                print("Socket timed out")
                connected = False
                connection.close()
            except Exception as e:
                print(e)
                # print("Closing data connection")
                # connection.close()
            # See if we need to quit
            finally:
                if q_quit.empty():
                    pass
                elif q_quit.get() == 'QUIT':
                    connection.close()
                    return

        time.sleep(0.2)


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
    # Queue to send quit signals to the server process
    q_quit_server = mp.Queue()
    # Data to send to read-msi-tcp. Max_size = 1 so we do not fill up memory when not connected.
    q_data_server = mp.Queue(maxsize=1)

    print("Starting flow meter threads")
    # Launch the subprocesses. mp.Process is used because it allows us to get around the global
    #    interpreter lock and read in both flow meters simultaneously.
    eastProcess = mp.Process(target=read_flowmeter, args=(q_trigger, q_data, portEast, addrEast, wait_time))
    westProcess = mp.Process(target=read_flowmeter, args=(q_trigger, q_data, portWest, addrWest, wait_time))
    eastProcess.start()
    westProcess.start()

    print("Starting server thread")
    HOST = '192.168.7.38'
    PORT = 5008  # Doesn't matter too much as long as it doesn't conflict with anything else
    serverProcess = mp.Process(target=send_data, args=(q_data_server, q_quit_server, HOST, PORT))
    serverProcess.start()

    # List to store the sample numpy arrays
    # samp_list = []
    # n_loop = 200
    try:
        # GPIO seems to need to be initilized inside of the try block. I am not sure why -- code
        #   fails when the following GPIO code is placed at the top of the program.
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(trigger_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        print("GPIO setup - complete")

        # for i in range(n_loop):
        while True:
            # Timeout after 500 ms so that we are not stuck waiting for a trigger forever.
            trigger = GPIO.wait_for_edge(trigger_pin, GPIO.RISING, timeout=500)
            if trigger is None:
                pass
                # print('trigger timeout')  # check for quit signal
            else:
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
                    print("\n" + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + ': ', end='')
                    qd = q_data.get(timeout=6)
                    # Figure out which flow meter the data came from and fill that part of the array
                    if qd[0] == addrEast:
                        temp_samples[0, :len(qd[1])] = qd[1]
                    elif qd[0] == addrWest:
                        temp_samples[1, :len(qd[1])] = qd[1]
                    print('{}, '.format('East (#2)' if qd[0] == 2 else 'West (#1)'), end='', flush=True)

                    # Same thing, but for the second flow meter.
                    qd = q_data.get(timeout=1)
                    if qd[0] == addrEast:
                        temp_samples[0, :len(qd[1])] = qd[1]
                    elif qd[0] == addrWest:
                        temp_samples[1, :len(qd[1])] = qd[1]
                    print('{}'.format('East (#2)' if qd[0] == 2 else 'West (#1)'), end='', flush=True)

                    # samp_list.append(temp_samples)

                    # Check to see if the server queue (max capacity of 1) is full so that multiple
                    #   shots are not sent (only one shot is sent).
                    if q_data_server.full():
                        q_data_server.get()  # Clear out the unsent data
                    q_data_server.put(temp_samples)

                    time.sleep(0.1)

                # If both of the flow meters are unresponsive (no data), then throw an exception.
                except queue.Empty:
                    print('Exception: no data from flow meter(s)')

    except KeyboardInterrupt:
        print("Interrupting processes...")

    finally:
        # np.savez('saved_flowmeter', samples=np.array(samp_list))

        q_trigger.put('QUIT')
        q_trigger.put('QUIT')
        q_quit_server.put('QUIT')
        # eastProcess.terminate()
        # westProcess.terminate()
        eastProcess.join()
        westProcess.join()
        serverProcess.join()
        eastProcess.close()
        westProcess.close()
        serverProcess.close()

        GPIO.cleanup()

        print('Done')
