"""Tests of the pressure day files: the layout and the functions that read it. They need numpy only:

	python -m unittest discover -s pfeiffer/tests
"""
import datetime
import math
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pressure_db  # noqa: E402
from pressure_db import DayFile, day_path  # noqa: E402

T0 = 1794470400.0  # 2026-11-12 00:00 in Los Angeles
DAY, NEXT_DAY = "20261112", "20261113"
NAN = math.nan


class DayFileTests(unittest.TestCase):
	def setUp(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.dir = Path(tmp.name)

	def day(self, day=DAY, gauges=("G1", "G2")):
		"""An open day file with the source TPG1; closed at the end of the test."""
		file = DayFile(day_path(self.dir, day), meta={"day": day})
		self.addCleanup(file.close)
		with file.transaction():
			file.add_source("TPG1", gauges)
		return file

	def fill(self, file, start, count, setting, pressures=(2e-6, 1e-3), step=0.1, seq=1):
		"""`count` readings from `start`, `step` apart, every gauge ok."""
		rows = [(start + i * step, seq + i, setting, 0.01, *(v for p in pressures for v in (p, 0, 0))) for i in range(count)]
		with file.transaction():
			return file.add_samples("TPG1", ("G1", "G2")[:len(pressures)], rows)

	def test_day_names_follow_los_angeles(self):
		self.assertEqual((pressure_db.day_of(T0 - 0.001), pressure_db.day_of(T0)), ("20261111", DAY))
		self.assertEqual(pressure_db.day_of(T0, "UTC"), DAY)  # 08:00 UTC
		self.assertEqual(day_path("/var/lib/pressure", DAY), Path("/var/lib/pressure/pressure_20261112.sqlite"))

	def test_readings_come_back_corrected_or_as_sent(self):
		file = self.day()
		with file.transaction():
			helium = file.add_setting(T0, "Helium", {"G1": 5.9, "G2": 1.0}, "start")
			argon = file.add_setting(T0 + 1, "Argon", {"G1": 0.8, "G2": 1.0}, "selection", T0 + 0.9, "someone")
			new = file.add_samples("TPG1", ("G1", "G2"), [
				(T0 + 0.0, 1, helium, 0.01, 2e-6, 0, 0, 1e-3, 1, 1),
				(T0 + 0.5, 2, helium, 0.01, NAN, 4, 4, 1e-3, 0, 0),  # G1 switched off: no pressure
				(T0 + 1.0, 3, argon, NAN, None, 8, -1, None, 8, -1),  # no reply from the controller
				(T0 + 1.5, 5, argon, 0.01, 4e-6, 0, 0, 2e-3, 0, 0),
				(T0 + 1.5, 6, argon, 0.01, 9.0, 0, 0, 9.0, 0, 0),  # a time already stored is left as it is
			])
		self.assertEqual(new, 4)
		self.assertEqual(file.last_sample("TPG1"), (T0 + 1.5, 5))
		r = pressure_db.read(self.dir, T0, T0 + 10)  # read while the file is open for writing
		np.testing.assert_array_equal(r.time, [T0, T0 + 0.5, T0 + 1.0, T0 + 1.5])
		np.testing.assert_allclose(r.pressure["G1"], [2e-6 * 5.9, NAN, NAN, 4e-6 * 0.8])
		np.testing.assert_allclose(r.pressure["G2"], [1e-3, 1e-3, NAN, 2e-3])
		np.testing.assert_array_equal(r.status["G1"], [0, 4, 8, 0])
		self.assertEqual([(s.time, s.species, s.multipliers["G1"]) for s in r.settings], [(T0, "Helium", 5.9), (T0 + 1.0, "Argon", 0.8)])
		raw = pressure_db.read(self.dir, datetime.datetime.fromtimestamp(T0 + 0.5, datetime.timezone.utc), T0 + 1.5, ["G1"], corrected=False)
		self.assertEqual(list(raw.pressure), ["G1"])
		np.testing.assert_allclose(raw.pressure["G1"], [NAN, NAN])  # start <= time < end
		np.testing.assert_allclose(pressure_db.read(self.dir, T0, T0 + 10, corrected=False).pressure["G1"], [2e-6, NAN, NAN, 4e-6])
		with sqlite3.connect(file.path) as db:  # plain SQL reads it too; no pressure is NULL
			self.assertEqual(db.execute("SELECT seq, p_raw_g1, status_g1, read_s FROM sample_tpg1 WHERE time = ?", (T0 + 1,)).fetchone(), (3, None, 8, None))
			self.assertEqual(db.execute("SELECT selected_at, selected_by, how FROM setting WHERE id = ?", (argon,)).fetchone(), (T0 + 0.9, "someone", "selection"))
		db.close()

	def test_minute_summaries_and_statistics(self):
		file = self.day()
		with file.transaction():
			helium = file.add_setting(T0, "Helium", {"G1": 2.0, "G2": 1.0}, "start")
			argon = file.add_setting(T0 + 90, "Argon", {"G1": 0.5, "G2": 1.0}, "selection")
		self.fill(file, T0, 600, helium, (2e-6, 1e-3))  # the first minute
		self.fill(file, T0 + 60, 300, helium, (4e-6, 1e-3))  # the second minute: the gas changes half way
		self.fill(file, T0 + 90, 300, argon, (8e-6, NAN))
		self.fill(file, T0 + 120, 10, argon, (6e-6, 1e-3))  # the third minute has begun
		with file.transaction():
			file.summarize("TPG1", end=T0 + 120)
		m = pressure_db.read_minutes(self.dir, T0, T0 + 600)
		np.testing.assert_array_equal(m.time, [T0, T0 + 60, T0 + 60])  # one summary per minute and setting, none of the open minute
		np.testing.assert_array_equal(m.count["G1"], [600, 300, 300])
		np.testing.assert_array_equal(m.count["G2"], [600, 300, 0])
		np.testing.assert_allclose(m.mean["G1"], [4e-6, 8e-6, 4e-6])  # corrected: x 2 in helium, x 0.5 in argon
		np.testing.assert_allclose(m.max["G2"], [1e-3, 1e-3, NAN])
		self.assertEqual([s.species for s in m.settings], ["Helium", "Argon"])
		np.testing.assert_allclose(pressure_db.read_minutes(self.dir, T0, T0 + 600, ["G1"], corrected=False).min["G1"], [2e-6, 4e-6, 8e-6])
		# Closing time: the open minute is summarized too, and a second call changes nothing.
		for _ in range(2):
			with file.transaction():
				file.summarize("TPG1")
		np.testing.assert_array_equal(pressure_db.read_minutes(self.dir, T0, T0 + 600).count["G1"], [600, 300, 300, 10])
		s = pressure_db.stats(self.dir, T0 + 60, T0 + 121, "G1")
		self.assertEqual(s.count, 610)
		np.testing.assert_allclose([s.min, s.max, s.first, s.last, s.change], [3e-6, 8e-6, 8e-6, 3e-6, -5e-6])
		self.assertAlmostEqual(s.mean, (300 * 8e-6 + 300 * 4e-6 + 10 * 3e-6) / 610)
		self.assertAlmostEqual(pressure_db.stats(self.dir, T0, T0 + 60, "G1", corrected=False).mean, 2e-6)
		empty = pressure_db.stats(self.dir, T0 + 3600, T0 + 7200, "G1")
		self.assertTrue(empty.count == 0 and math.isnan(empty.mean))

	def test_a_range_spans_days_and_a_gauge_can_be_new(self):
		first = self.day(DAY, ("G1",))
		with first.transaction():
			setting = first.add_setting(T0, "Helium", {"G1": 1.0}, "start")
			first.add_event(T0 + 86399, "gas", "an event", "G1")
		self.fill(first, T0 + 86399, 10, setting, (2e-6,))
		# No file for the next day's morning, then a file with one more gauge.
		second = self.day(NEXT_DAY, ("G1", "G2"))
		with second.transaction():
			setting = second.add_setting(T0 + 86400 + 3600, "Helium", {"G1": 1.0, "G2": 3.0}, "start")
			second.add_event(T0 + 86400 + 3600, "store start", "another")
		self.fill(second, T0 + 86400 + 3600, 5, setting, (3e-6, 1e-3))
		r = pressure_db.read(self.dir, T0, T0 + 3 * 86400)
		self.assertEqual(len(r.time), 15)
		self.assertTrue(np.all(np.diff(r.time) > 0))
		np.testing.assert_allclose(r.pressure["G2"], [NAN] * 10 + [3e-3] * 5)
		np.testing.assert_array_equal(r.status["G2"], [-1] * 10 + [0] * 5)
		self.assertEqual([s.multipliers for s in r.settings], [{"G1": 1.0}, {"G1": 1.0, "G2": 3.0}])
		self.assertEqual([e[1:] for e in pressure_db.events(self.dir, T0, T0 + 3 * 86400)], [("gas", "G1", "an event"), ("store start", None, "another")])
		# Nothing there is not an error: a range before the files, a directory that does not exist, an unknown source.
		for directory, source in ((self.dir, None), (self.dir / "missing", None), (self.dir, "TPG9")):
			nothing = pressure_db.read(directory, T0 - 10 * 86400, T0 - 9 * 86400, source=source)
			self.assertEqual((len(nothing.time), nothing.pressure, nothing.settings), (0, {}, ()))
		self.assertEqual(pressure_db.history(self.dir / "missing", "TPG1", T0, T0 + 3600, 100)["gas"], [])
		self.assertEqual(len(pressure_db.read(self.dir, T0, T0 + 3 * 86400, ["G9"]).pressure["G9"]), 15)  # a gauge no file has: NaN

	def test_history_for_a_chart(self):
		file = self.day()
		with file.transaction():
			helium = file.add_setting(T0, "Helium", {"G1": 2.0, "G2": 1.0}, "start")
			argon = file.add_setting(T0 + 1800, "Argon", {"G1": 0.5, "G2": 1.0}, "selection")
		self.fill(file, T0, 600, helium, (2e-6, 1e-3), step=1.0)  # 10 minutes in helium, one reading a second
		self.fill(file, T0 + 600, 1, helium, (NAN, NAN))  # the controller stops answering
		self.fill(file, T0 + 1800, 600, argon, (8e-6, NAN), step=1.0)  # back after 20 minutes, in argon; G2 has no pressure
		with file.transaction():
			file.summarize("TPG1")
		# One hour in 60 buckets, from the sample rows.
		h = pressure_db.history(self.dir, "TPG1", T0, T0 + 3600, 60)
		np.testing.assert_array_equal(h["t"], T0 + 60.0 * np.array([*range(11), *range(30, 40)]))  # only buckets that hold readings
		g1 = h["gauges"]["G1"]
		np.testing.assert_allclose(g1["mean"], [4e-6] * 10 + [NAN] + [4e-6] * 10)
		np.testing.assert_allclose(g1["min"], g1["max"])
		np.testing.assert_allclose(h["gauges"]["G2"]["max"], [1e-3] * 10 + [NAN] * 11)
		self.assertEqual(h["gas"], [[T0, "Helium"], [T0 + 1800, "Argon"]])
		np.testing.assert_allclose(pressure_db.history(self.dir, None, T0, T0 + 3600, 60, corrected=False)["gauges"]["G1"]["mean"],
		                           [2e-6] * 10 + [NAN] + [8e-6] * 10)
		# A bucket that spans the change of gas: each part is corrected with its own multiplier.
		whole = pressure_db.history(self.dir, "TPG1", T0, T0 + 3600, 1)
		np.testing.assert_allclose([whole["gauges"]["G1"][k][0] for k in ("min", "mean", "max")], [4e-6] * 3)
		# A day in 24 buckets, from the 1-minute summaries: the same numbers.
		day = pressure_db.history(self.dir, "TPG1", T0, T0 + 86400, 24)
		np.testing.assert_array_equal(day["t"], [T0])
		np.testing.assert_allclose(day["gauges"]["G1"]["mean"], [4e-6])
		np.testing.assert_allclose(day["gauges"]["G2"]["mean"], [1e-3])
		later = pressure_db.history(self.dir, "TPG1", T0 + 900, T0 + 900 + 86400, 2000)
		self.assertEqual(later["gas"], [[T0 + 900, "Helium"], [T0 + 1800, "Argon"]])  # in force at the start, then the change
		self.assertEqual(len(later["t"]), 10)
		for start, end, buckets in ((T0, T0, 10), (T0 + 10, T0, 10), (T0, T0 + 10, 0), (T0 + 7200, T0 + 9000, 10)):
			nothing = pressure_db.history(self.dir, "TPG1", start, end, buckets)
			self.assertEqual((len(nothing["t"]), [len(g["mean"]) for g in nothing["gauges"].values()]), (0, [0] * len(nothing["gauges"])))

	def test_gauge_and_source_rows_are_added_only_on_a_change(self):
		file = self.day()
		gauge = dict(name="Test port", source="TPG1", channel=1, model="PKR", device_gas="Nitrogen", device_name="CH1",
		             multipliers={"Helium": 5.9, "Nitrogen": 1.0}, base_warning_torr=5e-6, base_alarm_torr=None)
		with file.transaction():
			first = file.describe("gauge", "G1", T0, **gauge)
			same = file.describe("gauge", "G1", T0 + 1, **gauge)
			changed = file.describe("gauge", "G1", T0 + 2, **dict(gauge, model="IKR", base_alarm_torr=2e-5))
			file.describe("source", "TPG1", T0, driver="tpg366 0.1.0", model="TPG366", serial_number="1", firmware="2", unit="hPa", period_s=0.1)
		self.assertEqual(first["model"], (None, "PKR"))
		self.assertIsNone(same)
		self.assertEqual(changed, {"model": ("PKR", "IKR"), "base_alarm_torr": (None, 2e-5)})
		self.assertEqual(file.latest("gauge", "G1")["multipliers"], '{"Helium": 5.9, "Nitrogen": 1.0}')
		self.assertIsNone(file.latest("gauge", "G2"))
		with sqlite3.connect(file.path) as db:
			self.assertEqual(db.execute("SELECT valid_from, model FROM gauge ORDER BY id").fetchall(), [(T0, "PKR"), (T0 + 2, "IKR")])
			self.assertEqual(db.execute("SELECT unit, period_s FROM source").fetchall(), [("hPa", 0.1)])
		db.close()

	def test_meta_and_schema_version(self):
		file = self.day()
		self.assertEqual(file.meta(), {"schema_version": "1", "pressure_unit": "Torr", "day": DAY})
		with file.transaction():
			self.assertEqual(file.set_meta(host="a", day=DAY), {"host": None})
			self.assertEqual(file.set_meta(host="b"), {"host": "a"})
		with self.assertRaises(RuntimeError), file.transaction():  # a failed transaction leaves nothing behind
			file.set_meta(host="c")
			raise RuntimeError
		self.assertEqual(file.meta()["host"], "b")
		with file.transaction():
			file.set_meta(schema_version=2)
		file.close()
		with self.assertRaisesRegex(ValueError, "schema version 2"):
			DayFile(file.path)
		for key in ("g1", "G1; DROP TABLE meta", ""):
			with self.assertRaisesRegex(ValueError, "not a source or gauge key"):
				pressure_db.read(self.dir, T0, T0 + 1, source=key)

	def test_a_closed_file_is_one_plain_file(self):
		file = DayFile(day_path(self.dir, DAY))
		with file.transaction():
			file.add_source("TPG1", ("G1", "G2"))
			setting = file.add_setting(T0, "Helium", {"G1": 1.0, "G2": 1.0}, "start")
		self.fill(file, T0, 10, setting)
		self.assertIn(f"{file.path.name}-wal", os.listdir(self.dir))  # write-ahead log while it is written
		reader = sqlite3.connect(f"{file.path.as_uri()}?mode=ro", uri=True)
		reader.execute("SELECT COUNT(*) FROM sample_tpg1").fetchone()
		self.assertFalse(file.close())  # a reader holds it: left as it is, and still readable
		self.assertEqual(len(pressure_db.read(self.dir, T0, T0 + 10).time), 10)
		reader.close()
		file = DayFile(file.path)  # the next start
		self.assertTrue(file.close())
		self.assertEqual(os.listdir(self.dir), [file.path.name])
		self.assertEqual(file.path.read_bytes()[18:20], b"\x01\x01")  # the header says rollback journal, not write-ahead log
		self.assertEqual(len(pressure_db.read(self.dir, T0, T0 + 10).time), 10)
		self.assertEqual(pressure_db.last_sample(file.path, "TPG1"), (T0 + 0.9, 10))


if __name__ == "__main__":
	unittest.main()
