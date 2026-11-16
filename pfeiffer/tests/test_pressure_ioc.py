"""Tests of the pressure IOC module against the fake controller. No instrument needed.

	python -m unittest discover -s pfeiffer/tests

They need the diag_ioc package (bapsf_epics_core) and are skipped without it. The module tests use stand-in records.
The IOC tests start `python -m diag_ioc` in a subprocess with a private, loopback-only EPICS environment and read it
back over CA; they also need softioc and pyepics.
"""
import math
import os
import queue
import re
import sys
import tempfile
import time
import tomllib
import types
import unittest
from pathlib import Path
from unittest import mock

PFEIFFER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PFEIFFER))

try:
	from diag_ioc.host import parse_config
except ImportError:
	raise unittest.SkipTest("needs the diag_ioc package (bapsf_epics_core)") from None
from diag_ioc.module import ShotPipeline  # noqa: E402
from diag_ioc.records import INVALID_ALARM, MAJOR_ALARM, MINOR_ALARM, NO_ALARM  # noqa: E402
from diag_ioc.testing import IocProcess, epics_client, have, isolated_epics_env, monitor_all, pva_available, read_pv, wait_for  # noqa: E402

import pressure_gas  # noqa: E402
import pressure_ioc  # noqa: E402
import tpg366  # noqa: E402
from fake_tpg366 import FakeTpg366  # noqa: E402
from pressure_ioc import STATUS_NO_REPLY, BaseWindow, PressureModule, Sample  # noqa: E402
from test_pressure_config import options  # noqa: E402

DEPLOY = PFEIFFER / "deploy" / "diag-ioc"
HELIUM = pressure_gas.Selection("Helium", 0.0, pressure_gas.DEFAULT_BY)
# Records of the publish chain per gauge, in order.
GAUGE_CHAIN = ["P_RAW", "GAS_FACTOR", "P", "STATUS", "RAW_STATUS", "BASE"]


def reading(*channels):
	"""Driver channels from (pressure, status) pairs; the rest of the controller's six are 'no gauge'."""
	channels += ((math.nan, tpg366.STATUS_NO_GAUGE),) * (tpg366.CHANNELS - len(channels))
	return tuple(tpg366.ChannelReading(p, status, status) for p, status in channels)


class ExampleConfigTests(unittest.TestCase):
	def setUp(self):
		self.data = tomllib.loads((DEPLOY / "pressure.toml.example").read_text(encoding="utf-8"))

	def test_the_example_is_a_valid_host_config_with_placeholders(self):
		config = parse_config(self.data)
		self.assertTrue((DEPLOY / config.ioc.acf).is_file())
		(module,) = config.modules
		self.assertEqual((module.factory, module.listen), ("pressure_ioc:PressureModule", None))
		self.assertIs(module.timing_log, False)  # one line per sample would be 864,000 lines a day
		with self.assertRaisesRegex(ValueError, re.escape("source #1: address: the placeholder '<controller address>:8000'")):
			PressureModule(module.name, module.options)

	def test_the_example_runs_once_filled_in(self):
		filled = self.data["module"][0]["options"]
		filled["source"][0]["address"] = "controller.example"
		(gauge,) = filled["gauge"]
		gauge.update(name="Test port", base_warning_torr=5e-6, base_alarm_torr=2e-5)
		gauge["multiplier"].update(Helium=5.9, Hydrogen=2.4, Argon=0.8)
		module = PressureModule("pressure", filled)
		self.assertEqual((module.source.address, module.source.period_s), ("controller.example", 0.1))
		self.assertEqual(module.config.gas.default, "Helium")

	def test_a_bad_address_is_refused_at_construction(self):
		with self.assertRaisesRegex(ValueError, "source TPG1: controller address"):
			PressureModule("pressure", options(address="host:port"))


