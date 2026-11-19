"""pressure_store --config FILE --dir DIR: save every reading of the pressure IOC in the day's SQLite file.

	python pressure_store.py --config /etc/diag-ioc/pressure.toml --dir /var/lib/pressure

FILE is the IOC's own config: the store takes the PV prefix, the source and the gauges from it. pressure_db.py describes
the files and reads them. The files are the only record of the pressure, so:

- Its own process, fed only by PVs: the PVA group <P>:<S>:SAMPLE (one complete update per reading) and the single PVs
  that describe the controller, the gauges and the gas selection. Nothing in the IOC imports it, calls it or waits on
  it, so no disk trouble reaches the IOC. The monitor callbacks only queue.
- One commit a second, on its own thread. A commit that fails (a full disk) is tried again every second with everything
  received since, up to QUEUE_ITEMS items; beyond that the oldest are dropped and counted.
- A reading is stored as the gauge sent it, with the number of its setting row (the gas and each gauge's multiplier).
  A change of gas or multiplier adds a setting row; no reading is rewritten.
- Gaps are counted, not guessed: a jump in the IOC's sample counter is an event with the number of readings missed.
  Lost and regained connections, gauge status changes and changes of what the controller reports are events too.

Needs p4p and kernel IPv6 (PVXS), the diag_ioc package (bapsf_epics_core) and pfeiffer/ on the Python path.
"""
import argparse
import copy
import functools
import hashlib
import itertools
import logging
import math
import queue
import signal
import socket
import sys
import threading
import time
import tomllib
import zoneinfo
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import pressure_db
import tpg366
from diag_ioc.outage import LOG_FORMAT, Outage
from pressure_config import parse_options
from pressure_ioc import DRIVERS, STATUS_NO_REPLY

log = logging.getLogger(__name__)

VERSION = "0.1.0"
FACTORY = "pressure_ioc:PressureModule"  # the [[module]] of the config that this store records
COMMIT_S = 1.0
QUEUE_ITEMS = 6000  # 10 minutes of readings at 10 a second
EVENTS_PER_MINUTE = 10  # of one kind about one gauge or source; the rest of that minute are counted in one more event
STOP_TIMEOUT_S = 20.0  # for the last commit at exit; under the unit's TimeoutStopSec
STATUS_TEXT = {**tpg366.STATUS_TEXT, STATUS_NO_REPLY: "no reply"}

# PV name suffix -> column of the source and gauge tables, for what the controller reports about itself.
SOURCE_PVS = {"DRIVER": "driver", "MODEL": "model", "SERIAL": "serial_number", "FIRMWARE": "firmware", "UNIT": "unit"}
GAUGE_PVS = {"MODEL": "model", "DEVICE_GAS": "device_gas", "DEVICE_NAME": "device_name"}


@dataclass(frozen=True)
class Layout:
	"""What the store takes from the IOC's config file."""
	prefix: str  # the module's PV prefix
	options: object  # pressure_config.Options
	config_sha256: str

	@property
	def source(self):
		return self.options.sources[0]

	@property
	def gauge_keys(self):
		return tuple(gauge.key for gauge in self.options.gauges)

	@property
	def group(self):
		return f"{self.prefix}:{self.source.key}:SAMPLE"

	def pvs(self):
		"""{PV name: (what, key, field)} of the single PVs the store monitors. what is "source", "gauge", "gas" or "error"."""
		s, gas = f"{self.prefix}:{self.source.key}", self.options.gas.prefix
		names = {f"{s}:{suffix}": ("source", self.source.key, column) for suffix, column in SOURCE_PVS.items()}
		names[f"{s}:ERROR"] = ("error", self.source.key, None)
		for key in self.gauge_keys:
			names.update({f"{self.prefix}:{key}:{suffix}": ("gauge", key, column) for suffix, column in GAUGE_PVS.items()})
		names.update({f"{gas}:{suffix}": ("gas", None, suffix.lower()) for suffix in ("SPECIES", "SINCE", "BY")})
		return names


