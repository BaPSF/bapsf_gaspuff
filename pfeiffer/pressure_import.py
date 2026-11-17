"""pressure_import --config FILE --dir DIR OLD...: copy the old logger's HDF5 files into the store's day files.

	python pressure_import.py --config /etc/diag-ioc/pressure.toml --dir /var/lib/pressure --check /data/gauge
	python pressure_import.py --config /etc/diag-ioc/pressure.toml --dir /var/lib/pressure /data/gauge

OLD is a pressure_data_YYYY-MM-DD.hdf5 file written by Pfeiffer_control.py, or a directory of them. FILE is the
pressure IOC's own config: the source key, and which gauge key sits on which channel, come from it, as for the store.
--check reads every file and says what would be imported, without writing.

Each reading goes into the day file of its own time (pressure_db.py describes the layout) as the gauge sent it, so all
pressure data can be read and backed up in one place. The old files hold less than the store:

- No status: the old logger never saved it. status_<g> is NULL (read() returns -1), and a reading taken while a
  gauge was off or in error holds whatever number the controller sent.
- No gas selection: the setting row says species "not recorded", how "imported", multiplier 1 for every gauge, so the
  "corrected" pressure of an imported day is the gauge's own reading. The controller's own gas setting for the channel
  (the dataset attribute Gas) goes into the gauge row as device_gas.
- No unit check: the old logger wrote "Torr" without asking the controller which unit it was set to.
- No sample counter, read time, controller identity or gauge names stored in the controller.

A channel is imported under the gauge key the config gives it. A channel the config does not list, or whose gauge
the file names noSENSOR or noIDENT throughout, is left out and named in the report. The old files are only read.

An import adds an "import" event, with the old file's name as subject, to each day file it writes. A day file that
already has that event is left as it is, so running the import twice imports nothing twice. A day that has not ended
(today, in Los Angeles) is not written, because the store may be writing it.

Needs numpy and h5py; imports no EPICS library.
"""
import argparse
import datetime
import hashlib
import socket
import sys
import tomllib
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import pressure_db
import tpg366
from pressure_config import parse_options

VERSION = "0.1.0"
FACTORY = "pressure_ioc:PressureModule"
SPECIES = "not recorded"  # the setting row of imported readings
GROUP = "PfeifferVacuum"
NO_GAUGE = ("noSENSOR", "noIDENT")  # TID answers for a channel without a usable gauge
OLD_FILES = "pressure_data_*.hdf5"


@dataclass(frozen=True)
class Layout:
	"""What the import takes from the IOC's config file."""
	prefix: str
	source: str  # the source key
	gauges: dict  # {channel: pressure_config.Gauge} of the source's gauges


def load_layout(path):
	"""Layout from the IOC's TOML config file. OSError if it cannot be read, ValueError if it is not a pressure IOC's."""
	modules = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("module", [])
	modules = [m for m in modules if type(m) is dict and m.get("factory") == FACTORY] if type(modules) is list else []
	if len(modules) != 1:
		raise ValueError(f"{path}: expected one [[module]] with factory = {FACTORY!r}, found {len(modules)}")
	(module,) = modules
	options = parse_options(module.get("options"), {tpg366.DRIVER: tpg366.CHANNELS})
	(source,) = options.sources
	if source.driver != tpg366.DRIVER:
		raise ValueError(f"{path}: the old files hold TPG 366 readings, but source {source.key} uses the driver {source.driver}")
	return Layout(str(module.get("prefix")), source.key, {g.channel: g for g in options.gauges if g.source == source.key})


#===================================================================================================================================================
# Reading an old file

@dataclass
class OldFile:
	"""The readings of one old file, by channel; `skip` says why none can be imported."""
	path: Path
	time: np.ndarray = None  # epoch s, float64
	pressure: dict = field(default_factory=dict)  # {channel: float64 Torr}
	history: dict = field(default_factory=dict)  # {channel: [(epoch s, model, gas name), ...]} from the dataset attributes
	trimmed: dict = field(default_factory=dict)  # {channel: readings left out beyond the last time}
	dropped: int = 0  # readings without a usable time
	skip: str | None = None


def _texts(value):
	"""An attribute that the old logger wrote as a list, as a list of str (h5py may return bytes or one scalar)."""
	values = np.atleast_1d(value).tolist()
	return [v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v) for v in values]


def _numbers(value):
	return [float(v) for v in np.atleast_1d(value).tolist()]


def _open(path):
	import h5py  # here, so that --help and the tests that need no HDF5 file run without it
	try:
		return h5py.File(path, "r")
	except OSError:
		# A file the logger had open when it stopped still says it is being written; SWMR read mode opens it anyway.
		return h5py.File(path, "r", libver="latest", swmr=True)


