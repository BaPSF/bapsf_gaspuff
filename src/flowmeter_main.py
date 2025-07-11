from FlowMeterCommunication import FlowMeter
import datetime
import numpy as np
import time
import multiprocessing as mp
import queue
import h5py
import os
import subprocess 

from gpiozero import DigitalInputDevice
from sensirion_shdlc_sfc5xxx import Sfc5xxxScaling

# Configuration
HDF5_PATH = '/home/gaspuffpi/flow_meter/data'

class GPIOHandler:
    """
    Handles GPIO operations using the gpio_detect.so C library
    """
    def __init__(self, trigger_pin: int):
        self.trigger = DigitalInputDevice(trigger_pin, pull_up=False)
    
    def wait_for_trigger(self, timeout_ms: int) -> bool:
        """
        Wait for rising edge on trigger pin
        timeout_ms: timeout in milliseconds
        """
        return self.trigger.wait_for_active(timeout=timeout_ms / 1000)
    
    def cleanup(self) -> None:
        """Cleanup GPIO resources"""
        self.trigger.close()

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
        print("HDF5 file exists. Skipping creation/verification.")
        return
    
    timestamp = time.time()
    with h5py.File(file_name, "w", libver='latest') as f:
        ct = time.localtime(timestamp)
        f.attrs['created'] = ct
        print("HDF5 file created", time.strftime("%Y-%m-%d %H:%M:%S", ct))
        f.attrs['description'] = "Flow meter data from Sensirion flow meters"
        #f.attrs['data_length'] = 0  # Will be set when first data is received

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

def clear_swmr_flags(path, retries=5, delay=0.2):
    
    h5clear = "/usr/bin/h5clear"
    try:
        subprocess.run([h5clear, "-s", path], check=True)
        print("h5clear completed.")
    except subprocess.CalledProcessError as h5clear_err:
        print("h5clear failed:", h5clear_err)
        
def read_flowmeter(q_trigger, q_data, flow_meter_port, slave_address, wait_time=0.1):
    """Process function to continuously read from a flow meter device."""
    
    try:
        fm = FlowMeter(port=flow_meter_port, slave_address=slave_address)
    except Exception as e:
        print(f"[East reader] FAILED to open port {flow_meter_port}: {e}")
        return
    
    try:
        while True:
            trigger = q_trigger.get()
            if trigger == 'QUIT':
                #if fm is not None:
                    #fm.close()
                break
            elif trigger == 'TRIG':
                timestamp = time.time()
                
                try:
                        # Always open a fresh FlowMeter context on each trigger
                    time.sleep(wait_time)
                    buff = fm.device.read_measured_value_buffer(
                        Sfc5xxxScaling.USER_DEFINED, max_reads=3
                    )
                    q_data.put((slave_address, buff.values, timestamp))
                    continue
                except Exception as e:
                    print(f"Flow meter {slave_address} error: {e}")
                    q_data.put((slave_address, np.nan, timestamp))
            
                    
            else:
                print(f"Unknown command: '{trigger}'")
            
            time.sleep(0.1)
            
    except Exception as e:
        print(str(f"Error in flow meter {slave_address} process: {str(e)}"))
        raise
    finally:
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
            print(f"Warning: resizing from {data_length}→{len(flow_data)} samples")
            grp = f['FlowMeter_East']
            grp['flow_data'].resize((grp['flow_data'].shape[0], len(flow_data)))
            f.attrs['data_length'] = len(flow_data)

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
    trigger_pin = 25 #physical pin 22
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
    
    try:
        clear_swmr_flags(hdf5_file)
    except RuntimeError as e:
        print(f"[Error] Cannot clear SWMR flags: {e}")
        return  # or sys.exit(1)

    f = h5py.File(hdf5_file, 'a', libver='latest')
    f.swmr_mode = True

    print("Starting flow meter processes")
    eastProcess = mp.Process(target=read_flowmeter, 
                           args=(q_trigger, q_data, portEast, addrEast, wait_time))
    westProcess = mp.Process(target=read_flowmeter, 
                           args=(q_trigger, q_data, portWest, addrWest, wait_time))
    eastProcess.start()
    westProcess.start()

    # Initialize GPIO handler
    gpio_handler = GPIOHandler(trigger_pin=25)

    try:
        print("GPIO setup - complete")

        while True:
            # Wait for trigger with 500ms timeout
            if not gpio_handler.wait_for_trigger(timeout_ms=50):
                continue
            print("trigger received")

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

            # Handle midnight rollover
            try:
                created = f.attrs['created']
                file_date = datetime.date(*created[:3])
                today = datetime.date.today()
                if file_date != today:
                    f.close()
                    date = today
                    hdf5_file = f"{HDF5_PATH}/flow_data_{date}.hdf5"
                    init_hdf5_file(hdf5_file,
                                   east_info=(portEast, addrEast),
                                   west_info=(portWest, addrWest))
                    f = h5py.File(hdf5_file, 'a', libver='latest')
                    f.swmr_mode = True
            except Exception as e:
                print(f"Error checking file date: {e}")
                continue

            # Retrieve two packets
            try:
                # first packet
                addr1, flow1, ts1 = q_data.get(timeout=6)
                # second packet
                addr2, flow2, ts2 = q_data.get(timeout=6)
            except queue.Empty:
                print("Timeout waiting for flow meter data")
                continue

            # Save both packets
            try:
                # East first
                ok1 = save_flow_data(f, flow1, ts1, addr1 == addrEast)
                label1 = 'East' if addr1 == addrEast else 'West'
                print(f"{label1} saved ({len(flow1)} samples)", end=', ')
                # West next
                ok2 = save_flow_data(f, flow2, ts2, addr2 == addrEast)
                label2 = 'East' if addr2 == addrEast else 'West'
                print(f"{label2} saved ({len(flow2)} samples)")
                # Flush with error ignored
                try:
                    f.flush()
                except Exception as e:
                    print(f"[Warning] HDF5 flush failed (ignored): {e}")
            except Exception as e:
                print(f"Error saving data: {e}")
                time.sleep(0.5)

    except KeyboardInterrupt:
        print("Interrupting processes...")
    except Exception as e:
        print(f"Error: {str(e)}")
    finally:
        q_trigger.put('QUIT')
        q_trigger.put('QUIT')
        
        eastProcess.join()
        westProcess.join()
        
        f.close()

        if gpio_handler:
            gpio_handler.cleanup()
        print('Done')


if __name__ == "__main__":
    main()
