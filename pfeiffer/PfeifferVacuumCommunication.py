# -*- coding: utf-8 -*-
"""
Communication to Pfeiffer Vacuum gauge controller via Ethernet socket

Created on Mon Feb 28 11:14:40 2022
@author: benja

Modified Jul.16.2024 by Jia Han
-Improve the code structure
"""

import socket
import time
import sys
import numpy as np
import datetime

import matplotlib.pyplot as plt

class MaxiGauge:

    PRESSURE_READING_STATUS = { # pressure status defined on p.88
    0: 'Measurement data okay',
    1: 'Underrange',
    2: 'Overrange',
    3: 'Sensor error',
    4: 'Sensor off',
    5: 'No sensor',
    6: 'Identification error'
    }

    GAS_TYPE = { # gas type see communication protocol
        0: 'Nitrogen',
        1: 'Argon',
        2: 'Hydrogen',
        3: 'Helium',
        4: 'Neon',
        5: 'Krypton',
        6: 'Xenon',
        7: 'CAL',}

    def __init__(self, ip_addr, debug = False, verbose=False):
        
        self.debug = debug
        self.verbose = verbose
        if ip_addr != None:
            self.ip_addr = ip_addr
        else:
            raise MaxiGaugeError("No IP address provided. Please provide an IP address.")
        self.SERVER_PORT = 8000

        self.s = None


    def connect(self):
        if len(self.ip_addr.split('.')) == 4:
            if self.verbose:
                print("Looking for Pfeiffer gauge controller at", self.ip_addr, "\n", flush=True)

            RETRIES = 30
            retry_count = 0
            while retry_count < RETRIES:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(2) # if timeout, socket.timeout except will catch it, 05/08/2025
                    s.connect((self.ip_addr, self.SERVER_PORT))
                    if self.verbose:
                        print('...connection established at',time.ctime())
                    self.s = s
                    break

                except ConnectionRefusedError:
                    retry_count += 1
                    print('...connection refused, at',time.ctime(),' Is motor_server process running on remote machine?',
                            '  Retry', retry_count, '/', RETRIES, "on", str(self.ip_addr))
                except TimeoutError:
                    retry_count += 1
                    print('...connection attempt timed out, at',time.ctime(),
                            '  Retry', retry_count, '/', RETRIES, "on", str(self.ip_addr))
                    time.sleep(0.5) #05/08/2025
                except socket.timeout: #05/08/2025
                    retry_count += 1
                    print('...connection attempt timed out, at',time.ctime(),
                            '  Retry', retry_count, '/', RETRIES, "on", str(self.ip_addr))
                    time.sleep(0.5)
                except KeyboardInterrupt:
                    sys.exit('_______Halt due to CRTL_C________')

                if retry_count >= RETRIES:
                    ("Connection to Pfeiffer gauge controller at", self.ip_addr, "failed after", RETRIES, "attempts.")
                    s.close()     


    def disconnect(self):
        self.s.close()
        if self.verbose:
            print("\n Connection safely terminated.")    
#==============================================================================    
    def __repr__(self): 
        """ return a printable version: not a useful function """
        return self.id.decode()

    def __str__(self):
        """ return a string representation: as useless as __repr__() """
        return self.__repr__()

    def __bool__(self):
        """ boolean test if valid - assumes valid if the serial port reports is_open """
        if type(self.s) != type(None):
            return self.s.is_open()
        return False

    def __enter__(self):
        """ no special processing after __init__() """
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """ same as __del__() """
        self.__del__()

    def __del__(self):
        """ close up """
        if type(self.s) != type(None):
            self.s.close() 
            if self.verbose:
                print("Sucessfully terminated connection to gauge server")