def read_old(path, trim=False):
	"""OldFile from one HDF5 file of Pfeiffer_control.py. Every gauge dataset must hold one value per time, unless
	`trim`: then a dataset longer than the timestamp dataset is cut to its length (see main's --trim)."""
	old = OldFile(Path(path))
	try:
		f = _open(path)
	except OSError as e:
		old.skip = f"cannot be opened ({e}); if it is still marked as being written, run 'h5clear -s' on a copy and import the copy"
		return old
	with f:
		group = f.get(GROUP)
		if group is None or "timestamp" not in group:
			old.skip = f"no {GROUP}/timestamp dataset: not a file of the old logger"
			return old
		stamps = group["timestamp"]
		if stamps.dtype != np.float64:
			old.skip = f"the timestamp dataset is {stamps.dtype}, too coarse for epoch seconds (the logger made it so when it found it missing)"
			return old
		time_s = stamps[()]
		channels = sorted(int(name) for name in group if name.isdigit())
		for channel in channels:
			dataset = group[str(channel)]
			for name in (dataset.attrs.get("Unit"), group.attrs.get("unit")):
				if name is not None and _texts(name)[0] != "Torr":
					old.skip = f"channel {channel} is in {_texts(name)[0]}, not Torr"
					return old
			if len(dataset) < len(time_s) or (len(dataset) > len(time_s) and not trim):
				lengths = ", ".join(f"channel {c} {len(group[str(c)])}" for c in channels)
				old.skip = (f"the datasets differ in length (timestamp {len(time_s)}, {lengths}): a write failed part way, so the "
				            "readings after it may not line up with their times")
				return old
			if len(dataset) > len(time_s):
				old.trimmed[channel] = len(dataset) - len(time_s)
			# float32 in the file: its shortest decimal is the number the controller sent (it sends 5 digits)
			old.pressure[channel] = dataset[:len(time_s)].astype(str).astype(np.float64)
			models, gases, since = (dataset.attrs.get(name, []) for name in ("Model", "Gas", "Modified time"))
			gases = [tpg366.GAS_NAMES.get(int(code), f"code {int(code)}") for code in _numbers(gases)]
			old.history[channel] = list(zip(_numbers(since), _texts(models), gases))
	usable = np.isfinite(time_s) & (time_s > 0)
	old.dropped = int(np.count_nonzero(~usable))
	old.time = time_s[usable]
	old.pressure = {channel: p[usable] for channel, p in old.pressure.items()}
	return old


#===================================================================================================================================================
# Writing the day files

@dataclass
class Report:
	path: Path
	imported: dict = field(default_factory=dict)  # {day: readings new to the day file}
	already: list = field(default_factory=list)  # days that hold this file's import event
	later: list = field(default_factory=list)  # days not ended yet, not written
	gauges: dict = field(default_factory=dict)  # {channel: gauge key} imported
	left_out: dict = field(default_factory=dict)  # {channel: why}
	notes: list = field(default_factory=list)
	skip: str | None = None

	def lines(self):
		name = self.path.name
		if self.skip:
			return [f"{name}: NOT IMPORTED: {self.skip}"]
		out = [f"{name}: " + ", ".join(f"channel {c} -> {k}" for c, k in self.gauges.items())]
		out += [f"  {day}: {n} readings" for day, n in self.imported.items()]
		out += [f"  {day}: imported before, left as it is" for day in self.already]
		out += [f"  {day}: not written, the day has not ended" for day in self.later]
		out += [f"  channel {c} left out: {why}" for c, why in self.left_out.items()]
		out += [f"  {note}" for note in self.notes]
		return out


def _days(start, end, timezone):
	"""(YYYYMMDD, first epoch s, end epoch s) of each local day from the one holding `start` to the one holding `end`."""
	zone = zoneinfo.ZoneInfo(timezone)
	day = datetime.datetime.fromtimestamp(start, zone).date()
	while True:
		first, after = (datetime.datetime.combine(d, datetime.time(), zone).timestamp() for d in (day, day + datetime.timedelta(days=1)))
		yield day.strftime("%Y%m%d"), first, after
		if after > end:
			return
		day += datetime.timedelta(days=1)


def _in_force(history, start, end):
	"""The (time, model, gas) entries for the readings of one day: the one in force at `start` (the first one if all are
	later), then each change up to `end`."""
	entries = sorted(history)
	if not entries:
		return [(start, None, None)]
	before = [entry for entry in entries if entry[0] <= start] or entries[:1]
	return [(start, *before[-1][1:])] + [entry for entry in entries if start < entry[0] < end]


