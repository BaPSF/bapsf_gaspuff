"""The day files of the pressure monitor: the layout pressure_store.py writes, and the functions that read it.

Reading, from analysis code or the web page, while the store is writing or not:

	import pressure_db
	r = pressure_db.read("/var/lib/pressure", start, end)            # every reading: r.time, r.pressure["G1"], r.status["G1"]
	m = pressure_db.read_minutes("/var/lib/pressure", start, end)    # the 1-minute summaries
	s = pressure_db.stats("/var/lib/pressure", start, end, "G1")     # count, min, mean, max, first, last, change
	h = pressure_db.history("/var/lib/pressure", "TPG1", start, end, 2000)   # min, mean and max per time bucket, for a chart
	e = pressure_db.events("/var/lib/pressure", start, end)

`start` and `end` are epoch seconds or aware datetime objects; the range is start <= time < end and may span days.
Pressures are Torr. By default they are corrected for the gas: the gauge's own reading times the gauge's multiplier
for the gas selected at that reading. corrected=False returns the gauge's own reading. A day without a file, or a
range without readings, gives empty arrays, never an error. Readers open a file read-only, for one call.

One SQLite file per day, pressure_YYYYMMDD.sqlite, the day in Los Angeles local time. <s> is a source key (one
controller) and <g> a gauge key, both lower case:

	sample_<s>   one row per reading: time (epoch s, the key), seq (the IOC's sample counter), setting (the setting row
	             in force), read_s, and for each gauge p_raw_<g> (Torr, as the gauge sent it; NULL without a pressure),
	             status_<g> (0 ok ... 8 no reply) and raw_status_<g> (the controller's own code)
	minute_<s>   one row per clock minute and setting: time (epoch s of the minute's start), setting, count (readings),
	             and for each gauge count_<g>, min_<g>, mean_<g>, max_<g> over its readings that carry a pressure
	setting      the gas and each gauge's multiplier: a row for every change, so no reading is ever rewritten
	gauge        what is known about each gauge, a row for every change (name, channel, type, limits, multipliers)
	source       the same for each controller (driver, model, serial number, firmware, unit set on it, read period)
	event        gas changes, lost and regained connections, status changes, gaps, starts and stops
	meta         key and value: schema_version, day, time_zone, pressure_unit, host, software, prefix, config_sha256

Every file can be read alone: its first setting, gauge and source rows repeat what was in force when it began. The
controller's address is in no table. Needs numpy; imports no EPICS library.
"""
import contextlib
import datetime
import json
import math
import re
import sqlite3
import zoneinfo
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA_VERSION = 1
TIMEZONE = "America/Los_Angeles"  # names the day files; rows are keyed by epoch time, so a 23 or 25 hour day is harmless
UNIT = "Torr"
SAMPLE_RANGE_S = 2 * 3600.0  # history() reads a range up to this long from the sample rows, a longer one from the summaries
BUSY_S = 5.0  # wait this long for another connection's lock

GAUGE_FIELDS = ("name", "source", "channel", "model", "device_gas", "device_name", "multipliers", "base_warning_torr", "base_alarm_torr")
SOURCE_FIELDS = ("driver", "model", "serial_number", "firmware", "unit", "period_s")
_DESCRIBED = {"gauge": GAUGE_FIELDS, "source": SOURCE_FIELDS}

