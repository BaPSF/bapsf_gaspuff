"""Driver for the Pfeiffer TPG 366 MaxiGauge controller over its Ethernet interface (TCP).

Read-only: it sends AYT, UNI, TID, GAS, CID and PRX and never changes a controller setting. One connection is kept
open, because the controller accepts a single network client. Standard library only.

The driver contract shared by every pressure source is four calls: connect(), read(), describe() and close().
read() returns each channel's pressure in Torr with a status from the common set below; the controller's own code
is kept as raw_status. After any failure except a refused command (Tpg366Nak) the connection is closed: the caller
waits (ReconnectDelay) and calls connect() again.

Protocol (TPG 366 Communication Protocol, BG 5511 BEN): the host sends a three-letter mnemonic and <CR><LF>, the
controller answers <ACK><CR><LF> or <NAK><CR><LF>, the host sends <ENQ> and the controller sends the data line.
Built from PfeifferVacuumCommunication.py, which stays unchanged as the reference.

One thread calls connect(), read() and describe(); close() may be called from any thread.
"""
import math
import socket
import time
from dataclasses import dataclass

DRIVER = "tpg366"
DRIVER_VERSION = "0.1.0"
DEFAULT_PORT = 8000
CHANNELS = 6

# Common status codes, the same for every make of controller. The TPG 366 codes 0..6 match them one to one.
STATUS_OK = 0
STATUS_UNDERRANGE = 1
STATUS_OVERRANGE = 2
STATUS_SENSOR_ERROR = 3
STATUS_OFF = 4
STATUS_NO_GAUGE = 5
STATUS_IDENTIFICATION_ERROR = 6
STATUS_OTHER = 7
STATUS_TEXT = {
	STATUS_OK: "ok",
	STATUS_UNDERRANGE: "underrange",
	STATUS_OVERRANGE: "overrange",
	STATUS_SENSOR_ERROR: "sensor error",
	STATUS_OFF: "off",
	STATUS_NO_GAUGE: "no gauge",
	STATUS_IDENTIFICATION_ERROR: "identification error",
	STATUS_OTHER: "other",
}
# The value sent with these statuses is a pressure: the measurement, or the end of the gauge's range.
_STATUS_WITH_VALUE = (STATUS_OK, STATUS_UNDERRANGE, STATUS_OVERRANGE)

# GAS: the controller's own gas correction for each channel.
GAS_NAMES = {0: "Nitrogen", 1: "Argon", 2: "Hydrogen", 3: "Helium", 4: "Neon", 5: "Krypton", 6: "Xenon", 7: "Other"}

# UNI: the unit of every pressure the controller sends, and its factor to Torr. Volt (5) is not a pressure.
_PA_PER_TORR = 101325.0 / 760.0
UNIT_NAMES = {0: "mbar", 1: "Torr", 2: "Pa", 3: "Micron", 4: "hPa", 5: "Volt"}
_TO_TORR = {0: 100.0 / _PA_PER_TORR, 1: 1.0, 2: 1.0 / _PA_PER_TORR, 3: 1.0e-3, 4: 100.0 / _PA_PER_TORR}

# ERR word "abcd": one digit per error.
_ERROR_WORD = ("controller error", "hardware not installed", "inadmissible parameter", "syntax error")

_ETX = b"\x03"  # clears the controller's input buffer
_ENQ = b"\x05"
_ACK = b"\x06"
_NAK = b"\x15"
_END = b"\r\n"
_MAX_LINE = 1024  # the longest real line (PRX) is under 100 bytes
_SETTLE_QUIET_S = 0.2
_SETTLE_MAX_S = 1.0


class Tpg366Error(Exception):
	"""A failed exchange with the controller. Unless it is a Tpg366Nak, the connection has been closed."""


class Tpg366Nak(Tpg366Error):
	"""The controller refused a command (<NAK>). The connection stays open."""


@dataclass(frozen=True)
class ChannelReading:
	pressure_torr: float  # NaN unless status is ok, underrange or overrange
	status: int  # common code, STATUS_*
	raw_status: int  # the controller's own code


@dataclass(frozen=True)
class Reading:
	time: float  # host clock when the reply arrived, epoch seconds
	read_s: float  # duration of the exchange
	channels: tuple  # ChannelReading for channel 1..CHANNELS


