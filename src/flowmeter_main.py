"""Flow-meter data-acquisition entry point.

Runs on a Raspberry Pi: waits for a rising-edge GPIO trigger, fans the trigger
out to two child processes (one per Sensirion SFC5xxx flow meter), and writes
each shot's samples to a daily HDF5 file.

The C-level GPIO helper at :data:`GPIO_LIB_PATH` and the serial port paths
under ``/dev/serial/by-id/...`` are Pi-specific; this module is not expected
to import or run successfully on a development workstation.
"""

import ctypes
import datetime
import multiprocessing as mp
import os
import queue
import time

import h5py
import numpy as np

from FlowMeterCommunication import FlowMeter
from sensirion_shdlc_sfc5xxx import Sfc5xxxScaling

# --- Configuration ----------------------------------------------------------
# Storage
HDF5_PATH = '/home/pi/flow_meter/data'

# GPIO trigger
GPIO_LIB_PATH = "/home/generalpi/pi_gpio/gpio_detect.so"
TRIGGER_PIN = 25
TRIGGER_TIMEOUT_MS = 500

# Flow meters (East = slave 2, West = slave 0)
PORT_EAST = '/dev/serial/by-id/usb-FTDI_USB-RS485_Cable_AU05D9B7-if00-port0'
PORT_WEST = '/dev/serial/by-id/usb-FTDI_USB-RS485_Cable_AU050ZDN-if00-port0'
ADDR_EAST = 2
ADDR_WEST = 0

# Per-shot acquisition timing
READ_WAIT_S = 0.110            # Settle time after TRIG before reading the buffer
FIRST_METER_TIMEOUT_S = 6      # First queue.get may need to wait for the trigger to propagate
SECOND_METER_TIMEOUT_S = 1     # Second meter should already be ready by then
LOOP_SLEEP_S = 0.1
RETRY_BACKOFF_S = 1.0
MAX_FLOW_METER_RETRIES = 3


class GPIOHandler:
    """
    Handles GPIO operations using the gpio_detect.so C library
    """
    def __init__(self, trigger_pin):
        self.trigger_pin = trigger_pin

        # Load the GPIO C library
        if not os.path.exists(GPIO_LIB_PATH):
            raise FileNotFoundError(f"GPIO library not found at {GPIO_LIB_PATH}")

        try:
            self.gpio_lib = ctypes.CDLL(GPIO_LIB_PATH)
        except OSError as e:
            raise RuntimeError(f"Failed to load GPIO library: {e}")

        self._setup_gpio_functions()

        # Initialize GPIO
        if self.gpio_lib.initialize_pigpio() < 0:
            raise RuntimeError("Failed to initialize pigpio")

        # Setup trigger pin for input
        if self.gpio_lib.setup_gpio_pin(self.trigger_pin) < 0:
            raise RuntimeError(f"Failed to setup input pin {self.trigger_pin}")

    def _setup_gpio_functions(self):
        """Setup C function signatures"""
        # Initialize and cleanup
        self.gpio_lib.initialize_pigpio.restype = ctypes.c_int
        self.gpio_lib.terminate_pigpio.restype = None

        # Pin setup
        self.gpio_lib.setup_gpio_pin.argtypes = [ctypes.c_int]
        self.gpio_lib.setup_gpio_pin.restype = ctypes.c_int

        # GPIO operations
        self.gpio_lib.wait_for_gpio_high.argtypes = [ctypes.c_int, ctypes.c_int]
        self.gpio_lib.wait_for_gpio_high.restype = ctypes.c_bool

    def wait_for_trigger(self, timeout_ms=TRIGGER_TIMEOUT_MS):
        """
        Wait for rising edge on trigger pin.

        ``timeout_ms`` is converted to microseconds for the C library.
        """
        return self.gpio_lib.wait_for_gpio_high(self.trigger_pin, timeout_ms * 1000)

    def cleanup(self):
        """Cleanup GPIO resources"""
        self.gpio_lib.terminate_pigpio()


def get_current_day(timestamp):
    """Gets current day-of-year from a Unix timestamp."""
    ct = time.localtime(timestamp)
    return ct.tm_yday