#==============================================================================

    def debugMessage(self, message):
        if self.debug:
            print(repr(message))

    def write(self, what):
        self.debugMessage(what)
        self.s.sendall(what)
        
    def enquire(self):
        self.write(C["ENQ"])
        
    def read(self):
        data = ""
        while True:
            x = self.s.recv(1024)
            self.debugMessage(x)
            data += str(x, "utf-8")
            if len(data) > 1 and data[-2:] == str(LINE_TERMINATION,"utf-8"):
                break
        return data[:-len(str(LINE_TERMINATION,"utf-8"))]
        
    def getACKorNAK(self):
        returncode = self.read()
        self.debugMessage(returncode)
        # if acknowledgement ACK is not received
        if len(returncode) < 3:
            self.debugMessage("Only received a line termination from gauge, was expecting ACK or NAK.")
        if len(returncode) > 2 and returncode[-3] == C["NAK"]:
            self.enquire()
            returnedError = self.read()
            error = str(returnedError).split(",", 1)
            print(repr(error))
            errmsg = {"System Error": ERR_CODES[0][int(error[0])], "Gauge Error": ERR_CODES[1][int(error[1])]}
            raise MaxiGaugeNAK(errmsg)
        if len(returncode) > 2 and returncode[-3] != b"ACK":
            self.debugMessage("Expecting ACK or NAK from gauge but neither were sent.")
        # otherwise:
        else: 
            return returncode[:-(len(LINE_TERMINATION)+1)]
        
    def send(self, mnemonic, numEnquiries=1):
        self.write(mnemonic+LINE_TERMINATION)
        self.getACKorNAK()
        response = []
        for i in range(numEnquiries):
            self.enquire()
            response.append(self.read())
        return response
    
    def pressure(self, sensor):
        # self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # self.s.connect((self.host, self.port))
        if sensor < 1 or sensor > 6:
            raise MaxiGaugeError("Sensor can only be between 1 and 6. You choose " + str(sensor))
        reading = self.send(b"PR%d" % sensor, 1)  ## reading will have the form x,x.xxxEsx <CR><LF> (see p.88)
        # self.s.close()
        try:
            r = reading[0].split(',')
            status = int(r[0])
            pressure = float(r[-1])
        except:
            raise MaxiGaugeError("Problem interpreting the returned line:\n%s" % reading)
        return status, pressure
    
    def get_all_pressure_reading(self): #05/08/2025

        try:
            resp = np.array(self.send(b"PRX", 1)[0].split(','))
        except Exception as e:
            raise MaxiGaugeError(f"Problem with pressure response: {e}") 
        
        try:
            statarr = [int(stat) for stat in resp[::2]]
            presarr = [float(pres) for pres in resp[1::2]]
            return statarr, presarr
        except Exception as e:
            raise MaxiGaugeError(f"Malformed pressure data: {resp} — {e}")
        
    
    def get_device_id(self): #05/08/2025
        try:
            resp = self.send(b"TID", 1)
            return resp[0].split(',')
        except Exception as e:
            raise MaxiGaugeError(f"Device ID retrieval failed: {e}")

    def get_gas_type(self): #05/08/2025
        try:
            resp = self.send(b"GAS", 1)
            gas_type = resp[0].split(',')
            gas_type = [int(gas) for gas in gas_type]
            return gas_type
        except Exception as e:
            raise MaxiGaugeError(f"Gas type retrieval failed: {e}")



class MaxiGaugeError(Exception):
    ### Error codes as defined on p. 97
    pass
ERR_CODES = [
{
        0: 'No error',
        1: 'Watchdog has responded',
        2: 'Task fail error',
        4: 'IDCX idle error',
        8: 'Stack overflow error',
    16: 'EPROM error',
    32: 'RAM error',
    64: 'EEPROM error',
    128: 'Key error',
    4096: 'Syntax error',
    8192: 'Inadmissible parameter',
    16384: 'No hardware',
    32768: 'Fatal error'
} ,
{
        0: 'No error',
        1: 'Sensor 1: Measurement error',
        2: 'Sensor 2: Measurement error',
        4: 'Sensor 3: Measurement error',
        8: 'Sensor 4: Measurement error',
    16: 'Sensor 5: Measurement error',
    32: 'Sensor 6: Measurement error',
    512: 'Sensor 1: Identification error',
    1024: 'Sensor 2: Identification error',
    2048: 'Sensor 3: Identification error',
    4096: 'Sensor 4: Identification error',
    8192: 'Sensor 5: Identification error',
    16384: 'Sensor 6: Identification error',
}
]


class MaxiGaugeNAK(MaxiGaugeError):
    pass


       
