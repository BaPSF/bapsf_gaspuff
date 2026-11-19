"""The read-only browser page of the pressure IOC.

	python pressure_web.py --prefix LAPD:MACH:PRESSURE --source TPG1 --gas-prefix LAPD:MACH:GAS --store-dir /var/lib/pressure

A PVA client of the IOC on this machine, and its own process: the IOC and the store never wait on it.

- It monitors the group <prefix>:<source>:SAMPLE, one update per reading, keeps the last LIVE_SECONDS in memory and
  sends each browser a frame every FRAME_PERIOD_S with the readings since that browser's last frame. A browser that
  falls behind gets them in its next frame and holds up nobody else.
- What a gauge is called, its model, its limits and the gas choices come from the IOC's records too, so the page
  needs no config file and follows an IOC restart by itself. The gauges are the ones in the group.
- History comes from the store's day files through pressure_db.history(), read-only. The page keeps no copy.
- It never writes a PV and the page sends nothing back: the gas row only shows the selection made with
  pressure_gas.py on the IOC's machine.

Needs p4p, fastapi, uvicorn and websockets (constraints.txt), and diag_ioc for the log format.
"""
import argparse
import asyncio
import collections
import concurrent.futures
import contextlib
import itertools
import json
import logging
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from diag_ioc.outage import LOG_FORMAT, Outage
from web_shell import (LOG_LINES_PER_MIN, SEND_TIMEOUT_S, TRY_AGAIN_LATER, RateLimiter, ThrottledLog, Viewers, client_address,
                       server_settings)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "pressure_web_static"
DEFAULT_PORT = 8081  # deploy/nginx/ proxies /diagnostic/pressure/ to it
ZONE = "America/Los_Angeles"  # the page shows every time in this zone, never the browser's; the store's day files use it too
LIVE_SECONDS = 300.0  # the live chart: every reading of this long
LIVE_ROWS_MAX = 20000  # readings held; over 66 a second the live chart covers less than LIVE_SECONDS
FRAME_PERIOD_S = 0.5  # also how soon a socket notices its browser left: only a send can tell
WAITING_AFTER_S = 3.0  # the page says "Waiting" this long after the last reading (the IOC's stale_seconds)
HISTORY_POINTS_MAX = 2000
HISTORY_SPAN_MAX_S = 32 * 86400.0  # the longest range button is 30 days
HISTORY_RATE_PER_S = 2.0  # /api/history requests per client address
HISTORY_BURST = 5
HISTORY_WAITING_MAX = 4  # history reads running or queued; one more gets 503
# EPICS alarm severity of <gauge>:BASE -> the tile's colour. INVALID (3) is a base that is not ready yet.
_BASE_LEVELS = {1: "warn", 2: "alarm"}


@dataclass(frozen=True)
class Names:
	"""Which IOC records the page reads."""
	prefix: str  # the pressure module's prefix, e.g. LAPD:MACH:PRESSURE
	source: str  # the source key, e.g. TPG1
	gas_prefix: str  # the gas selection's prefix, e.g. LAPD:MACH:GAS

	@property
	def group(self):
		return f"{self.prefix}:{self.source}:SAMPLE"

	def machine_pvs(self):
		"""{PV name: what it is} of the records that are not about one gauge."""
		return {f"{self.gas_prefix}:SPECIES": "species", f"{self.gas_prefix}:SINCE": "gas_since",
		        f"{self.prefix}:BASE_WINDOW_H": "base_window_h", f"{self.prefix}:{self.source}:PERIOD_S": "period_s"}

	def gauge_pvs(self, key):
		"""{PV name: what it is} of gauge `key`'s records outside the group."""
		return {f"{self.prefix}:{key}:{record}": what for record, what in (
			("NAME", "name"), ("CHANNEL", "channel"), ("MODEL", "model"), ("DEVICE_GAS", "device_gas"), ("BASE", "base"))}


@dataclass(frozen=True)
class Reading:
	"""One reading of the source. gauges is aligned with `keys`: (p, p_raw, gas_factor, status, base) of each gauge,
	pressures in Torr, NaN for none."""
	keys: tuple
	time: float  # epoch s, the IOC machine's clock
	seq: int
	gas: str
	gauges: tuple
	index: int = 0  # counts the readings the Hub has taken; set there


