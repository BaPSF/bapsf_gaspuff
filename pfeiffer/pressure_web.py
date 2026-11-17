"""pressure_web --config FILE --dir DIR: the read-only browser page of the pressure monitor.

	python pressure_web.py --config /etc/diag-ioc/pressure.toml --dir /var/lib/pressure

FILE is the IOC's own config (the prefix, the source, the gauges, their limits and multipliers) and DIR the store's
directory of day files. Serves 127.0.0.1:8081 (PRESSURE_WEB_HOST, PRESSURE_WEB_PORT); the lab opens it through nginx
(deploy/nginx/) at http://<lab address>/diagnostic/pressure/.

- Live: monitors the PVA group <P>:<S>:SAMPLE (one complete update per reading) and the single PVs of the gas
  selection, the controller link and the gauge types. The last LIVE_S seconds of readings are kept in memory and sent
  to each page over one WebSocket, a batch at most every SEND_PERIOD_S, with a status frame at least every second.
- History: GET api/history reads the store's day files with pressure_db.history(), read-only, one query at a time.
  The page never writes anything: no PV, no file, no gas selection (that is pressure_gas.py on the IOC's machine).
- Its own process: the IOC and the store never wait on it. The p4p callbacks only hand values to the event loop.
- Limits as on the interferometer page: 20 viewers, 3 pages per address, rate-limited history requests.

Needs the diag_ioc package (bapsf_epics_core) with p4p, fastapi, uvicorn and websockets, and pfeiffer/ on the path.
"""
import argparse
import asyncio
import collections
import concurrent.futures
import contextlib
import dataclasses
import itertools
import json
import logging
import math
import os
import socket
import sqlite3
import sys
import time
import types
from pathlib import Path

import numpy as np

import pressure_db
from diag_ioc.outage import LOG_FORMAT, Outage
from pressure_store import STATUS_TEXT, load_layout, pv_value

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "pressure_web_static"
LIVE_S = 300.0  # the live chart: every reading of the last 5 minutes
SEND_PERIOD_S = 0.5  # new readings go to a page in batches, at most this often: 5 readings at 10 a second
STATUS_PERIOD_S = 1.0  # a status frame at least this often, readings or not; also how soon a socket notices its browser left
WAITING_AFTER_S = 3.0  # no reading for this long: the page says it is waiting (the example config's stale_seconds)
HISTORY_POINTS_MAX = 2000
HISTORY_RANGE_MAX_S = 400 * 86400.0
HISTORY_QUEUE_MAX = 8  # history queries waiting for the one reader thread; more get 503
STOP_TIMEOUT_S = 5  # for open sockets at exit; under the unit's TimeoutStopSec (30 s)

# Limits, the interferometer page's (bapsf_interferometer interf_web, D18): about 20 viewers on a machine that does
# other work.
MAX_VIEWERS = 20  # open /ws sockets in all; more are closed with TRY_AGAIN_LATER
MAX_VIEWERS_PER_ADDRESS = 3
TRY_AGAIN_LATER = 1013  # the WebSocket close code for a refused socket
# uvicorn's limit_concurrency counts every open connection, /ws and idle keep-alive ones included; beyond it an HTTP
# request gets 503. A viewer holds 2 (its /ws, and its api/history connection) plus the few its browser opens to load
# the page. uvicorn does not apply the limit to a WebSocket upgrade, hence MAX_VIEWERS.
MAX_CONNECTIONS = 4 * MAX_VIEWERS
KEEP_ALIVE_S = 30  # a connection whose next request has not fully arrived this long after the last response is closed
SEND_TIMEOUT_S = 30.0  # a /ws send that cannot finish (a browser that stopped reading) ends that socket
TCP_USER_TIMEOUT_S = 90  # Linux: the kernel drops a connection whose sent data stays unacknowledged this long
WS_MAX_SIZE = 4096  # bytes of an incoming WebSocket message; the page sends none
PING_INTERVAL_S = 30.0
PING_TIMEOUT_S = 60.0
HISTORY_RATE_PER_S = 2.0  # api/history requests per client address
HISTORY_BURST = 5
LOG_LINES_PER_MIN = 30  # per throttled log: uvicorn's, refused /ws


#===================================================================================================================================================
# PV updates -> plain values. `value` is a p4p Value with unwrapping off (Context(nt=False)), or the same fields as
# nested dicts.

