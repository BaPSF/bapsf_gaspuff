from flowmeter_main import read_flowmeter, save_flow_data
import datetime
import numpy as np
import time
import multiprocessing as mp
import queue
import h5py
import os
import subprocess 

from gpiozero import DigitalInputDevice

# Configuration
HDF5_PATH = '/home/gaspuffpi/flow_meter/data'
os.environ['HDF5_USE_FILE_LOCKING'] = 'FALSE'

class GPIOHandler:
    def __init__(self, trigger_pin: int):
        self.trigger = DigitalInputDevice(trigger_pin, pull_up=False)
    
    def wait_for_trigger(self, timeout_ms: int) -> bool:
        return self.trigger.wait_for_active(timeout=timeout_ms / 1000)
    
    def cleanup(self) -> None:
        self.trigger.close()

# (Helper functions unchanged)
def get_current_day(timestamp):
    ct = time.localtime(timestamp)
    return ct.tm_yday

# Initialize HDF5: only include East
def init_hdf5_file(file_name, east_info=None):
    if os.path.exists(file_name):
        print("HDF5 file exists. Skipping creation/verification.")
        return
    timestamp = time.time()
    with h5py.File(file_name, "w", libver='latest') as f:
        ct = time.localtime(timestamp)
        f.attrs['created'] = ct
        print("HDF5 file created", time.strftime("%Y-%m-%d %H:%M:%S", ct))
        f.attrs['description'] = "Flow meter data from Sensirion flow meters"
        # Create group for East only
        if east_info is not None:
            grp = f.require_group("FlowMeter_East")
            grp.attrs['description'] = "Flow measurements from East flow meter"
            grp.attrs['port'] = east_info[0]
            grp.attrs['address'] = east_info[1]
            grp.attrs['unit'] = "standard liter per minute"
            grp.create_dataset("flow_data", (0, 0), maxshape=(None, None), dtype=np.float32)
            grp.create_dataset("timestamp", (0,), maxshape=(None,), dtype=np.float64)

def clear_swmr_flags(path, retries=5, delay=0.2):
    
    h5clear = "/usr/bin/h5clear"
    try:
        subprocess.run([h5clear, "-s", path], check=True)
        print("h5clear completed.")
    except subprocess.CalledProcessError as h5clear_err:
        print("h5clear failed:", h5clear_err)

def main():
    """Main function to run flow meter data acquisition"""
    # Flow meter configuration
    trigger_pin = 25 #physical pin 22
    portEast = '/dev/serial/by-id/usb-FTDI_USB-RS485_Cable_AU05D9B7-if00-port0'
    addrEast = 2
    wait_time = 0.110

    # Setup queues
    q_trigger = mp.Queue()
    q_data = mp.Queue()
    date = datetime.date.today()
    hdf5_file = f"{HDF5_PATH}/flow_data_{date}.hdf5"
    init_hdf5_file(hdf5_file, east_info=(portEast, addrEast))
    
    
    try:
        clear_swmr_flags(hdf5_file)
    except RuntimeError as e:
        print(f"[Error] Cannot clear SWMR flags: {e}")
        return  # or sys.exit(1)
    
    f = h5py.File(hdf5_file, 'a', libver='latest')
    f.swmr_mode = True

    print("Starting flow meter process for East only")
    eastProcess = mp.Process(
        target=read_flowmeter,
        args=(q_trigger, q_data, portEast, addrEast, wait_time)
    )
    eastProcess.start()

    # Initialize GPIO handler
    gpio_handler = GPIOHandler(trigger_pin=trigger_pin)

    try:
        print("GPIO setup - complete")

        while True:
            # Wait for trigger with 500ms timeout
            if not gpio_handler.wait_for_trigger(timeout_ms=50):
                continue
            print("trigger received")

            # Trigger East process only
            if eastProcess.is_alive():
                q_trigger.put('TRIG')
            else:
                print("East process is dead. Exiting.")
                break

            # 2) Handle midnight rollover
            try:
                created = f.attrs['created']
                file_date = datetime.date(*created[:3])
                today     = datetime.date.today()
                if file_date != today:
                    # close old file, re-init new file & handle
                    f.close()
                    date      = today
                    hdf5_file = f"{HDF5_PATH}/flow_data_{date}.hdf5"
                    init_hdf5_file(hdf5_file, east_info=(portEast, addrEast))
                    f = h5py.File(hdf5_file, 'a', libver='latest')
                    f.swmr_mode = True
            except Exception as e:
                print(f"Error checking file date: {e}")
                continue
                # continue anyway

            # 3) Get the packet from the reader
            try:
                addr, flow_data, ts = q_data.get(timeout=6)
            except queue.Empty:
                print("Timeout waiting for East flow meter data")
                continue

            # 4) Append to the already-open file
            try:
                ok = save_flow_data(f, flow_data, ts, is_east=True)
                ts_str = datetime.datetime.fromtimestamp(ts).isoformat()
                print(f"East saved packet ({len(flow_data)} samples) at {ts_str}", flush=True)
                try:
                    f.flush()
                except Exception as e:
                    print(f"[Warning] HDF5 flush failed (ignored): {e}")
            except Exception as e:
                print(f"Error saving data: {e}")
                time.sleep(0.5)
                continue

    except KeyboardInterrupt:
        print("Interrupting process...")
    except Exception as e:
        print(f"Error: {str(e)}")
    finally:
        # clean up
        q_trigger.put('QUIT')
        eastProcess.join()
        f.close()
        gpio_handler.cleanup()
        print("Done")

if __name__ == "__main__":
    main()
