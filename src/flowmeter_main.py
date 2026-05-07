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
from typing import NamedTuple, Sequence, Union

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
ERROR_BACKOFF_S = 0.5
MAX_FLOW_METER_RETRIES = 3

# Mapping from slave address → HDF5 group name. Single source of truth so that
# downstream helpers don't reach for ADDR_EAST / ADDR_WEST module globals.
ADDR_TO_GROUP = {ADDR_EAST: "FlowMeter_East", ADDR_WEST: "FlowMeter_West"}


# --- Data types -------------------------------------------------------------
class MeterShot(NamedTuple):
    """One shot's worth of data flowing from a worker to the main process.

    ``values`` is normally a sequence of floats; on acquisition failure the
    worker stores ``np.nan`` as a scalar sentinel — see :pymeth:`is_failure`.
    ``sampling_time`` is the device-reported seconds-between-samples for this
    shot (``np.nan`` on failure).
    """
    slave_address: int
    values: Union[Sequence[float], float]
    timestamp: float
    sampling_time: float

    @property
    def is_failure(self) -> bool:
        return np.isscalar(self.values) and np.isnan(self.values)


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
        # data_length is established by the first successful shot; until then,
        # f.attrs.get('data_length') returns None.

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

                    time.sleep(wait_time)
                    buff = fm.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED, max_reads=3)
                    if buff.lost_values > 0:
                        print(f"Flow meter {slave_address} ring-buffer overrun: {buff.lost_values} samples lost")
                    q_data.put(MeterShot(slave_address, buff.values, timestamp, buff.sampling_time))
                    consecutive_errors = 0  # Reset error counter on successful read

                except Exception as e:
                    consecutive_errors += 1
                    print(f"Flow meter {slave_address} error ({consecutive_errors}/{MAX_FLOW_METER_RETRIES}): {e}")

                    # Send NaN sentinel on failure so the main process always sees a pair.
                    q_data.put(MeterShot(slave_address, np.nan, timestamp, float('nan')))

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


# --- HDF5 write helpers -----------------------------------------------------
def _ensure_data_length(f, length):
    """Establish or validate the per-shot sample length for the file.

    On the first successful shot, records ``f.attrs['data_length']`` and
    widens every existing ``flow_data`` dataset to ``(0, length)``. On
    subsequent shots, validates ``length`` matches.

    Returns the established length, or ``None`` if a mismatch is detected.
    """
    current = f.attrs.get('data_length')
    if current is None:
        f.attrs['data_length'] = length
        for grp_name in ("FlowMeter_East", "FlowMeter_West"):
            if grp_name in f:
                f[grp_name]["flow_data"].resize((0, length))
        return length
    if current != length:
        return None
    return current


def _nan_payload(f):
    """Return a NaN-filled array sized to the file's per-shot sample length.

    Returns ``None`` if the length has not yet been established (no successful
    shot has been written), in which case the failure cannot be persisted.
    """
    length = f.attrs.get('data_length')
    if length is None:
        return None
    return np.full(length, np.nan)


def append_shot(f, group_name, values, timestamp):
    """Append one shot's ``values`` and ``timestamp`` to ``group_name``."""
    grp = f[group_name]
    flow_dataset = grp["flow_data"]
    time_dataset = grp["timestamp"]

    flow_dataset.resize((flow_dataset.shape[0] + 1, flow_dataset.shape[1]))
    time_dataset.resize((time_dataset.shape[0] + 1,))

    flow_dataset[-1, :] = values
    time_dataset[-1] = timestamp