@dataclasses.dataclass(frozen=True)
class GaugeReading:
	p_raw: float  # Torr, as the gauge sent it; NaN without a pressure
	gas_factor: float
	p: float  # p_raw x gas_factor
	status: int
	raw_status: int
	base: float  # Torr, the IOC's base pressure (lowest 1-minute mean of p_raw); NaN until ready


@dataclasses.dataclass(frozen=True)
class Reading:
	"""One <P>:<S>:SAMPLE update."""
	seq: int
	time: float  # epoch s, the IOC host's clock
	gas: str
	read_s: float
	gauges: tuple  # GaugeReading, in the order of the configured gauges


def reading_from_group(value, gauge_keys):
	"""Reading of one group update, or None for the IOC's state before its first sample after a start (seq or time
	still 0). Every field comes from this one update."""
	seq, time_s = int(value["seq"]), float(value["time"])
	if seq == 0 or time_s == 0:
		return None
	if not math.isfinite(time_s):
		raise ValueError(f"sample {seq} has the time {time_s}")
	gauges = []
	for key in gauge_keys:
		g = value[key.lower()]
		gauges.append(GaugeReading(float(g["p_raw"]), float(g["gas_factor"]), float(g["p"]), int(g["status"]), int(g["raw_status"]),
		                           float(g["base"])))
	return Reading(seq, time_s, str(value["gas"]), float(value["read_s"]), tuple(gauges))


def _number(value):
	"""JSON has no NaN or infinity: JSON.parse rejects them."""
	return value if math.isfinite(value) else None


def _short(value):
	"""A pressure for the page: 4 significant digits, which the page shows 3 of."""
	return float(f"{value:.4g}") if math.isfinite(value) else None


def single_pvs(layout):
	"""{PV name: (what, key)} of the single PVs the page monitors: "gas" (key species/since/by), "source" (connected,
	error) or (gauge key, model/device_gas)."""
	s, gas = f"{layout.prefix}:{layout.source.key}", layout.options.gas.prefix
	names = {f"{gas}:{suffix}": ("gas", suffix.lower()) for suffix in ("SPECIES", "SINCE", "BY")}
	names.update({f"{s}:CONNECTED": ("source", "connected"), f"{s}:ERROR": ("source", "error")})
	for key in layout.gauge_keys:
		names.update({f"{layout.prefix}:{key}:MODEL": (key, "model"), f"{layout.prefix}:{key}:DEVICE_GAS": (key, "device_gas")})
	return names


def page_info(layout):
	"""What the page needs from the config, sent once (api/info): nothing in it changes while this process runs."""
	options = layout.options
	return {
		"prefix": layout.prefix, "source": layout.source.key, "period_s": layout.source.period_s, "live_s": LIVE_S,
		"base_window_h": options.base_window_h, "base_ready_min": options.base_ready_min, "species": list(options.gas.species),
		"gas_prefix": options.gas.prefix, "time_zone": pressure_db.TIMEZONE, "waiting_after_s": WAITING_AFTER_S,
		"history_points_max": HISTORY_POINTS_MAX, "history_range_max_s": HISTORY_RANGE_MAX_S, "sample_range_s": pressure_db.SAMPLE_RANGE_S,
		"status_text": {str(code): text for code, text in STATUS_TEXT.items()},
		"gauges": [{"key": g.key, "name": g.name, "channel": g.channel, "pv": f"{layout.prefix}:{g.key}:P",
		            "warning": g.base_warning_torr, "alarm": g.base_alarm_torr,
		            "multipliers": dict(zip(options.gas.species, g.multiplier))} for g in options.gauges],
	}


#===================================================================================================================================================
# The live data: event loop only, so no lock

