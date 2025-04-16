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

USE_MOCK = False # true for local debugging 

if USE_MOCK:
    from MockGauge import MockGauge as MaxiGauge
else:
    from PfeifferVacuumCommunication import MaxiGauge
# from PfeifferVacuumCommunication import MaxiGauge

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
		print("HDF5 file exists. Verifying structure...")
		with h5py.File(file_name, 'a') as f:
			grp = f.require_group("PfeifferVacuum")
			gauge_ls = controller.get_device_id()
			gas_ls = controller.get_gas_type()

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

	controller.connect()
	stat_ls, pres_ls = controller.get_all_pressure_reading()
	timestamp = time.time()
	gauge_ls = controller.get_device_id()
	gas_ls = controller.get_gas_type()
	controller.disconnect()

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

def main():

	pfController = MaxiGauge(ip_addr=ip_address)
	count = 0 # count the number of pressure readings saved
	# Create a new HDF5 file; if it already exists, do nothing
	date = datetime.date.today()
	hdf5_ifn = f"{hdf5_path}\\pressure_data_{date}.hdf5"
	init_hdf5_file(hdf5_ifn, pfController)

	while True: 
		try: 
			with h5py.File(hdf5_ifn, 'a', libver='latest') as f: 
				f.swmr_mode = True
				while True: # Continuously save pressure reading to the HDF5 file
					try:
						time.sleep(0.001) 
						
						timestamp, stat_ls, pres_ls, gauge_ls, gas_ls = get_pressure_reading(pfController)

						if count % 100 == 0:
							print(f"Pressure reading: {pres_ls[0]} at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))}")

						# Save the data to the HDF5 file
						fc_day = f.attrs['created'][-2] # Check if the day has changed
						cd = get_current_day(timestamp)
						if fc_day != cd: # if so, create a new HDF5 file
							hdf5_ifn = f"{hdf5_path}\\pressure_data_{cd}.hdf5"
							init_hdf5_file(hdf5_ifn, pfController)
							break

						# Lock the file before writing
						lockfile = f"{hdf5_ifn}_PfeifferVacuum.lock" 
						lock_fd = acquire_lock(lockfile)
						save_pressure_reading(f, timestamp, pres_ls, gauge_ls, gas_ls)
						release_lock(lock_fd)

						count += 1

						if count % 50 == 0: 
								f.flush() 
								break

					except OSError:
						print("Unable to open hdf5 file. Retry...")
						time.sleep(0.5)
						continue

					except Exception as e:
						print(f"Operation error: {e}. Reopening file...")
						time.sleep(0.5)
						break

		except KeyboardInterrupt:
			print("Keyboard interrupt detected. Exiting...")
			break
		
		except Exception as catastrophic_error:
			print(f"CRITICAL FAILURE: {catastrophic_error}")
			time.sleep(5)

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

if __name__ == "__main__":
	main()
	  

