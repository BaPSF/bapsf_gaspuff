"""Tests of the pressure store. No instrument needed.

	python -m unittest discover -s pfeiffer/tests

They need the diag_ioc package (bapsf_epics_core) and are skipped without it. Most feed the store the updates a PVA
monitor would deliver, as nested dicts. One runs the IOC module on the fake controller and hands its published
readings to the store. The last starts the real IOC and the real store process and needs p4p and kernel IPv6.
"""
import contextlib
import io
import math
import os
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import tomllib
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

PFEIFFER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PFEIFFER))

try:
	from diag_ioc.module import ShotPipeline
except ImportError:
	raise unittest.SkipTest("needs the diag_ioc package (bapsf_epics_core)") from None
from diag_ioc.testing import ChildProcess, IocProcess, have, pva_available, wait_for  # noqa: E402

import pressure_db  # noqa: E402
import pressure_gas  # noqa: E402
import pressure_store  # noqa: E402
from fake_tpg366 import FakeTpg366  # noqa: E402
from pressure_config import parse_options  # noqa: E402
from pressure_ioc import PressureModule  # noqa: E402
from pressure_store import Layout, Store, load_layout, pv_value, sample_from_group  # noqa: E402
from test_pressure_config import DRIVER_CHANNELS, options  # noqa: E402
from test_pressure_ioc import DEPLOY, FakeBuilder, ioc_config  # noqa: E402

T0 = 1794470400.0  # 2026-11-12 00:00 in Los Angeles
DAY, NEXT_DAY = "20261112", "20261113"
NAN = math.nan
OK1, OK2 = (2e-6, 5.9, 0, 0), (1e-3, 1.0, 0, 0)  # p_raw, gas_factor, status, raw_status of G1 and G2
NO_REPLY = (NAN, 5.9, 8, -1), (NAN, 1.0, 8, -1)


def group(seq, time_s, gas="Helium", g1=OK1, g2=OK2, read_s=0.01):
	"""One update of the PVA group P:TPG1:SAMPLE, as nested dicts."""
	fields = ("p_raw", "gas_factor", "status", "raw_status")
	return {"seq": seq, "time": time_s, "gas": gas, "read_s": read_s, "g1": dict(zip(fields, g1)), "g2": dict(zip(fields, g2))}