_FILE = re.compile(r"pressure_(\d{8})\.sqlite")
_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,15}")  # pressure_config's rule for source and gauge keys: safe inside a column name
_TABLES = (
	"CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID",
	# how: 'start' (in force when the file, or the store's record, began), 'selection' (another gas was selected),
	# 'config' (a multiplier changed), 'imported'. multipliers: JSON {gauge key: multiplier}.
	"CREATE TABLE IF NOT EXISTS setting (id INTEGER PRIMARY KEY, valid_from REAL NOT NULL, species TEXT NOT NULL, "
	"multipliers TEXT NOT NULL, how TEXT NOT NULL, selected_at REAL, selected_by TEXT)",
	# multipliers: JSON {species: this gauge's multiplier}.
	"CREATE TABLE IF NOT EXISTS gauge (id INTEGER PRIMARY KEY, valid_from REAL NOT NULL, key TEXT NOT NULL, name TEXT, "
	"source TEXT, channel INTEGER, model TEXT, device_gas TEXT, device_name TEXT, multipliers TEXT, base_warning_torr REAL, "
	"base_alarm_torr REAL)",
	"CREATE TABLE IF NOT EXISTS source (id INTEGER PRIMARY KEY, valid_from REAL NOT NULL, key TEXT NOT NULL, driver TEXT, "
	"model TEXT, serial_number TEXT, firmware TEXT, unit TEXT, period_s REAL)",
	"CREATE TABLE IF NOT EXISTS event (id INTEGER PRIMARY KEY, time REAL NOT NULL, kind TEXT NOT NULL, subject TEXT, text TEXT NOT NULL)",
	"CREATE INDEX IF NOT EXISTS event_time ON event (time)",
)


def day_of(time_s, timezone=TIMEZONE):
	"""YYYYMMDD of the day file that holds epoch time `time_s`."""
	return datetime.datetime.fromtimestamp(time_s, zoneinfo.ZoneInfo(timezone)).strftime("%Y%m%d")


def day_path(directory, day):
	return Path(directory) / f"pressure_{day}.sqlite"


def _check_keys(*keys):
	for key in keys:
		if type(key) is not str or not _KEY.fullmatch(key):
			raise ValueError(f"{key!r} is not a source or gauge key (a capital letter, then up to 15 capitals, digits or _)")


def _sample_columns(key):
	k = key.lower()
	return (f"p_raw_{k}", "REAL"), (f"status_{k}", "INTEGER"), (f"raw_status_{k}", "INTEGER")


def _minute_columns(key):
	k = key.lower()
	return (f"count_{k}", "INTEGER"), (f"min_{k}", "REAL"), (f"mean_{k}", "REAL"), (f"max_{k}", "REAL")


def _gauges(db, source):
	"""Gauge keys with columns in the sample table of `source`, in column order; None without the table."""
	columns = [row[1] for row in db.execute(f"PRAGMA table_info(sample_{source.lower()})")]
	return [name[6:].upper() for name in columns if name.startswith("p_raw_")] if columns else None


#===================================================================================================================================================
# Writing

