"""Tests of the import of the old logger's HDF5 files. They need numpy and h5py and are skipped without h5py:

	python -m unittest discover -s pfeiffer/tests

The old files are made here with the layout of Pfeiffer_control.py's init_hdf5_file() and save_pressure_reading().
"""
import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
	import h5py
except ImportError:
	raise unittest.SkipTest("needs h5py") from None

import pressure_db  # noqa: E402
import pressure_import  # noqa: E402

T0 = 1794470400.0  # 2026-11-12 00:00 in Los Angeles
LATER = T0 + 3 * 86400  # "now" for the import: both days have ended

CONFIG = """
[[module]]
name = "pressure"
factory = "pressure_ioc:PressureModule"
prefix = "LAPD:MACH:PRESSURE"

[module.options.gas]
prefix = "LAPD:MACH:GAS"
species = ["Helium", "Nitrogen"]
default = "Helium"
selection_file = "/var/lib/diag-ioc/pressure/gas_species"

[[module.options.source]]
key = "TPG1"
driver = "tpg366"
address = "127.0.0.1:8000"
period_s = 0.1
""" + "".join(f"""
[[module.options.gauge]]
key = "{key}"
source = "TPG1"
channel = {channel}
name = "Port {channel}"
multiplier = {{ Helium = 5.9, Nitrogen = 1.0 }}
""" for key, channel in (("G1", 1), ("G2", 2), ("G4", 4)))


def write_old(path, time_s, channels, time_dtype=np.float64):
	"""An old logger file: channels is {channel: (pressures, [models], [gas codes], [modified times])}."""
	with h5py.File(path, "w", libver="latest") as f:
		group = f.require_group("PfeifferVacuum")
		group.attrs["unit"] = "Torr"
		for channel, (pressures, models, gases, since) in channels.items():
			dataset = group.create_dataset(str(channel), data=np.array(pressures, dtype="f"), maxshape=(None,))
			dataset.attrs["Model"] = models
			dataset.attrs["Unit"] = "Torr"
			dataset.attrs["Gas"] = gases
			dataset.attrs["Modified time"] = since
		group.create_dataset("timestamp", data=np.array(time_s, dtype=time_dtype), maxshape=(None,))


