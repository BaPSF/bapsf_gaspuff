"""diag_ioc module of the machine pressure monitor: it reads the gauge controller itself and publishes every reading.

Config: factory = "pressure_ioc:PressureModule" with no `listen`; deploy/diag-ioc/pressure.toml.example lists the
options and the README lists the PVs. <P> is the module's prefix, <S> a source key (one controller) and <G> a gauge key.

- One reader thread per source keeps the connection (the controller takes one client), reads every period_s on a fixed
  clock and hands each reading to the host's pipeline. Controller trouble never raises into the pipeline: the first
  failed read of an outage publishes every gauge as INVALID with status 8 and the reason in <S>:ERROR, and the reader
  reconnects with a growing delay.
- Every reading is published as one record chain, <S>:SAMPLE:SEQ first, and as the PVA group <P>:<S>:SAMPLE, so a
  client never sees a mix of two readings.
- <G>:P = <G>:P_RAW x <G>:GAS_FACTOR, this gauge's multiplier for the gas selected on this machine (pressure_gas.py).
- <G>:BASE is the lowest 1-minute mean of P_RAW in the trailing window, with the alarm limits. It is compared before
  the multiplier, so a limit does not move when the gas selection changes.

Imports neither softioc (the host passes `builder`) nor any EPICS library.
"""
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from functools import partial

import numpy as np

import tpg366
from diag_ioc.module import DiagnosticModule
from diag_ioc.outage import Outage
from diag_ioc.records import (INVALID_ALARM, MAJOR_ALARM, MINOR_ALARM, READ_ALARM, SOFT_ALARM, UDF_ALARM, add_group, chain,
                              fit_text, passive, scalar)
from pressure_config import STRING_MAX, parse_options
from pressure_gas import DEFAULT_BY, Selection, read_selection

log = logging.getLogger(__name__)

# Gauge status codes, the same for every make of controller: 0..7 come from the driver (tpg366.STATUS_*).
STATUS_NO_REPLY = 8  # set here: the controller did not answer, so there is no reading
RAW_STATUS_NO_REPLY = -1
DEVICE_GAS = "Nitrogen"  # the controller's own gas correction must stay here (multiplier 1): GAS_FACTOR does the correcting
DESCRIBE_PERIOD_S = 60.0  # how often the unit, gauge types, gas settings and names are read again
GAS_POLL_S = 1.0
ERROR_TEXT_BYTES = 256  # <S>:ERROR length, NUL included
STOP_TIMEOUT_S = 5.0

_INVALID = {"severity": INVALID_ALARM, "alarm": READ_ALARM}
_UNDEFINED = {"severity": INVALID_ALARM, "alarm": UDF_ALARM}
_MINOR = {"severity": MINOR_ALARM, "alarm": READ_ALARM}
# Under- and overrange send the end of the gauge's range, so the value stands with a MINOR alarm. Every other
# status but ok has no pressure.
_PRESSURE_ALARM = {tpg366.STATUS_OK: {}, tpg366.STATUS_UNDERRANGE: _MINOR, tpg366.STATUS_OVERRANGE: _MINOR}
# Readings that count towards the base pressure. Underrange counts: the pressure is at or below the value sent.
_BASE_STATUSES = (tpg366.STATUS_OK, tpg366.STATUS_UNDERRANGE)


@dataclass(frozen=True)
class Driver:
	"""One kind of controller. make(address, timeout_s) returns an object with connect() and describe() (a description
	shaped like tpg366.Description), read() (like tpg366.Reading, pressures in Torr, common status codes) and close()
	(safe from another thread). A failed exchange raises `error`."""
	channels: int
	error: type
	make: object


DRIVERS = {
	tpg366.DRIVER: Driver(tpg366.CHANNELS, tpg366.Tpg366Error,
	                      lambda address, timeout_s: tpg366.Tpg366(*tpg366.parse_address(address), timeout_s=timeout_s)),
}