class Live:
	"""The readings of the last `seconds`, each as the JSON text of one chart row [time, gas index, p of each gauge],
	numbered n = 1, 2, ... A clock that goes back clears the rows and starts a new generation, so every page is sent
	the rows afresh and a chart's times only grow."""

	def __init__(self, species, seconds=LIVE_S):
		self.seconds = seconds
		self.generation = 0
		self.n = 0  # the number of the newest row
		self._species = {name: i for i, name in enumerate(species)}
		self._rows = collections.deque()  # (n, time, text), oldest first

	def add(self, reading):
		if self._rows and reading.time <= self._rows[-1][1]:
			self._rows.clear()
			self.generation += 1
		self.n += 1
		row = [round(reading.time, 3), self._species.get(reading.gas, -1), *(_short(g.p) for g in reading.gauges)]
		self._rows.append((self.n, reading.time, json.dumps(row, allow_nan=False, separators=(",", ":"))))
		while self._rows[0][1] < reading.time - self.seconds:
			self._rows.popleft()

	def after(self, sent):
		"""(texts, mark, reset) for a page that holds the rows up to `sent`, a mark from a previous call or None: the rows
		it lacks, or with reset every row (a new page, a new generation, or one that fell behind the rows kept)."""
		mark = (self.generation, self.n)
		first = self._rows[0][0] if self._rows else self.n + 1
		if sent is None or sent[0] != self.generation or sent[1] + 1 < first:
			return [text for _, _, text in self._rows], mark, True
		return [text for _, _, text in itertools.islice(self._rows, sent[1] + 1 - first, None)], mark, False


class Hub:
	"""The newest reading and PV values, and the viewers. Event loop only, except that the feed's callbacks reach it
	through call_soon_threadsafe."""

	def __init__(self, layout, clock=time.time):
		self.layout = layout
		self.live = Live(layout.options.gas.species)
		self.latest = None  # the newest Reading
		self.ioc = False  # the group monitor is connected
		self.pvs = {}  # {(what, key): value} of the single PVs
		self.viewers = collections.Counter()  # client address: its open /ws sockets
		self._clock = clock
		self._changed = asyncio.Event()

	@contextlib.contextmanager
	def admitted(self, address):
		"""Yields None while holding one of the MAX_VIEWERS slots for `address`, or why it is refused (the close reason the
		page shows). Check and count happen with no await between, so two sockets cannot both take the last slot."""
		if self.viewers.total() >= MAX_VIEWERS:
			yield f"the page already has {MAX_VIEWERS} viewers; try again later"
		elif self.viewers[address] >= MAX_VIEWERS_PER_ADDRESS:
			yield f"{MAX_VIEWERS_PER_ADDRESS} pages are already open from this address; try again later"
		else:
			self.viewers[address] += 1
			try:
				yield None
			finally:
				self.viewers -= collections.Counter([address])  # drops the address at 0

	def publish(self, reading):
		self.latest = reading
		self.live.add(reading)
		self._notify()

	def set_pv(self, what, value):
		self.pvs[what] = value
		self._notify()

	def set_ioc(self, connected):
		self.ioc = connected
		self._notify()

	def _notify(self):
		changed, self._changed = self._changed, asyncio.Event()
		changed.set()

	async def wait_change(self, timeout):
		with contextlib.suppress(TimeoutError):
			await asyncio.wait_for(self._changed.wait(), timeout)

	def status(self):
		"""The status part of every frame, as JSON text. Times are the IOC host's clock, never the browser's."""
		now, r, pvs = self._clock(), self.latest, self.pvs
		age = None if r is None else max(0.0, now - r.time)
		connected = pvs.get(("source", "connected"))
		gauges = {}
		for i, key in enumerate(self.layout.gauge_keys):
			g = r.gauges[i] if r is not None else None
			gauges[key] = {"model": pvs.get((key, "model")), "device_gas": pvs.get((key, "device_gas"))}
			if g is not None:
				gauges[key].update(p=_number(g.p), p_raw=_number(g.p_raw), gas_factor=_number(g.gas_factor), status=g.status,
				                   raw_status=g.raw_status, base=_number(g.base))
		since = pvs.get(("gas", "since"))
		return json.dumps({
			"now": now, "age_s": age, "waiting": age is None or age >= WAITING_AFTER_S, "ioc": self.ioc,
			"connected": None if connected is None else connected == "yes", "error": pvs.get(("source", "error")) or "",
			"gas": {"species": pvs.get(("gas", "species")), "since": since if since else None, "by": pvs.get(("gas", "by")) or ""},
			"sample": None if r is None else {"seq": r.seq, "time": r.time, "gas": r.gas, "read_s": _number(r.read_s)},
			"gauges": gauges,
		}, allow_nan=False, separators=(",", ":"))