def load_layout(path):
	"""Layout from the IOC's TOML config file. OSError if it cannot be read, ValueError if it is not a pressure IOC's."""
	data = Path(path).read_bytes()
	modules = tomllib.loads(data.decode("utf-8")).get("module", [])
	modules = [m for m in modules if type(m) is dict and m.get("factory") == FACTORY] if type(modules) is list else []
	if len(modules) != 1:
		raise ValueError(f"{path}: expected one [[module]] with factory = {FACTORY!r}, found {len(modules)}")
	(module,) = modules
	if type(module.get("prefix")) is not str:
		raise ValueError(f"{path}: the pressure [[module]] has no prefix")
	options = parse_options(module.get("options"), {name: driver.channels for name, driver in DRIVERS.items()})
	return Layout(module["prefix"], options, hashlib.sha256(data).hexdigest())


#===================================================================================================================================================
# PV updates -> plain values. `value` is a p4p Value with unwrapping off (Context(nt=False)), or the same fields as
# nested dicts.

@dataclass(frozen=True)
class GaugeSample:
	p_raw: float  # Torr; NaN without a pressure
	gas_factor: float
	status: int
	raw_status: int


@dataclass(frozen=True)
class GroupSample:
	"""One <P>:<S>:SAMPLE update."""
	seq: int
	time: float  # epoch s of the reading
	gas: str
	read_s: float
	gauges: tuple  # GaugeSample, in the order of the configured gauges


def sample_from_group(value, gauge_keys):
	"""GroupSample of one group update, or None for the IOC's state before its first sample after a start (seq or time
	still 0), which a new or reconnected monitor receives. Every field comes from this one update."""
	seq, time_s = int(value["seq"]), float(value["time"])
	if seq == 0 or time_s == 0:
		return None
	if not math.isfinite(time_s):
		raise ValueError(f"sample {seq} has the time {time_s}")
	gauges = []
	for key in gauge_keys:
		gauge = value[key.lower()]
		p_raw, gas_factor = float(gauge["p_raw"]), float(gauge["gas_factor"])
		if not math.isfinite(gas_factor):
			raise ValueError(f"sample {seq} gauge {key} has gas_factor {gas_factor}")
		gauges.append(GaugeSample(p_raw if math.isfinite(p_raw) else math.nan, gas_factor, int(gauge["status"]), int(gauge["raw_status"])))
	read_s = float(value["read_s"])
	return GroupSample(seq, time_s, str(value["gas"]), read_s if math.isfinite(read_s) else math.nan, tuple(gauges))


def pv_value(value):
	"""The value of a single-record PV update: text, a float, or for an enum record its selected text."""
	v = value["value"]
	if isinstance(v, str):
		return v
	if isinstance(v, (int, float, np.number)):
		return float(v)
	if isinstance(v, np.ndarray):  # a long string record is served as a NUL-terminated char array
		return v.astype(np.uint8).tobytes().split(b"\0", 1)[0].decode("utf-8", "replace")
	index, choices = int(v["index"]), list(v["choices"])
	return str(choices[index]) if 0 <= index < len(choices) else ""


#===================================================================================================================================================
# The writer

@dataclass
class _State:
	"""What the writer remembers between commits. A failed commit puts back the copy taken before it."""
	last: tuple | None = None  # (seq, time) of the last sample taken
	live: bool = False  # a sample was taken since this process started: the counter can be followed
	lost: bool = False  # the last sample was the one that starts a controller outage
	status: dict = field(default_factory=dict)  # {gauge key: status of its last reading}
	setting_key: tuple | None = None  # (species, multipliers) of the last sample
	setting_id: int | None = None  # its setting row in the open file
	pv: dict = field(default_factory=dict)  # {(what, key, field): latest value}
	describe: bool = True  # gauge and source rows are due: something changed, or the file is new
	has_samples: bool = False  # the open file holds readings
	summarized_to: float | None = None  # start of the newest minute seen in the open file; the minutes before it are summarized
	limits: dict = field(default_factory=dict)  # {(kind, subject): [minute, listed, hidden, readings]}, see _limited()
	stored: int = 0
	repeated: int = 0  # updates received again, not stored twice
	missed: int = 0  # readings the sample counter skipped
	dropped: int = 0  # items dropped here while commits failed


