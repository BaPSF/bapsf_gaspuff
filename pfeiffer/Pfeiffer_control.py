# -*- coding: utf-8 -*-
"""
Save pressure reading from Pfeiffer Vacuum gauge 

Modified Jul.16.2024 by Jia Han
-Improve the code structure
"""

import time
import datetime
import os
import h5py
import numpy as np
import portalocker
import subprocess

from PfeifferVacuumCommunication import MaxiGauge, MaxiGaugeError #updated by Jingxuan, raise maxigauge errors

#===============================================================================================================================================
#===CHANGE THE FOLLOWING PARAMETERS IF NECCESSARY=================================================================================================
ip_address = "192.168.7.44"
hdf5_path = r"C:\data\gauge"
#===============================================================================================================================================
#===============================================================================================================================================

def get_current_day(timestamp):
	'''
	gets current day from the timestamp
	'''
	ct = time.localtime(timestamp)
	return ct.tm_yday

#===============================================================================================================================================
# Locking mechanism to prevent read and write conflicts
def acquire_lock(lockfile):
    fd = open(lockfile, 'w')
    portalocker.lock(fd, portalocker.LOCK_EX)
    return fd

def release_lock(fd):
    portalocker.unlock(fd)
    fd.close()
#===============================================================================================================================================

def init_hdf5_file(file_name, controller):

	if not os.path.exists(file_name):
		print("Creating new HDF5 file...")
	
		# Get gauges connected to the controller
		controller.connect()
		gauge_ls = controller.get_device_id()
		gas_ls = controller.get_gas_type()
		timestamp = time.time()
		controller.disconnect()
	
		with h5py.File(file_name, "w",  libver='latest') as f:
			ct = time.localtime(timestamp)
			f.attrs['created'] = ct
			print("HDF5 file created ", time.strftime("%Y-%m-%d %H:%M:%S", ct))
			f.attrs['description'] = "Pressure data. See group description and attribute for more info."

			grp = f.require_group("PfeifferVacuum")
			grp.attrs['description'] = "Pressure reading from Pfeiffer Vacuum gauge using MaxiGauge controller TPG 366. See dataset description for info about the specific gauge."
			grp.attrs['unit'] = "Torr"
			grp.attrs['Gas type'] = str(MaxiGauge.GAS_TYPE)

			for i, gauge_id in enumerate(gauge_ls):

				dataset_name = str(i+1) # sensor number starts from 1
				p_dataset = grp.require_dataset(dataset_name, (0,), maxshape=(None,), dtype='f')
				p_dataset.attrs['Model'] = [gauge_id]
				p_dataset.attrs['Unit'] = "Torr"
				p_dataset.attrs['Gas'] = [gas_ls[i]]
				p_dataset.attrs['Modified time'] = [timestamp]
				p_dataset.attrs['description'] = "Pressure reading from the sensor. Attribute 'Model', 'Gas', 'Modified time' are lists.  When a new gauge or gas setting is applied, dataset attribute will be modified accordingly by appending to the list."

			t_dataset = grp.create_dataset("timestamp", (0,), maxshape=(None,), dtype=np.float64)
			t_dataset.attrs['description'] = "seconds since epoch: January 1, 1970, 00:00:00 (UTC)"
			t_dataset.attrs['unit'] = "s"

	else:
		print("HDF5 file exists. Verifying structure...") #updated by Jingxuan, check for incorrect file structure
		with h5py.File(file_name, 'a') as f:
			grp = f.require_group("PfeifferVacuum")
			controller.connect()
			gauge_ls = controller.get_device_id()
			gas_ls = controller.get_gas_type()
			timestamp = time.time()
			controller.disconnect()

			for i, gauge_id in enumerate(gauge_ls):
				dataset_name = str(i+1)
				if dataset_name not in grp:
					p_dataset = grp.create_dataset(dataset_name, (0,), maxshape=(None,), dtype='f')
					p_dataset.attrs['Model'] = [gauge_id]
					p_dataset.attrs['Unit'] = "Torr"
					p_dataset.attrs['Gas'] = [gas_ls[i]]
					p_dataset.attrs['Modified time'] = [time.time()]
					p_dataset.attrs['description'] = "Pressure reading from the sensor."

			if "timestamp" not in grp:
				grp.create_dataset("timestamp", (0,), maxshape=(None,), dtype='f')

def get_pressure_reading(controller):
	try:
		controller.connect()
		stat_ls, pres_ls = controller.get_all_pressure_reading()
		timestamp = time.time()
		gauge_ls = controller.get_device_id()
		gas_ls = controller.get_gas_type()
		controller.disconnect()
	except MaxiGaugeError as e: #05/13/2025
		print("MaxiGauge communication error, read failed:", e)
		controller.disconnect()
		time.sleep(0.5)
		return None, None, None, None, None

	return timestamp, stat_ls, pres_ls, gauge_ls, gas_ls

def save_pressure_reading(f, timestamp, pres_ls, gauge_ls, gas_ls):

	grp = f["PfeifferVacuum"]
	
	for i, pres in enumerate(pres_ls): # save pressure reading for each sensor
		dataset_name = str(i+1)
		p_dataset = grp[dataset_name]
		p_dataset.resize((p_dataset.shape[0]+1,))
		p_dataset[-1] = pres

		if (p_dataset.attrs['Model'][-1] != gauge_ls[i]) or (p_dataset.attrs['Gas'][-1] != gas_ls[i]):
			p_dataset.attrs['Model'].append(gauge_ls[i])
			p_dataset.attrs['Gas'].append(gas_ls[i])
			p_dataset.attrs['Modified time'].append(timestamp)

	t_dataset = grp["timestamp"] 		# save timestamp
	t_dataset.resize((t_dataset.shape[0]+1,))
	t_dataset[-1] = timestamp
	
	# print("Pressure reading saved at ", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)))