def parse_sample(value):
	"""Reading of one <prefix>:<source>:SAMPLE value given as nested dicts (p4p's Value.todict()); None for the IOC's
	state before its first reading. A gauge is every sub-structure that holds p and status, so the group's own alarm
	and timeStamp are passed over; the field g1 is the gauge key G1."""
	when = float(value["time"])
	if not (math.isfinite(when) and when > 0):
		return None
	keys, gauges = [], []
	for name, field in value.items():
		if isinstance(field, dict) and "p" in field and "status" in field:
			keys.append(name.upper())
			gauges.append((float(field["p"]), float(field["p_raw"]), float(field["gas_factor"]), int(field["status"]),
			               float(field["base"])))
	if not keys:
		raise ValueError("the group holds no gauge")
	return Reading(tuple(keys), when, int(value["seq"]), str(value["gas"]), tuple(gauges))


def _number(value):
	"""JSON has no NaN or infinity: JSON.parse rejects them."""
	return float(value) if value is not None and math.isfinite(value) else None


def _limit(value):
	"""An alarm limit of <gauge>:BASE, or None for one that is not set (the record then holds 0 or NaN)."""
	return float(value) if isinstance(value, (int, float)) and math.isfinite(value) and value > 0 else None


def _plain(value):
	"""A p4p Value as nested dicts; anything else (a test's dict) as it is."""
	return value.todict() if hasattr(value, "todict") else value


def history_message(result):
	"""The /api/history response of what pressure_db.history() returned: {"t": [...], "gauges": {key: {"mean": [...],
	"min": [...], "max": [...]}}, "gas": [[epoch s, species], ...]}. ValueError if a series is not as long as "t"."""
	t = [float(v) for v in result["t"]]
	gauges = {}
	for key, series in result["gauges"].items():
		gauges[str(key)] = {name: [_number(v) for v in series[name]] for name in ("mean", "min", "max") if name in series}
		for name, values in gauges[str(key)].items():
			if len(values) != len(t):
				raise ValueError(f"{key} {name}: {len(values)} values for {len(t)} times")
	return {"t": t, "gauges": gauges, "gas": [[float(when), str(species)] for when, species in result.get("gas", ())]}


