"""The part of a read-only monitor page's server that is not about any one instrument: viewer limits, a request
rate limit, a throttled log and the uvicorn settings.

Taken from the interferometer page (bapsf_interferometer, interf_web), which runs with the same limits. It has
nothing of pressure in it, so it can move to the shared package (bapsf_epics_core) as it is. Needs uvicorn and
websockets only where server_settings() is called.
"""
import collections
import contextlib
import logging
import socket
import time

log = logging.getLogger(__name__)

# Limits: the page is for about 20 viewers and shares its machine's CPU with the IOC.
MAX_VIEWERS = 20  # open /ws sockets in all; more are closed with TRY_AGAIN_LATER
MAX_VIEWERS_PER_ADDRESS = 3  # open /ws sockets from one client address
TRY_AGAIN_LATER = 1013  # the WebSocket close code for a refused socket
# uvicorn's limit_concurrency counts every open connection, /ws and idle keep-alive ones included, and an HTTP request
# beyond it gets 503. A viewer holds its /ws and a connection for the history requests, plus the 2-3 its browser
# opens to load the page. uvicorn does not apply the limit to a WebSocket upgrade, hence MAX_VIEWERS.
MAX_CONNECTIONS = 4 * MAX_VIEWERS
# A connection is closed if its next request has not fully arrived this long after it opened or after its last
# response: idle, or headers dribbled in (_timed_h11_protocol).
KEEP_ALIVE_S = 30
SEND_TIMEOUT_S = 30.0  # a /ws send that cannot finish, a browser that stopped reading, ends that socket
# Linux only: the kernel drops a connection whose sent data stays unacknowledged this long, so a socket ended by
# SEND_TIMEOUT_S leaves no connection behind that counts against MAX_CONNECTIONS.
TCP_USER_TIMEOUT_S = 90
WS_MAX_SIZE = 4096  # bytes of an incoming WebSocket message; the page sends none
PING_INTERVAL_S = 30.0
PING_TIMEOUT_S = 60.0  # a brief network hiccup keeps the viewer; a dead peer is dropped within PING_INTERVAL_S + this
STOP_TIMEOUT_S = 5  # for open sockets at exit; under the unit's TimeoutStopSec
LOG_LINES_PER_MIN = 30  # per throttled log: uvicorn's (a line for every WebSocket accept and every 503), refused /ws


class Viewers:
	"""The open /ws sockets, counted per client address. Event loop only, so no lock."""

	def __init__(self):
		self.open = collections.Counter()  # client address: its open /ws sockets

	def total(self):
		return self.open.total()

	@contextlib.contextmanager
	def admitted(self, address):
		"""Yields None while holding one of the MAX_VIEWERS slots for `address`, or why it is refused (the close reason
		the page shows). Check and count happen with no await between, so two sockets cannot both take the last slot."""
		if self.open.total() >= MAX_VIEWERS:
			yield f"the page already has {MAX_VIEWERS} viewers; try again later"
		elif self.open[address] >= MAX_VIEWERS_PER_ADDRESS:
			yield f"{MAX_VIEWERS_PER_ADDRESS} pages are already open from this address; try again later"
		else:
			self.open[address] += 1
			try:
				yield None
			finally:
				self.open -= collections.Counter([address])  # drops the address at 0


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


def _timed_h11_protocol():
	"""uvicorn's h11 protocol whose keep-alive timer (KEEP_ALIVE_S) also covers each request until its headers are
	complete, plus TCP_USER_TIMEOUT_S. In uvicorn 0.54 that timer starts only after a response and any byte cancels it,
	so a connection that never completes a request stays open. The timer is still cancelled when a request's headers are
	complete (handle_events, WebSocket upgrades included). Overrides two of H11Protocol's methods: recheck on upgrade."""
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
	"""uvicorn.Config keywords. The caller also throttles uvicorn's log (ThrottledLog on "uvicorn.error")."""
	# ws and lifespan are named so trouble stops the start: with "auto", a missing websockets package leaves every /ws
	# refused, and a failed start of the monitor can read as "lifespan unsupported" and serve a page with no data.
	# No deflate: the frames are small, and each browser's copy would cost the event loop.
	# X-Forwarded-For from the proxy on this host (deploy/nginx/) gives the viewer's address, which the per-address
	# limits count against. Named, not uvicorn's default: that reads FORWARDED_ALLOW_IPS from the environment, so a
	# stray env file would change whose header is believed.
	return dict(host=host, port=port, proxy_headers=True, forwarded_allow_ips="127.0.0.1",
	            http=_timed_h11_protocol(), ws="websockets-sansio",
	            ws_per_message_deflate=False, ws_max_size=WS_MAX_SIZE, ws_ping_interval=PING_INTERVAL_S,
	            ws_ping_timeout=PING_TIMEOUT_S, limit_concurrency=MAX_CONNECTIONS, timeout_keep_alive=KEEP_ALIVE_S,
	            lifespan="on", log_config=None, access_log=False, timeout_graceful_shutdown=STOP_TIMEOUT_S)


def client_address(connection):
	"""The client address a request or WebSocket counts against (the viewer cap, a rate limit)."""
	return connection.client.host if connection.client else ""