class Feed:
	"""The p4p callbacks. They never block and must not raise: p4p closes a subscription whose callback raises."""

	def __init__(self, layout, loop, hub):
		self.layout, self.loop, self.hub = layout, loop, hub
		self.group = f"{layout.prefix}:{layout.source.key}:SAMPLE"
		self.pvs = single_pvs(layout)
		self._last = None  # (seq, time) of the last reading passed on
		self._connected = None  # None until p4p's first notice
		self._monitor = Outage(log, f"PVA monitor of {self.group}")
		self._reads = Outage(log, f"reading {self.group}")
		self._pv_reads = Outage(log, "reading a PV update")

	def on_group(self, value):
		"""A notice of the group monitor: a value, or an exception (Disconnected, which p4p also sends once before the
		first connection; RemoteError)."""
		if isinstance(value, Exception):
			if self._connected is not None:
				self._monitor.failed(value)
			if self._connected is not False:
				self.loop.call_soon_threadsafe(self.hub.set_ioc, False)
			self._connected = False
			return
		if not self._connected:
			self._connected = True
			if self._monitor.since is None:
				log.info("%s connected", self.group)
			else:
				self._monitor.ended()
			self.loop.call_soon_threadsafe(self.hub.set_ioc, True)
		try:
			reading = reading_from_group(value, self.layout.gauge_keys)
		except Exception as e:  # a group without these fields: the IOC runs another config
			self._reads.failed(e, "update not shown", exc_info=True)
			return
		self._reads.ended()
		if reading is None or (reading.seq, reading.time) == self._last:  # the same reading again after a reconnect
			return
		self._last = (reading.seq, reading.time)
		self.loop.call_soon_threadsafe(self.hub.publish, reading)

	def on_pv(self, name, value):
		"""A notice of the monitor of the single PV `name`. Disconnects are left to the group's monitor."""
		if isinstance(value, Exception):
			return
		try:
			text = pv_value(value)
		except Exception as e:
			self._pv_reads.failed(e, f"{name}: update not used", exc_info=True)
			return
		self._pv_reads.ended()
		self.loop.call_soon_threadsafe(self.hub.set_pv, self.pvs[name], text)


def monitor_ioc(feed):
	"""Start the p4p monitors; returns a function that stops them."""
	from p4p.client.thread import Context  # bapsf_epics_core's clients extra

	ctx = Context("pva", nt=False)  # EPICS_PVA_* from the environment
	subscriptions = [ctx.monitor(feed.group, feed.on_group, notify_disconnect=True)]
	subscriptions += [ctx.monitor(name, lambda value, name=name: feed.on_pv(name, value)) for name in feed.pvs]
	log.info("monitoring %s and %d single PVs", feed.group, len(feed.pvs))

	def stop():
		for subscription in subscriptions:
			subscription.close()
		ctx.close()

	return stop


#===================================================================================================================================================
# History

def history_json(directory, source, start, end, points, corrected):
	"""pressure_db.history() as the JSON text api/history returns: {"t": [...], "gauges": {key: {"mean", "min", "max":
	[...]}}, "gas": [[epoch s, species], ...], "from": "readings" or "minutes"}; null where a gauge has no pressure."""
	h = pressure_db.history(directory, source, start, end, points, corrected=corrected)

	def numbers(array):
		return [_number(v) for v in np.asarray(array, dtype=float).tolist()]

	return json.dumps({"t": [round(t, 3) for t in h["t"].tolist()],
	                   "gauges": {key: {k: numbers(v) for k, v in values.items()} for key, values in h["gauges"].items()},
	                   "gas": h["gas"], "from": "readings" if end - start <= pressure_db.SAMPLE_RANGE_S else "minutes"},
	                  allow_nan=False, separators=(",", ":"))


#===================================================================================================================================================
# The server. These parts are the interferometer page's (interf_web), copied; both can move to bapsf_epics_core.

class RateLimiter:
	"""A token bucket per key: `rate` per s on average, up to `burst` at once. Event loop only, so no lock."""

	def __init__(self, rate, burst):
		self.rate, self.burst = rate, burst
		self._buckets = {}  # key: (tokens, time of the last call)

	def allow(self, key):
		now = time.monotonic()
		if len(self._buckets) > 1024:  # forget keys whose bucket is full again, so a stream of addresses cannot grow it
			self._buckets = {k: b for k, b in self._buckets.items() if now - b[1] < self.burst / self.rate}
		tokens, last = self._buckets.get(key, (self.burst, now))
		tokens = min(self.burst, tokens + (now - last) * self.rate)
		allowed = tokens >= 1
		self._buckets[key] = (tokens - allowed, now)
		return allowed


