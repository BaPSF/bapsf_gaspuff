# -*- coding: utf-8 -*-
"""
Waveform generator Agilent and Keysight control using socket
Commands are send and received as ASCII

Remote control command see: http://ecelabs.njit.edu/student_resources/33220_user_guide.pdf
Or Google search Agilent 33220A user guide
"""


import sys
if sys.version_info[0] < 3: raise RuntimeError('This script should be run under Python 3')

import socket
#import numpy
import time



class wavegen_control:
	MSIPA_CACHE_FN = 'wavegen_server_ip_address_cache.tmp'
	WAVEGEN_SERVER_PORT = 5025
	BUF_SIZE = 4096

	#- - - - - - - - - - - - - - - - -

	def __init__(self, server_ip_addr = None, msipa_cache_fn = None, verbose = True):

		self.verbose = verbose
		if msipa_cache_fn == None:
			self.msipa_cache_fn = self.MSIPA_CACHE_FN
		else:
			self.msipa_cache_fn = msipa_cache_fn

		# if we get an ip address argument, set that as the suggest server IP address, otherwise look in cache file
		if server_ip_addr != None:
			self.server_ip_addr = server_ip_addr
		else:
			try:
				# later: save the successfully determined wavegen server IP address in a file on disk
				# now: read the previously saved file as a first guess for the wavegen server IP address:
				self.server_ip_addr = None
				with open(self.msipa_cache_fn, 'r') as f:
					self.server_ip_addr = f.readline()
			except FileNotFoundError:
				self.server_ip_adddr = None

		# - - - - - - - - - - - - - - - - - - - - - - -

		if self.server_ip_addr != None  and  len(self.server_ip_addr) > 0:
			try:
				print('looking for wavegen server at', self.server_ip_addr,end='\n',flush=True)
				t = self.send_text('*IDN?')
				if t != None:
					print('Wavegen found: ', t)
					need_search = False
				else:
					print('*IDN? returned empty response. Something went wrong.')

			except TimeoutError:
				print('...timed out')
		


		#with open(self.msipa_cache_fn, 'w') as f:
		#	f.write(self.server_ip_addr)

########################################################################################################
########################################################################################################

	def __repr__(self):
		""" return a printable version: not a useful function """
		return self.server_ip_addr + '; ' + self.msipa_cache_fn + '; ' + self.verbose


	def __str__(self):
		""" return a string representation: """
		return self.__repr__()

	def __bool__(self):
		""" boolean test if valid - assumes valid if the server IP address is defined """
		return self.server_ip_addr != None

	def __enter__(self):
		""" no special processing after __init__() """
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		""" no special processing after __init__() """

	def __del__(self):
		""" no special processing after __init__() """

