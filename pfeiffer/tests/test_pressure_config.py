"""Tests of the pressure IOC's options and of the gas selection file and command. Standard library only:

	python -m unittest discover -s pfeiffer/tests
"""
import contextlib
import copy
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pressure_gas  # noqa: E402
from pressure_config import parse_gas, parse_options  # noqa: E402

DRIVER_CHANNELS = {"tpg366": 6}
SPECIES = ["Helium", "Hydrogen", "Argon", "Nitrogen"]


def options(selection_file="/var/lib/diag-ioc/pressure/gas_species", address="127.0.0.1:8000", period_s=0.1):
	"""Valid [module.options] with two gauges; tests change a copy."""
	multiplier = {"Helium": 5.9, "Hydrogen": 2.4, "Argon": 0.8, "Nitrogen": 1.0}
	return {
		"gas": {"prefix": "TEST:GAS", "species": list(SPECIES), "default": "Helium", "selection_file": selection_file},
		"source": [{"key": "TPG1", "driver": "tpg366", "address": address, "period_s": period_s}],
		"gauge": [
			{"key": "G1", "source": "TPG1", "channel": 1, "name": "Test port", "multiplier": dict(multiplier),
			 "base_warning_torr": 5e-6, "base_alarm_torr": 2e-5},
			{"key": "G2", "source": "TPG1", "channel": 3, "name": "Foreline", "multiplier": dict.fromkeys(SPECIES, 1)},
		],
	}


def changed(path, value, base=None):
	"""A copy of options() with the item at `path` (keys and list indexes) set to `value`, or removed if value is ...."""
	result = copy.deepcopy(base if base is not None else options())
	item = result
	for key in path[:-1]:
		item = item[key]
	if value is ...:
		del item[path[-1]]
	else:
		item[path[-1]] = value
	return result


class OptionsTests(unittest.TestCase):
	def test_valid_options(self):
		config = parse_options(options(), DRIVER_CHANNELS)
		self.assertEqual((config.base_window_h, config.base_ready_min), (12.0, 10))
		self.assertEqual((config.gas.species, config.gas.default), (tuple(SPECIES), "Helium"))
		(source,) = config.sources
		self.assertEqual((source.key, source.driver, source.address, source.period_s, source.timeout_s),
		                 ("TPG1", "tpg366", "127.0.0.1:8000", 0.1, 2.0))
		g1, g2 = config.gauges
		self.assertEqual((g1.key, g1.channel, g1.name, g1.multiplier), ("G1", 1, "Test port", (5.9, 2.4, 0.8, 1.0)))
		self.assertEqual((g1.base_warning_torr, g1.base_alarm_torr), (5e-6, 2e-5))
		self.assertEqual((g2.multiplier, g2.base_warning_torr, g2.base_alarm_torr), ((1.0, 1.0, 1.0, 1.0), None, None))

	def test_invalid_options_name_the_problem(self):
		two_sources = changed(("source",), options()["source"] + [dict(options()["source"][0], key="TPG2")])
		cases = [
			("unknown keys ['base_window']", changed(("base_window",), 12)),
			("missing keys ['gas']", changed(("gas",), ...)),
			("base_window_h must be", changed(("base_window_h",), 0)),
			("base_ready_min must be", changed(("base_ready_min",), 721)),
			("base_ready_min must be", changed(("base_ready_min",), 1.5)),
			("gas: prefix must be", changed(("gas", "prefix"), "TEST:GAS:")),
			("gas: species must list", changed(("gas", "species"), [])),
			("gas: species must be unique", changed(("gas", "species"), ["Helium", "helium"])),
			("gas: default must be one of", changed(("gas", "default"), "Neon")),
			("gas: selection_file must be an absolute path", changed(("gas", "selection_file"), "gas_species")),
			("at least one [[module.options.source]]", changed(("source",), [])),
			("exactly one [[module.options.source]]", two_sources),
			("source #1: key must be", changed(("source", 0, "key"), "tpg1")),
			("source #1: key must be", changed(("source", 0, "key"), "STAT")),
			("source #1: driver must be one of ['tpg366']", changed(("source", 0, "driver"), "other")),
			("source #1: period_s must be", changed(("source", 0, "period_s"), True)),
			("source #1: timeout_s must be", changed(("source", 0, "timeout_s"), -1)),
			("source #1: unknown keys ['period']", changed(("source", 0, "period"), 1)),
			("at least one [[module.options.gauge]]", changed(("gauge",), [])),
			("gauge #2: source must be one of ['TPG1']", changed(("gauge", 1, "source"), "TPG2")),
			("gauge #1: channel must be 1 to 6", changed(("gauge", 0, "channel"), 7)),
			("gauge #1: name must be", changed(("gauge", 0, "name"), "x" * 40)),
			("gauge #2: no multiplier for Argon", changed(("gauge", 1, "multiplier", "Argon"), ...)),
			("gauge #2: multiplier for unlisted species ['Neon']", changed(("gauge", 1, "multiplier", "Neon"), 1.0)),
			("gauge #1: each multiplier must be a positive number", changed(("gauge", 0, "multiplier", "Helium"), 0)),
			("gauge #1: base limits must be positive numbers", changed(("gauge", 0, "base_alarm_torr"), float("nan"))),
			("gauge #1: base_warning_torr must be below base_alarm_torr", changed(("gauge", 0, "base_warning_torr"), 1e-3)),
			("keys must be unique", changed(("gauge", 1, "key"), "G1")),
			("keys must be unique", changed(("gauge", 1, "key"), "TPG1")),
			("the same source and channel", changed(("gauge", 1, "channel"), 1)),
		]
		for i, (fragment, data) in enumerate(cases):
			with self.subTest(case=i, fragment=fragment), self.assertRaises(ValueError) as raised:
				parse_options(data, DRIVER_CHANNELS)
			self.assertIn(fragment, str(raised.exception))

	def test_a_placeholder_left_in_place_is_named(self):
		cases = [
			("source #1: address: the placeholder '<controller address>:8000'", changed(("source", 0, "address"), "<controller address>:8000")),
			("gauge #2: name: the placeholder", changed(("gauge", 1, "name"), "<where the gauge sits>")),
			("gauge #1: base_alarm_torr: the placeholder", changed(("gauge", 0, "base_alarm_torr"), "<limit>")),
			("gauge #2: multiplier: Argon: the placeholder", changed(("gauge", 1, "multiplier", "Argon"), "<multiplier>")),
		]
		for fragment, data in cases:
			with self.subTest(fragment), self.assertRaises(ValueError) as raised:
				parse_options(data, DRIVER_CHANNELS)
			self.assertIn(fragment, str(raised.exception))