class BaseWindow:
	"""Base pressure of one gauge: the lowest 1-minute mean in the trailing `window_minutes`.

	A minute is a clock minute. It counts once it is over, if it holds at least `min_count` readings. The base is NaN
	until `ready_minutes` such minutes are in the window, so it starts as NaN after every IOC start.
	"""

	def __init__(self, window_minutes, ready_minutes, min_count):
		self._window = window_minutes
		self._ready = ready_minutes
		self._min_count = min_count
		self._means = deque()  # (minute, mean) of the minutes that count, oldest first
		self._minute = None  # the clock minute being summed
		self._sum = 0.0
		self._count = 0
		self._lowest = math.nan

	def add(self, time_s, pressure=None):
		"""Move on to `time_s` (epoch s), add `pressure` if given, and return the base pressure."""
		minute = int(time_s // 60)
		if self._minute is None:
			self._minute = minute
		if minute > self._minute:  # a clock set back keeps summing the same minute
			if self._count >= self._min_count:
				self._means.append((self._minute, self._sum / self._count))
			self._minute, self._sum, self._count = minute, 0.0, 0
			while self._means and self._means[0][0] < minute - self._window:
				self._means.popleft()
			self._lowest = min(mean for _, mean in self._means) if len(self._means) >= self._ready else math.nan
		if pressure is not None:
			self._sum += pressure
			self._count += 1
		return self._lowest


@dataclass(frozen=True)
class Sample:
	"""Reader -> analyze(): one reading of the source, or with channels None the failed read that starts an outage."""
	seq: int  # counts this IOC's samples; a gap is a sample the pipeline dropped
	time: float  # epoch s: when the reply arrived, or when the read failed
	read_s: float
	channels: tuple | None  # the driver's reading of every controller channel
	selection: Selection  # the gas selected when the sample was taken


@dataclass(frozen=True)
class GaugeValues:
	p_raw: float
	gas_factor: float
	p: float
	status: int
	raw_status: int
	base: float


@dataclass(frozen=True)
class Analyzed:
	sample: Sample
	gauges: tuple  # GaugeValues, in the order of the configured gauges


class PressureModule(DiagnosticModule):
	"""ValueError at construction names a bad option (pressure_config.parse_options)."""

	def __init__(self, name, options):
		super().__init__(name, options)
		self.config = parse_options(self.options, {key: driver.channels for key, driver in DRIVERS.items()})
		(self.source,) = self.config.sources
		driver = DRIVERS[self.source.driver]
		try:
			self._driver = driver.make(self.source.address, self.source.timeout_s)
		except ValueError as e:
			raise ValueError(f"pressure options: source {self.source.key}: {e}") from None
		self._driver_error = driver.error
		window_minutes = round(self.config.base_window_h * 60)
		min_count = max(1, int(30 / self.source.period_s))  # half of a minute's readings
		self._bases = [BaseWindow(window_minutes, self.config.base_ready_min, min_count) for _ in self.config.gauges]
		self._species_index = {name: i for i, name in enumerate(self.config.gas.species)}
		self._selection = None  # the gas Selection in force; set in start()
		self._default_selection = None
		self._seq = 0
		self._connected = None  # None until the first read or failure, then True or False
		self._connects = 0
		self._submit = None
		self._stop = threading.Event()
		self._threads = []
		self._outage = Outage(log, f"source {self.source.key}")
		self._gas_outage = Outage(log, f"gas selection file {self.config.gas.selection_file}")
		self._device_gas_outage = Outage(log, f"source {self.source.key} gas setting check")

	#===========================================================================================================================================
	# Records

	def create_records(self, builder):
		s, gas = self.source.key, self.config.gas
		# The publish chain: SAMPLE:SEQ is the I/O Intr head, so its set() runs the whole chain, the last gauge's BASE last.
		makers = [
			("seq", partial(builder.longIn, f"{s}:SAMPLE:SEQ", TSE=-2, DESC="sample counter since IOC start")),
			("time", partial(scalar, builder, f"{s}:SAMPLE:TIME", egu="s", prec=3, desc="epoch s of the reading")),
			# MPST: a stringin posts only on change by default, and the gas repeats.
			("gas", partial(passive, builder.stringIn, f"{s}:SAMPLE:GAS", "gas selected at this reading", MPST="Always")),
			("read_s", partial(scalar, builder, f"{s}:READ_S", egu="s", prec=4, desc="duration of the read exchange")),
		]
		for gauge in self.config.gauges:
			k = gauge.key
			limits = {}
			if gauge.base_warning_torr is not None:
				limits.update(HIGH=gauge.base_warning_torr, HSV="MINOR")
			if gauge.base_alarm_torr is not None:
				limits.update(HIHI=gauge.base_alarm_torr, HHSV="MAJOR")
			makers += [
				((k, "p_raw"), partial(scalar, builder, f"{k}:P_RAW", egu="Torr", prec=3, desc="pressure as the gauge sent it")),
				((k, "gas_factor"), partial(passive, builder.aIn, f"{k}:GAS_FACTOR", "multiplier for the selected gas", PREC=3)),
				((k, "p"), partial(scalar, builder, f"{k}:P", egu="Torr", prec=3, desc="pressure, P_RAW x GAS_FACTOR")),
				((k, "status"), partial(passive, builder.longIn, f"{k}:STATUS", "gauge status code, 0 = ok")),
				((k, "raw_status"), partial(passive, builder.longIn, f"{k}:RAW_STATUS", "the controller's own status code")),
				((k, "base"), partial(passive, builder.aIn, f"{k}:BASE", "lowest 1-minute mean of P_RAW", EGU="Torr", PREC=3, **limits)),
			]
		keys, makes = zip(*makers)
		self._chain = dict(zip(keys, chain(makes)))

		# Outside the chain: the link to the controller and what it reports about itself, set by the reader.
		text = builder.stringIn
		self._source_records = {
			"connected": builder.boolIn(f"{s}:CONNECTED", ZNAM="no", ONAM="yes", ZSV="MAJOR", DESC="the controller answers"),
			"error": builder.longStringIn(f"{s}:ERROR", length=ERROR_TEXT_BYTES, DESC="why the last read failed"),
			"reconnects": builder.longIn(f"{s}:RECONNECTS", initial_value=0, DESC="connections regained since IOC start"),
			"driver": text(f"{s}:DRIVER", DESC="driver and its version"),
			"model": text(f"{s}:MODEL", DESC="controller model, as it reports"),
			"serial_number": text(f"{s}:SERIAL", DESC="controller serial number"),
			"firmware": text(f"{s}:FIRMWARE", DESC="controller firmware version"),
			"unit": text(f"{s}:UNIT", DESC="unit set on the controller"),
		}
		builder.aIn(f"{s}:PERIOD_S", initial_value=self.source.period_s, EGU="s", PREC=3, DESC="read period")
		builder.aIn("BASE_WINDOW_H", initial_value=self.config.base_window_h, EGU="h", PREC=1, DESC="hours the base pressure looks back")
		self._gauge_records = {}
		for gauge in self.config.gauges:
			k = gauge.key
			# Fixed from the config.
			builder.stringIn(f"{k}:NAME", initial_value=gauge.name, DESC="where the gauge sits")
			builder.stringIn(f"{k}:SOURCE", initial_value=gauge.source, DESC="source key of its controller")
			builder.longIn(f"{k}:CHANNEL", initial_value=gauge.channel, DESC="channel on its controller")
			builder.WaveformIn(f"{k}:GAS_FACTORS", initial_value=np.array(gauge.multiplier), PREC=3,
			                   DESC="multipliers, in GAS:SPECIES order")
			self._gauge_records[k] = {
				"model": text(f"{k}:MODEL", DESC="gauge type, as the controller reports"),
				"gas": text(f"{k}:DEVICE_GAS", DESC="the controller's own gas setting"),
				"name": text(f"{k}:DEVICE_NAME", DESC="name stored in the controller"),
			}

		# The gas selection has its own prefix: it belongs to the machine, not to pressure.
		prefix = builder.RecordName("")[:-1]
		builder.SetDeviceName(gas.prefix)
		try:
			self._gas_records = {
				"species": builder.mbbIn("SPECIES", *gas.species, DESC="gas selected on the IOC machine"),
				"since": builder.aIn("SINCE", EGU="s", PREC=0, DESC="epoch s the gas was selected"),
				"by": builder.stringIn("BY", DESC="account that selected the gas"),
			}
		finally:
			builder.SetDeviceName(prefix)

		self._add_group(builder.RecordName(f"{s}:SAMPLE"))
		return self._chain[(self.config.gauges[-1].key, "base")]

	def _add_group(self, group):
		"""PVA group <P>:<S>:SAMPLE: one update per sample, posted when the last gauge's BASE processes."""
		r = self._chain
		add_group(r["seq"], group, {"": {"+type": "meta"}, "seq": {"+type": "plain"}})
		for key in ("time", "gas", "read_s"):
			add_group(r[key], group, {key: {"+type": "plain"}})
		for gauge in self.config.gauges:
			field = gauge.key.lower()
			for key in ("p_raw", "gas_factor", "p", "status", "raw_status"):
				add_group(r[(gauge.key, key)], group, {f"{field}.{key}": {"+type": "plain"}})
			trigger = {"+trigger": "*"} if gauge is self.config.gauges[-1] else {}
			add_group(r[(gauge.key, "base")], group, {f"{field}.base": {"+type": "plain", **trigger}})

	#===========================================================================================================================================
	# Pipeline worker: one sample at a time

	def analyze(self, sample):
		"""Sample -> Analyzed. It keeps each gauge's base window, so the pipeline calls it once per sample, in order."""
		index = self._species_index[sample.selection.species]
		gauges = []
		for gauge, base in zip(self.config.gauges, self._bases):
			factor = gauge.multiplier[index]
			if sample.channels is None:
				p_raw, status, raw_status = math.nan, STATUS_NO_REPLY, RAW_STATUS_NO_REPLY
			else:
				channel = sample.channels[gauge.channel - 1]
				p_raw, status, raw_status = channel.pressure_torr, channel.status, channel.raw_status
			counts = status in _BASE_STATUSES and math.isfinite(p_raw)
			gauges.append(GaugeValues(p_raw, factor, p_raw * factor, status, raw_status, base.add(sample.time, p_raw if counts else None)))
		return Analyzed(sample, tuple(gauges))

	def publish(self, result):
		sample, r, ts = result.sample, self._chain, result.sample.time
		no_reply = _INVALID if sample.channels is None else {}
		r["time"].set(ts, timestamp=ts)
		r["gas"].set(sample.selection.species, timestamp=ts)
		r["read_s"].set(sample.read_s, timestamp=ts, **no_reply)
		for gauge, values in zip(self.config.gauges, result.gauges):
			k = gauge.key
			alarm = _PRESSURE_ALARM.get(values.status, _INVALID)  # never a number without its alarm when the gauge has no reading
			r[(k, "p_raw")].set(values.p_raw, timestamp=ts, **alarm)
			r[(k, "gas_factor")].set(values.gas_factor, timestamp=ts)
			r[(k, "p")].set(values.p, timestamp=ts, **alarm)
			r[(k, "status")].set(values.status, timestamp=ts, **no_reply)
			r[(k, "raw_status")].set(values.raw_status, timestamp=ts, **no_reply)
			r[(k, "base")].set(values.base, timestamp=ts, **(_UNDEFINED if math.isnan(values.base) else {}))
		r["seq"].set(sample.seq, timestamp=ts)  # last: processing the head publishes the sample

	#===========================================================================================================================================
	# Threads

	def start(self, host):
		self._submit = host.pipeline.submit
		self._default_selection = Selection(self.config.gas.default, time.time(), DEFAULT_BY)
		self._poll_gas()  # before the reader starts: every sample carries a selection
		for target, name in ((self._watch_gas, "gas"), (self._read, self.source.key)):
			thread = threading.Thread(target=target, name=f"pressure-{name}", daemon=True)
			thread.start()
			self._threads.append(thread)

	def stop(self):
		self._stop.set()
		self._driver.close()  # wakes a read waiting for the controller
		for thread in self._threads:
			thread.join(STOP_TIMEOUT_S)
		self._driver.close()  # a connect that was under way has finished by now

	def _watch_gas(self):
		while not self._stop.wait(GAS_POLL_S):
			self._poll_gas()

	def _poll_gas(self):
		"""Read the selection file; publish the selection when it changed. A file that cannot be read or names no listed
		species changes nothing (at IOC start: the default is used)."""
		try:
			selection = read_selection(self.config.gas) or self._default_selection
		except (OSError, ValueError) as e:
			selection = self._selection or self._default_selection
			self._gas_outage.failed(e, f"the selection stays {selection.species}")
		else:
			self._gas_outage.ended()
		if selection == self._selection:
			return
		self._selection = selection
		r = self._gas_records
		r["species"].set(self._species_index[selection.species])
		r["since"].set(selection.since)
		r["by"].set(fit_text(selection.by, STRING_MAX + 1))
		log.info("gas selection: %s, by %s", selection.species, selection.by)

	def _read(self):
		"""The reader thread: connect, read on the fixed clock until something fails, wait, connect again."""
		delay = tpg366.ReconnectDelay()
		while not self._stop.is_set():
			try:
				self._describe(self._driver.connect())
				self._read_until_failure(delay)
			except Exception as e:
				if self._stop.is_set():
					break  # stop() closed the connection
				self._driver.close()
				self._lost(e)
				self._stop.wait(delay.next())

	def _read_until_failure(self, delay):
		period = self.source.period_s
		tick = time.monotonic()
		describe_at = tick + DESCRIBE_PERIOD_S
		while not self._stop.is_set():
			reading = self._driver.read()
			if not self._connected:
				self._found()
				delay.reset()
			self._seq += 1
			self._submit(Sample(self._seq, reading.time, reading.read_s, reading.channels, self._selection))
			now = time.monotonic()
			if now >= describe_at:  # also refreshes the unit the driver converts from
				self._describe(self._driver.describe())
				describe_at = now + DESCRIBE_PERIOD_S
				now = time.monotonic()
			tick += period * max(1, math.ceil((now - tick) / period))  # the next tick of the fixed clock; a missed one is skipped
			self._stop.wait(tick - now)

	def _found(self):
		"""The first good read after a connect: the source is up."""
		self._connected = True
		self._connects += 1
		r = self._source_records
		r["connected"].set(1)
		r["error"].set("")
		r["reconnects"].set(self._connects - 1)
		self._outage.ended()

	def _lost(self, error):
		"""A failed connect or read. The first of an outage publishes one sample with no reading."""
		expected = isinstance(error, (self._driver_error, OSError))
		text = str(error) if expected else f"{type(error).__name__}: {error}"
		self._outage.failed(text, exc_info=not expected)
		r = self._source_records
		r["error"].set(fit_text(text, ERROR_TEXT_BYTES), severity=MAJOR_ALARM, alarm=SOFT_ALARM)
		if self._connected is not False:
			self._connected = False
			r["connected"].set(0)
			self._seq += 1
			self._submit(Sample(self._seq, time.time(), math.nan, None, self._selection))

	def _describe(self, description):
		"""Publish what the controller reports about itself and its gauges, and check its own gas settings."""
		r = self._source_records
		r["driver"].set(_short(f"{description.driver} {description.driver_version}"))
		for key in ("model", "serial_number", "firmware", "unit"):
			r[key].set(_short(getattr(description, key)))
		corrected = []
		for gauge in self.config.gauges:
			info, r = description.channels[gauge.channel - 1], self._gauge_records[gauge.key]
			r["model"].set(_short(info.model))
			r["name"].set(_short(info.name))
			r["gas"].set(_short(info.gas), **({} if info.gas == DEVICE_GAS else _MINOR))
			if info.gas != DEVICE_GAS:
				corrected.append(f"{gauge.key} is set to {info.gas}")
		if corrected:
			self._device_gas_outage.failed("; ".join(corrected), f"the controller must stay on {DEVICE_GAS}, or GAS_FACTOR corrects a second time")
		else:
			self._device_gas_outage.ended()


def _short(text):
	return fit_text(text, STRING_MAX + 1)
