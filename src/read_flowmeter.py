from flow_meter import FlowMeter
import datetime
import numpy as np
import time
import multiprocessing as mp
import queue
import h5py
import os
import ctypes
import signal
import sys

from sensirion_shdlc_sfc5xxx import Sfc5xxxScaling

# Configuration
HDF5_PATH = '/home/pi/flow_meter/data'

class GPIOHandler:
    """
    Handles GPIO operations using the gpio_detect.so C library
    """
    def __init__(self, trigger_pin):
        self.trigger_pin = trigger_pin
        
        # Load the GPIO C library
        gpio_lib_path = "/home/generalpi/pi_gpio/gpio_detect.so"
        if not os.path.exists(gpio_lib_path):
            raise FileNotFoundError(f"GPIO library not found at {gpio_lib_path}")
            
        try:
            self.gpio_lib = ctypes.CDLL(gpio_lib_path)
        except OSError as e:
            raise RuntimeError(f"Failed to load GPIO library: {str(e)}")
        
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
    
    def wait_for_trigger(self, timeout_ms=500):
        """
        Wait for rising edge on trigger pin
        timeout_ms: timeout in milliseconds
        """
        return self.gpio_lib.wait_for_gpio_high(self.trigger_pin, timeout_ms * 1000)  # Convert to microseconds
    
    def cleanup(self):
        """Cleanup GPIO resources"""
        self.gpio_lib.terminate_pigpio()

def get_current_day(timestamp):
    """Gets current day from the timestamp"""
    ct = time.localtime(timestamp)
    return ct.tm_yday

def init_hdf5_file(file_name, east_info=None, west_info=None):
    """
    Initialize HDF5 file for flow meter data storage.
    
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

def read_flowmeter(q_trigger, q_data, flow_meter_port, slave_address, wait_time=0.1):
    """Process function to continuously read from a flow meter device."""
    fm = None
    consecutive_errors = 0
    MAX_RETRIES = 3
    
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
                    print(f"Flow meter {slave_address} error ({consecutive_errors}/{MAX_RETRIES}): {str(e)}")
                    
                    # Send NaN data on failure
                    q_data.put((slave_address, np.nan, timestamp))
                    
                    # Close and reset connection
                    if fm is not None:
                        try:
                            fm.close()
                        except:
                            pass
                        fm = None
                    
                    # Add delay between retries
                    if consecutive_errors < MAX_RETRIES:
                        time.sleep(1.0)  # Wait before retry
                    else:
                        print(f"Flow meter {slave_address} failed after {MAX_RETRIES} attempts")
                        consecutive_errors = 0  # Reset for next trigger
                    
            else:
                print(f"Unknown command: '{trigger}'")
            
            time.sleep(0.1)
            
    except Exception as e:
        print(f"Fatal error in flow meter {slave_address} process: {str(e)}")
        raise
    finally:
        if fm is not None:
            try:
                fm.close()
            except:
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

def main():
    """Main function to run flow meter data acquisition"""
    # Flow meter configuration
    trigger_pin = 25
    portEast = '/dev/serial/by-id/usb-FTDI_USB-RS485_Cable_AU05D9B7-if00-port0'
    portWest = '/dev/serial/by-id/usb-FTDI_USB-RS485_Cable_AU050ZDN-if00-port0'
    addrEast = 2
    addrWest = 0
    wait_time = 0.110

    # Setup queues
    q_trigger = mp.Queue()
    q_data = mp.Queue()

    # Create HDF5 file
    date = datetime.date.today()
    hdf5_file = f"{HDF5_PATH}/flow_data_{date}.hdf5"
    init_hdf5_file(hdf5_file, 
                   east_info=(portEast, addrEast),
                   west_info=(portWest, addrWest))

    print("Starting flow meter processes")
    eastProcess = mp.Process(target=read_flowmeter, 
                           args=(q_trigger, q_data, portEast, addrEast, wait_time))
    westProcess = mp.Process(target=read_flowmeter, 
                           args=(q_trigger, q_data, portWest, addrWest, wait_time))
    eastProcess.start()
    westProcess.start()

    # Initialize GPIO handler
    gpio_handler = None

    try:
        gpio_handler = GPIOHandler(trigger_pin)
        print("GPIO setup - complete")

        while True:
            # Wait for trigger with 500ms timeout
            if gpio_handler.wait_for_trigger(timeout_ms=500):
                # Trigger flow meters
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
                    print("\n" + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + ': ', end='')
                    
                    # Check if we need a new file for a new day
                    current_time = time.time()
                    try:
                        with h5py.File(hdf5_file, 'r') as f:
                            if f.attrs['created'][-2] != get_current_day(current_time):
                                date = datetime.date.today()
                                hdf5_file = f"{HDF5_PATH}/flow_data_{date}.hdf5"
                                init_hdf5_file(hdf5_file, 
                                             east_info=(portEast, addrEast),
                                             west_info=(portWest, addrWest))
                    except Exception as e:
                        print(f"Error checking file date: {str(e)}")
                        continue

                    # Get data from both flow meters first
                    try:
                        # First flow meter
                        qd1 = q_data.get(timeout=6)
                        first_meter_data = (qd1[1], qd1[2], qd1[0] == addrEast)
                        
                        # Second flow meter
                        qd2 = q_data.get(timeout=1)
                        second_meter_data = (qd2[1], qd2[2], qd2[0] == addrEast)
                        
                    except queue.Empty:
                        print("Timeout waiting for flow meter data")
                        continue

                    # Then save data in a single file operation
                    try:
                        with h5py.File(hdf5_file, 'a', libver='latest') as f:
                            # Enable SWMR mode for better crash resistance
                            f.swmr_mode = True
                            
                            # Save first flow meter data
                            if not save_flow_data(f, *first_meter_data):
                                print("Data length error in first flow meter")
                            print('{} {}'.format(
                                'East (#2)' if qd1[0] == 2 else 'West (#1)',
                                '[ERROR]' if np.isscalar(qd1[1]) and np.isnan(qd1[1]) else ''
                            ), end=', ', flush=True)

                            # Save second flow meter data
                            if not save_flow_data(f, *second_meter_data):
                                print("Data length error in second flow meter")
                            print('{} {}'.format(
                                'East (#2)' if qd2[0] == 2 else 'West (#1)',
                                '[ERROR]' if np.isscalar(qd2[1]) and np.isnan(qd2[1]) else ''
                            ), flush=True)
                            
                            # Explicitly flush to disk
                            f.flush()
                            
                    except OSError as e:
                        print(f"Error saving to HDF5 file: {str(e)}")
                        continue

                except Exception as e:
                    print(f"Error in trigger handling: {str(e)}")
                    time.sleep(0.5)
                    continue

    except KeyboardInterrupt:
        print("Interrupting processes...")
    except Exception as e:
        print(f"Error: {str(e)}")
    finally:
        q_trigger.put('QUIT')
        q_trigger.put('QUIT')
        
        eastProcess.join()
        westProcess.join()
        
        eastProcess.close()
        westProcess.close()

        if gpio_handler:
            gpio_handler.cleanup()
        print('Done')


if __name__ == "__main__":
    main()