class Store:
	"""on_group and on_pv are the monitor callbacks: they copy each update onto a bounded queue. write() commits what is
	queued; run() calls it once a second on the writer thread, which alone touches the files."""

	def __init__(self, directory, layout, timezone=pressure_db.TIMEZONE, clock=time.time):
		self.directory, self.layout, self.timezone = Path(directory), layout, timezone
		self.unreadable = 0  # updates the callbacks could not read (their lock)
		self.overflowed = 0  # updates the callbacks dropped with the queue full (their lock)
		self._clock = clock
		self._pvs = layout.pvs()
		self._queue = queue.Queue(QUEUE_ITEMS)
		self._pending = deque()  # (time, kind, payload) items not yet committed, oldest first
		self._state = _State()
		self._file = None  # the open pressure_db.DayFile
		self._day = None
		self._finished = threading.Event()
		self._lock = threading.Lock()  # the callbacks run on p4p's worker threads
		self._connected = False
		self._monitor = Outage(log, f"PVA monitor of {layout.group}")
		self._reads = Outage(log, "reading a PV update")
		self._full = Outage(log, "store queue")
		self._writes = Outage(log, f"writing in {self.directory}")
		self._resume()
		self._pending.append((clock(), "event", ("store start", f"pressure_store {VERSION} started", None)))

	#-----------------------------------------------------------------------------------------------------------------------------------------------
	# Monitor callbacks: any thread

	def on_group(self, value):
		"""A notice of the group monitor: a value, or an exception (Disconnected, which p4p also sends once before the first
		connection; RemoteError)."""
		with self._lock:
			if isinstance(value, Exception):
				if self._connected:
					self._monitor.failed(value)
					self._put("event", ("ioc disconnected", f"{self.layout.group}: {type(value).__name__}: {value}".rstrip(": "), None))
				self._connected = False
				return
			if not self._connected:
				self._connected = True
				if self._monitor.since is None:
					log.info("%s connected", self.layout.group)
				else:
					self._monitor.ended()
				self._put("event", ("ioc connected", self.layout.group, None))
			try:
				sample = sample_from_group(value, self.layout.gauge_keys)
			except Exception as e:  # a group without these fields: the IOC runs another config
				self.unreadable += 1
				self._reads.failed(e, f"{self.layout.group}: update not stored", exc_info=True)
				return
			if sample is not None:
				self._put("sample", sample, sample.time)

	def on_pv(self, name, value):
		"""A notice of the monitor of the single PV `name`. Exceptions (disconnects) are left to the group's monitor."""
		if isinstance(value, Exception):
			return
		with self._lock:
			try:
				self._put("pv", (self._pvs[name], pv_value(value)))
			except Exception as e:
				self.unreadable += 1
				self._reads.failed(e, f"{name}: update not used", exc_info=True)

	def _put(self, kind, payload, when=None):
		try:
			self._queue.put_nowait((self._clock() if when is None else when, kind, payload))
		except queue.Full:
			self.overflowed += 1
			self._full.failed("full", "update not stored")
		else:
			self._full.ended()

	#-----------------------------------------------------------------------------------------------------------------------------------------------
	# The writer thread

	def run(self):
		"""One commit a second until finish(), then the last one, and the file is closed."""
		while not self._finished.wait(COMMIT_S):
			self.write()
		self.close()

	def finish(self):
		"""End run(); call it after closing the monitors."""
		self._finished.set()

	def write(self):
		"""Commit everything queued. Returns False if a commit failed: what it held is kept for the next call."""
		self._take()
		try:
			while self._pending:
				self._commit()
		except Exception as e:
			self._writes.failed(e, f"{len(self._pending)} updates kept for the next try", exc_info=not isinstance(e, OSError))
			self._close_file()  # opened again at the next try
			return False
		self._writes.ended()
		return True

	def close(self):
		"""The last commit, with a "store stop" event, then close the file. Logs what could not be written."""
		self._take()
		self._pending.append((self._clock(), "event", ("store stop", f"pressure_store stopped: {self.summary()}", None)))
		if not self.write():
			log.error("%d updates not stored at exit", len(self._pending))
		self._close_file()

	def _take(self):
		"""Move what the callbacks queued to _pending, which keeps the newest QUEUE_ITEMS items."""
		while True:
			try:
				self._pending.append(self._queue.get_nowait())
			except queue.Empty:
				break
		while len(self._pending) > QUEUE_ITEMS:
			self._pending.popleft()
			self._state.dropped += 1

	def summary(self):
		s = self._state
		return (f"{s.stored} readings stored, {s.missed} missed (gaps in the sample counter), {s.repeated} received twice, "
		        f"{s.dropped} dropped here while writes failed, {self.overflowed} dropped with the queue full, "
		        f"{self.unreadable} unreadable updates")

	def _resume(self):
		"""Take the last stored sample of the newest day file that has one as the baseline, so the sample the IOC sends
		again to a new monitor is not taken for a new one."""
		try:
			for path in sorted(self.directory.glob("pressure_*.sqlite"), reverse=True):
				last = pressure_db.last_sample(path, self.layout.source.key)
				if last is not None:
					self._state.last = (last[1], last[0])
					return
		except Exception as e:
			log.warning("cannot read the last stored sample (%s: %s); a repeated sample may raise events twice", type(e).__name__, e)

	def _commit(self):
		"""One transaction: the leading items of _pending that belong to one day file. A sample belongs to the day of
		its own time, everything else to the day it arrived."""
		while self._pending and self._pending[0][1] == "sample" and self._repeated(self._pending[0][2]):
			self._pending.popleft()  # before a file is chosen: the repeated sample can be days old
		if not self._pending:
			return
		day = pressure_db.day_of(self._pending[0][0], self.timezone)
		count = 0
		for item in self._pending:
			if pressure_db.day_of(item[0], self.timezone) != day:
				break
			count += 1
		items = list(itertools.islice(self._pending, count))
		saved = copy.deepcopy(self._state)
		try:
			file = self._open(day)
			with file.transaction():
				self._apply(file, items)
		except BaseException:
			self._state = saved
			raise
		for _ in range(count):
			self._pending.popleft()

	def _open(self, day):
		if self._file is not None and self._day != day:
			self._close_file()
		if self._file is None:
			layout, s = self.layout, self._state
			file = pressure_db.DayFile(pressure_db.day_path(self.directory, day))
			try:
				with file.transaction():
					changed = file.set_meta(day=day, time_zone=self.timezone, host=socket.gethostname(), prefix=layout.prefix,
					                        software=f"pressure_store {VERSION}", config_sha256=layout.config_sha256)
					if changed.get("config_sha256") is not None:
						file.add_event(self._clock(), "config", f"the config file changed (sha256 {changed['config_sha256'][:12]} -> {layout.config_sha256[:12]})")
					file.add_source(layout.source.key, layout.gauge_keys)
				s.has_samples = file.last_sample(layout.source.key) is not None
			except BaseException:
				file.close()
				raise
			s.setting_id, s.summarized_to, s.describe = None, None, True
			self._file, self._day = file, day
			log.info("writing %s", file.path)
		return self._file

	def _close_file(self):
		"""Summarize the open file's last minute and close it. Never raises."""
		file, self._file, self._day = self._file, None, None
		if file is None:
			return
		try:
			with file.transaction():
				file.summarize(self.layout.source.key)
		except Exception as e:
			log.warning("%s: the last minute's summary was not written (%s: %s); the next start writes it", file.path, type(e).__name__, e)
		if not file.close():
			log.warning("%s stays in write-ahead-log mode: a reader had it open", file.path)

	def _apply(self, file, items):
		s, source, keys = self._state, self.layout.source.key, self.layout.gauge_keys
		rows = []
		for i, (when, kind, payload) in enumerate(items):
			if kind == "pv":
				what, value = payload
				if s.pv.get(what) != value:
					s.pv[what] = value
					s.describe = s.describe or what[0] in ("source", "gauge")
			elif kind == "event":
				name, text, subject = payload
				self._limited(file, when, name, text, subject)
			else:
				row = self._sample(file, payload, items, i)
				if row is not None:
					rows.append(row)
		if rows:
			new = file.add_samples(source, keys, rows)
			s.stored += new
			s.repeated += len(rows) - new  # a time the file already holds
			s.has_samples = True
			times = [row[0] for row in rows]
			oldest, newest = (math.floor(t / 60) * 60 for t in (min(times), max(times)))
			if s.summarized_to is None or newest > s.summarized_to:
				# The minutes before the newest are over. A sample older than those (a clock set back) has its minute redone.
				file.summarize(source, start=oldest if s.summarized_to is not None and oldest < s.summarized_to else None, end=newest)
				s.summarized_to = newest
			elif oldest < s.summarized_to:
				file.summarize(source, start=oldest, end=s.summarized_to)
		now = items[-1][0]
		if s.describe and s.has_samples:
			self._describe(file, now)
			s.describe = False
		self._flush_limits(file, now)

	def _repeated(self, sample):
		"""True for the IOC's last sample sent again, to a new or reconnected monitor. It is not stored twice and raises
		no event, but it tells where the controller and the gauges stand."""
		s = self._state
		if (sample.seq, sample.time) != s.last:
			return False
		s.repeated += 1
		s.lost = _lost(sample)
		if not s.lost:
			s.status.update(zip(self.layout.gauge_keys, (gauge.status for gauge in sample.gauges)))
		return True

	def _sample(self, file, sample, items, index):
		"""Events and the setting row of one sample; returns its row for the sample table, or None for a repeated one."""
		s, keys, source = self._state, self.layout.gauge_keys, self.layout.source.key
		if self._repeated(sample):
			return None
		if s.live:
			step = sample.seq - s.last[0]
			if step > 1:
				s.missed += step - 1
				self._limited(file, sample.time, "gap", f"{step - 1} readings missed: the sample counter went from {s.last[0]} to {sample.seq}",
				              source, step - 1)
			elif step < 1:
				self._limited(file, sample.time, "ioc restart", f"the sample counter went from {s.last[0]} to {sample.seq}", source)
		s.last, s.live = (sample.seq, sample.time), True
		lost = _lost(sample)
		if lost != s.lost:
			if lost:
				reason = self._error_for(items, index)
				self._limited(file, sample.time, "source lost", f"the controller does not answer: {reason}", source)
			else:
				self._limited(file, sample.time, "source recovered", "the controller answers again", source)
				s.pv[("error", source, None)] = ""
			s.lost = lost
		if not lost:  # the sample that starts an outage says nothing about the gauges
			for key, gauge in zip(keys, sample.gauges):
				old = s.status.get(key)
				if old is not None and old != gauge.status:
					self._limited(file, sample.time, "status", f"{_status(old)} -> {_status(gauge.status)}", key)
				s.status[key] = gauge.status
		setting = self._setting(file, sample, items, index)
		return (sample.time, sample.seq, setting, sample.read_s, *(v for g in sample.gauges for v in (g.p_raw, g.status, g.raw_status)))

	def _error_for(self, items, index):
		"""Why the source was lost at items[index]: a sample and <P>:<S>:ERROR arrive on separate monitors, in either order."""
		key = ("error", self.layout.source.key, None)
		reason = self._state.pv.get(key)
		if reason:
			return reason
		for _, kind, payload in items[index + 1:]:
			if kind == "pv" and payload[0] == key:
				return payload[1] or "no reason given"
			if kind == "sample" and not _lost(payload):
				break
		return "no reason given"

	def _gas_for(self, items, index, species):
		"""(selected_at, selected_by) for `species` at items[index]: GAS:* and SAMPLE arrive on separate monitors."""
		pv = {name: self._state.pv.get(("gas", None, name)) for name in ("species", "since", "by")}
		ahead = []
		for _, kind, payload in items[index + 1:]:
			if kind == "sample" and payload.gas != species:
				break
			if kind == "pv" and payload[0][0] == "gas":
				ahead.append((payload[0][2], payload[1]))
		if pv["species"] == species and any(field == "species" and value != species for field, value in ahead):
			return pv["since"], pv["by"]
		matched = pv["species"] == species
		seen = set()
		for field, value in ahead:
			if field in seen or (matched and field == "species" and value != species):
				if matched:
					break
				seen.clear()
			pv[field] = value
			seen.add(field)
			matched = pv["species"] == species
		return (pv["since"], pv["by"]) if matched else (None, None)

	def _setting(self, file, sample, items, index):
		"""The id of the sample's setting row in `file`, added if the gas or a multiplier is not that of the last row."""
		s, keys = self._state, self.layout.gauge_keys
		key = (sample.gas, tuple(gauge.gas_factor for gauge in sample.gauges))
		if key == s.setting_key and s.setting_id is not None:
			return s.setting_id
		multipliers = dict(zip(keys, key[1]))
		last = file.last_setting()
		if last is not None and (last[1], last[2]) == (sample.gas, multipliers):
			setting = last[0]  # the same setting goes on, after a restart of the store
		else:
			previous = s.setting_key or (last and (last[1], tuple(last[2].get(k) for k in keys)))
			how = "start" if not previous or previous == key else "selection" if previous[0] != sample.gas else "config"
			since, by = self._gas_for(items, index, sample.gas)
			setting = file.add_setting(sample.time, sample.gas, multipliers, how, since, by)
			if how == "selection":
				self._limited(file, sample.time, "gas", f"{previous[0]} -> {sample.gas}" + (f", selected by {by}" if by else ""))
			elif how == "config":
				changes = [f"{k} {old} -> {new}" for k, old, new in zip(keys, previous[1], key[1]) if old != new]
				self._limited(file, sample.time, "gas", f"multipliers for {sample.gas} changed: {', '.join(changes)}")
		s.setting_key, s.setting_id = key, setting
		return setting

	def _describe(self, file, when):
		"""Add gauge and source rows where the config or what the controller reports differs from the file's latest rows.
		A value the controller has not reported yet (an empty PV) is left as the last one known."""
		layout, s = self.layout, self._state
		species = layout.options.gas.species

		def reported(what, key, columns):
			return {column: s.pv.get((what, key, column)) or None for column in columns}

		source = layout.source
		described = [("source", source.key, dict(reported("source", source.key, SOURCE_PVS.values()), period_s=source.period_s))]
		for gauge in layout.options.gauges:
			described.append(("gauge", gauge.key, dict(
				reported("gauge", gauge.key, GAUGE_PVS.values()), name=gauge.name, source=gauge.source, channel=gauge.channel,
				multipliers=dict(zip(species, gauge.multiplier)), base_warning_torr=gauge.base_warning_torr, base_alarm_torr=gauge.base_alarm_torr)))
		for table, key, fields in described:
			previous = file.latest(table, key)
			if previous is not None:
				for column in (SOURCE_PVS if table == "source" else GAUGE_PVS).values():
					fields[column] = fields[column] or previous[column]
			changes = file.describe(table, key, when, **fields)
			changes = [f"{name} {old} -> {new}" for name, (old, new) in (changes or {}).items() if old is not None]
			if changes:
				self._limited(file, when, table, "; ".join(changes), key)

	def _limited(self, file, when, kind, text, subject=None, readings=0):
		"""Add an event, but list at most EVENTS_PER_MINUTE of one kind and subject per clock minute: a gauge at the end
		of its range can change status ten times a second. _flush_limits() writes one event that counts the rest."""
		minute = int(when // 60)
		entry = self._state.limits.setdefault((kind, subject), [minute, 0, 0, 0])
		if entry[0] != minute:
			self._flush_limit(file, kind, subject, entry)
			entry[:] = [minute, 0, 0, 0]
		if entry[1] < EVENTS_PER_MINUTE:
			entry[1] += 1
			file.add_event(when, kind, text, subject)
		else:
			entry[2] += 1
			entry[3] += readings

	def _flush_limit(self, file, kind, subject, entry):
		minute, _, hidden, readings = entry
		if hidden:
			text = f"{hidden} more '{kind}' events in this minute are not listed" + (f" ({readings} readings missed)" if readings else "")
			file.add_event((minute + 1) * 60.0, kind, text, subject)

	def _flush_limits(self, file, when):
		minute, limits = int(when // 60), self._state.limits
		for (kind, subject), entry in list(limits.items()):
			if entry[0] != minute:
				self._flush_limit(file, kind, subject, entry)
				del limits[(kind, subject)]


def _lost(sample):
	"""The sample that starts a controller outage: no gauge has a reading."""
	return all(gauge.status == STATUS_NO_REPLY for gauge in sample.gauges)


def _status(code):
	return f"{code} {STATUS_TEXT.get(code, 'unknown')}"


def main(argv=None):
	parser = argparse.ArgumentParser(prog="pressure_store", description="Save every reading of the pressure IOC in the day's SQLite file.")
	parser.add_argument("--config", required=True, help="the pressure IOC's TOML config file")
	parser.add_argument("--dir", required=True, type=Path, help="directory of the day files, on a local disk")
	parser.add_argument("--timezone", default=pressure_db.TIMEZONE, help=f"time zone whose days name the files (default {pressure_db.TIMEZONE})")
	args = parser.parse_args(argv)
	try:
		layout = load_layout(args.config)
		zoneinfo.ZoneInfo(args.timezone)
		args.dir.mkdir(parents=True, exist_ok=True)
	except (OSError, ValueError, zoneinfo.ZoneInfoNotFoundError) as e:  # tomllib.TOMLDecodeError is a ValueError
		print(f"{parser.prog}: {e}", file=sys.stderr)
		return 2
	logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)  # under systemd the journal keeps it
	from p4p.client.thread import Context  # imported here so the rest loads without p4p

	# A plain flag: Event.set() in a handler can deadlock with the main thread holding the Event's lock in wait().
	stopping = []
	for signum in (signal.SIGTERM, signal.SIGINT):
		signal.signal(signum, lambda signum, frame: stopping.append(signum))
	ctx = Context("pva", nt=False)  # EPICS_PVA_* from the environment; nt=False: every update as the plain structure
	store = Store(args.dir, layout, args.timezone)
	thread = threading.Thread(target=store.run, name="pressure_store writer", daemon=True)  # daemon: a hung write cannot block exit
	thread.start()
	try:
		monitors = [ctx.monitor(layout.group, store.on_group, notify_disconnect=True)]
		monitors += [ctx.monitor(name, functools.partial(store.on_pv, name)) for name in layout.pvs()]
		log.info("monitoring %s and %d single PVs, storing in %s", layout.group, len(monitors) - 1, args.dir)
		while not stopping and thread.is_alive():
			time.sleep(0.5)
		for monitor in monitors:
			monitor.close()
	finally:
		ctx.close()
		died = not thread.is_alive()  # only a bug ends the writer before finish()
		store.finish()
		thread.join(STOP_TIMEOUT_S)
		if thread.is_alive():
			log.error("the writer is still busy after %.0f s", STOP_TIMEOUT_S)
		log.info("stopped: %s", store.summary())
	return 1 if died or thread.is_alive() else 0


if __name__ == "__main__":
	sys.exit(main())