class StoreTests(unittest.TestCase):
	def setUp(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.dir = Path(tmp.name)
		self.now = T0 + 3600
		self.layout = Layout("P", parse_options(options(), DRIVER_CHANNELS), "a" * 64)

	def store(self, layout=None):
		store = Store(self.dir, layout or self.layout, clock=lambda: self.now)
		self.addCleanup(store._close_file)
		return store

	def feed(self, store, *updates):
		"""Group updates, each arriving at its own time."""
		for update in updates:
			self.now = update["time"]
			store.on_group(update)

	def describe(self, store, **change):
		"""The single PVs, as the IOC serves them once the controller has answered."""
		values = {"P:TPG1:DRIVER": "tpg366 0.1.0", "P:TPG1:MODEL": "TPG366", "P:TPG1:SERIAL": "44990000", "P:TPG1:FIRMWARE": "010100",
		          "P:TPG1:UNIT": "hPa", "P:TPG1:ERROR": "", "P:G1:MODEL": "PKR", "P:G1:DEVICE_GAS": "Nitrogen", "P:G1:DEVICE_NAME": "CH1",
		          "P:G2:MODEL": "TPR/PCR", "P:G2:DEVICE_GAS": "Nitrogen", "P:G2:DEVICE_NAME": "CH3", "TEST:GAS:SINCE": T0 + 60.0,
		          "TEST:GAS:BY": "operator", "TEST:GAS:SPECIES": {"index": 0, "choices": ["Helium", "Hydrogen", "Argon", "Nitrogen"]}}
		self.assertEqual(set(values), set(self.layout.pvs()))
		for name, value in dict(values, **change).items():
			store.on_pv(name, {"value": value})

	def query(self, sql, day=DAY):
		db = sqlite3.connect(pressure_db.day_path(self.dir, day))
		try:
			return db.execute(sql).fetchall()
		finally:
			db.close()

	def events(self, day=DAY):
		return self.query("SELECT kind, subject, text FROM event ORDER BY id", day)

	def test_readings_are_stored_with_their_setting_and_what_describes_them(self):
		store = self.store()
		self.describe(store)
		store.on_group({"seq": 0, "time": 0.0, "gas": "", "read_s": 0.0, "g1": {}, "g2": {}})  # the IOC before its first sample
		self.feed(store, group(1, T0 + 3600.0), group(2, T0 + 3600.1, g2=(NAN, 1.0, 4, 4)), group(3, T0 + 3600.2))
		self.assertTrue(store.write())
		self.assertEqual(self.query("SELECT time, seq, setting, read_s, p_raw_g1, status_g1, p_raw_g2, status_g2, raw_status_g2 FROM sample_tpg1"),
		                 [(T0 + 3600.0, 1, 1, 0.01, 2e-6, 0, 1e-3, 0, 0), (T0 + 3600.1, 2, 1, 0.01, 2e-6, 0, None, 4, 4),
		                  (T0 + 3600.2, 3, 1, 0.01, 2e-6, 0, 1e-3, 0, 0)])
		self.assertEqual(self.query("SELECT valid_from, species, multipliers, how, selected_at, selected_by FROM setting"),
		                 [(T0 + 3600.0, "Helium", '{"G1": 5.9, "G2": 1.0}', "start", T0 + 60.0, "operator")])
		self.assertEqual(self.query("SELECT key, name, source, channel, model, device_gas, device_name, multipliers, base_warning_torr, base_alarm_torr FROM gauge"), [
			("G1", "Test port", "TPG1", 1, "PKR", "Nitrogen", "CH1", '{"Helium": 5.9, "Hydrogen": 2.4, "Argon": 0.8, "Nitrogen": 1.0}', 5e-6, 2e-5),
			("G2", "Foreline", "TPG1", 3, "TPR/PCR", "Nitrogen", "CH3", '{"Helium": 1.0, "Hydrogen": 1.0, "Argon": 1.0, "Nitrogen": 1.0}', None, None)])
		self.assertEqual(self.query("SELECT key, driver, model, serial_number, firmware, unit, period_s FROM source"),
		                 [("TPG1", "tpg366 0.1.0", "TPG366", "44990000", "010100", "hPa", 0.1)])
		meta = dict(self.query("SELECT key, value FROM meta"))
		self.assertEqual({k: meta[k] for k in ("schema_version", "pressure_unit", "day", "time_zone", "prefix", "config_sha256")},
		                 {"schema_version": "1", "pressure_unit": "Torr", "day": DAY, "time_zone": "America/Los_Angeles", "prefix": "P", "config_sha256": "a" * 64})
		self.assertEqual((meta["software"], bool(meta["host"])), (f"pressure_store {pressure_store.VERSION}", True))
		self.assertEqual(self.events(), [("store start", None, f"pressure_store {pressure_store.VERSION} started"), ("ioc connected", None, "P:TPG1:SAMPLE"),
		                                 ("status", "G2", "0 ok -> 4 off"), ("status", "G2", "4 off -> 0 ok")])
		self.assertNotIn("127.0.0.1", "".join(map(str, self.query("SELECT * FROM source") + self.query("SELECT * FROM meta"))))  # no controller address
		# A gauge is exchanged: a new gauge row and an event. An empty report (the IOC restarted, the controller has
		# not answered yet) changes nothing.
		self.describe(store, **{"P:G1:MODEL": "IKR", "P:G2:MODEL": "", "P:TPG1:UNIT": ""})
		self.feed(store, group(4, T0 + 3600.3))
		self.assertTrue(store.write())
		self.assertEqual(self.query("SELECT key, model, valid_from FROM gauge ORDER BY id"), [("G1", "PKR", T0 + 3600.2), ("G2", "TPR/PCR", T0 + 3600.2), ("G1", "IKR", T0 + 3600.3)])
		self.assertEqual(self.events()[-1], ("gauge", "G1", "model PKR -> IKR"))
		self.assertEqual(len(self.query("SELECT * FROM source")), 1)
		r = pressure_db.read(self.dir, T0, T0 + 86400)
		np.testing.assert_allclose(r.pressure["G1"], [2e-6 * 5.9] * 4)
		self.assertEqual(store.summary().split(",")[0], "4 readings stored")

	def test_a_change_of_gas_or_multiplier_adds_a_setting(self):
		store = self.store()
		self.describe(store)
		self.feed(store, group(1, T0 + 3600.0), group(2, T0 + 3600.1))
		store.write()
		self.describe(store, **{"TEST:GAS:SPECIES": {"index": 2, "choices": ["Helium", "Hydrogen", "Argon", "Nitrogen"]}, "TEST:GAS:BY": "someone",
		                         "TEST:GAS:SINCE": T0 + 3600.15})
		argon = dict(gas="Argon", g1=(2e-6, 0.8, 0, 0))
		self.feed(store, group(3, T0 + 3600.2, **argon), group(4, T0 + 3600.3, **argon))
		store.write()
		# The multiplier changes with the same gas (the config was edited and the IOC restarted); the selection PVs are not in yet.
		self.describe(store, **{"TEST:GAS:SPECIES": {"index": 0, "choices": ["Helium", "Hydrogen", "Argon", "Nitrogen"]}})
		self.feed(store, group(1, T0 + 3600.4, gas="Argon", g1=(2e-6, 0.9, 0, 0)))
		store.write()
		self.assertEqual(self.query("SELECT id, valid_from, species, multipliers, how, selected_at, selected_by FROM setting"), [
			(1, T0 + 3600.0, "Helium", '{"G1": 5.9, "G2": 1.0}', "start", T0 + 60.0, "operator"),
			(2, T0 + 3600.2, "Argon", '{"G1": 0.8, "G2": 1.0}', "selection", T0 + 3600.15, "someone"),
			(3, T0 + 3600.4, "Argon", '{"G1": 0.9, "G2": 1.0}', "config", None, None)])
		self.assertEqual(self.query("SELECT setting FROM sample_tpg1 ORDER BY time"), [(1,), (1,), (2,), (2,), (3,)])
		self.assertEqual([e for e in self.events() if e[0] == "gas"], [("gas", None, "Helium -> Argon, selected by someone"),
		                                                              ("gas", None, "multipliers for Argon changed: G1 0.8 -> 0.9")])
		store.close()  # the open minute is summarized: one summary per setting, so none mixes two gases
		m = pressure_db.read_minutes(self.dir, T0, T0 + 86400)
		np.testing.assert_array_equal(m.time, [T0 + 3600] * 3)
		np.testing.assert_array_equal(m.count["G1"], [2, 2, 1])
		np.testing.assert_allclose(m.mean["G1"], [2e-6 * 5.9, 2e-6 * 0.8, 2e-6 * 0.9])
		self.assertEqual(pressure_db.history(self.dir, "TPG1", T0 + 3600, T0 + 3660, 6)["gas"], [[T0 + 3600.0, "Helium"], [T0 + 3600.2, "Argon"]])

	def test_gaps_restarts_outages_and_status_changes_are_events(self):
		store = self.store()
		self.describe(store, **{"P:TPG1:ERROR": np.frombuffer(b"PRX: no reply in 2 s\0\0", dtype=np.uint8)})
		t = T0 + 3600
		self.feed(store, group(1, t), group(2, t + 0.1), group(5, t + 0.4),  # 3 and 4 never arrived
		          group(6, t + 0.5, g1=NO_REPLY[0], g2=NO_REPLY[1], read_s=NAN),  # the controller stops answering
		          group(7, t + 2.0, g1=(2e-6, 5.9, 1, 1)),  # back, G1 underrange
		          group(8, t + 2.1, g1=(2e-6, 5.9, 1, 1)), group(9, t + 2.2),
		          group(1, t + 9.0), group(2, t + 9.1))  # the IOC restarted
		with self.assertLogs("pressure_store", "WARNING"):
			store.on_group(RuntimeError("Disconnected"))
			store.on_group(RuntimeError("Disconnected"))
		self.feed(store, group(2, t + 9.1), group(40, t + 13.0))  # reconnected: the last sample again, then 37 were missed
		with self.assertLogs("pressure_store", "WARNING"):
			store.on_group({"seq": 41, "time": t + 13.1})  # a group without the gauges: the IOC runs another config
		self.assertTrue(store.write())
		self.assertEqual(self.events(), [
			("store start", None, f"pressure_store {pressure_store.VERSION} started"),
			("ioc connected", None, "P:TPG1:SAMPLE"),
			("gap", "TPG1", "2 readings missed: the sample counter went from 2 to 5"),
			("source lost", "TPG1", "the controller does not answer: PRX: no reply in 2 s"),
			("source recovered", "TPG1", "the controller answers again"),
			("status", "G1", "0 ok -> 1 underrange"),
			("status", "G1", "1 underrange -> 0 ok"),
			("ioc restart", "TPG1", "the sample counter went from 9 to 1"),
			("ioc disconnected", None, "P:TPG1:SAMPLE: RuntimeError: Disconnected"),
			("ioc connected", None, "P:TPG1:SAMPLE"),
			("gap", "TPG1", "37 readings missed: the sample counter went from 2 to 40"),
		])
		self.assertEqual(self.query("SELECT seq, p_raw_g1, status_g1, raw_status_g1, read_s FROM sample_tpg1 WHERE status_g1 = 8"), [(6, None, 8, -1, None)])
		self.assertEqual(self.query("SELECT COUNT(*) FROM sample_tpg1"), [(10,)])
		self.assertEqual(store.summary(), "10 readings stored, 39 missed (gaps in the sample counter), 1 received twice, 0 dropped here while writes "
		                                  "failed, 0 dropped with the queue full, 1 unreadable updates")
		store.close()
		self.assertEqual(self.events()[-1][0], "store stop")

	def test_a_gauge_that_flickers_does_not_flood_the_events(self):
		store = self.store()
		t = T0 + 3600
		self.feed(store, *(group(i + 1, t + i * 0.1, g1=(2e-6, 5.9, i % 2, i % 2)) for i in range(31)))  # 30 changes within a minute
		store.write()
		self.assertEqual(len(self.events()), 2 + pressure_store.EVENTS_PER_MINUTE)
		self.feed(store, group(32, t + 60.0, g1=(2e-6, 5.9, 1, 1)))  # the next minute: the count of those left out, and one more change
		store.write()
		events = self.events()[2 + pressure_store.EVENTS_PER_MINUTE:]
		self.assertEqual(events, [("status", "G1", "20 more 'status' events in this minute are not listed"), ("status", "G1", "0 ok -> 1 underrange")])
		self.assertEqual(self.query("SELECT COUNT(*) FROM sample_tpg1 WHERE status_g1 = 1"), [(16,)])  # every status is in the rows

	def test_a_restart_of_the_store_goes_on_in_the_same_file(self):
		store = self.store()
		self.describe(store)
		self.feed(store, group(1, T0 + 3600.0), group(2, T0 + 3600.1, g1=NO_REPLY[0], g2=NO_REPLY[1]))
		store.close()
		self.assertEqual(os.listdir(self.dir), [f"pressure_{DAY}.sqlite"])  # closed: one plain file
		self.now = T0 + 7200
		store = self.store()
		self.describe(store)
		self.feed(store, group(2, T0 + 3600.1, g1=NO_REPLY[0], g2=NO_REPLY[1]),  # the IOC's last sample, sent again to the new monitor
		          group(900, T0 + 7200.0), group(901, T0 + 7200.1))
		store.close()
		self.assertEqual(self.query("SELECT seq, setting FROM sample_tpg1 ORDER BY time"), [(1, 1), (2, 1), (900, 1), (901, 1)])
		self.assertEqual(len(self.query("SELECT * FROM setting")), 1)  # the same setting goes on
		self.assertEqual(len(self.query("SELECT * FROM gauge")), 2)
		# Readings published while the store was down are not counted as a gap: the time between the rows shows it.
		self.assertEqual([e[0] for e in self.events()], ["store start", "ioc connected", "source lost", "store stop",
		                                                 "store start", "ioc connected", "source recovered", "store stop"])
		np.testing.assert_array_equal(pressure_db.read_minutes(self.dir, T0, T0 + 86400).count["G1"], [1, 2])
		# Another config file: noted in the file's events.
		self.now = T0 + 7300
		self.store(Layout("P", self.layout.options, "b" * 64)).close()
		self.assertIn(("config", None, "the config file changed (sha256 aaaaaaaaaaaa -> bbbbbbbbbbbb)"), self.events())

	def test_a_new_day_gets_a_new_file(self):
		store = self.store()
		self.describe(store)
		midnight = T0 + 86400
		self.feed(store, group(1, midnight - 0.2), group(2, midnight - 0.1), group(3, midnight), group(4, midnight + 0.1))
		self.assertTrue(store.write())
		self.assertEqual(sorted(p.name for p in self.dir.glob("*.sqlite")), [f"pressure_{DAY}.sqlite", f"pressure_{NEXT_DAY}.sqlite"])
		self.assertEqual(pressure_db.day_path(self.dir, DAY).read_bytes()[18:20], b"\x01\x01")  # yesterday is closed: one plain file
		self.assertEqual(self.query("SELECT seq FROM sample_tpg1 ORDER BY time"), [(1,), (2,)])
		self.assertEqual(self.query("SELECT time, count, count_g1 FROM minute_tpg1"), [(midnight - 60, 2, 2)])
		# The new file can be read alone: it repeats the setting and what describes the gauges and the controller.
		self.assertEqual(self.query("SELECT seq, setting FROM sample_tpg1 ORDER BY time", NEXT_DAY), [(3, 1), (4, 1)])
		self.assertEqual(self.query("SELECT valid_from, species, how FROM setting", NEXT_DAY), [(midnight, "Helium", "start")])
		self.assertEqual(self.query("SELECT key, model FROM gauge", NEXT_DAY), [("G1", "PKR"), ("G2", "TPR/PCR")])
		self.assertEqual(self.query("SELECT unit FROM source", NEXT_DAY), [("hPa",)])
		self.assertEqual(self.events(NEXT_DAY), [])
		r = pressure_db.read(self.dir, midnight - 1, midnight + 1)
		np.testing.assert_array_equal(r.time, [midnight - 0.2, midnight - 0.1, midnight, midnight + 0.1])
		self.assertEqual(len(r.settings), 1)

	def test_a_failed_commit_is_tried_again_and_nothing_is_stored_twice(self):
		store = self.store()
		self.describe(store)
		self.feed(store, group(1, T0 + 3600.0), group(4, T0 + 3600.3))
		with mock.patch.object(pressure_db.DayFile, "add_samples", side_effect=sqlite3.OperationalError("database or disk is full")):
			with self.assertLogs("pressure_store", "WARNING") as logs:
				self.assertFalse(store.write())
				self.assertFalse(store.write())
		self.assertEqual(len(logs.output), 1)  # one line for the outage, not one a second
		self.assertIn("database or disk is full; 19 updates kept for the next try", logs.output[0])
		self.assertEqual(self.query("SELECT COUNT(*) FROM sample_tpg1") + self.query("SELECT COUNT(*) FROM event"), [(0,), (0,)])
		self.feed(store, group(5, T0 + 3600.4))
		with self.assertLogs("pressure_store", "INFO") as logs:
			self.assertTrue(store.write())
		self.assertTrue(any("recovered" in line for line in logs.output))
		self.assertEqual(self.query("SELECT seq FROM sample_tpg1 ORDER BY time"), [(1,), (4,), (5,)])
		self.assertEqual(self.events(), [("store start", None, f"pressure_store {pressure_store.VERSION} started"), ("ioc connected", None, "P:TPG1:SAMPLE"),
		                                 ("gap", "TPG1", "2 readings missed: the sample counter went from 1 to 4")])
		self.assertEqual((len(self.query("SELECT * FROM setting")), len(self.query("SELECT * FROM gauge"))), (1, 2))
		self.assertIn("3 readings stored, 2 missed", store.summary())

	def test_a_full_queue_drops_updates_and_counts_them(self):
		with mock.patch.object(pressure_store, "QUEUE_ITEMS", 5):
			store = self.store()
			with self.assertLogs("pressure_store", "WARNING") as logs:
				self.feed(store, *(group(i, T0 + 3600 + i * 0.1) for i in range(1, 11)))  # the writer is stuck: 5 fit, counting the connect event
			self.assertEqual(len(logs.output), 1)
			store.write()
			self.feed(store, group(11, T0 + 3601.1))
			store.write()
		self.assertEqual(self.query("SELECT seq FROM sample_tpg1 ORDER BY time"), [(1,), (2,), (3,), (4,), (11,)])
		self.assertEqual(self.events()[-1], ("gap", "TPG1", "6 readings missed: the sample counter went from 4 to 11"))
		self.assertIn("6 dropped with the queue full", store.summary())

	def test_the_layout_comes_from_the_ioc_config(self):
		example = DEPLOY / "pressure.toml.example"
		with self.assertRaisesRegex(ValueError, "the placeholder '<controller address>:8000' is still in place"):
			load_layout(example)
		config = self.dir / "pressure.toml"
		config.write_text(ioc_config("LAPD:MACH:PRESSURE", types.SimpleNamespace(host="127.0.0.1", port=8000), self.dir / "gas_species"), encoding="utf-8")
		layout = load_layout(config)
		self.assertEqual((layout.prefix, layout.group, layout.gauge_keys), ("LAPD:MACH:PRESSURE", "LAPD:MACH:PRESSURE:TPG1:SAMPLE", ("G1", "G2")))
		self.assertEqual(len(layout.config_sha256), 64)
		pvs = layout.pvs()
		self.assertEqual(len(pvs), 6 + 2 * 3 + 3)
		self.assertEqual((pvs["LAPD:MACH:PRESSURE:TPG1:SERIAL"], pvs["LAPD:MACH:PRESSURE:G2:DEVICE_GAS"], pvs["LAPD:MACH:PRESSUREGAS:BY"]),
		                 (("source", "TPG1", "serial_number"), ("gauge", "G2", "device_gas"), ("gas", None, "by")))
		config.write_text('[ioc]\nname = "X:IOC"\n[[module]]\nname = "other"\nfactory = "other:Module"\nprefix = "X"\n', encoding="utf-8")
		with self.assertRaisesRegex(ValueError, "expected one .* found 0"):
			load_layout(config)
		with contextlib.redirect_stderr(io.StringIO()) as stderr:
			self.assertEqual(pressure_store.main(["--config", str(self.dir / "missing.toml"), "--dir", str(self.dir)]), 2)
		self.assertIn("missing.toml", stderr.getvalue())

	def test_the_process_monitors_stores_and_stops_on_sigterm(self):
		"""main() with a stand-in for p4p's Context: the monitors it opens, one commit a second, a clean stop."""
		opened = {}

		class Context:
			def __init__(self, provider, nt=None):
				opened["context"] = (provider, nt)

			def monitor(self, name, callback, notify_disconnect=False):
				opened[name] = callback
				return types.SimpleNamespace(close=lambda: opened.setdefault("closed", []).append(name))

			def close(self):
				opened["context closed"] = True

		config = self.dir / "pressure.toml"
		config.write_text(ioc_config("P", types.SimpleNamespace(host="127.0.0.1", port=8000), self.dir / "gas_species"), encoding="utf-8")
		layout = load_layout(config)
		start = time.time()

		def ioc():
			wait_for(lambda: len(opened) == 2 + len(layout.pvs()), 10, "the monitors")
			opened["P:TPG1:MODEL"]({"value": "TPG366"})
			for seq in (1, 2, 3):
				opened["P:TPG1:SAMPLE"](group(seq, time.time()))
				time.sleep(0.01)
			wait_for(lambda: len(pressure_db.read(self.dir / "store", start, start + 60).time) == 3, 10, "the commit")  # while it runs
			os.kill(os.getpid(), signal.SIGTERM)

		modules = {name: types.ModuleType(name) for name in ("p4p", "p4p.client", "p4p.client.thread")}
		modules["p4p.client.thread"].Context = Context
		handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
		thread = threading.Thread(target=ioc, daemon=True)
		try:
			with mock.patch.dict(sys.modules, modules), mock.patch("logging.basicConfig"):
				thread.start()
				code = pressure_store.main(["--config", str(config), "--dir", str(self.dir / "store")])
		finally:
			for signum, handler in handlers.items():
				signal.signal(signum, handler)
		thread.join(10)
		self.assertEqual(code, 0)
		self.assertEqual(opened["context"], ("pva", False))
		self.assertEqual(set(opened["closed"]), {layout.group, *layout.pvs()})
		self.assertTrue(opened["context closed"])
		day = pressure_db.day_path(self.dir / "store", pressure_db.day_of(start))
		self.assertEqual(os.listdir(self.dir / "store"), [day.name])  # closed: one plain file
		db = sqlite3.connect(day)
		self.addCleanup(db.close)
		self.assertEqual(db.execute("SELECT seq FROM sample_tpg1 ORDER BY time").fetchall(), [(1,), (2,), (3,)])
		self.assertEqual(db.execute("SELECT kind FROM event ORDER BY id").fetchall(), [("store start",), ("ioc connected",), ("store stop",)])
		self.assertEqual(db.execute("SELECT model FROM source").fetchall(), [("TPG366",)])
		self.assertEqual(db.execute("SELECT SUM(count) FROM minute_tpg1").fetchall(), [(3,)])

	@unittest.skipUnless(have("p4p"), "needs p4p")
	def test_p4p_values_are_read_like_the_dicts(self):
		from p4p import Type, Value
		from p4p.nt import NTEnum, NTScalar
		gauge = ("S", None, [("p_raw", "d"), ("gas_factor", "d"), ("p", "d"), ("status", "i"), ("raw_status", "i"), ("base", "d")])
		kind = Type([("seq", "i"), ("time", "d"), ("gas", "s"), ("read_s", "d"), ("g1", gauge), ("g2", gauge)])
		update = group(3, T0 + 0.25, g2=(NAN, 1.0, 4, 4))
		sample = sample_from_group(Value(kind, update), ("G1", "G2"))
		self.assertEqual(repr(sample), repr(sample_from_group(update, ("G1", "G2"))))
		self.assertEqual((sample.seq, sample.time, sample.gas, sample.gauges[0].p_raw, sample.gauges[1].status), (3, T0 + 0.25, "Helium", 2e-6, 4))
		self.assertEqual(pv_value(NTScalar("s").wrap("TPG366")), "TPG366")
		self.assertEqual(pv_value(NTScalar("d").wrap(T0 + 0.5)), T0 + 0.5)
		self.assertEqual(pv_value(NTEnum().wrap({"index": 2, "choices": ["Helium", "Hydrogen", "Argon", "Nitrogen"]})), "Argon")
		self.assertEqual(pv_value(NTScalar("ab").wrap(np.frombuffer(b"PRX: no reply\0\0", dtype=np.int8))), "PRX: no reply")


#===================================================================================================================================================
# The IOC module on the fake controller, its published readings handed to the store

class FakeControllerTests(unittest.TestCase):
	def test_what_the_fake_controller_sends_is_what_the_file_holds(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		directory, selection_file = Path(tmp.name) / "store", Path(tmp.name) / "gas_species"
		directory.mkdir()
		fake = FakeTpg366().start()
		self.addCleanup(fake.stop)
		fake.pressures_torr = [2.5e-6, 0.0, 4.0e-4, 0.0, 0.0, 0.0]  # G1 is channel 1, G2 channel 3
		data = options(str(selection_file), f"{fake.host}:{fake.port}", period_s=0.02)
		data["source"][0]["timeout_s"] = 0.5
		module = PressureModule("pressure", data)
		builder = FakeBuilder("P")
		module.create_records(builder)
		records = builder.records
		store = Store(directory, Layout("P", module.config, "c" * 64))

		def last(name):
			return records[name].last[0]

		def published(result, publish=module.publish):
			"""After each publish(): the update a monitor of the group P:TPG1:SAMPLE gets."""
			publish(result)
			update = {"seq": last("P:TPG1:SAMPLE:SEQ"), "time": last("P:TPG1:SAMPLE:TIME"), "gas": last("P:TPG1:SAMPLE:GAS"), "read_s": last("P:TPG1:READ_S")}
			for key in ("G1", "G2"):
				update[key.lower()] = {name.lower(): last(f"P:{key}:{name}") for name in ("P_RAW", "GAS_FACTOR", "STATUS", "RAW_STATUS")}
			store.on_group(update)

		def single_pvs():
			"""The single PVs, as their monitors deliver them."""
			for name in store.layout.pvs():
				value = last(name)
				if name == "TEST:GAS:SPECIES":
					value = {"index": value, "choices": list(module.config.gas.species)}
				store.on_pv(name, {"value": "" if value is None else value})

		module.publish = published
		pipeline = ShotPipeline(module, depth=2, timing_log=False)
		start = time.time()
		module.start(types.SimpleNamespace(pipeline=pipeline))
		self.addCleanup(pipeline.close)
		self.addCleanup(module.stop)
		wait_for(lambda: (last("P:TPG1:SAMPLE:SEQ") or 0) >= 10, 5, "ten readings")
		single_pvs()
		self.assertTrue(store.write())
		pressure_gas.write_selection(module.config.gas, "argon")
		wait_for(lambda: last("P:G1:GAS_FACTOR") == 0.8, 5, "the argon multiplier")
		with self.assertLogs("pressure_ioc", "WARNING"):
			fake.drop_client()
			wait_for(lambda: last("P:G1:STATUS") == 8, 5, "the sample with no reading")
			single_pvs()
			self.assertTrue(store.write())
			wait_for(lambda: last("P:G1:STATUS") == 0 and last("P:TPG1:RECONNECTS") == 1, 10, "readings to resume")
		wait_for(lambda: last("P:TPG1:SAMPLE:SEQ") >= 80, 10, "more readings")
		module.stop()
		pipeline.close()
		single_pvs()
		store.close()

		r = pressure_db.read(directory, start - 1, time.time() + 1, corrected=False)
		count = last("P:TPG1:SAMPLE:SEQ")
		self.assertEqual(len(r.time), count)  # every published reading, once
		self.assertTrue(np.all(np.diff(r.time) > 0))
		lost = r.status["G1"] == 8
		self.assertEqual(lost.sum(), 1)
		np.testing.assert_array_equal(r.status["G2"] == 8, lost)
		np.testing.assert_array_equal(r.pressure["G1"][~lost], np.full(count - 1, 2.5e-6))  # the numbers the controller sent
		np.testing.assert_array_equal(r.pressure["G2"][~lost], np.full(count - 1, 4.0e-4))
		self.assertTrue(np.isnan(r.pressure["G1"][lost]).all())
		self.assertEqual([(s.species, s.multipliers) for s in r.settings], [("Helium", {"G1": 5.9, "G2": 1.0}), ("Argon", {"G1": 0.8, "G2": 1.0})])
		corrected = pressure_db.read(directory, start - 1, time.time() + 1).pressure["G1"]
		changed = np.searchsorted(r.time, r.settings[1].time)
		np.testing.assert_allclose(corrected[:changed], 2.5e-6 * 5.9)
		np.testing.assert_allclose(corrected[changed:][~lost[changed:]], 2.5e-6 * 0.8)
		events = pressure_db.events(directory, start - 1, time.time() + 1)
		self.assertEqual([e[1] for e in events], ["store start", "ioc connected", "gas", "source lost", "source recovered", "store stop"])
		self.assertEqual(events[3][3], "the controller does not answer: PRX: connection closed by the controller")
		self.assertRegex(events[2][3], r"^Helium -> Argon, selected by \S")
		db = sqlite3.connect(pressure_db.day_path(directory, pressure_db.day_of(start)))
		self.addCleanup(db.close)
		self.assertEqual(db.execute("SELECT key, name, channel, model, device_gas, device_name FROM gauge").fetchall(),
		                 [("G1", "Test port", 1, "PKR", "Nitrogen", "FAKE_1"), ("G2", "Foreline", 3, "TPR/PCR", "Nitrogen", "FAKE_3")])
		self.assertEqual(db.execute("SELECT key, model, serial_number, firmware, unit, period_s FROM source").fetchall(),
		                 [("TPG1", "TPG366", "00000000", "010100", "Torr", 0.02)])
		self.assertEqual(db.execute("SELECT how, selected_by IS NOT NULL FROM setting").fetchall(), [("start", 1), ("selection", 1)])


#===================================================================================================================================================
# The real IOC and the real store process, over PVA

@unittest.skipUnless(have("softioc", "epics") and pva_available(), "needs softioc, pyepics, p4p and kernel IPv6")
class PvaTests(unittest.TestCase):
	def test_the_store_process_records_the_ioc(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		directory, config = Path(tmp.name) / "store", Path(tmp.name) / "pressure.toml"
		prefix = f"PRESS{os.getpid()}"
		fake = FakeTpg366().start()
		self.addCleanup(fake.stop)
		text = ioc_config(prefix, fake, Path(tmp.name) / "gas_species")
		config.write_text(text, encoding="utf-8")
		gas = pressure_gas.parse_gas(tomllib.loads(text)["module"][0]["options"]["gas"])
		start = time.time()
		with IocProcess(text, f"{prefix}:IOC:HEARTBEAT", python_path=[PFEIFFER]) as ioc:
			with ChildProcess([PFEIFFER / "pressure_store.py", "--config", config, "--dir", directory], python_path=[PFEIFFER]) as store:
				store.wait_for_lines("connected")
				time.sleep(2.0)
				pressure_gas.write_selection(gas, "argon")
				time.sleep(3.0)
				self.assertEqual(store.stop(), 0, store.log())
				self.assertIn("0 unreadable updates", store.log())
			log = ioc.log()
		r = pressure_db.read(directory, start, time.time(), corrected=False)
		self.assertGreaterEqual(len(r.time), 40, log)  # 5 s at 10 a second
		steps = np.diff(r.time)
		self.assertAlmostEqual(float(np.median(steps)), 0.1, delta=0.02)
		np.testing.assert_array_equal(r.pressure["G1"], np.full(len(r.time), 2.0e-6))
		np.testing.assert_array_equal(r.status["G2"], 0)
		self.assertEqual([s.species for s in r.settings], ["Helium", "Argon"])
		events = [e[1] for e in pressure_db.events(directory, start, time.time())]
		self.assertEqual(events, ["store start", "ioc connected", "gas", "store stop"])
		db = sqlite3.connect(pressure_db.day_path(directory, pressure_db.day_of(start)))
		self.addCleanup(db.close)
		self.assertEqual(db.execute("SELECT model, unit FROM source").fetchall(), [("TPG366", "Torr")])
		self.assertEqual(db.execute("SELECT key, model, device_gas FROM gauge").fetchall(), [("G1", "TPR/PCR", "Nitrogen"), ("G2", "TPR/PCR", "Nitrogen")])
		self.assertEqual(db.execute("SELECT how, selected_by IS NOT NULL FROM setting").fetchall(), [("start", 1), ("selection", 1)])


if __name__ == "__main__":
	unittest.main()