@dataclass(frozen=True)
class ChannelInfo:
	model: str  # TID: gauge type as the controller identifies it, e.g. "PKR", "noSENSOR"
	gas_code: int  # GAS
	gas: str  # GAS_NAMES[gas_code]
	name: str  # CID: the measurement point name stored in the controller


@dataclass(frozen=True)
class Description:
	driver: str
	driver_version: str
	model: str  # AYT fields, "" where the controller does not report one
	part_number: str
	serial_number: str
	firmware: str
	hardware: str
	unit: str  # the controller's unit setting; read() converts from it to Torr
	channels: tuple  # ChannelInfo for channel 1..CHANNELS


class ReconnectDelay:
	"""Growing wait between connection attempts: first_s, then times `factor` each failure, at most max_s."""

	def __init__(self, first_s=1.0, factor=2.0, max_s=30.0):
		self._first_s = first_s
		self._factor = factor
		self._max_s = max_s
		self._next_s = first_s

	def next(self):
		"""Seconds to wait before the next attempt."""
		delay = self._next_s
		self._next_s = min(self._next_s * self._factor, self._max_s)
		return delay

	def reset(self):
		"""Call after a successful connect()."""
		self._next_s = self._first_s


def parse_address(text):
	""" "host" or "host:port" -> (host, port); the port defaults to DEFAULT_PORT."""
	host, sep, port = text.strip().partition(":")
	if not host or (sep and not (port.isdigit() and 0 < int(port) < 65536)):
		raise ValueError(f"controller address {text!r} is not host or host:port")
	return host, int(port) if sep else DEFAULT_PORT


