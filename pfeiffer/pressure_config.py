"""The options of the pressure IOC module: [module.options] of the diag_ioc config file, checked.

deploy/diag-ioc/pressure.toml.example documents every key. A bad or missing value, an unknown key, or a placeholder
left in place is a ValueError that names the key, so the IOC never runs on a made-up address, limit or multiplier.
Standard library only.
"""
import math
import os
import posixpath
import re
from dataclasses import dataclass

BASE_WINDOW_H = 12.0
BASE_READY_MIN = 10
TIMEOUT_S = 2.0
STRING_MAX = 39  # characters an EPICS string record holds
SPECIES_MAX = 16  # states of an EPICS multi-bit record

_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,15}")  # a PV name part; lower-cased, a field name of the PVA group
_RESERVED = ("STAT", "SHOT", "IOC")  # record groups the diag_ioc host owns under the module's prefix
_SPECIES = re.compile(r"[A-Za-z][A-Za-z0-9 _-]{0,24}")
_PLACEHOLDER = re.compile(r"<[^<>]+>")
_OPTIONS = {"base_window_h", "base_ready_min", "gas", "source", "gauge"}
_GAS = {"prefix", "species", "default", "selection_file"}
_SOURCE = {"key", "driver", "address", "period_s", "timeout_s"}
_GAUGE = {"key", "source", "channel", "name", "multiplier", "base_warning_torr", "base_alarm_torr"}


@dataclass(frozen=True)
class Gas:
	prefix: str  # PV prefix of the selection records, e.g. LAPD:MACH:GAS
	species: tuple  # the choices, in the order of the SPECIES record's states
	default: str  # the selection while the selection file does not exist
	selection_file: str  # absolute path; written by pressure_gas.py, read by the IOC

	def match(self, text):
		"""The listed species `text` names, upper or lower case; ValueError if none."""
		for name in self.species:
			if name.lower() == text.strip().lower():
				return name
		raise ValueError(f"{text.strip()!r} is not one of {', '.join(self.species)}")


@dataclass(frozen=True)
class Source:
	key: str
	driver: str
	address: str
	period_s: float
	timeout_s: float


@dataclass(frozen=True)
class Gauge:
	key: str
	source: str
	channel: int  # 1-based channel of the source's controller
	name: str  # where the gauge sits
	multiplier: tuple  # this gauge's multiplier for each of Gas.species, in that order
	base_warning_torr: float | None
	base_alarm_torr: float | None


@dataclass(frozen=True)
class Options:
	base_window_h: float
	base_ready_min: int
	gas: Gas
	sources: tuple
	gauges: tuple


def _require(condition, message):
	if not condition:
		raise ValueError(f"pressure options: {message}")


def _number(value):
	return type(value) in (int, float) and math.isfinite(value)  # type() is: bool is an int subclass


def _positive(value):
	return _number(value) and value > 0


def _text(value, limit=STRING_MAX):
	return type(value) is str and bool(value.strip()) and len(value.encode("utf-8")) <= limit


def _table(where, value, allowed, required):
	_require(type(value) is dict, f"{where} must be a table")
	_require(not set(value) - allowed, f"{where}: unknown keys {sorted(set(value) - allowed)}")
	_require(not required - set(value), f"{where}: missing keys {sorted(required - set(value))}")
	return value


def _tables(name, value):
	_require(type(value) is list and value, f"at least one [[module.options.{name}]] table is required")
	return value


def _no_placeholder(value, path=()):
	"""Refuse a "<...>" string anywhere under `value`, naming its key."""
	if type(value) is dict:
		for key, item in value.items():
			_no_placeholder(item, (*path, str(key)))
	elif type(value) is list:
		for i, item in enumerate(value):
			_no_placeholder(item, (*path[:-1], f"{path[-1]} #{i + 1}"))
	else:
		_require(not (type(value) is str and _PLACEHOLDER.search(value)),
		         f"{': '.join(path)}: the placeholder {value!r} is still in place; enter the real value")


def parse_gas(table):
	"""Gas from the [module.options.gas] table."""
	_no_placeholder(table, ("gas",))
	table = _table("gas", table, _GAS, _GAS)
	prefix = table["prefix"]
	_require(type(prefix) is str and prefix and not any(c.isspace() for c in prefix) and "" not in prefix.split(":"),
	         "gas: prefix must be a PV prefix without a trailing colon")
	species = table["species"]
	_require(type(species) is list and 1 <= len(species) <= SPECIES_MAX, f"gas: species must list 1 to {SPECIES_MAX} names")
	_require(all(type(s) is str and _SPECIES.fullmatch(s) for s in species),
	         "gas: each species must be a letter, then up to 24 letters, digits, spaces, _ or -")
	_require(len({s.lower() for s in species}) == len(species), f"gas: species must be unique, got {species}")
	_require(table["default"] in species, f"gas: default must be one of {species}")
	path = table["selection_file"]
	_require(type(path) is str and (os.path.isabs(path) or posixpath.isabs(path)),
	         "gas: selection_file must be an absolute path")
	return Gas(prefix, tuple(species), table["default"], path)