#===============================================================================================================================================
def init_log_dir(log_dir="C:\\data\\gauge"):
	"""
	Initializes the log directory if it does not exist.
	"""
	log_path = os.path.join(log_dir, "connection_log.txt")
	if not os.path.isfile(log_path):
		with open(log_path, "a") as f:
			f.write("")  # create empty file
		print(f"Log file created at {log_path}")
	else:
		print(f"Log file already exists at {log_path}")

def log_connection_event(event_time, status, log_dir="C:\\data\\gauge", error_message=None): #05/27/2025
    """
    status: "LOST" or "RECOVERED"
    """
    log_path = os.path.join(log_dir, "connection_log.txt")

    with open(log_path, "a") as f:
        line = f"{status} connection at {event_time.strftime('%Y-%m-%d %H:%M:%S')}"
        if error_message:
            line += f" | {error_message}"
        f.write(line + "\n")


#===============================================================================================================================================

def main():

	pfController = MaxiGauge(ip_addr=ip_address)
	count = 0 # count the number of pressure readings saved
	# Create a new HDF5 file; if it already exists, do nothing
	date = datetime.date.today()
	hdf5_ifn = f"{hdf5_path}\\pressure_data_{date}.hdf5"

	connection_lost = False #pressure gauge communication status

	try:
		init_hdf5_file(hdf5_ifn, pfController)
	except OSError as e: #updated by Jingxuan, h5clear module for unexpected lock
		if "SWMR" in str(e) or "already open for write" in str(e):
			print("SWMR lock detected during init. Attempting h5clear recovery...")
			try:
				subprocess.run(["h5clear", "-s", hdf5_ifn], check=True)
				print("h5clear succeeded. Retrying init_hdf5_file...")
				init_hdf5_file(hdf5_ifn, pfController)
			except subprocess.CalledProcessError as h5clear_err:
				print("h5clear failed during init:", h5clear_err)
				return  # Abort run if recovery fails
		else:
			raise  # re-raise other unknown errors

	try:
		init_log_dir(log_dir=hdf5_path)  # Initialize log directory
	except OSError as e:
		print(f"Failed to initialize log directory: {e}")
		return

	while True: # Continuously save pressure reading to the HDF5 file
		try:
			time.sleep(0.001) 
	
			try: #updated by Jingxuan, raise communication errors 
				returns = get_pressure_reading(pfController)
				if returns == (None, None, None, None, None): #05/27/2025
					continue
				timestamp, stat_ls, pres_ls, gauge_ls, gas_ls = returns 

				if connection_lost:
					log_connection_event(datetime.datetime.now(), "RECOVERED")
					connection_lost = False

			except MaxiGaugeError as e:
				print("MaxiGauge communication error:", e)
				if not connection_lost: #05/27/2025
					log_connection_event(datetime.datetime.now(), "LOST", error_message=str(e))
					connection_lost = True
				pfController.disconnect()
				time.sleep(1)
				pfController.connect()
				continue
			except Exception as e:
				print("Unexpected error:", e)
				raise 

			count += 1

			if count % 100 == 0:
				print(f"Pressure reading: {pres_ls[0]} at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))}")

			# Lock the file before writing
			# lockfile = f"{hdf5_ifn}_PfeifferVacuum.lock" 
			# lock_fd = acquire_lock(lockfile)

			# Save the data to the HDF5 file
			f = h5py.File(hdf5_ifn, 'a', libver='latest')
			try:
				fc_day = f.attrs['created'][-2] # Check if the day has changed
				cd = get_current_day(timestamp)
				if fc_day != cd: # if so, create a new HDF5 file
					f.close()
					date = datetime.date.today()
					hdf5_ifn = f"{hdf5_path}\\pressure_data_{date}.hdf5"
					init_hdf5_file(hdf5_ifn, pfController)
					f = h5py.File(hdf5_ifn, 'a', libver='latest')
					continue
				
				try: #updatd by Jingxuan, catches pressure errors while reading
					save_pressure_reading(f, timestamp, pres_ls, gauge_ls, gas_ls)
					f.close()
				except Exception as e:
						print("Failed to write pressure reading:", e)
			except OSError as e:
					print("Write error, did not save data", e)
					time.sleep(0.5)
					continue

			
			
			# release_lock(lock_fd)

		except KeyboardInterrupt:
			print("Keyboard interrupt detected. Exiting...")
			break

		except OSError as e: #updated by Jingxuan, h5clear module for unexpected lock
			if "SWMR" in str(e) or "already open for write" in str(e):
				print("Detected SWMR lock. Attempting auto-recovery using h5clear...")
				try:
					subprocess.run(["C:/Program Files/HDF_Group/HDF5/1.14.6/bin/h5clear.exe", "-s", hdf5_ifn], check=True) #update 5/1
					print("h5clear completed successfully. Retrying...")
					time.sleep(2)
					continue  # Retry the loop
				except subprocess.CalledProcessError as h5clear_err:
					print("h5clear failed:", h5clear_err)
					break  # Exit
			else:
				print("Unable to open HDF5 file. Retrying...")
				time.sleep(0.01)
				continue

		except Exception as e: #updated by Jingxuan, catches random errors
			print(f"Operation error: {e}. Reopening file...")
			time.sleep(0.5)
			break

		

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == "__main__":
	try:
		main()
	except KeyboardInterrupt:
			print("Keyboard interrupt detected. Exiting...")

	  