class DayFile:
	"""One day file open for writing; it is created if missing. ValueError if it has another schema version.

	One writing process per file: pressure_store.py, or an importer on a day the store does not write. Every method
	that writes must run inside `with file.transaction():`, which commits at its end or rolls everything back.
	While open, the file is in write-ahead-log mode with a -wal and a -shm file beside it; close() folds them in.
	"""

	def __init__(self, path, meta=None):
		"""`meta` ({key: value}) is written when the file is created."""
		self.path = Path(path)
		self._db = sqlite3.connect(self.path, isolation_level=None, timeout=BUSY_S)  # isolation_level None: transaction() alone begins and ends
		try:
			self._db.execute("PRAGMA journal_mode=WAL")  # readers and the writer do not block each other; a crash leaves the file sound
			self._db.execute("PRAGMA synchronous=FULL")  # every commit reaches the disk: a power cut loses at most the second being written
			with self.transaction():
				for statement in _TABLES:
					self._db.execute(statement)
				version = self._db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
				if version is None:
					self.set_meta(schema_version=SCHEMA_VERSION, pressure_unit=UNIT, **(meta or {}))
				elif version[0] != str(SCHEMA_VERSION):
					raise ValueError(f"{self.path} has schema version {version[0]}; this code writes version {SCHEMA_VERSION}")
		except BaseException:
			self._db.close()
			raise

	@contextlib.contextmanager
	def transaction(self):
		self._db.execute("BEGIN IMMEDIATE")
		try:
			yield
			self._db.execute("COMMIT")
		except BaseException:
			if self._db.in_transaction:
				self._db.execute("ROLLBACK")
			raise

	def close(self):
		"""Close the file and leave it as one plain file (rollback-journal mode): any account that can read it can then
		open it, also from a read-only directory, and a copy of it is complete. Returns False if a reader had the file
		open, which prevents that: the file then stays in write-ahead-log mode, readable while its -wal and -shm exist."""
		try:
			self._db.execute("PRAGMA busy_timeout = 1000")
			return self._db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
		except sqlite3.Error:
			return False
		finally:
			self._db.close()

	def meta(self):
		return dict(self._db.execute("SELECT key, value FROM meta"))

	def set_meta(self, **values):
		"""Set meta values (stored as text). Returns {key: previous value, None if new} of the keys whose value changed."""
		before = self.meta()
		changed = {key: before.get(key) for key, value in values.items() if before.get(key) != str(value)}
		self._db.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", [(key, str(values[key])) for key in changed])
		return changed

	def add_source(self, source, gauges):
		"""Make sure the sample and minute tables of `source` exist and have columns for `gauges` (keys). A gauge new to
		the file gets its columns; the rows before it hold NULL there."""
		_check_keys(source, *gauges)
		s = source.lower()
		known = _gauges(self._db, source)
		if known is None:
			self._db.execute(f"CREATE TABLE sample_{s} (time REAL PRIMARY KEY, seq INTEGER, setting INTEGER NOT NULL REFERENCES setting (id), "
			                 f"read_s REAL) WITHOUT ROWID")
			self._db.execute(f"CREATE TABLE minute_{s} (time INTEGER NOT NULL, setting INTEGER NOT NULL REFERENCES setting (id), "
			                 f"count INTEGER NOT NULL, PRIMARY KEY (time, setting)) WITHOUT ROWID")
			known = []
		for key in gauges:
			if key not in known:
				for table, columns in ((f"sample_{s}", _sample_columns(key)), (f"minute_{s}", _minute_columns(key))):
					for name, kind in columns:
						self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

	def add_samples(self, source, gauges, rows):
		"""Insert `rows`: (time, seq, setting, read_s, then p_raw, status and raw_status for each of `gauges`). NaN and
		None are stored as NULL. A row whose time the table already holds is left as it is; returns how many were new."""
		_check_keys(source, *gauges)
		columns = ["time", "seq", "setting", "read_s"] + [name for key in gauges for name, _ in _sample_columns(key)]
		sql = f"INSERT OR IGNORE INTO sample_{source.lower()} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
		return self._db.executemany(sql, rows).rowcount

	def last_sample(self, source):
		"""(time, seq) of the latest reading of `source`, or None."""
		if _gauges(self._db, source) is None:
			return None
		return self._db.execute(f"SELECT time, seq FROM sample_{source.lower()} ORDER BY time DESC LIMIT 1").fetchone()

	def summarize(self, source, start=None, end=None):
		"""Write the 1-minute summaries of the clock minutes from `start` up to `end` (epoch s, multiples of 60) from the
		sample rows, replacing those already there. start None: from the last minute summarized, which may have been cut
		short. end None: up to the last reading."""
		s = source.lower()
		if start is None:
			start = self._db.execute(f"SELECT IFNULL(MAX(time), 0) FROM minute_{s}").fetchone()[0]
		if end is None:
			end = math.inf
		gauges = _gauges(self._db, source)
		columns = ["time", "setting", "count"] + [name for key in gauges for name, _ in _minute_columns(key)]
		values = ["CAST(time / 60 AS INTEGER) * 60", "setting", "COUNT(*)"]
		for key in gauges:
			p = _sample_columns(key)[0][0]
			values += [f"COUNT({p})", f"MIN({p})", f"AVG({p})", f"MAX({p})"]
		self._db.execute(f"DELETE FROM minute_{s} WHERE time >= ? AND time < ?", (start, end))
		self._db.execute(f"INSERT INTO minute_{s} ({', '.join(columns)}) SELECT {', '.join(values)} FROM sample_{s} "
		                 f"WHERE time >= ? AND time < ? GROUP BY 1, 2", (start, end))

	def last_setting(self):
		"""(id, species, {gauge key: multiplier}) of the latest setting row, or None."""
		row = self._db.execute("SELECT id, species, multipliers FROM setting ORDER BY id DESC LIMIT 1").fetchone()
		return None if row is None else (row[0], row[1], json.loads(row[2]))

	def add_setting(self, valid_from, species, multipliers, how, selected_at=None, selected_by=None):
		"""Add a setting row and return its id. `multipliers` is {gauge key: multiplier}."""
		return self._db.execute("INSERT INTO setting (valid_from, species, multipliers, how, selected_at, selected_by) VALUES (?, ?, ?, ?, ?, ?)",
		                        (valid_from, species, json.dumps(multipliers), how, selected_at, selected_by)).lastrowid

	def latest(self, table, key):
		"""{field: value} of the latest row of `key` in the "gauge" or "source" table, or None. multipliers is JSON text."""
		names = _DESCRIBED[table]
		row = self._db.execute(f"SELECT {', '.join(names)} FROM {table} WHERE key = ? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
		return None if row is None else dict(zip(names, row))

	def describe(self, table, key, valid_from, **fields):
		"""Record what is known about a gauge (table "gauge", GAUGE_FIELDS) or a controller ("source", SOURCE_FIELDS).
		A row is added when a field differs from the key's latest row. Returns {field: (old, new)} of the fields that
		differ (old is None for every field of a key's first row), or None when nothing changed."""
		names = _DESCRIBED[table]
		new = {name: json.dumps(v) if type(v) is dict else v for name, v in ((name, fields.get(name)) for name in names)}
		old = self.latest(table, key) or dict.fromkeys(names)
		changes = {name: (old[name], new[name]) for name in names if old[name] != new[name]}
		if not changes:
			return None
		self._db.execute(f"INSERT INTO {table} (valid_from, key, {', '.join(names)}) VALUES (?, ?, {', '.join('?' * len(names))})",
		                 (valid_from, key, *new.values()))
		return changes

	def add_event(self, time_s, kind, text, subject=None):
		self._db.execute("INSERT INTO event (time, kind, subject, text) VALUES (?, ?, ?, ?)", (time_s, kind, subject, text))


#===================================================================================================================================================
# Reading

@dataclass(frozen=True)
class Setting:
	time: float  # epoch s of the first reading (or summary) under it in the range read
	species: str
	multipliers: dict  # {gauge key: multiplier}


@dataclass(frozen=True)
class Readings:
	time: np.ndarray  # epoch s, ascending
	pressure: dict  # {gauge key: float64 Torr, NaN where the gauge had no pressure}
	status: dict  # {gauge key: int16 status code, -1 where none was stored (imported days)}
	settings: tuple  # Setting, in time order: the gas and multipliers along the range


@dataclass(frozen=True)
class Minutes:
	time: np.ndarray  # epoch s of each clock minute's start, ascending; a minute with a change of setting appears once per setting
	count: dict  # {gauge key: readings in the minute that carry a pressure}
	min: dict  # {gauge key: float64 Torr, NaN where count is 0}; mean and max alike
	mean: dict
	max: dict
	settings: tuple


@dataclass(frozen=True)
class Stats:
	count: int  # readings that carry a pressure
	min: float
	mean: float
	max: float
	first: float
	last: float
	change: float  # last - first


def _epoch(when):
	return when.timestamp() if isinstance(when, datetime.datetime) else float(when)


def _files(directory, start, end):
	"""The day files that can hold times in [start, end), oldest first: by name, a day wider on each side, so that the
	time zone that named them does not matter. The queries select by time."""
	utc = datetime.timezone.utc
	first = (datetime.datetime.fromtimestamp(start, utc) - datetime.timedelta(days=1)).strftime("%Y%m%d")
	last = (datetime.datetime.fromtimestamp(end, utc) + datetime.timedelta(days=1)).strftime("%Y%m%d")
	days = {m[1]: path for path in Path(directory).glob("pressure_*.sqlite") if (m := _FILE.fullmatch(path.name))}
	return [days[day] for day in sorted(days) if first <= day <= last]


@contextlib.contextmanager
def _reader(path):
	"""A read-only connection inside one read transaction: every query of the block sees the same state of the file."""
	db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None, timeout=BUSY_S)
	try:
		db.execute("BEGIN")
		yield db
	finally:
		db.close()