def _source(index, table, driver_channels):
	where = f"source #{index + 1}"
	table = _table(where, table, _SOURCE, {"key", "driver", "address", "period_s"})
	_require(type(table["key"]) is str and _KEY.fullmatch(table["key"]) and table["key"] not in _RESERVED,
	         f"{where}: key must be a capital letter, then up to 15 capitals, digits or _, and not one of {', '.join(_RESERVED)}")
	_require(table["driver"] in driver_channels, f"{where}: driver must be one of {sorted(driver_channels)}")
	_require(_text(table["address"], 255), f"{where}: address must be the controller's address")
	_require(_positive(table["period_s"]), f"{where}: period_s must be a positive number")
	timeout_s = table.get("timeout_s", TIMEOUT_S)
	_require(_positive(timeout_s), f"{where}: timeout_s must be a positive number")
	return Source(table["key"], table["driver"], table["address"].strip(), float(table["period_s"]), float(timeout_s))


def _gauge(index, table, sources, driver_channels, species):
	where = f"gauge #{index + 1}"
	table = _table(where, table, _GAUGE, {"key", "source", "channel", "name", "multiplier"})
	_require(type(table["key"]) is str and _KEY.fullmatch(table["key"]) and table["key"] not in _RESERVED,
	         f"{where}: key must be a capital letter, then up to 15 capitals, digits or _, and not one of {', '.join(_RESERVED)}")
	_require(table["source"] in sources, f"{where}: source must be one of {sorted(sources)}")
	channels = driver_channels[sources[table["source"]].driver]
	_require(type(table["channel"]) is int and 1 <= table["channel"] <= channels, f"{where}: channel must be 1 to {channels}")
	_require(_text(table["name"]), f"{where}: name must be 1 to {STRING_MAX} characters")
	multiplier = table["multiplier"]
	_require(type(multiplier) is dict, f"{where}: multiplier must be a table")
	missing = [s for s in species if s not in multiplier]
	_require(not missing, f"{where}: no multiplier for {', '.join(missing)}")
	_require(not set(multiplier) - set(species), f"{where}: multiplier for unlisted species {sorted(set(multiplier) - set(species))}")
	_require(all(_positive(multiplier[s]) for s in species), f"{where}: each multiplier must be a positive number")
	limits = [table.get(key) for key in ("base_warning_torr", "base_alarm_torr")]
	_require(all(limit is None or _positive(limit) for limit in limits), f"{where}: base limits must be positive numbers")
	_require(None in limits or limits[0] < limits[1], f"{where}: base_warning_torr must be below base_alarm_torr")
	return Gauge(table["key"], table["source"], table["channel"], table["name"].strip(),
	             tuple(float(multiplier[s]) for s in species), *(None if limit is None else float(limit) for limit in limits))


def parse_options(options, driver_channels):
	"""Options from the [module.options] table. `driver_channels` is {driver name: number of channels}."""
	_no_placeholder(options)
	options = _table("[module.options]", options, _OPTIONS, {"gas", "source", "gauge"})
	base_window_h = options.get("base_window_h", BASE_WINDOW_H)
	_require(_positive(base_window_h), "base_window_h must be a positive number")
	base_ready_min = options.get("base_ready_min", BASE_READY_MIN)
	_require(type(base_ready_min) is int and 1 <= base_ready_min <= base_window_h * 60,
	         "base_ready_min must be a whole number of minutes from 1 to the length of the base window")
	gas = parse_gas(options["gas"])
	sources = tuple(_source(i, t, driver_channels) for i, t in enumerate(_tables("source", options["source"])))
	# Several controllers under one prefix need one publish chain each; the diag_ioc host runs one chain per module today.
	_require(len(sources) == 1, "this version reads one source; list exactly one [[module.options.source]]")
	by_key = {s.key: s for s in sources}
	gauges = tuple(_gauge(i, t, by_key, driver_channels, gas.species) for i, t in enumerate(_tables("gauge", options["gauge"])))
	keys = [s.key for s in sources] + [g.key for g in gauges]
	_require(len(set(keys)) == len(keys), f"source and gauge keys must be unique, got {keys}")
	places = [(g.source, g.channel) for g in gauges]
	_require(len(set(places)) == len(places), "two gauges name the same source and channel")
	return Options(float(base_window_h), base_ready_min, gas, sources, gauges)
