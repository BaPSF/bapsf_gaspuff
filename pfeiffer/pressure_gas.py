"""The gas selection of the machine: one small file on the IOC's own machine, and the command that changes it.

	python pressure_gas.py --config /etc/diag-ioc/pressure.toml          show the selection and the choices
	python pressure_gas.py --config /etc/diag-ioc/pressure.toml argon    select a gas

Which gas is in the machine is known only to a person, so a person selects it. The file ([module.options.gas]
selection_file) holds the species name and nothing else. Its modification time is when the gas was selected and its
owner is who selected it, so both come from the operating system, not from the caller. The pressure IOC reads the
file once a second and never writes it; while the file does not exist the selection is the config's default. Who may
select a gas is who may write the file's directory. Standard library only.
"""
import argparse
import os
import sys
import time
import tomllib
from dataclasses import dataclass

from pressure_config import parse_gas

try:
	import pwd
except ImportError:  # Windows: the owner is reported as a number
	pwd = None

DEFAULT_BY = "(default)"  # Selection.by while no one has selected a gas
_MAX_BYTES = 256


@dataclass(frozen=True)
class Selection:
	species: str
	since: float  # epoch seconds
	by: str  # the account that selected it, or DEFAULT_BY


def _account(uid):
	try:
		return pwd.getpwuid(uid).pw_name
	except (AttributeError, KeyError):
		return f"uid {uid}"


def read_selection(gas):
	"""The Selection in gas.selection_file, or None if the file does not exist. OSError if it cannot be read,
	ValueError if it does not name a listed species."""
	try:
		with open(gas.selection_file, "rb") as f:
			status = os.fstat(f.fileno())
			data = f.read(_MAX_BYTES)
	except FileNotFoundError:
		return None
	try:
		species = gas.match(data.decode("utf-8"))
	except ValueError as e:  # UnicodeDecodeError included
		raise ValueError(f"{gas.selection_file}: {e}") from None
	return Selection(species, status.st_mtime, _account(status.st_uid))


def write_selection(gas, text):
	"""Select the species `text` names (upper or lower case) and return its listed name. The file is replaced in
	one step, so the IOC never reads half of it. ValueError for an unlisted species, OSError if the write fails."""
	species = gas.match(text)
	tmp = f"{gas.selection_file}.{os.getpid()}.tmp"
	try:
		with open(tmp, "w", encoding="utf-8") as f:
			f.write(species + "\n")
			f.flush()
			os.fsync(f.fileno())
		os.replace(tmp, gas.selection_file)
	except OSError:
		try:
			os.unlink(tmp)
		except OSError:
			pass
		raise
	return species


def _gas_options(path):
	with open(path, "rb") as f:
		modules = tomllib.load(f).get("module", [])
	tables = [m["options"]["gas"] for m in modules if type(m) is dict and "gas" in m.get("options", {})]
	if len(tables) != 1:
		raise ValueError(f"expected one [[module]] with a [module.options.gas] table, found {len(tables)}")
	return parse_gas(tables[0])


def main(argv=None):
	parser = argparse.ArgumentParser(description="Show or change the gas selection of the pressure IOC on this machine.")
	parser.add_argument("--config", required=True, help="the pressure IOC's TOML config file")
	parser.add_argument("species", nargs="?", help="the gas to select, upper or lower case; leave out to show the selection")
	args = parser.parse_args(argv)
	try:
		gas = _gas_options(args.config)
		if args.species is not None:
			print(f"gas selection: {write_selection(gas, args.species)}")
			return 0
		selection = read_selection(gas)
	except (OSError, ValueError) as e:  # tomllib.TOMLDecodeError is a ValueError
		print(f"{parser.prog}: {e}", file=sys.stderr)
		return 1
	if selection is None:
		print(f"gas selection: {gas.default} (the default; no gas has been selected)")
	else:
		since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(selection.since))
		print(f"gas selection: {selection.species} (since {since}, by {selection.by})")
	print(f"choices: {', '.join(gas.species)}")
	return 0


if __name__ == "__main__":
	sys.exit(main())