def import_file(path, layout, directory, timezone=pressure_db.TIMEZONE, write=True, trim=False, now=None):
	"""Import one old file into the day files in `directory`. Returns a Report; nothing is written if `write` is False."""
	report = Report(Path(path))
	old = read_old(path, trim)
	if old.skip:
		report.skip = old.skip
		return report
	for channel in old.pressure:
		gauge = layout.gauges.get(channel)
		models = {model for _, model, _ in old.history[channel]}
		if gauge is None:
			report.left_out[channel] = f"the config lists no gauge of {layout.source} on it"
		elif models and models <= set(NO_GAUGE):
			report.left_out[channel] = f"the file names no gauge on it ({', '.join(sorted(models))})"
		else:
			report.gauges[channel] = gauge.key
	if old.dropped:
		report.notes.append(f"{old.dropped} readings without a usable time left out")
	if old.trimmed:
		report.notes.append("trimmed to the timestamp dataset: " + ", ".join(f"channel {c} {n} readings" for c, n in old.trimmed.items()))
	if not report.gauges or not len(old.time):
		report.skip = "no readings of a configured gauge"
		return report

	channels, keys = list(report.gauges), list(report.gauges.values())
	sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
	today = pressure_db.day_of(datetime.datetime.now().timestamp() if now is None else now, timezone)
	for day, first, after in _days(float(old.time.min()), float(old.time.max()), timezone):
		rows = (old.time >= first) & (old.time < after)
		if not rows.any():
			continue
		if day >= today:
			report.later.append(day)
			continue
		time_s = old.time[rows]
		start, end = float(time_s.min()), float(time_s.max())
		if any(kind == "import" and subject == report.path.name for _, kind, subject, _ in pressure_db.events(directory, first, after)):
			report.already.append(day)
			continue
		if not write:
			report.imported[day] = len(time_s)
			continue
		meta = {"day": day, "time_zone": timezone, "host": socket.gethostname(), "prefix": layout.prefix, "software": f"pressure_import {VERSION}"}
		file = pressure_db.DayFile(pressure_db.day_path(directory, day), meta)
		try:
			with file.transaction():
				file.add_source(layout.source, keys)
				setting = file.add_setting(start, SPECIES, {key: 1.0 for key in keys}, "imported")
				pressures = [old.pressure[c][rows].tolist() for c in channels]
				table = [(t, None, setting, None, *(v for p in values for v in (p, None, None))) for t, *values in zip(time_s.tolist(), *pressures)]
				new = file.add_samples(layout.source, keys, table)
				file.describe("source", layout.source, start, driver="Pfeiffer_control.py")
				for channel, key in report.gauges.items():
					gauge = layout.gauges[channel]
					for valid_from, model, gas in _in_force(old.history[channel], start, end):
						file.describe("gauge", key, valid_from, name=gauge.name, source=layout.source, channel=channel, model=model, device_gas=gas)
				text = (f"{new} readings of {', '.join(keys)} imported from {report.path.name} (sha256 {sha256[:12]}), written by the old logger "
				        f"without status, gas selection or unit check")
				if len(table) > new:
					text += f"; {len(table) - new} readings at a time already stored left out"
				file.add_event(start, "import", "; ".join([text, *report.notes]), report.path.name)
				file.summarize(layout.source, start=int(start // 60) * 60, end=int(end // 60) * 60 + 60)
		finally:
			file.close()
		report.imported[day] = new
	return report


def old_files(paths):
	"""The old files named by `paths`: each file as given, each directory's pressure_data_*.hdf5 files by name."""
	found = []
	for path in map(Path, paths):
		found += sorted(path.glob(OLD_FILES)) if path.is_dir() else [path]
	return found


def main(argv=None):
	parser = argparse.ArgumentParser(prog="pressure_import", description="Copy the old logger's HDF5 files into the pressure store's day files.")
	parser.add_argument("--config", required=True, help="the pressure IOC's TOML config file")
	parser.add_argument("--dir", required=True, type=Path, help="directory of the day files (the store's --dir)")
	parser.add_argument("--timezone", default=pressure_db.TIMEZONE, help=f"time zone whose days name the files (default {pressure_db.TIMEZONE})")
	parser.add_argument("--check", action="store_true", help="read the files and report, write nothing")
	parser.add_argument("--trim", action="store_true", help="import a file whose gauge datasets are longer than its timestamp dataset, up to the "
	                    "timestamp's length. The old logger leaves such files when a gauge or gas changed; a failed write anywhere else "
	                    "would shift the readings after it, which cannot be told from the file")
	parser.add_argument("old", nargs="+", help="old HDF5 files, or directories of them")
	args = parser.parse_args(argv)
	try:
		layout = load_layout(args.config)
		zoneinfo.ZoneInfo(args.timezone)
		if not args.check:
			args.dir.mkdir(parents=True, exist_ok=True)
	except (OSError, ValueError, zoneinfo.ZoneInfoNotFoundError) as e:
		print(f"{parser.prog}: {e}", file=sys.stderr)
		return 2
	files = old_files(args.old)
	skipped = 0
	for path in files:
		try:
			report = import_file(path, layout, args.dir, args.timezone, write=not args.check, trim=args.trim)
		except Exception as e:  # a broken file or a full disk: the day being written was rolled back
			report = Report(Path(path), skip=f"{type(e).__name__}: {e}")
		skipped += report.skip is not None
		print("\n".join(report.lines()), flush=True)
	print(f"{len(files)} files, {skipped} not imported" + (" (--check: nothing written)" if args.check else ""))
	return 1 if skipped else 0


if __name__ == "__main__":
	sys.exit(main())