########################################################################################################
########################################################################################################

	def open_socket(self, port = 5025):
		# Open a socket, retries 30 times if connection fails
		RETRIES = 30
		retry_count = 0
		while retry_count < RETRIES:
			try:
				session = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
				# session.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 0)
				# session.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, 0)
				session.connect((self.server_ip_addr,port))
				break
			except ConnectionRefusedError:
				retry_count += 1
				print('\nConnection refused, at',time.ctime(),
				           '  Retry count', retry_count, '/', RETRIES, "on", str(self.server_ip_addr))
			except TimeoutError:
				retry_count += 1
				print('\nConnection attempt timed out, at',time.ctime(),
				           '  Retry count', retry_count, '/', RETRIES, "on", str(self.server_ip_addr))
			except IOError:
				retry_count += 1
				print('\nConnection failed, at',time.ctime(),
				           '  Retry count', retry_count, '/', RETRIES, "on", str(self.server_ip_addr))
			except KeyboardInterrupt:
				print('\n______Halted due to Ctrl-C______ at', time.ctime())
				return session

		if retry_count >= RETRIES:
			input(" pausing in wavegen_socket_control.py open_socket(), socket can't connect. hit Enter to try again, or ^C: ")
			
			return self.open_socket()  # tail-recurse if retry is requested
				
		return session
	
	def send_text(self, command):
		# Open a socket session, sends input command, get response, close socket session when done
		
		s = self.open_socket()

		message = command + '\n'
		response = 'No response'

		try:
			s.send(message.encode())
			# get response if the command is a question
			if command.find('?') >= 0:
				response = s.recv(4096).decode()
				length = len(response)
				if length == 0:
					print('Empty response is given...Try slowing down the command (sleep at least 0.4s between commands)')
				elif response[len(response)-1] == "\n":
					response=response[:-1]
	
		except TimeoutError:
			print('socket opened but sending command time out. check command.')
		except KeyboardInterrupt:
			print('\n______Halted due to Ctrl-C______')

		s.close()

		# Sleep 0.4s after each command
		time.sleep(0.5)
		#print(' | response is', response,'end')

		return response

	def send_dac_data(self, data):
		# Data input in numpy array
		# binary integer or decimal integer values from -8191 to +8191
	
		s = self.open_socket()

		message = 'DATA:DAC VOLATILE, #5%05i'%(data.size*2)
		response = 'No response'

		try:
			s.send(message.encode())
			s.send(data.tobytes())
#			s.send('\n'.encode())
			# get response if the command is a question
		except TimeoutError:
			print('socket opened but sending command time out. check command.')
		except KeyboardInterrupt:
			print('\n______Halted due to Ctrl-C______')

		s.close()

		# Sleep 0.4s after each command
#		time.sleep(0.5)
		#print(' | response is', response,'end')
#		response = self.send_text('FUNC USER')

		return response

#-------------------------------------------------------
	'''
	Output on(1)/off(0)
	'''

	@property
	def output(self):
		resp = int(self.send_text('OUTP?'))
		return(resp)
	
	@output.setter
	def output(self, out):
		if out == 1:
			self.send_text('OUTP ON')
		else:
			self.send_text('OUTP OFF')
			

#-------------------------------------------------------

	'''
	Query and set the voltage offset
	'''

	@property
	def DCoffset(self):
		resp = float(self.send_text('VOLTage:OFFSet?'))
		return(resp)

	@DCoffset.setter
	def DCoffset(self, offset):
		self.send_text('VOLTage:OFFSet '+str(offset))

#-------------------------------------------------------
	'''
	Query and set the peak to peak amplitude
	'''
	
	@property
	def amplitude(self):
		resp = float(self.send_text('VOLT?'))
		return(resp)

	@amplitude.setter
	def amplitude(self, amp):
		self.send_text('VOLT'+str(amp))

#-------------------------------------------------------
	'''
	Query and set the high level and low level
	The value is an iterable with two items [HiLevel, LoLevel]
	'''

	@property
	def voltage_level(self):
		HiLevel = float(self.send_text('VOLT:HIGH?'))
		LoLevel = float(self.send_text('VOLT:LOW?'))
		
		return(HiLevel, LoLevel)

	@voltage_level.setter
	def voltage_level(self, level):
		try:
			hi, lo = level
		except ValueError:
			raise ValueError('The voltage level setter needs an iterable with two items: [HiLevel, LoLevel]')
		else:
			self.send_text('VOLT:HIGH '+str(hi))
			self.send_text('VOLT:LOW '+str(lo))

#-------------------------------------------------------

	@property
	def frequency(self):
		resp = float(self.send_text('FREQ?'))
		return(resp)

	@frequency.setter
	def frequency(self, freq):
		self.send_text('FREQ'+str(freq))