### ------- Control Symbols as defined on p. 81 of the english
###         manual for the Pfeiffer Vacuum TPG256A  -----------
C = { 
  'ETX': b"\x03", # End of Text (Ctrl-C)   Reset the interface
  'CR':  b"\x0D", # Carriage Return        Go to the beginning of line
  'LF':  b"\x0A", # Line Feed              Advance by one line
  'ENQ': b"\x05", # Enquiry                Request for data transmission
  'ACQ': b"\x06", # Acknowledge            Positive report signal
  'NAK': b"\x15", # Negative Acknowledge   Negative report signal
  'ESC': b"\x1b", # Escape
}

# LINE_TERMINATION=C['CR']+C['LF'] # CR, LF and CRLF are all possible (p.82)
LINE_TERMINATION=C["CR"]+C["LF"] # CR, LF and CRLF are all possible (p.82)

### Mnemonics as defined on p. 85
M = [
  'BAU', # Baud rate                           Baud rate                                    95
  'CAx', # Calibration factor Sensor x         Calibration factor sensor x (1 ... 6)        92
  'CID', # Measurement point names             Measurement point names                      88
  'DCB', # Display control Bargraph            Bargraph                                     89
  'DCC', # Display control Contrast            Display control contrast                     90
  'DCD', # Display control Digits              Display digits                               88
  'DCS', # Display control Screensave          Display control screensave                   90
  'DGS', # Degas                               Degas                                        93
  'ERR', # Error Status                        Error status                                 97
  'FIL', # Filter time constant                Filter time constant                         92
  'FSR', # Full scale range of linear sensors  Full scale range of linear sensors           93
  'LOC', # Parameter setup lock                Parameter setup lock                         91
  'NAD', # Node (device) address for RS485     Node (device) address for RS485              96
  'OFC', # Offset correction                   Offset correction                            93
  'OFC', # Offset correction                   Offset correction                            93
  'PNR', # Program number                      Program number                               98
  'PRx', # Status, Pressure sensor x (1 ... 6) Status, Pressure sensor x (1 ... 6)          88
  'PUC', # Underrange Ctrl                     Underrange control                           91
  'RSX', # Interface                           Interface                                    94
  'SAV', # Save default                        Save default                                 94
  'SCx', # Sensor control                      Sensor control                               87
  'SEN', # Sensor on/off                       Sensor on/off                                86
  'SPx', # Set Point Control Source for Relay xThreshold value setting, Allocation          90
  'SPS', # Set Point Status A,B,C,D,E,F        Set point status                             91
  'TAI', # Test program A/D Identify           Test A/D converter identification inputs    100
  'TAS', # Test program A/D Sensor             Test A/D converter measurement value inputs 100
  'TDI', # Display test                        Display test                                 98
  'TEE', # EEPROM test                         EEPROM test                                 100
  'TEP', # EPROM test                          EPROM test                                   99
  'TID', # Sensor identification               Sensor identification                       101
  'TKB', # Keyboard test                       Keyboard test                                99
  'TRA', # RAM test                            RAM test                                     99
  'UNI', # Unit of measurement (Display)       Unit of measurement (pressure)               89
  'WDT', # Watchdog and System Error Control   Watchdog and system error control           101
]
        


#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================


if __name__ == "__main__":
    
    gauge = MaxiGauge(ip_addr="192.168.7.44", verbose=False)

    gauge.connect()
    gas_ls = gauge.get_gas_type()
    gas_type = MaxiGauge.GAS_TYPE[int(gas_ls[0])]
    print(gas_type)
    gauge.disconnect()

    '''
    pres_ls = []
    time_ls = []


    timeout = False
    while timeout == False:
        st = time.time()
        time.sleep(0.005)
        gauge.connect()
        gauge_id = gauge.get_device_id()
        gas_ls = gauge.get_gas_type()
        stat_arr, pres_arr = gauge.get_all_pressure_reading()
        time_ls.append(time.time())
        # print('Pressure = ', pres_arr[0])
        pres_ls.append(pres_arr[0])
        gauge.disconnect()
        print("Time taken: %.2f" % (time.time()-st))

        if time.time()-st > 10:
            timeout = True
            print('Timeout reached')
            break

    time_vals = [datetime.datetime.fromtimestamp(t) for t in time_ls]

    plt.figure()
    plt.plot(time_vals, pres_ls)
    plt.xlabel('Time')
    plt.ylabel('Pressure(Torr)')
    plt.title('Pressure vs Time')
    plt.show()
    '''