class ThrottledLog(logging.Filter):
	"""On one logger: at most `per_minute` records below ERROR each minute, then a count of those dropped."""

	def __init__(self, per_minute, clock=time.monotonic):
		super().__init__()
		self.per_minute, self._clock = per_minute, clock
		self._start, self._passed, self._dropped = clock(), 0, 0

	def filter(self, record):
		if record.levelno >= logging.ERROR:
			return True
		now = self._clock()
		if now - self._start >= 60:
			if self._dropped:
				log.warning("%d lines of the %s log dropped in %.0f s (limit %d a minute)", self._dropped, record.name,
				            now - self._start, self.per_minute)
			self._start, self._passed, self._dropped = now, 0, 0
		if self._passed < self.per_minute:
			self._passed += 1
			return True
		self._dropped += 1
		return False


_refused_log = log.getChild("refused")
_refused_log.addFilter(ThrottledLog(LOG_LINES_PER_MIN))


def _timed_h11_protocol():
	"""uvicorn's h11 protocol whose keep-alive timer (KEEP_ALIVE_S) also covers each request until its headers are
	complete, plus TCP_USER_TIMEOUT_S. In uvicorn 0.54 that timer starts only after a response and any byte cancels it,
	so a connection that never completes a request would stay open. Overrides two of H11Protocol's methods: recheck on
	an upgrade of uvicorn."""
	from uvicorn.protocols.http.h11_impl import H11Protocol

	class TimedH11(H11Protocol):
		def connection_made(self, transport):
			super().connection_made(transport)
			self.timeout_keep_alive_task = self.loop.call_later(self.timeout_keep_alive, self.timeout_keep_alive_handler)
			sock = transport.get_extra_info("socket")
			if sock is not None and hasattr(socket, "TCP_USER_TIMEOUT"):
				sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, int(TCP_USER_TIMEOUT_S * 1000))

		def data_received(self, data):  # H11Protocol's, without its _unset_keepalive_if_required()
			self.conn.receive_data(data)
			self.handle_events()

	return TimedH11


def server_settings(host, port):
	"""uvicorn.Config keywords. main also throttles uvicorn's log (ThrottledLog)."""
	# ws and lifespan named, so trouble stops the start instead of serving a page with no data. X-Forwarded-For only
	# from the proxy on this host (deploy/nginx/): it gives the viewer's address, which the per-address limits count.
	return dict(host=host, port=port, proxy_headers=True, forwarded_allow_ips="127.0.0.1",
	            http=_timed_h11_protocol(), ws="websockets-sansio",
	            ws_per_message_deflate=False, ws_max_size=WS_MAX_SIZE, ws_ping_interval=PING_INTERVAL_S,
	            ws_ping_timeout=PING_TIMEOUT_S, limit_concurrency=MAX_CONNECTIONS, timeout_keep_alive=KEEP_ALIVE_S,
	            lifespan="on", log_config=None, access_log=False, timeout_graceful_shutdown=STOP_TIMEOUT_S)


def _address(connection):
	"""The client address a request or WebSocket counts against."""
	return connection.client.host if connection.client else ""


def _within_send_timeout(awaitable):
	return asyncio.wait_for(awaitable, SEND_TIMEOUT_S)