class GasSelectionTests(unittest.TestCase):
	def setUp(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.file = Path(tmp.name) / "gas_species"
		self.gas = parse_gas(options(str(self.file))["gas"])
		self.config = Path(tmp.name) / "pressure.toml"
		self.config.write_text(
			'[ioc]\nname = "X:IOC"\n[[module]]\nname = "pressure"\nfactory = "pressure_ioc:PressureModule"\nprefix = "X"\n'
			f'[module.options.gas]\nprefix = "X:GAS"\nspecies = {SPECIES}\ndefault = "Helium"\nselection_file = "{self.file.as_posix()}"\n',
			encoding="utf-8")

	def run_command(self, *args):
		out, err = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			code = pressure_gas.main(["--config", str(self.config), *args])
		return code, out.getvalue(), err.getvalue()

	def test_no_file_is_no_selection(self):
		self.assertIsNone(pressure_gas.read_selection(self.gas))

	def test_write_then_read(self):
		before = time.time()
		self.assertEqual(pressure_gas.write_selection(self.gas, " argon "), "Argon")
		self.assertEqual(self.file.read_text(), "Argon\n")
		self.assertEqual(sorted(self.file.parent.iterdir()), sorted([self.file, self.config]))  # no temporary file left behind
		selection = pressure_gas.read_selection(self.gas)
		self.assertEqual(selection.species, "Argon")
		self.assertAlmostEqual(selection.since, before, delta=5.0)
		self.assertEqual(selection.since, self.file.stat().st_mtime)
		if pressure_gas.pwd is not None:
			self.assertEqual(selection.by, pressure_gas.pwd.getpwuid(os.getuid()).pw_name)

	def test_an_unlisted_species_is_refused(self):
		with self.assertRaisesRegex(ValueError, "'Neon' is not one of Helium, Hydrogen, Argon, Nitrogen"):
			pressure_gas.write_selection(self.gas, "Neon")
		self.assertFalse(self.file.exists())
		for content in (b"Neon\n", b"", b"\xff\xfe"):
			self.file.write_bytes(content)
			with self.subTest(content=content), self.assertRaisesRegex(ValueError, "gas_species"):
				pressure_gas.read_selection(self.gas)

	def test_the_command_shows_and_selects(self):
		code, out, _ = self.run_command()
		self.assertEqual(code, 0)
		self.assertIn("gas selection: Helium (the default; no gas has been selected)", out)
		self.assertIn("choices: Helium, Hydrogen, Argon, Nitrogen", out)
		self.assertEqual(self.run_command("hydrogen")[:2], (0, "gas selection: Hydrogen\n"))
		self.assertEqual(self.file.read_text(), "Hydrogen\n")
		self.assertRegex(self.run_command()[1], r"gas selection: Hydrogen \(since \d{4}-\d\d-\d\d \d\d:\d\d:\d\d, by .+\)")
		code, out, err = self.run_command("neon")
		self.assertEqual((code, out), (1, ""))
		self.assertIn("'neon' is not one of", err)
		self.assertEqual(self.file.read_text(), "Hydrogen\n")


if __name__ == "__main__":
	unittest.main()