class Hub:
	"""The last LIVE_SECONDS of readings and what the page shows beside them. Event loop only, so no lock.

	Each /ws socket keeps the state frame() returned and passes it back, so every socket gets each reading once however
	slowly it reads."""

	def __init__(self, names, has_history, watch_gauge=lambda key: None):
		self.names = names
		self.has_history = has_history
		self.viewers = Viewers()
		self.keys = ()  # the gauge keys of the group, in its field order: the columns of a frame's "p"
		self.latest = None
		self.watch_gauge = watch_gauge  # called for each gauge key the group shows: start monitoring its records
		self._rows = collections.deque()
		self._gas_changes = collections.deque()  # (time, species) of each change of gas inside _rows
		self._index = 0
		self._epoch = 0  # counts the times _rows started again; a socket on an older one is told to reset
		self._machine = {}
		self._gauges = {}  # gauge key: what its records say
		self._meta_version = 0
		self._bad_pv = Outage(log, "reading a record of the IOC")

	def add(self, reading):
		"""Take one reading. The same one again (the IOC resends its last to a new monitor) is passed over."""
		latest = self.latest
		if latest is not None and (reading.time, reading.seq) == (latest.time, latest.seq):
			return
		if reading.keys != self.keys or (latest is not None and reading.time <= latest.time):
			# Other gauges (the IOC restarted with another config), or a clock set back: the chart starts again.
			self._rows.clear()
			self._gas_changes.clear()
			self._epoch += 1
			if reading.keys != self.keys:
				new = [key for key in reading.keys if key not in self.keys]
				self.keys = reading.keys
				self._meta_version += 1
				for key in new:
					self.watch_gauge(key)
		if self._rows and reading.gas != self._rows[-1].gas:
			self._gas_changes.append((reading.time, reading.gas))
		self._index += 1
		reading = Reading(reading.keys, reading.time, reading.seq, reading.gas, reading.gauges, self._index)
		self._rows.append(reading)
		self.latest = reading
		while self._rows[0].time < reading.time - LIVE_SECONDS or len(self._rows) > LIVE_ROWS_MAX:
			self._rows.popleft()
		while self._gas_changes and self._gas_changes[0][0] < self._rows[0].time:
			self._gas_changes.popleft()

	def set_pv(self, key, what, value):
		"""Take the value (nested dicts) of one record beside the group: `what` of gauge `key`, or of the machine for
		key None. A value that cannot be read changes nothing."""
		try:
			new = self._read_pv(what, value)
		except Exception as e:
			self._bad_pv.failed(e, f"{what} of {key or 'the machine'} not shown")
			return
		self._bad_pv.ended()
		held = self._machine if key is None else self._gauges.setdefault(key, {})
		if any(held.get(name) != item for name, item in new.items()):
			held.update(new)
			self._meta_version += 1

	@staticmethod
	def _read_pv(what, value):
		item = value["value"]
		severity = int(value.get("alarm", {}).get("severity", 0))
		if what == "species":  # an enum: the choices are the species, in the IOC's order
			choices = [str(choice) for choice in item["choices"]]
			index = int(item["index"])
			return {"species": choices, "selected": choices[index] if 0 <= index < len(choices) else None}
		if what in ("gas_since", "base_window_h", "period_s"):
			return {what: _limit(item)}
		if what == "channel":
			return {"channel": int(item)}
		if what == "device_gas":  # the IOC raises an alarm on it when the controller corrects for a gas itself
			return {"device_gas": str(item).strip(), "device_gas_alarm": severity != 0}
		if what == "base":
			limits = value.get("valueAlarm", {})
			return {"base_level": _BASE_LEVELS.get(severity, ""), "warn": _limit(limits.get("highWarningLimit")),
			        "alarm": _limit(limits.get("highAlarmLimit"))}
		return {what: str(item).strip()}  # name, model

	def meta(self):
		"""What the page shows that is not a reading. The gauges are in the order of their controller channel."""
		gauges = []
		for key in self.keys:
			held = self._gauges.get(key, {})
			gauges.append({"key": key, "name": held.get("name", ""), "channel": held.get("channel"), "model": held.get("model", ""),
			               "device_gas": held.get("device_gas", ""), "device_gas_alarm": held.get("device_gas_alarm", False),
			               "base_level": held.get("base_level", ""), "warn": held.get("warn"), "alarm": held.get("alarm")})
		gauges.sort(key=lambda gauge: (gauge["channel"] is None, gauge["channel"] or 0, gauge["key"]))
		machine = self._machine
		return {"zone": ZONE, "source": self.names.source, "live_seconds": LIVE_SECONDS, "history": self.has_history,
		        "species": machine.get("species", []), "gas_since": machine.get("gas_since"),
		        "base_window_h": machine.get("base_window_h"), "period_s": machine.get("period_s"), "gauges": gauges}

	def frame(self, state=None):
		"""(text, state): the next /ws frame for a socket whose last frame left `state`; None for its first.

		The frame carries the readings the socket has not had ("t", and "p" with one list per key of "keys": the
		pressure corrected for gas), the newest reading in full for the tiles, and "meta" when it changed. "reset"
		tells the page to drop the readings it holds. Times are this machine's clock, never the browser's."""
		cursor, epoch, meta_version = state or (0, None, None)
		rows, latest, now = self._rows, self.latest, time.time()
		reset = epoch != self._epoch
		count = len(rows) if reset else min(len(rows), self._index - cursor)
		new = list(itertools.islice(rows, len(rows) - count, None))
		age = None if latest is None else max(0.0, now - latest.time)
		message = {
			"now": now, "age_s": age, "waiting": age is None or age >= WAITING_AFTER_S, "reset": reset, "keys": self.keys,
			"t": [row.time for row in new],
			"p": [[_number(row.gauges[column][0]) for row in new] for column in range(len(self.keys))],
			"gas": latest.gas if latest is not None else self._machine.get("selected"),
			"gas_changes": [list(change) for change in self._gas_changes],
			"latest": None if latest is None else {"time": latest.time, "gauges": {
				key: {"p": _number(p), "p_raw": _number(p_raw), "gas_factor": _number(factor), "status": status, "base": _number(base)}
				for key, (p, p_raw, factor, status, base) in zip(latest.keys, latest.gauges)}},
		}
		if meta_version != self._meta_version:
			message["meta"] = self.meta()
		return json.dumps(message, allow_nan=False), (self._index, self._epoch, self._meta_version)