class Tpg366:
	def __init__(self, host, port=DEFAULT_PORT, timeout_s=2.0):
		"""timeout_s limits the TCP connect and each wait for one line from the controller."""
		self.host = host
		self.port = port
		self.timeout_s = timeout_s
		self._sock = None
		self._buffer = b""
		self._unit_code = None

	@property
	def connected(self):
		return self._sock is not None

	def connect(self):
		"""Open the connection and return describe(). Raises Tpg366Error; the connection is then closed."""
		self.close()
		try:
			sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
		except OSError as e:
			raise Tpg366Error(f"connect failed: {_os_error_text(e)}") from e
		try:
			sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
		except OSError:
			pass
		self._sock = sock
		self._buffer = b""
		try:
			self._settle()
			return self.describe()
		except Tpg366Error:
			self.close()
			raise

	def close(self):
		sock, self._sock = self._sock, None
		self._unit_code = None
		if sock is None:
			return
		try:
			sock.shutdown(socket.SHUT_RDWR)  # wakes a recv() blocked in another thread
		except OSError:
			pass
		sock.close()

	def read(self):
		"""One PRX exchange: all channels at once."""
		if self._unit_code is None:
			raise Tpg366Error("not connected")
		to_torr = _TO_TORR[self._unit_code]
		t0 = time.perf_counter()
		text = self._query("PRX")
		now = time.time()
		read_s = time.perf_counter() - t0
		fields = self._fields("PRX", text, 2 * CHANNELS)
		channels = []
		try:
			for raw_text, value_text in zip(fields[0::2], fields[1::2]):
				raw_status = int(raw_text)
				value = float(value_text)
				status = raw_status if STATUS_OK <= raw_status <= STATUS_IDENTIFICATION_ERROR else STATUS_OTHER
				has_value = status in _STATUS_WITH_VALUE and math.isfinite(value)
				channels.append(ChannelReading(value * to_torr if has_value else math.nan, status, raw_status))
		except ValueError as e:
			raise self._fail(f"PRX reply {text!r} is not status,pressure pairs") from e
		return Reading(now, read_s, tuple(channels))

	def describe(self):
		"""What the controller reports about itself and its channels. Also refreshes the unit read() converts from,
		so call it again from time to time: the unit can be changed on the controller's front panel."""
		try:
			identity = ([f.strip() for f in self._query("AYT").split(",")] + [""] * 5)[:5]
		except Tpg366Nak:
			identity = [""] * 5
		unit_text = self._query("UNI")
		models = self._fields("TID", self._query("TID"), CHANNELS)
		gas_texts = self._fields("GAS", self._query("GAS"), CHANNELS)
		names = self._fields("CID", self._query("CID"), CHANNELS)
		try:
			unit_code = int(unit_text)
			gas_codes = [int(g) for g in gas_texts]
		except ValueError as e:
			raise self._fail(f"UNI reply {unit_text!r} or GAS reply {gas_texts!r} is not numeric") from e
		if unit_code not in _TO_TORR:
			unit = UNIT_NAMES.get(unit_code, f"code {unit_code}")
			raise self._fail(f"controller unit is {unit}, which cannot be converted to Torr; set a pressure unit on the controller")
		self._unit_code = unit_code
		channels = tuple(
			ChannelInfo(model, gas, GAS_NAMES.get(gas, f"code {gas}"), name)
			for model, gas, name in zip(models, gas_codes, names)
		)
		return Description(DRIVER, DRIVER_VERSION, *identity, UNIT_NAMES[unit_code], channels)

	def _settle(self):
		"""Clear the controller's input buffer and discard what it was still sending (a continuous output left
		running stops at the first byte it receives)."""
		self._send(_ETX)
		sock = self._sock
		end = time.monotonic() + _SETTLE_MAX_S
		try:
			while sock is not None and time.monotonic() < end:
				sock.settimeout(_SETTLE_QUIET_S)
				if not sock.recv(4096):
					raise self._fail("connection closed by the controller")
		except TimeoutError:
			pass
		except OSError as e:
			raise self._fail(_os_error_text(e)) from e

	def _query(self, mnemonic):
		self._send(mnemonic.encode("ascii") + _END)
		reply = self._line(mnemonic)
		if reply == _NAK:
			self._send(_ENQ)  # with no valid request pending, <ENQ> returns the error word
			raise Tpg366Nak(f"{mnemonic} refused: {_error_word_text(self._line(mnemonic))}")
		if reply != _ACK:
			raise self._fail(f"{mnemonic}: expected ACK, got {reply!r}")
		self._send(_ENQ)
		try:
			return self._line(mnemonic).decode("ascii").strip()
		except UnicodeDecodeError as e:
			raise self._fail(f"{mnemonic}: reply is not ASCII") from e

	def _fields(self, mnemonic, text, count):
		fields = [f.strip() for f in text.split(",")]
		if len(fields) != count:
			raise self._fail(f"{mnemonic} reply {text!r} has {len(fields)} fields, expected {count}")
		return fields

	def _send(self, data):
		sock = self._sock
		if sock is None:
			raise Tpg366Error("not connected")
		try:
			sock.settimeout(self.timeout_s)
			sock.sendall(data)
		except OSError as e:
			raise self._fail(_os_error_text(e)) from e

	def _line(self, mnemonic):
		"""The next line from the controller, without its <CR><LF>."""
		sock = self._sock
		if sock is None:
			raise Tpg366Error("not connected")
		end = time.monotonic() + self.timeout_s
		while _END not in self._buffer:
			remaining = end - time.monotonic()
			if remaining <= 0:
				raise self._fail(f"{mnemonic}: no reply within {self.timeout_s:g} s")
			if len(self._buffer) > _MAX_LINE:
				raise self._fail(f"{mnemonic}: {len(self._buffer)} bytes without a line end")
			try:
				sock.settimeout(remaining)
				data = sock.recv(4096)
			except TimeoutError as e:
				raise self._fail(f"{mnemonic}: no reply within {self.timeout_s:g} s") from e
			except OSError as e:
				raise self._fail(f"{mnemonic}: {_os_error_text(e)}") from e
			if not data:
				raise self._fail(f"{mnemonic}: connection closed by the controller")
			self._buffer += data
		line, _, self._buffer = self._buffer.partition(_END)
		return line

	def _fail(self, message):
		"""Close the connection (the stream may be out of step) and return the error to raise."""
		self.close()
		return Tpg366Error(message)


def _os_error_text(e):
	return e.strerror or str(e) or type(e).__name__


def _error_word_text(word):
	text = word.decode("ascii", "replace").strip()
	if len(text) != len(_ERROR_WORD) or set(text) - set("01"):
		return f"error word {text!r}"
	return ", ".join(name for digit, name in zip(text, _ERROR_WORD) if digit == "1") or "no error reported"