class BaseWindowTests(unittest.TestCase):
	def fill(self, base, minute, pressure, count=10):
		for i in range(count):
			value = base.add(minute * 60 + i, pressure)
		return value

	def test_lowest_minute_mean_once_enough_minutes_are_in(self):
		base = BaseWindow(window_minutes=5, ready_minutes=2, min_count=5)
		self.assertTrue(math.isnan(self.fill(base, 100, 4.0)))
		self.assertTrue(math.isnan(self.fill(base, 101, 2.0)))  # one full minute in, two needed
		self.assertEqual(self.fill(base, 102, 9.0), 2.0)  # minutes 100 and 101; 102 is still being summed
		self.assertEqual(self.fill(base, 103, 1.0), 2.0)
		self.assertEqual(base.add(104 * 60), 1.0)

	def test_a_minute_with_too_few_readings_does_not_count(self):
		base = BaseWindow(window_minutes=5, ready_minutes=1, min_count=5)
		self.fill(base, 100, 3.0)
		self.fill(base, 101, 0.5, count=4)
		self.assertEqual(base.add(102 * 60), 3.0)

	def test_minutes_leave_the_window(self):
		base = BaseWindow(window_minutes=3, ready_minutes=1, min_count=5)
		self.fill(base, 100, 1.0)
		self.fill(base, 101, 5.0)
		self.assertEqual(base.add(103 * 60), 1.0)
		self.assertEqual(base.add(104 * 60), 5.0)  # minute 100 is older than three minutes
		self.assertTrue(math.isnan(base.add(105 * 60)))  # nothing left: invalid again


#===============================================================================================================================================
# The module with stand-in records

class FakeRecord:
	def __init__(self, name, kind, fields):
		self.name, self.kind, self.fields, self.infos, self.sets = name, kind, fields, [], []

	def set(self, value, severity=NO_ALARM, alarm=NO_ALARM, timestamp=None):
		self.sets.append((value, severity, timestamp))

	def add_info(self, key, value):
		self.infos.append((key, value))

	@property
	def last(self):
		"""(value, severity, timestamp) of the last set(), or the initial value."""
		return self.sets[-1] if self.sets else (self.fields.get("initial_value"), None, None)


class FakeBuilder:
	"""softioc's builder as far as PressureModule uses it: every record constructor makes a FakeRecord."""

	def __init__(self, prefix):
		self.prefix = prefix
		self.records = {}

	def RecordName(self, name):
		return f"{self.prefix}:{name}"

	def SetDeviceName(self, prefix):
		self.prefix = prefix

	def __getattr__(self, kind):
		def make(name, *states, **fields):
			record = FakeRecord(self.RecordName(name), kind, dict(fields, states=states))
			self.records[record.name] = record
			return record
		return make