class IocFeed:
	"""The p4p callbacks. They only convert and hand over to the event loop, and must not raise: p4p closes a
	subscription whose callback raises."""

	def __init__(self, names, loop, hub):
		self.names, self.loop, self.hub = names, loop, hub
		self._watch = None  # watch(PV name, callback) while the monitors run
		self._gauges = set()  # the gauge keys whose records are monitored
		self._connected = None  # None until p4p's first notice
		self._monitor = Outage(log, f"PVA monitor of {names.group}")
		self._reads = Outage(log, f"reading {names.group}")

	def start(self, watch):
		"""Monitor the group and the machine's records through watch(PV name, callback); a gauge's records follow
		when the group first shows the gauge (watch_gauge, on the event loop)."""
		self._watch = watch
		watch(self.names.group, self.on_sample)
		for name, what in self.names.machine_pvs().items():
			watch(name, lambda value, what=what: self.on_pv(None, what, value))

	def stop(self):
		self._watch = None

	def watch_gauge(self, key):
		if self._watch is not None and key not in self._gauges:
			self._gauges.add(key)
			for name, what in self.names.gauge_pvs(key).items():
				self._watch(name, lambda value, what=what: self.on_pv(key, what, value))

	def on_sample(self, value):
		"""A notice of the group: a value, or an exception (Disconnected, RemoteError). The page goes to its waiting
		state by itself when readings stop."""
		if isinstance(value, Exception):
			# Only p4p's very first notice is skipped, always a Disconnected: an error before any connection must reach the log.
			if self._connected is not None:
				self._monitor.failed(value)
			self._connected = False
			return
		if not self._connected:
			self._connected = True
			if self._monitor.since is None:
				log.info("%s connected", self.names.group)
			else:
				self._monitor.ended()
		try:
			reading = parse_sample(_plain(value))
		except Exception as e:  # a group without these fields: another module or layout
			self._reads.failed(e, "update not shown", exc_info=True)
			return
		self._reads.ended()
		if reading is not None:
			self.loop.call_soon_threadsafe(self.hub.add, reading)

	def on_pv(self, key, what, value):
		"""A notice of one record beside the group. A record that is not there, or an IOC that is down, changes
		nothing: the page keeps what it has."""
		if isinstance(value, Exception):
			return
		try:
			value = _plain(value)
		except Exception as e:
			log.warning("unreadable %s of %s (%s)", what, key or "the machine", e)
			return
		self.loop.call_soon_threadsafe(self.hub.set_pv, key, what, value)


def monitor_ioc(feed):
	"""Start the p4p monitors `feed` asks for; returns a function that stops them."""
	from p4p.client.thread import Context

	ctx = Context("pva", nt=False)  # EPICS_PVA_* from the environment; nt=False: every value arrives as its plain structure
	subscriptions = []
	feed.start(lambda name, callback: subscriptions.append(ctx.monitor(name, callback, notify_disconnect=True)))
	log.info("monitoring %s", feed.names.group)

	def stop():
		feed.stop()
		for subscription in subscriptions:
			subscription.close()
		ctx.close()

	return stop


def store_history():
	"""pressure_db.history, the store's reader, or None where the store's code is not installed."""
	try:
		import pressure_db
		return pressure_db.history
	except (ImportError, AttributeError) as e:
		log.warning("the page has no history: %s: %s", type(e).__name__, e)
		return None


def _within_send_timeout(awaitable):
	"""Every /ws send goes through this: a browser that stopped reading blocks a send until the buffer drains."""
	return asyncio.wait_for(awaitable, SEND_TIMEOUT_S)