def create_app(layout, directory, monitor=monitor_ioc, clock=time.time):
	"""`monitor(feed)` starts the data in and returns its stop function; tests pass one without p4p."""
	from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
	from fastapi.responses import FileResponse, Response
	from fastapi.staticfiles import StaticFiles

	directory = Path(directory)
	info = json.dumps(page_info(layout), allow_nan=False)
	history_limit = RateLimiter(HISTORY_RATE_PER_S, HISTORY_BURST)
	history_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="pressure_web history")
	history_outage = Outage(log, f"reading the day files in {directory}")
	state = types.SimpleNamespace(hub=None, feed=None, queued=0)  # shared by the handlers; tests read it

	@contextlib.asynccontextmanager
	async def lifespan(app):
		state.hub = Hub(layout, clock)
		state.feed = Feed(layout, asyncio.get_running_loop(), state.hub)
		stop = monitor(state.feed)
		try:
			yield
		finally:
			stop()
			history_pool.shutdown(cancel_futures=True)

	# No openapi_url, so no generated docs either: they load from a CDN the lab network may not reach.
	app = FastAPI(lifespan=lifespan, openapi_url=None)
	app.state.pressure = state
	app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

	@app.get("/")
	async def page():
		return FileResponse(STATIC_DIR / "index.html")

	@app.get("/api/info")
	async def api_info():
		return Response(info, media_type="application/json")

	@app.get("/api/history")
	async def api_history(request: Request, start: float, end: float, points: int = Query(ge=1, le=HISTORY_POINTS_MAX), raw: bool = False):
		if not history_limit.allow(_address(request)):
			raise HTTPException(429, "too many history requests from this address", headers={"Retry-After": "1"})
		if not (math.isfinite(start) and math.isfinite(end) and start < end):
			raise HTTPException(422, "start and end must be finite, with start < end")
		if end - start > HISTORY_RANGE_MAX_S:
			raise HTTPException(422, f"the range may be at most {HISTORY_RANGE_MAX_S / 86400:.0f} days")
		if state.queued >= HISTORY_QUEUE_MAX:
			raise HTTPException(503, "busy with other history requests; try again", headers={"Retry-After": "2"})
		state.queued += 1
		try:
			text = await asyncio.get_running_loop().run_in_executor(
				history_pool, history_json, directory, layout.source.key, start, end, points, not raw)
		except (OSError, sqlite3.Error, ValueError) as e:
			history_outage.failed(e, "history not shown")
			raise HTTPException(503, "the day files cannot be read now") from None
		finally:
			state.queued -= 1
		history_outage.ended()
		return Response(text, media_type="application/json")

	@app.websocket("/ws")
	async def stream(websocket: WebSocket):
		"""Never receives. Ends when the browser leaves or a send takes over SEND_TIMEOUT_S."""
		hub, address = state.hub, _address(websocket)
		loop = asyncio.get_running_loop()
		with hub.admitted(address) as refusal, contextlib.suppress(WebSocketDisconnect, TimeoutError):
			# Accepted even when refused: only an accepted socket carries a close code and reason to the page.
			await _within_send_timeout(websocket.accept())
			if refusal is not None:
				_refused_log.info("/ws from %s refused: %s", address, refusal)
				await _within_send_timeout(websocket.close(TRY_AGAIN_LATER, refusal))
				return
			sent, last_send = None, -math.inf
			while True:
				await asyncio.sleep(max(0.0, last_send + SEND_PERIOD_S - loop.time()))  # readings that arrive meanwhile join the batch
				rows, sent, reset = hub.live.after(sent)
				frame = f'{{"kind":"{"live" if reset else "rows"}","rows":[{",".join(rows)}],"status":{hub.status()}}}'
				last_send = loop.time()
				await _within_send_timeout(websocket.send_text(frame))
				if hub.live.n == sent[1]:  # nothing new arrived during the send
					await hub.wait_change(STATUS_PERIOD_S)

	return app


def main(argv=None):
	parser = argparse.ArgumentParser(prog="pressure_web", description="read-only browser page of the pressure IOC and its store")
	parser.add_argument("--config", required=True, help="the IOC's config file (TOML)")
	parser.add_argument("--dir", required=True, type=Path, help="the store's directory of day files")
	args = parser.parse_args(argv)
	logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)  # under systemd the journal keeps it
	try:
		layout = load_layout(args.config)
	except (OSError, ValueError) as e:
		print(f"pressure_web: {e}", file=sys.stderr)
		return 2
	import uvicorn

	logging.getLogger("uvicorn.error").addFilter(ThrottledLog(LOG_LINES_PER_MIN))
	host, port = os.environ.get("PRESSURE_WEB_HOST", "127.0.0.1"), int(os.environ.get("PRESSURE_WEB_PORT", "8081"))
	log.info("pressure_web for %s, day files in %s, on %s:%d", layout.prefix, args.dir, host, port)
	uvicorn.run(create_app(layout, args.dir), **server_settings(host, port))
	return 0


if __name__ == "__main__":
	sys.exit(main())
