# 04/25/2025 Update — Control and GUI Improvements  
_Edited by Jingxuan Xu_

---

## Control (`Pfeiffer_control.py`)

### System Flowchart  
<img src="https://github.com/user-attachments/assets/38071e3b-27e2-47a6-9ea9-99ac4bc5b55b" alt="Control Flowchart" width="600"/>

### Summary of Changes
- Installed `HDF5 1.14.6` from [HDF Group](https://www.hdfgroup.org/download-hdf5/).  
  This version is compatible with `h5py 3.13.0` used by the control script.
- Added a periodic flush mechanism in `main()` to ensure data is saved regularly, reducing risk of data loss in the event of a crash.
- Integrated automatic HDF5 recovery using `h5clear` to reset files left in a SWMR-locked state.
- Wrapped major file operations in `try/except` blocks with error report and retry messages.

---

## GUI (`Pfeiffer_GUI.py`)

### System Flowchart  
<img src="https://github.com/user-attachments/assets/cda01287-b3c6-4f5f-ab78-c093acfc01b3" alt="GUI Flowchart" width="600"/>

### Summary of Changes
- Removed all file locking logic from the GUI and disabled HDF5 OS-level locking by setting the environment variable.
- Implemented `try/except` blocks around some processes to ensure stability and avoid crashes during file access errors.
- Added type checks to ensure PyQt signals are only emitted with valid data types, preventing crashes when the control script is actively writing.
- Introduced logic to skip plotting when read errors occur or when the file is temporarily locked by control.