def create_app(names, store_dir=None, history=None, monitor=monitor_ioc):
	"""`history(directory, source, start, end, buckets, corrected=...)` is the store's reader (pressure_db.history),
	used when `store_dir` is given too. `monitor(feed)` starts the data in and returns its stop function; tests pass
	one without p4p."""
	from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
	from fastapi.responses import FileResponse, JSONResponse
	from fastapi.staticfiles import StaticFiles

	if store_dir is None:
		history = None
	hub = Hub(names, history is not None)
	history_limit = RateLimiter(HISTORY_RATE_PER_S, HISTORY_BURST)
	history_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="pressure_web history")
	history_waiting = 0
	history_outage = Outage(log, f"reading history from {store_dir}")
	refused_log = log.getChild("refused")  # uvicorn logs a refused /ws socket only as "[accepted]"
	refused_log.addFilter(ThrottledLog(LOG_LINES_PER_MIN))

	@contextlib.asynccontextmanager
	async def lifespan(app):
		feed = IocFeed(names, asyncio.get_running_loop(), hub)
		hub.watch_gauge = feed.watch_gauge
		stop = monitor(feed)
		try:
			yield
		finally:
			stop()
			history_pool.shutdown(wait=False, cancel_futures=True)

	# No openapi_url, so no generated docs either: they load from a CDN the lab network may not reach.
	app = FastAPI(lifespan=lifespan, openapi_url=None)
	app.state.hub = hub
	app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

	@app.get("/")
	async def page():
		return FileResponse(STATIC_DIR / "index.html")

	@app.get("/api/history")
	async def api_history(request: Request, start: float, end: float, points: int = Query(ge=1, le=HISTORY_POINTS_MAX),
	                      raw: bool = False):
		"""Bucket means of [start, end) from the store's files: corrected for gas, or with raw as the gauges sent them."""
		nonlocal history_waiting
		if history is None:
			raise HTTPException(404, "this server has no history: the store is not installed")
		if not history_limit.allow(client_address(request)):
			raise HTTPException(429, "too many history requests from this address", headers={"Retry-After": "1"})
		if not (math.isfinite(start) and math.isfinite(end) and start < end and end - start <= HISTORY_SPAN_MAX_S):
			raise HTTPException(422, f"start and end must be finite, with start < end, at most {HISTORY_SPAN_MAX_S / 86400:.0f} days apart")
		if history_waiting >= HISTORY_WAITING_MAX:
			raise HTTPException(503, "the server is busy reading history; try again", headers={"Retry-After": "2"})
		history_waiting += 1
		try:
			# One file read at a time, off the event loop: a slow disk holds up neither the frames nor the p4p hand-over.
			result = await asyncio.get_running_loop().run_in_executor(
				history_pool, lambda: history_message(history(store_dir, names.source, start, end, points, corrected=not raw)))
		except Exception as e:
			history_outage.failed(e, exc_info=True)
			raise HTTPException(500, "the history could not be read") from None
		finally:
			history_waiting -= 1
		history_outage.ended()
		return JSONResponse(result)  # JSONResponse here, or FastAPI's encoder walks every number once more

	@app.websocket("/ws")
	async def stream(websocket: WebSocket):
		"""Never receives. Ends when the browser leaves or a send takes over SEND_TIMEOUT_S; uvicorn then closes the
		connection."""
		address = client_address(websocket)
		with hub.viewers.admitted(address) as refusal, contextlib.suppress(WebSocketDisconnect, TimeoutError):
			# Accepted even when refused: only an accepted socket carries a close code and reason to the page.
			await _within_send_timeout(websocket.accept())
			if refusal is not None:
				refused_log.info("/ws from %s refused: %s", address, refusal)
				await _within_send_timeout(websocket.close(TRY_AGAIN_LATER, refusal))
				return
			state = None
			while True:
				text, state = hub.frame(state)
				await _within_send_timeout(websocket.send_text(text))
				await asyncio.sleep(FRAME_PERIOD_S)

	return app


def main(argv=None):
	parser = argparse.ArgumentParser(prog="pressure-web", description="read-only browser page of the pressure IOC on this machine")
	parser.add_argument("--prefix", required=True, help="the pressure module's PV prefix (its config's prefix)")
	parser.add_argument("--source", required=True, help="the source key; the group is <prefix>:<source>:SAMPLE")
	parser.add_argument("--gas-prefix", required=True, help="the gas selection's PV prefix ([module.options.gas] prefix)")
	parser.add_argument("--store-dir", type=Path, help="the store's folder of day files; without it the page has no history")
	parser.add_argument("--host", default="127.0.0.1", help="address to serve on (default: loopback, for the proxy on this machine)")
	parser.add_argument("--port", type=int, default=DEFAULT_PORT)
	args = parser.parse_args(argv)
	logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)  # under systemd the journal keeps it
	import uvicorn

	logging.getLogger("uvicorn.error").addFilter(ThrottledLog(LOG_LINES_PER_MIN))
	names = Names(args.prefix, args.source, args.gas_prefix)
	history = store_history() if args.store_dir is not None else None
	uvicorn.run(create_app(names, args.store_dir, history), **server_settings(args.host, args.port))
	return 0


if __name__ == "__main__":
	sys.exit(main())