def init_hdf5_file(file_name, east_info=None, west_info=None):
    """
    Initialize HDF5 file for flow meter data storage.

    No-op when ``file_name`` already exists. On first creation, writes the
    file-level attributes and creates an empty ``flow_data`` / ``timestamp``
    dataset pair under each meter's group; the datasets are resized when the
    first shot establishes the per-shot sample length.

    Parameters
    ----------
    file_name : str
        Path to HDF5 file
    east_info, west_info : tuple
        (port, address) for each flow meter
    """
    if os.path.exists(file_name):
        print("HDF5 file exists")
        return

    timestamp = time.time()
    with h5py.File(file_name, "w", libver='latest') as f:
        ct = time.localtime(timestamp)
        f.attrs['created'] = ct
        print("HDF5 file created", time.strftime("%Y-%m-%d %H:%M:%S", ct))
        f.attrs['description'] = "Flow meter data from Sensirion flow meters"
        f.attrs['data_length'] = None  # Will be set when first data is received

        # Create groups for each flow meter
        for name, info in [("East", east_info), ("West", west_info)]:
            if info is None:
                continue

            grp = f.require_group(f"FlowMeter_{name}")
            grp.attrs['description'] = f"Flow measurements from {name} flow meter"
            grp.attrs['port'] = info[0]
            grp.attrs['address'] = info[1]
            grp.attrs['unit'] = "standard liter per minute"

            # Create datasets with initial size 0
            # Actual size will be set when first data is received
            grp.create_dataset("flow_data", (0, 0), maxshape=(None, None), dtype=np.float32)
            grp.create_dataset("timestamp", (0,), maxshape=(None,), dtype=np.float64)


def read_flowmeter(q_trigger, q_data, flow_meter_port, slave_address, wait_time=READ_WAIT_S):
    """Process function to continuously read from a flow meter device."""
    fm = None
    consecutive_errors = 0

    try:
        while True:
            trigger = q_trigger.get()
            if trigger == 'QUIT':
                if fm is not None:
                    fm.close()
                return
            elif trigger == 'TRIG':
                timestamp = time.time()
                try:
                    # Try to initialize flow meter if it's not connected
                    if fm is None:
                        fm = FlowMeter(port=flow_meter_port, slave_address=slave_address)
                        consecutive_errors = 0  # Reset error counter on successful connection

                    with fm:
                        time.sleep(wait_time)
                        buff = fm.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED, max_reads=3)
                        q_data.put((slave_address, buff.values, timestamp))
                        consecutive_errors = 0  # Reset error counter on successful read

                except Exception as e:
                    consecutive_errors += 1
                    print(f"Flow meter {slave_address} error ({consecutive_errors}/{MAX_FLOW_METER_RETRIES}): {e}")

                    # Send NaN data on failure
                    q_data.put((slave_address, np.nan, timestamp))

                    # Close and reset connection
                    if fm is not None:
                        try:
                            fm.close()
                        except Exception:
                            pass
                        fm = None

                    # Add delay between retries
                    if consecutive_errors < MAX_FLOW_METER_RETRIES:
                        time.sleep(RETRY_BACKOFF_S)
                    else:
                        print(f"Flow meter {slave_address} failed after {MAX_FLOW_METER_RETRIES} attempts")
                        consecutive_errors = 0  # Reset for next trigger

            else:
                print(f"Unknown command: '{trigger}'")

            time.sleep(LOOP_SLEEP_S)

    except Exception as e:
        print(f"Fatal error in flow meter {slave_address} process: {e}")
        raise
    finally:
        if fm is not None:
            try:
                fm.close()
            except Exception:
                pass


def save_flow_data(f, flow_data, timestamp, is_east):
    """
    Save flow data to appropriate group in HDF5 file.
    Handles both normal data and NaN data from failed readings.
    """
    data_length = f.attrs.get('data_length')

    # If flow_data is NaN (failed reading), create array of NaNs
    if np.isscalar(flow_data) and np.isnan(flow_data):
        if data_length is None:
            print("Cannot save NaN data - data_length not yet established")
            return False
        flow_data = np.full(data_length, np.nan)
    else:
        # Normal data handling
        if data_length is None:
            # First successful reading establishes data length
            data_length = len(flow_data)
            f.attrs['data_length'] = data_length
            # Resize datasets to proper width
            for grp_name in ["FlowMeter_East", "FlowMeter_West"]:
                if grp_name in f:
                    f[grp_name]["flow_data"].resize((0, data_length))
        elif len(flow_data) != data_length:
            print(f"Warning: Data length mismatch. Expected {data_length}, got {len(flow_data)}")
            return False

    grp_name = "FlowMeter_East" if is_east else "FlowMeter_West"
    grp = f[grp_name]

    # Extend datasets
    flow_dataset = grp["flow_data"]
    time_dataset = grp["timestamp"]

    flow_dataset.resize((flow_dataset.shape[0] + 1, flow_dataset.shape[1]))
    time_dataset.resize((time_dataset.shape[0] + 1,))

    # Save data
    flow_dataset[-1, :] = flow_data
    time_dataset[-1] = timestamp

    return True


def _format_meter_label(slave_addr, payload):
    """Format the per-meter status string used in the trigger log line."""
    name = 'East (#2)' if slave_addr == ADDR_EAST else 'West (#1)'
    error_tag = '[ERROR]' if np.isscalar(payload) and np.isnan(payload) else ''
    return f"{name} {error_tag}"