def save_flow_data(f, flow_data, timestamp, group_name, sampling_time=None):
    """Persist one shot to ``group_name``, handling the NaN failure sentinel.

    On the first successful (non-NaN) shot for a given group, records the
    device-reported ``sampling_time`` (seconds between samples) as a group
    attribute so downstream analysis doesn't have to guess the rate.

    Returns ``True`` on success, ``False`` when the shot must be dropped
    (length mismatch, or a failure arrived before the first good shot
    established a sample length).
    """
    is_failure = np.isscalar(flow_data) and np.isnan(flow_data)

    if is_failure:
        payload = _nan_payload(f)
        if payload is None:
            print("Cannot save NaN data - data_length not yet established")
            return False
    else:
        established = _ensure_data_length(f, len(flow_data))
        if established is None:
            print(f"Warning: Data length mismatch. Expected {f.attrs['data_length']}, got {len(flow_data)}")
            return False
        payload = flow_data
        if sampling_time is not None and not np.isnan(sampling_time):
            grp = f[group_name]
            if 'sampling_time' not in grp.attrs:
                grp.attrs['sampling_time'] = sampling_time

    append_shot(f, group_name, payload, timestamp)
    return True


# --- Per-trigger orchestration ---------------------------------------------
def _format_meter_label(shot):
    """Format the per-meter status string used in the trigger log line."""
    name = 'East (#2)' if shot.slave_address == ADDR_EAST else 'West (#1)'
    error_tag = '[ERROR]' if shot.is_failure else ''
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


def _save_meter_pair(hdf5_file, first_shot, second_shot):
    """Open the HDF5 file once and append both shots, with status prints."""
    with h5py.File(hdf5_file, 'a', libver='latest') as f:
        # Enable SWMR mode for better crash resistance
        f.swmr_mode = True

        if not save_flow_data(f, first_shot.values, first_shot.timestamp,
                              ADDR_TO_GROUP[first_shot.slave_address],
                              sampling_time=first_shot.sampling_time):
            print("Data length error in first flow meter")
        print(_format_meter_label(first_shot), end=', ', flush=True)

        if not save_flow_data(f, second_shot.values, second_shot.timestamp,
                              ADDR_TO_GROUP[second_shot.slave_address],
                              sampling_time=second_shot.sampling_time):
            print("Data length error in second flow meter")
        print(_format_meter_label(second_shot), flush=True)

        # Explicitly flush to disk
        f.flush()


def _trigger_meters(q_trigger, east_process, west_process):
    """Send TRIG to each living worker. Return False if both are dead."""
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
        return False
    return True


def _collect_meter_shots(q_data):
    """Drain the two MeterShot messages produced by one trigger.

    Returns ``(first, second)`` or ``None`` if either ``queue.get`` times out.
    """
    try:
        first = q_data.get(timeout=FIRST_METER_TIMEOUT_S)
        second = q_data.get(timeout=SECOND_METER_TIMEOUT_S)
        return first, second
    except queue.Empty:
        print("Timeout waiting for flow meter data")
        return None


def _handle_trigger(q_trigger, q_data, east_process, west_process,
                    hdf5_file, east_info, west_info):
    """Run one trigger cycle.

    Returns the path to use for the next iteration, or ``None`` to signal
    that the main loop should exit (both workers are dead).
    """
    if not _trigger_meters(q_trigger, east_process, west_process):
        return None

    print("\n" + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + ': ', end='')

    try:
        hdf5_file = _roll_hdf5_file_for_new_day(
            hdf5_file, time.time(), east_info, west_info,
        )
    except Exception as e:
        print(f"Error checking file date: {e}")
        return hdf5_file

    shots = _collect_meter_shots(q_data)
    if shots is None:
        return hdf5_file

    try:
        _save_meter_pair(hdf5_file, shots[0], shots[1])
    except OSError as e:
        print(f"Error saving to HDF5 file: {e}")

    return hdf5_file


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
            if not gpio_handler.wait_for_trigger(timeout_ms=TRIGGER_TIMEOUT_MS):
                continue

            try:
                next_path = _handle_trigger(
                    q_trigger, q_data, east_process, west_process,
                    hdf5_file, east_info, west_info,
                )
            except Exception as e:
                print(f"Error in trigger handling: {e}")
                time.sleep(ERROR_BACKOFF_S)
                continue

            if next_path is None:
                break
            hdf5_file = next_path

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