def _source(db, source):
	"""The source key to read in this file, or None if the file has no readings of it. `source` None: the file's only one."""
	names = [name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name GLOB 'sample_*'")]
	sources = [name[7:].upper() for name in names]
	if source is None:
		if len(sources) > 1:
			raise ValueError(f"the file holds the sources {', '.join(sources)}: pass source=")
		return sources[0] if sources else None
	return source if source in sources else None


def _settings(db):
	"""{setting id: (valid_from, species, {gauge key: multiplier})}."""
	return {row[0]: (row[1], row[2], json.loads(row[3])) for row in db.execute("SELECT id, valid_from, species, multipliers FROM setting")}


def _rows(directory, start, end, source, kind, columns, group=""):
	"""For each day file with rows of table `kind` ("sample", "minute") in the range, oldest first: (gauge keys of the
	file, _settings(), float array of the rows). A row is the two leading `columns`, then columns(key) for each gauge."""
	if source is not None:
		_check_keys(source)
	for path in _files(directory, start, end):
		with _reader(path) as db:
			file_source = _source(db, source)
			if file_source is None:
				continue
			gauges = _gauges(db, file_source)
			select = list(columns(None)) + [c for key in gauges for c in columns(key)]
			rows = db.execute(f"SELECT {', '.join(select)} FROM {kind}_{file_source.lower()} WHERE time >= ? AND time < ? {group} ORDER BY 1, 2",
			                  (start, end)).fetchall()
			if rows:
				yield gauges, _settings(db), np.array(rows, dtype=float).reshape(len(rows), len(select))  # NULL becomes NaN


def _factors(settings, ids, key, corrected):
	"""The multiplier of gauge `key` for each row, from the rows' setting ids; 1 if not corrected."""
	if not corrected:
		return 1.0
	table = np.full(max(settings) + 1, np.nan)
	for setting, (_, _, multipliers) in settings.items():
		table[setting] = multipliers.get(key, np.nan)
	return table[ids]


def _along(parts, wanted, fields, fills):
	"""Join per-file pieces. parts: [(time, {gauge key: [array per field]}, [Setting])]. Returns time, one {gauge key:
	array} per field for the `wanted` gauges (None: every gauge met, in order of appearance), and the settings."""
	keys = list(wanted) if wanted is not None else list(dict.fromkeys(key for _, gauges, _ in parts for key in gauges))
	time_s = np.concatenate([t for t, _, _ in parts]) if parts else np.empty(0)
	out = []
	for i, (dtype, fill) in enumerate(zip(fields, fills)):
		out.append({key: np.concatenate([gauges[key][i] if key in gauges else np.full(len(t), fill) for t, gauges, _ in parts]).astype(dtype)
		            if parts else np.empty(0, dtype) for key in keys})
	settings = []
	for _, _, file_settings in parts:
		for setting in file_settings:
			if not settings or (settings[-1].species, settings[-1].multipliers) != (setting.species, setting.multipliers):
				settings.append(setting)
	return time_s, out, tuple(settings)


def _changes(settings, time_s, ids):
	"""[Setting] at the first row and at every row where the setting id changes."""
	starts = np.concatenate(([0], np.flatnonzero(np.diff(ids)) + 1))
	return [Setting(float(time_s[i]), settings[ids[i]][1], settings[ids[i]][2]) for i in starts]


def read(directory, start, end, gauges=None, corrected=True, source=None):
	"""Every reading with start <= time < end, as a Readings. `gauges`: the keys wanted (default: every gauge the files
	hold). `source`: the controller's key, needed only if the files hold several."""
	start, end = _epoch(start), _epoch(end)

	def columns(key):
		if key is None:
			return "time", "setting"
		p, status, _ = (name for name, _ in _sample_columns(key))
		return p, f"IFNULL({status}, -1)"

	parts = []
	for keys, settings, a in _rows(directory, start, end, source, "sample", columns):
		ids = a[:, 1].astype(int)
		values = {key: [a[:, 2 + 2 * i] * _factors(settings, ids, key, corrected), a[:, 3 + 2 * i]] for i, key in enumerate(keys)}
		parts.append((a[:, 0], values, _changes(settings, a[:, 0], ids)))
	time_s, (pressure, status), settings = _along(parts, gauges, (np.float64, np.int16), (np.nan, -1))
	return Readings(time_s, pressure, status, settings)


def read_minutes(directory, start, end, gauges=None, corrected=True, source=None):
	"""The 1-minute summaries of the clock minutes that start in [start, end), as a Minutes. A minute still being
	filled has no summary yet: the store writes it when the minute is over."""
	start, end = _epoch(start), _epoch(end)

	def columns(key):
		return ("time", "setting") if key is None else [name for name, _ in _minute_columns(key)]

	parts = []
	for keys, settings, a in _rows(directory, start, end, source, "minute", columns):
		ids = a[:, 1].astype(int)
		values = {}
		for i, key in enumerate(keys):
			factor = _factors(settings, ids, key, corrected)
			count, low, mean, high = (a[:, 2 + 4 * i + j] for j in range(4))
			values[key] = [np.nan_to_num(count), low * factor, mean * factor, high * factor]
		parts.append((a[:, 0], values, _changes(settings, a[:, 0], ids)))
	time_s, (count, low, mean, high), settings = _along(parts, gauges, (np.int64, np.float64, np.float64, np.float64), (0, np.nan, np.nan, np.nan))
	return Minutes(time_s, count, low, mean, high, settings)


def stats(directory, start, end, gauge, corrected=True, source=None):
	"""Stats of one gauge over every reading in [start, end) that carries a pressure; NaN values when there is none.
	The files do the arithmetic, so no reading is loaded."""
	start, end = _epoch(start), _epoch(end)
	_check_keys(gauge)
	p = _sample_columns(gauge)[0][0]
	count, total, low, high, first, last = 0, 0.0, math.inf, -math.inf, math.nan, math.nan
	for path in _files(directory, start, end):
		with _reader(path) as db:
			file_source = _source(db, source)
			if file_source is None or gauge not in _gauges(db, file_source):
				continue
			factors = {setting: multipliers.get(gauge, math.nan) if corrected else 1.0 for setting, (_, _, multipliers) in _settings(db).items()}
			rows = f"FROM sample_{file_source.lower()} WHERE time >= ? AND time < ? AND {p} IS NOT NULL"
			for setting, n, lowest, sum_, highest in db.execute(f"SELECT setting, COUNT(*), MIN({p}), SUM({p}), MAX({p}) {rows} GROUP BY setting", (start, end)):
				count, total = count + n, total + sum_ * factors[setting]
				low, high = min(low, lowest * factors[setting]), max(high, highest * factors[setting])
			ends = [db.execute(f"SELECT {p}, setting {rows} ORDER BY time {order} LIMIT 1", (start, end)).fetchone() for order in ("ASC", "DESC")]
			if ends[0] is not None:
				if math.isnan(first):
					first = ends[0][0] * factors[ends[0][1]]
				last = ends[1][0] * factors[ends[1][1]]
	if not count:
		return Stats(0, *(math.nan,) * 6)
	return Stats(count, low, total / count, high, first, last, last - first)


def history(directory, source, start, end, buckets, corrected=True):
	"""Min, mean and max per time bucket, for a chart: [start, end) cut into `buckets` equal buckets.

	Returns {"t": epoch s of the start of each bucket that holds readings, ascending,
	         "gauges": {gauge key: {"mean": array, "min": array, "max": array}}, as long as "t", NaN where the gauge has
	                   no pressure in the bucket,
	         "gas": [[epoch s, species], ...]: the selection in force at `start` (if the files tell), then each change}.
	A bucket without any reading is left out, so a step in "t" longer than a bucket is a time without readings. A range
	up to SAMPLE_RANGE_S is read from the sample rows; a longer one from the 1-minute summaries, so its buckets are never
	finer than a minute and it lacks the minute still being filled. `source` None: the files' only source.
	"""
	start, end, buckets = _epoch(start), _epoch(end), int(buckets)
	result = {"t": np.empty(0), "gauges": {}, "gas": []}
	if not (math.isfinite(start) and math.isfinite(end) and end > start and buckets >= 1):
		return result
	width = (end - start) / buckets
	bucket = f"CAST((time - {start!r}) / {width!r} AS INTEGER)"
	if end - start <= SAMPLE_RANGE_S:
		def columns(key):
			if key is None:
				return bucket, "setting", "COUNT(*)"
			p = _sample_columns(key)[0][0]
			return f"COUNT({p})", f"MIN({p})", f"AVG({p})", f"MAX({p})"
		kind = "sample"
	else:
		def columns(key):
			if key is None:
				return bucket, "setting", "SUM(count)"
			n, low, mean, high = (name for name, _ in _minute_columns(key))
			return f"SUM({n})", f"MIN({low})", f"SUM({mean} * {n}) / SUM({n})", f"MAX({high})"  # SUM skips the NULL means; their counts are 0
		kind = "minute"

	rows = np.zeros(buckets)  # readings per bucket, with or without a pressure
	gauges = {}  # {gauge key: [count, sum, min, max] per bucket}
	for keys, settings, a in _rows(directory, start, end, source, kind, columns, group="GROUP BY 1, 2"):
		b = np.clip(a[:, 0].astype(int), 0, buckets - 1)
		ids = a[:, 1].astype(int)
		np.add.at(rows, b, a[:, 2])
		for i, key in enumerate(keys):
			total = gauges.setdefault(key, [np.zeros(buckets), np.zeros(buckets), np.full(buckets, np.inf), np.full(buckets, -np.inf)])
			factor = _factors(settings, ids, key, corrected)
			count, low, mean, high = (a[:, 3 + 4 * i + j] for j in range(4))
			count = np.nan_to_num(count)
			np.add.at(total[0], b, count)
			np.add.at(total[1], b, np.nan_to_num(mean * factor) * count)
			np.fmin.at(total[2], b, low * factor)  # fmin and fmax skip NaN
			np.fmax.at(total[3], b, high * factor)
	held = rows > 0
	result["t"] = start + np.flatnonzero(held) * width
	for key, (count, total, low, high) in gauges.items():
		some = count[held] > 0
		with np.errstate(invalid="ignore", divide="ignore"):
			result["gauges"][key] = {"mean": np.where(some, total[held] / count[held], np.nan), "min": np.where(some, low[held], np.nan),
			                         "max": np.where(some, high[held], np.nan)}
	result["gas"] = _gas(directory, start, end)
	return result


def _gas(directory, start, end):
	"""[[epoch s, species], ...]: the selection in force at `start`, then each change before `end`."""
	selections = []
	for path in _files(directory, start, end):
		with _reader(path) as db:
			selections += db.execute("SELECT valid_from, species FROM setting WHERE valid_from < ? ORDER BY valid_from, id", (end,)).fetchall()
	gas = []
	for valid_from, species in selections:
		if valid_from <= start:
			gas = [[start, species]]
		elif not gas or gas[-1][1] != species:
			gas.append([valid_from, species])
	return gas


def last_sample(path, source):
	"""(time, seq) of the latest reading of `source` in the day file `path`, or None."""
	_check_keys(source)
	with _reader(Path(path)) as db:
		if _gauges(db, source) is None:
			return None
		return db.execute(f"SELECT time, seq FROM sample_{source.lower()} ORDER BY time DESC LIMIT 1").fetchone()


def events(directory, start, end):
	"""[(time, kind, subject, text), ...] of the events with start <= time < end, in time order."""
	start, end = _epoch(start), _epoch(end)
	found = []
	for path in _files(directory, start, end):
		with _reader(path) as db:
			found += db.execute("SELECT time, kind, subject, text FROM event WHERE time >= ? AND time < ? ORDER BY time, id", (start, end)).fetchall()
	return found