#-------------------------------------------------------

	'''
	Query and set the high level and low level
	The property value is a long string containing function, frequency, amplitude, DC offset.
	The setter input value is an iterable with four items: [function, frequency, amplitude, DC offset]
	'''

	'''
	APPLy
		:SINusoid [<frequency> [,<amplitude> [,<offset>] ]]
		:SQUare [<frequency> [,<amplitude> [,<offset>] ]]
		:RAMP [<frequency> [,<amplitude> [,<offset>] ]]
		:PULSe [<frequency> [,<amplitude> [,<offset>] ]]
		:NOISe [<frequency|(any number or DEF)> [,<amplitude> [,<offset>] ]]
		:DC [<frequency|(any number or DEF)> [,<amplitude>|DEF>1 [,<offset>] ]]
		:USER [<frequency> [,<amplitude> [,<offset>] ]]
	'''
	@property
	def mode(self):
		resp = self.send_text('APPL?')
		print('mode, freq, amp, offset are ' + resp)
		return(resp)


	def apply(self, mode):
		try:
			func, freq, amp, offset = mode
		except ValueError:
			raise ValueError('The mode setter needs an iterable with four items: [function, frequency, amplitude, DC offset]')
			
		self.send_text('APPL:' + func + ' ' + str(freq) + ',' + str(amp) + ',' + str(offset))

#-------------------------------------------------------
	'''
	return the current output function,
	without information on freq, amp or offset
	'''
	@property
	def function(self):
		resp = self.send_text('FUNC?')
		return(resp)
	
	'''
	set output function with default parameters
	'''
	@function.setter
	def function(self, func):
		self.send_text('FUNC ' + func)

#-------------------------------------------------------
	'''
	Set pulse width in units of seconds
	'''
	@property
	def pulse_width(self):
		resp = self.send_text('FUNC:PULS:WIDT?')
		return(resp)
	
	'''
	set output function with default parameters
	'''
	@pulse_width.setter
	def pulse_width(self, width):
		self.send_text('FUNC:PULS:WIDT ' + str(width))
		
#-------------------------------------------------------
	'''
	Set pulse period in units of seconds
	'''
	@property
	def pulse_period(self):
		resp = self.send_text('FUNC:PULS:PER?')
		return(resp)
	
	'''
	set output function with default parameters
	'''
	@pulse_period.setter
	def pulse_period(self, period):
		self.send_text('FUNC:PULS:PER ' + str(period))
#-------------------------------------------------------

	# def burst(self):
	# 	resp = self.send_text('BURS:PHAS?')
	# 	return(resp)
	'''
	set up burst mode.
	input has four items: enable(True/False), ncycles, phase, mode(TRIG/GAT)
	when using the setter to disable burst mode, ncycles, phase and mode can be filled with any random thing
	'''
	
	def burst(self, enable, ncycles, phase, mode='TRIG', source='EXT', period=None):
		try:
			if enable:

				self.send_text('BURS:MODE '+ mode)
				self.send_text('BURS:NCYC '+ str(ncycles))
				if period != None:
					self.send_text('BURS:INT:PER '+ str(period))
				self.send_text('BURS:PHAS '+ str(phase))
				self.send_text('TRIG:SOUR '+ source)
				self.send_text('BURS:STAT ON')
			else:
				self.send_text('BURS:STAT OFF')
		except ValueError:
			raise ValueError('The burst setter needs an iterable with four items: [enable(True or False), ncycles, phase, mode]')
		

	def system_error(self):
		return self.send_text('SYST:ERR?')

#----------------------FOR TEST---------------------------------#
if __name__ == '__main__':
	#reply = 'none'
	keysight = wavegen_control(server_ip_addr = '192.168.0.106')
	agilent = wavegen_control(server_ip_addr = '192.168.0.104')
	
	keysight.DCoffset = 0
	
#	keysight.send_text('DATA:ATTR:POIN?')
	#funcnow = agilent.function
	#print('current:', funcnow)
#	agilent.function='SIN'
#	time.sleep(4)
#	agilent.set_output(False)
	#agilent.function='RAMP'
	#time.sleep(0.4)
	#funcnext = agilent.function()
	#print('after change:', funcnext)
	