class ModuleTests(unittest.TestCase):
	def setUp(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.selection_file = Path(tmp.name) / "gas_species"
		self.fake = FakeTpg366().start()
		self.addCleanup(self.fake.stop)

	def module(self, **change):
		"""A PressureModule on the fake controller with its records built; G1 is channel 1, G2 channel 3."""
		data = options(str(self.selection_file), f"{self.fake.host}:{self.fake.port}", period_s=0.02)
		data["source"][0]["timeout_s"] = 0.5
		module = PressureModule("pressure", dict(data, **change))
		self.builder = FakeBuilder("P")
		self.tail = module.create_records(self.builder)
		return module

	def start(self, module):
		pipeline = ShotPipeline(module, depth=2, timing_log=False)
		module.start(types.SimpleNamespace(pipeline=pipeline))
		self.addCleanup(module.stop)
		self.addCleanup(pipeline.close)

	def last(self, name):
		return self.builder.records[name].last

	def value(self, name):
		return self.last(name)[0]

	def test_records_chain_and_group(self):
		module = self.module()
		records = self.builder.records
		self.assertEqual(self.builder.prefix, "P")  # put back after the gas records
		self.assertEqual([r for r in records if r.startswith("TEST:GAS:")], ["TEST:GAS:SPECIES", "TEST:GAS:SINCE", "TEST:GAS:BY"])
		self.assertEqual(records["TEST:GAS:SPECIES"].fields["states"], ("Helium", "Hydrogen", "Argon", "Nitrogen"))
		# The chain: SEQ is the head, every other record is passive with a device timestamp, the last gauge's BASE the tail.
		expected = ["P:TPG1:SAMPLE:SEQ", "P:TPG1:SAMPLE:TIME", "P:TPG1:SAMPLE:GAS", "P:TPG1:READ_S"]
		expected += [f"P:{g}:{name}" for g in ("G1", "G2") for name in GAUGE_CHAIN]
		order, record = [], records["P:TPG1:SAMPLE:SEQ"]
		while record is not None:
			order.append(record.name)
			record = record.fields.get("FLNK")
		self.assertEqual(order, expected)
		self.assertIs(self.tail, records["P:G2:BASE"])
		self.assertNotIn("SCAN", records["P:TPG1:SAMPLE:SEQ"].fields)
		self.assertTrue(all(records[n].fields["SCAN"] == "Passive" and records[n].fields["TSE"] == -2 for n in expected[1:]))
		# Base limits are alarm limits of BASE; a gauge without limits has none.
		base = records["P:G1:BASE"].fields
		self.assertEqual((base["HIGH"], base["HSV"], base["HIHI"], base["HHSV"]), (5e-6, "MINOR", 2e-5, "MAJOR"))
		self.assertFalse({"HIGH", "HIHI"} & set(records["P:G2:BASE"].fields))
		# The group: every chain record once, the tail triggers it.
		fields = {}
		for name in expected:
			((key, value),) = records[name].infos
			self.assertEqual((key, list(value)), ("Q:group", ["P:TPG1:SAMPLE"]))
			fields.update(value["P:TPG1:SAMPLE"])
		self.assertEqual(list(fields), ["", "seq", "time", "gas", "read_s"] + [f"{g}.{n.lower()}" for g in ("g1", "g2") for n in GAUGE_CHAIN])
		self.assertEqual([name for name, spec in fields.items() if "+trigger" in spec], ["g2.base"])
		# Fixed from the config.
		self.assertEqual([self.value(f"P:G2:{n}") for n in ("NAME", "SOURCE", "CHANNEL")], ["Foreline", "TPG1", 3])
		self.assertEqual(list(self.value("P:G1:GAS_FACTORS")), [5.9, 2.4, 0.8, 1.0])
		self.assertEqual(self.value("P:TPG1:PERIOD_S"), 0.02)
		self.assertEqual(module.config.gauges[1].channel, 3)

	def test_analyze_applies_the_multiplier_and_keeps_the_base_before_it(self):
		module = self.module(base_ready_min=1)
		argon = pressure_gas.Selection("Argon", 0.0, "someone")
		for i in range(1500):  # one full clock minute at 50 a second, in helium
			result = module.analyze(Sample(i, 6000 + i * 0.04, 0.001, reading((2e-6, 0), (0, 5), (1e-3, 1)), HELIUM))
		g1, g2 = result.gauges
		self.assertEqual((g1.p_raw, g1.gas_factor, g1.status), (2e-6, 5.9, 0))
		self.assertAlmostEqual(g1.p, 2e-6 * 5.9)
		self.assertTrue(math.isnan(g1.base))
		g1, g2 = module.analyze(Sample(1500, 6060.0, 0.001, reading((4e-6, 0), (0, 5), (7e2, 2)), argon)).gauges
		self.assertAlmostEqual(g1.base, 2e-6)  # the gauge's own reading, not helium-corrected
		self.assertAlmostEqual(g2.base, 1e-3)  # underrange readings count
		self.assertEqual((g1.gas_factor, g1.p), (0.8, 4e-6 * 0.8))
		# A failed read: no pressure, status 8, the multiplier still published.
		g1, g2 = module.analyze(Sample(1501, 6061.0, math.nan, None, argon)).gauges
		self.assertTrue(math.isnan(g1.p_raw) and math.isnan(g1.p))
		self.assertEqual((g1.status, g1.raw_status, g1.gas_factor), (STATUS_NO_REPLY, -1, 0.8))

	def test_readings_are_published_with_their_alarms(self):
		self.fake.pressures_torr = [2e-6, 0.0, 5e-4, 0.0, 0.0, 0.0]
		self.fake.statuses = [1, 0, 4, 0, 0, 0]  # G1 underrange, G2 (channel 3) switched off
		module = self.module()
		self.start(module)
		wait_for(lambda: self.value("P:TPG1:SAMPLE:SEQ") and self.value("P:TPG1:SAMPLE:SEQ") >= 5, 5, "five samples")
		ts = self.value("P:TPG1:SAMPLE:TIME")
		self.assertAlmostEqual(ts, time.time(), delta=5.0)
		self.assertEqual(self.last("P:G1:P_RAW"), (2e-6, MINOR_ALARM, ts))
		self.assertEqual(self.last("P:G1:GAS_FACTOR"), (5.9, NO_ALARM, ts))
		self.assertEqual(self.last("P:G1:P"), (2e-6 * 5.9, MINOR_ALARM, ts))
		self.assertEqual(self.last("P:G1:STATUS"), (1, NO_ALARM, ts))
		p, severity, _ = self.last("P:G2:P")
		self.assertTrue(math.isnan(p))
		self.assertEqual((severity, self.value("P:G2:STATUS"), self.value("P:G2:RAW_STATUS")), (INVALID_ALARM, 4, 4))
		self.assertEqual(self.last("P:G1:BASE")[1], INVALID_ALARM)  # no full minute yet
		self.assertEqual(self.value("P:TPG1:SAMPLE:GAS"), "Helium")
		self.assertEqual(self.builder.records["P:TPG1:SAMPLE:SEQ"].sets[-1][2], self.value("P:TPG1:SAMPLE:TIME"))
		# What the controller reports, and the link.
		self.assertEqual([self.value(f"P:TPG1:{n}") for n in ("CONNECTED", "ERROR", "RECONNECTS")], [1, "", 0])
		self.assertEqual([self.value(f"P:TPG1:{n}") for n in ("DRIVER", "MODEL", "SERIAL", "FIRMWARE", "UNIT")],
		                 [f"tpg366 {tpg366.DRIVER_VERSION}", "TPG366", "00000000", "010100", "Torr"])
		self.assertEqual([self.value(f"P:G2:{n}") for n in ("MODEL", "DEVICE_NAME")], ["TPR/PCR", "FAKE_3"])
		self.assertEqual(self.last("P:G2:DEVICE_GAS")[:2], ("Nitrogen", NO_ALARM))
		self.assertEqual({c for c in self.fake.commands if c != "PRX"}, {"AYT", "UNI", "TID", "GAS", "CID"})  # read-only

	def test_a_controller_gas_setting_other_than_nitrogen_is_a_minor_alarm(self):
		self.fake.gases = [0, 0, 1, 0, 0, 0]
		module = self.module()
		with self.assertLogs("pressure_ioc", "WARNING") as logs:
			self.start(module)
			wait_for(lambda: self.value("P:TPG1:CONNECTED") == 1, 5, "the first reading")
		self.assertEqual(self.last("P:G1:DEVICE_GAS")[:2], ("Nitrogen", NO_ALARM))
		self.assertEqual(self.last("P:G2:DEVICE_GAS")[:2], ("Argon", MINOR_ALARM))
		self.assertIn("G2 is set to Argon", logs.output[0])

	def test_the_controller_is_described_again_while_reading(self):
		module = self.module()
		with mock.patch.object(pressure_ioc, "DESCRIBE_PERIOD_S", 0.1), self.assertLogs("pressure_ioc", "WARNING"):
			self.start(module)
			wait_for(lambda: self.value("P:TPG1:UNIT") == "Torr", 5, "the first description")
			raw = self.value("P:G1:P_RAW")
			self.fake.unit = 4  # someone sets hPa on the front panel
			self.fake.gases = [3, 0, 0, 0, 0, 0]
			wait_for(lambda: self.value("P:TPG1:UNIT") == "hPa", 5, "the unit to be read again")
			self.assertEqual(self.last("P:G1:DEVICE_GAS")[:2], ("Helium", MINOR_ALARM))
			time.sleep(0.1)
			self.assertAlmostEqual(self.value("P:G1:P_RAW"), raw, delta=raw * 1e-4)  # still Torr

	def test_the_gas_selection_follows_the_file(self):
		module = self.module()
		self.start(module)
		species, since, by = (self.builder.records[f"TEST:GAS:{n}"] for n in ("SPECIES", "SINCE", "BY"))
		self.assertEqual((species.last[0], by.last[0]), (0, pressure_gas.DEFAULT_BY))
		self.assertAlmostEqual(since.last[0], time.time(), delta=5.0)
		wait_for(lambda: self.value("P:G1:GAS_FACTOR") == 5.9, 5, "the helium multiplier")
		pressure_gas.write_selection(module.config.gas, "argon")
		wait_for(lambda: self.value("P:G1:GAS_FACTOR") == 0.8, 5, "the argon multiplier")
		self.assertEqual((species.last[0], since.last[0]), (2, self.selection_file.stat().st_mtime))
		self.assertNotEqual(by.last[0], pressure_gas.DEFAULT_BY)
		self.assertEqual(self.value("P:TPG1:SAMPLE:GAS"), "Argon")
		self.assertEqual(self.value("P:G2:GAS_FACTOR"), 1.0)
		# A file that names no listed species changes nothing.
		self.selection_file.write_text("Neon\n")
		with self.assertLogs("pressure_ioc", "WARNING") as logs:
			wait_for(lambda: logs.output, 5, "the warning about the file")
		self.assertIn("the selection stays Argon", logs.output[0])
		self.assertEqual(species.last[0], 2)

	def test_an_outage_publishes_one_invalid_sample_then_recovers(self):
		module = self.module()
		self.start(module)
		wait_for(lambda: self.value("P:TPG1:CONNECTED") == 1, 5, "the first reading")
		with self.assertLogs("pressure_ioc", "WARNING"):
			self.fake.drop_client()
			wait_for(lambda: self.value("P:TPG1:CONNECTED") == 0, 5, "the loss to be noticed")
		error, severity, _ = self.last("P:TPG1:ERROR")
		self.assertEqual((error, severity), ("PRX: connection closed by the controller", MAJOR_ALARM))
		wait_for(lambda: self.value("P:G1:STATUS") == STATUS_NO_REPLY, 5, "the sample with no reading")
		for gauge in ("G1", "G2"):
			p, severity, _ = self.last(f"P:{gauge}:P")
			self.assertTrue(math.isnan(p))
			self.assertEqual(severity, INVALID_ALARM)
			self.assertEqual(self.last(f"P:{gauge}:STATUS")[:2], (STATUS_NO_REPLY, INVALID_ALARM))
		self.assertEqual(self.last("P:G1:GAS_FACTOR")[:2], (5.9, NO_ALARM))
		lost_seq = self.value("P:TPG1:SAMPLE:SEQ")
		# The reader connects again after its first delay (1 s) and readings resume, counted on.
		wait_for(lambda: self.value("P:TPG1:CONNECTED") == 1, 5, "the reconnect")
		wait_for(lambda: self.value("P:G1:STATUS") == 0 and self.value("P:TPG1:SAMPLE:SEQ") > lost_seq, 5, "readings to resume")
		self.assertEqual([self.value(f"P:TPG1:{n}") for n in ("ERROR", "RECONNECTS")], ["", 1])
		self.assertEqual([s[0] for s in self.builder.records["P:G1:STATUS"].sets].count(STATUS_NO_REPLY), 1)
		self.assertEqual(self.fake.connections, 2)

	def test_no_controller_at_start_and_a_prompt_stop(self):
		self.fake.stop()
		module = self.module()
		with self.assertLogs("pressure_ioc", "WARNING"):
			self.start(module)
			wait_for(lambda: self.value("P:G1:STATUS") == STATUS_NO_REPLY, 5, "the sample with no reading")
		self.assertEqual(self.value("P:TPG1:CONNECTED"), 0)
		self.assertIn("connect failed", self.value("P:TPG1:ERROR"))
		t0 = time.monotonic()
		module.stop()  # in the middle of the reconnect delay
		self.assertLess(time.monotonic() - t0, 1.0)


#===============================================================================================================================================
# The real IOC, read over CA

def ioc_config(prefix, fake, selection_file):
	return f"""
[ioc]
name = "{prefix}:IOC"
acf = "{(DEPLOY / 'readonly.acf').as_posix()}"

[[module]]
name = "pressure"
factory = "pressure_ioc:PressureModule"
prefix = "{prefix}"
stale_seconds = 3
timing_log = false

[module.options.gas]
prefix = "{prefix}GAS"
species = ["Helium", "Hydrogen", "Argon", "Nitrogen"]
default = "Helium"
selection_file = "{Path(selection_file).as_posix()}"

[[module.options.source]]
key = "TPG1"
driver = "tpg366"
address = "{fake.host}:{fake.port}"
period_s = 0.1
timeout_s = 0.5

[[module.options.gauge]]
key = "G1"
source = "TPG1"
channel = 1
name = "Test port"
base_warning_torr = 5e-6
base_alarm_torr = 2e-5
[module.options.gauge.multiplier]
Helium = 5.9
Hydrogen = 2.4
Argon = 0.8
Nitrogen = 1.0

[[module.options.gauge]]
key = "G2"
source = "TPG1"
channel = 3
name = "Foreline"
[module.options.gauge.multiplier]
Helium = 1.0
Hydrogen = 1.0
Argon = 1.0
Nitrogen = 1.0
"""


@unittest.skipUnless(have("softioc", "epics"), "needs softioc and pyepics")
class IocTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.prefix = f"PRESS{os.getpid()}"
		cls.fake = FakeTpg366().start()
		cls.addClassCleanup(cls.fake.stop)
		tmp = tempfile.TemporaryDirectory()
		cls.addClassCleanup(tmp.cleanup)
		cls.selection_file = Path(tmp.name) / "gas_species"
		cls.ioc = IocProcess(ioc_config(cls.prefix, cls.fake, cls.selection_file), f"{cls.prefix}:IOC:HEARTBEAT", python_path=[PFEIFFER])
		cls.enterClassContext(cls.ioc)
		wait_for(lambda: read_pv(f"{cls.prefix}:TPG1:CONNECTED", ioc=cls.ioc)["value"] == 1, 10, "the first reading")
		cls.gas = pressure_gas.parse_gas(tomllib.loads(ioc_config(cls.prefix, cls.fake, cls.selection_file))["module"][0]["options"]["gas"])

	def read(self, name, **kw):
		return read_pv(name if name.startswith(f"{self.prefix}GAS") else f"{self.prefix}:{name}", ioc=self.ioc, **kw)

	def value(self, name, **kw):
		return self.read(name, **kw)["value"]

	def test_every_sample_is_one_update_at_the_configured_rate(self):
		names = [f"{self.prefix}:{n}" for n in ("TPG1:SAMPLE:SEQ", "TPG1:SAMPLE:TIME", "G1:P_RAW", "G1:P", "G2:P")]
		updates = queue.Queue()
		pvs = monitor_all(names, lambda *update: updates.put(update), timeout=10)
		try:
			by_stamp = {}
			deadline = time.monotonic() + 3.0
			while time.monotonic() < deadline:
				name, value, stamp, severity = updates.get(timeout=5)
				by_stamp.setdefault(stamp, {})[name.removeprefix(f"{self.prefix}:")] = value
		finally:
			for pv in pvs:
				pv.disconnect()
		complete = [(stamp, sample) for stamp, sample in sorted(by_stamp.items()) if len(sample) == len(names)]
		self.assertGreaterEqual(len(complete), 20, self.ioc.log())  # 3 s at 10 a second, less the edges
		for stamp, sample in complete:  # each record of a sample carries the reading's time and that sample's values
			self.assertAlmostEqual(sample["TPG1:SAMPLE:TIME"], stamp, places=5)
			self.assertAlmostEqual(sample["G1:P"], sample["G1:P_RAW"] * 5.9)
		seqs = [sample["TPG1:SAMPLE:SEQ"] for _, sample in complete]
		self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))))
		steps = [b - a for (a, _), (b, _) in zip(complete, complete[1:])]
		self.assertAlmostEqual(sum(steps) / len(steps), 0.1, delta=0.01)

	def test_records_over_ca_are_read_only(self):
		self.assertEqual(self.value("G1:NAME"), "Test port")
		self.assertEqual((self.value("G1:BASE.HIGH"), self.value("G1:BASE.HIHI")), (5e-6, 2e-5))
		base = self.read("G1:BASE")
		self.assertTrue(math.isnan(base["value"]))  # no full minute yet
		self.assertEqual(base["severity"], INVALID_ALARM)
		self.assertEqual(self.value("TPG1:UNIT", as_string=True), "Torr")
		self.assertEqual(self.value("G2:MODEL", as_string=True), "TPR/PCR")
		self.assertEqual(self.read("TPG1:CONNECTED")["severity"], NO_ALARM)
		self.assertEqual(self.value("STAT:STALE"), 0)
		self.assertEqual(self.value("STAT:ERROR", as_string=True), "")
		epics = epics_client()
		for name in ("G1:P", "G1:BASE.HIGH", f"{self.prefix}GAS:SPECIES"):
			pv = epics.get_pv(name if "GAS" in name else f"{self.prefix}:{name}", connect=True, timeout=5)
			self.assertFalse(pv.write_access, name)
		records = self.ioc.db_path.read_text()
		for name in (f"{self.prefix}:G1:P", f"{self.prefix}:TPG1:SAMPLE:SEQ", f"{self.prefix}GAS:SPECIES", f"{self.prefix}:STAT:STALE"):
			self.assertIn(f'"{name}"', records)

	def test_the_gas_command_changes_the_multiplier(self):
		gas = f"{self.prefix}GAS"
		self.assertEqual(self.value(f"{gas}:SPECIES", as_string=True), "Helium")
		self.assertEqual(self.value(f"{gas}:BY", as_string=True), pressure_gas.DEFAULT_BY)
		pressure_gas.write_selection(self.gas, "hydrogen")
		self.addCleanup(self.selection_file.unlink)
		wait_for(lambda: self.value(f"{gas}:SPECIES", as_string=True) == "Hydrogen", 5, "the selection to be read")
		self.assertAlmostEqual(self.value(f"{gas}:SINCE"), self.selection_file.stat().st_mtime, places=3)
		wait_for(lambda: self.value("G1:GAS_FACTOR") == 2.4, 5, "the hydrogen multiplier")
		self.assertEqual(self.value("TPG1:SAMPLE:GAS", as_string=True), "Hydrogen")
		self.assertEqual(self.value("G2:GAS_FACTOR"), 1.0)

	def test_an_outage_is_invalid_then_recovers(self):
		reconnects = self.value("TPG1:RECONNECTS")
		self.fake.drop_client()
		wait_for(lambda: self.read("G1:P")["severity"] == INVALID_ALARM, 5, "G1:P to go INVALID")
		self.assertEqual(self.value("G1:STATUS"), STATUS_NO_REPLY)
		wait_for(lambda: self.value("TPG1:RECONNECTS") == reconnects + 1, 10, "the reconnect")
		wait_for(lambda: self.read("G1:P")["severity"] == NO_ALARM, 5, "readings to resume")
		self.assertEqual(self.value("TPG1:ERROR", as_string=True), "")
		self.assertEqual(self.value("STAT:ERROR", as_string=True), "", self.ioc.log())
		self.assertEqual(self.value("STAT:ACK_TIMEOUTS"), 0)

	@unittest.skipUnless(pva_available(), "needs p4p and kernel IPv6")
	def test_the_pva_group_carries_one_sample(self):
		os.environ.update(isolated_epics_env())
		from p4p.client.thread import Context
		with Context("pva") as ctx:
			sample = ctx.get(f"{self.prefix}:TPG1:SAMPLE", timeout=5)
		self.assertGreater(sample.seq, 0)
		self.assertAlmostEqual(sample.time, time.time(), delta=10.0)
		self.assertIn(sample.gas, self.gas.species)
		self.assertAlmostEqual(sample.g1.p, sample.g1.p_raw * sample.g1.gas_factor)
		self.assertEqual((sample.g1.status, sample.g2.status), (0, 0))


if __name__ == "__main__":
	unittest.main()