class ImportTests(unittest.TestCase):
	def setUp(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.tmp = Path(tmp.name)
		self.store = self.tmp / "store"
		self.store.mkdir()
		config = self.tmp / "pressure.toml"
		config.write_text(CONFIG, encoding="utf-8")
		self.config = config
		self.layout = pressure_import.load_layout(config)

	def test_readings_go_into_the_day_file_of_their_time(self):
		time_s = T0 - 1.5 + 0.1 * np.arange(30)  # across midnight in Los Angeles
		g1 = [float(f"{1.2345 + i / 1000:.4f}e-06") for i in range(30)]  # five digits, as the controller sends them
		path = self.tmp / "pressure_data_2026-11-11.hdf5"
		write_old(path, time_s, {
			1: (g1, ["PKR"], [0], [T0 - 3600]),
			2: ([7.5e-3] * 30, ["PKR", "IKR"], [0, 3], [T0 - 3600, T0 + 0.5]),  # the gauge changed after midnight
			3: ([1e-3] * 30, ["PKR"], [0], [T0 - 3600]),  # not in the config
			4: ([2e-2] * 30, ["noSENSOR"], [0], [T0 - 3600]),  # in the config, no gauge on it
		})
		report = pressure_import.import_file(path, self.layout, self.store, now=LATER)
		self.assertIsNone(report.skip)
		self.assertEqual((report.imported, report.gauges), ({"20261111": 15, "20261112": 15}, {1: "G1", 2: "G2"}))
		self.assertEqual(sorted(report.left_out), [3, 4])

		r = pressure_db.read(self.store, T0 - 10, T0 + 10)
		np.testing.assert_array_equal(r.time, time_s)
		self.assertEqual(r.pressure["G1"].tolist(), g1)  # the controller's numbers, not float32's
		np.testing.assert_array_equal(r.pressure["G2"], 7.5e-3)
		np.testing.assert_array_equal(r.status["G1"], -1)
		self.assertEqual({(s.species, tuple(s.multipliers.values())) for s in r.settings}, {("not recorded", (1.0, 1.0))})
		np.testing.assert_array_equal(pressure_db.read(self.store, T0 - 10, T0 + 10, corrected=False).pressure["G1"], r.pressure["G1"])
		self.assertEqual(pressure_db.read_minutes(self.store, T0 - 60, T0 + 60).count["G1"].tolist(), [15, 15])
		self.assertEqual([e[1:3] for e in pressure_db.events(self.store, T0 - 10, T0 + 10)], [("import", path.name)] * 2)

		with contextlib.closing(sqlite3.connect(pressure_db.day_path(self.store, "20261112"))) as db:
			self.assertEqual(db.execute("SELECT valid_from, model, device_gas, name, channel FROM gauge WHERE key = 'G2' ORDER BY id").fetchall(),
			                 [(T0, "PKR", "Nitrogen", "Port 2", 2), (T0 + 0.5, "IKR", "Helium", "Port 2", 2)])
			self.assertEqual(db.execute("SELECT seq, read_s, status_g1 FROM sample_tpg1 LIMIT 1").fetchone(), (None, None, None))
			self.assertEqual(db.execute("PRAGMA journal_mode").fetchone(), ("delete",))  # one plain file

		again = pressure_import.import_file(path, self.layout, self.store, now=LATER)
		self.assertEqual((again.imported, again.already), ({}, ["20261111", "20261112"]))
		self.assertEqual(len(pressure_db.events(self.store, T0 - 10, T0 + 10)), 2)

	def test_a_day_that_has_not_ended_is_not_written(self):
		path = self.tmp / "pressure_data_2026-11-11.hdf5"
		write_old(path, T0 - 1 + np.arange(2.0), {1: ([1e-6] * 2, ["PKR"], [0], [T0 - 3600])})
		report = pressure_import.import_file(path, self.layout, self.store, now=T0 + 60)
		self.assertEqual((report.imported, report.later), ({"20261111": 1}, ["20261112"]))

	def test_datasets_of_different_lengths(self):
		path = self.tmp / "pressure_data_2026-11-11.hdf5"
		# The logger's gauge-change bug: channel 1 got two more values, the timestamp none.
		write_old(path, T0 - 10 + np.arange(5.0), {1: ([1e-6] * 7, ["PKR"], [0], [T0 - 3600]), 2: ([1e-3] * 5, ["PKR"], [0], [T0 - 3600])})
		self.assertIn("differ in length (timestamp 5, channel 1 7, channel 2 5)", pressure_import.import_file(path, self.layout, self.store, now=LATER).skip)
		report = pressure_import.import_file(path, self.layout, self.store, now=LATER, trim=True)
		self.assertEqual((report.imported, report.notes), ({"20261111": 5}, ["trimmed to the timestamp dataset: channel 1 2 readings"]))
		write_old(path, T0 - 10 + np.arange(5.0), {1: ([1e-6] * 4, ["PKR"], [0], [T0 - 3600])})  # shorter: never imported
		self.assertIsNotNone(pressure_import.import_file(path, self.layout, self.store, now=LATER, trim=True).skip)

	def test_command(self):
		old = self.tmp / "old"
		old.mkdir()
		write_old(old / "pressure_data_2025-06-01.hdf5", [1748761200.0, 1748761201.0], {1: ([1e-6] * 2, ["PKR"], [0], [1748761200.0])})
		write_old(old / "pressure_data_2025-06-02.hdf5", [1748847600.0], {1: ([1e-6], ["PKR"], [0], [1748847600.0])}, time_dtype="f")
		args = ["--config", str(self.config), "--dir", str(self.store), str(old)]
		with contextlib.redirect_stdout(io.StringIO()) as out:
			self.assertEqual(pressure_import.main(["--check", *args]), 1)
		self.assertIn("20250601: 2 readings", out.getvalue())
		self.assertIn("pressure_data_2025-06-02.hdf5: NOT IMPORTED: the timestamp dataset is float32", out.getvalue())
		self.assertEqual(list(self.store.iterdir()), [])
		with contextlib.redirect_stdout(io.StringIO()):
			self.assertEqual(pressure_import.main(args), 1)
		self.assertEqual([p.name for p in self.store.iterdir()], ["pressure_20250601.sqlite"])


if __name__ == "__main__":
	unittest.main()