def _roll_hdf5_file_for_new_day(current_path, current_time, east_info, west_info):
    """Return the path to write to, rolling over to a new file when the day changes."""
    with h5py.File(current_path, 'r') as f:
        # struct_time[-2] is tm_yday
        if f.attrs['created'][-2] == get_current_day(current_time):
            return current_path

    new_date = datetime.date.today()
    new_path = f"{HDF5_PATH}/flow_data_{new_date}.hdf5"
    init_hdf5_file(new_path, east_info=east_info, west_info=west_info)
    return new_path


def _save_meter_pair(hdf5_file, first_meter_data, second_meter_data, first_msg, second_msg):
    """Open the HDF5 file once and append both meters' shots, with status prints."""
    with h5py.File(hdf5_file, 'a', libver='latest') as f:
        # Enable SWMR mode for better crash resistance
        f.swmr_mode = True

        if not save_flow_data(f, *first_meter_data):
            print("Data length error in first flow meter")
        print(_format_meter_label(first_msg[0], first_msg[1]), end=', ', flush=True)

        if not save_flow_data(f, *second_meter_data):
            print("Data length error in second flow meter")
        print(_format_meter_label(second_msg[0], second_msg[1]), flush=True)

        # Explicitly flush to disk
        f.flush()


def main():
    """Main function to run flow meter data acquisition"""
    east_info = (PORT_EAST, ADDR_EAST)
    west_info = (PORT_WEST, ADDR_WEST)

    # Setup queues
    q_trigger = mp.Queue()
    q_data = mp.Queue()

    # Create HDF5 file
    date = datetime.date.today()
    hdf5_file = f"{HDF5_PATH}/flow_data_{date}.hdf5"
    init_hdf5_file(hdf5_file, east_info=east_info, west_info=west_info)

    print("Starting flow meter processes")
    east_process = mp.Process(target=read_flowmeter,
                              args=(q_trigger, q_data, PORT_EAST, ADDR_EAST, READ_WAIT_S))
    west_process = mp.Process(target=read_flowmeter,
                              args=(q_trigger, q_data, PORT_WEST, ADDR_WEST, READ_WAIT_S))
    east_process.start()
    west_process.start()

    # Initialize GPIO handler
    gpio_handler = None

    try:
        gpio_handler = GPIOHandler(TRIGGER_PIN)
        print("GPIO setup - complete")

        while True:
            # Wait for trigger with configured timeout
            if gpio_handler.wait_for_trigger(timeout_ms=TRIGGER_TIMEOUT_MS):
                # Trigger flow meters
                if east_process.is_alive():
                    q_trigger.put('TRIG')
                else:
                    print("East process is dead. ", end='')

                if west_process.is_alive():
                    q_trigger.put('TRIG')
                else:
                    print("West process is dead. ", end='')

                if not east_process.is_alive() and not west_process.is_alive():
                    print("Both flow meter processes are dead. Exiting.")
                    break

                try:
                    print("\n" + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + ': ', end='')

                    # Roll the HDF5 file over if the day changed since file creation.
                    current_time = time.time()
                    try:
                        hdf5_file = _roll_hdf5_file_for_new_day(
                            hdf5_file, current_time, east_info, west_info,
                        )
                    except Exception as e:
                        print(f"Error checking file date: {e}")
                        continue

                    # Get data from both flow meters first
                    try:
                        # First flow meter
                        east_msg = q_data.get(timeout=FIRST_METER_TIMEOUT_S)
                        first_meter_data = (east_msg[1], east_msg[2], east_msg[0] == ADDR_EAST)

                        # Second flow meter
                        west_msg = q_data.get(timeout=SECOND_METER_TIMEOUT_S)
                        second_meter_data = (west_msg[1], west_msg[2], west_msg[0] == ADDR_EAST)

                    except queue.Empty:
                        print("Timeout waiting for flow meter data")
                        continue

                    # Then save data in a single file operation
                    try:
                        _save_meter_pair(
                            hdf5_file,
                            first_meter_data, second_meter_data,
                            east_msg, west_msg,
                        )
                    except OSError as e:
                        print(f"Error saving to HDF5 file: {e}")
                        continue

                except Exception as e:
                    print(f"Error in trigger handling: {e}")
                    time.sleep(0.5)
                    continue

    except KeyboardInterrupt:
        print("Interrupting processes...")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        for _ in (east_process, west_process):
            q_trigger.put('QUIT')

        east_process.join()
        west_process.join()

        east_process.close()
        west_process.close()

        if gpio_handler:
            gpio_handler.cleanup()
        print('Done')


if __name__ == "__main__":
    main()
