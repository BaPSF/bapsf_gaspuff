"""Tests of the pressure page (pressure_web.py). No instrument needed.

	python -m unittest discover -s pfeiffer/tests

They need the diag_ioc package (bapsf_epics_core); the server tests also fastapi, uvicorn and websockets, and the
session tests node. The server runs on loopback with a stand-in for the p4p monitors that hands the feed the updates a
PVA monitor would deliver, as nested dicts: made up, or published by the IOC module reading the fake controller.
"""
import contextlib
import http.client
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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
from diag_ioc.testing import free_port, have, wait_for  # noqa: E402

import pressure_db  # noqa: E402
import pressure_gas  # noqa: E402
import pressure_web as web  # noqa: E402
from fake_tpg366 import FakeTpg366  # noqa: E402
from pressure_ioc import PressureModule  # noqa: E402
from pressure_store import Layout, Store  # noqa: E402
from test_pressure_config import options  # noqa: E402
from test_pressure_ioc import FakeBuilder, ioc_config  # noqa: E402

WEB = have("fastapi", "uvicorn", "websockets")
if WEB:
	import uvicorn
	from websockets.exceptions import ConnectionClosed
	from websockets.sync.client import connect

HOST = "127.0.0.1"
DEPLOY = PFEIFFER / "deploy"
NAN = math.nan
T0 = 1794470400.0  # 2026-11-12 00:00 in Los Angeles
OK1, OK2 = (2e-6, 5.9, 0, 0, 3e-6), (1e-3, 1.0, 0, 0, NAN)  # p_raw, gas_factor, status, raw_status, base of G1 and G2


def group(seq, time_s, gas="Helium", g1=OK1, g2=OK2, read_s=0.01):
	"""One update of the PVA group P:TPG1:SAMPLE, as nested dicts."""
	def gauge(values):
		p_raw, factor, status, raw_status, base = values
		return {"p_raw": p_raw, "gas_factor": factor, "p": p_raw * factor, "status": status, "raw_status": raw_status, "base": base}
	return {"seq": seq, "time": time_s, "gas": gas, "read_s": read_s, "g1": gauge(g1), "g2": gauge(g2)}


def layout(selection_file="/var/lib/diag-ioc/pressure/gas_species", address="127.0.0.1:8000", period_s=0.1):
	from pressure_config import parse_options
	from pressure_ioc import DRIVERS
	return Layout("P", parse_options(options(selection_file, address, period_s), {k: d.channels for k, d in DRIVERS.items()}), "c" * 64)


#===================================================================================================================================================
# The parts without a server

class ReadingTests(unittest.TestCase):
	def test_a_group_update_is_one_reading(self):
		r = web.reading_from_group(group(3, T0 + 0.5, gas="Argon", g2=(NAN, 1.0, 8, -1, NAN)), ("G1", "G2"))
		self.assertEqual((r.seq, r.time, r.gas), (3, T0 + 0.5, "Argon"))
		self.assertEqual((r.gauges[0].p_raw, r.gauges[0].gas_factor, r.gauges[0].p, r.gauges[0].base), (2e-6, 5.9, 2e-6 * 5.9, 3e-6))
		self.assertEqual((r.gauges[1].status, r.gauges[1].raw_status), (8, -1))
		self.assertTrue(math.isnan(r.gauges[1].p))
		self.assertIsNone(web.reading_from_group(group(0, 0.0), ("G1", "G2")))  # the IOC's state before its first sample
		with self.assertRaises(KeyError):
			web.reading_from_group(group(1, T0), ("G1", "G3"))

	@unittest.skipUnless(have("p4p"), "needs p4p")
	def test_p4p_values_are_read_like_the_dicts(self):
		from p4p import Type, Value
		gauge = ("S", None, [("p_raw", "d"), ("gas_factor", "d"), ("p", "d"), ("status", "i"), ("raw_status", "i"), ("base", "d")])
		kind = Type([("seq", "i"), ("time", "d"), ("gas", "s"), ("read_s", "d"), ("g1", gauge), ("g2", gauge)])
		update = group(3, T0 + 0.25)
		self.assertEqual(repr(web.reading_from_group(Value(kind, update), ("G1", "G2"))), repr(web.reading_from_group(update, ("G1", "G2"))))

	def test_the_live_rows_follow_each_page(self):
		live = web.Live(("Helium", "Argon"), seconds=1.0)
		texts, mark, reset = live.after(None)
		self.assertEqual((texts, reset), ([], True))
		reading = web.reading_from_group(group(1, T0), ("G1", "G2"))
		live.add(reading)
		self.assertEqual(live.after(mark)[0], ['[1794470400.0,0,1.18e-05,0.001]'])  # 4 significant digits
		for seq in range(2, 25):  # 2.3 s at 10 a second: the oldest fall out
			live.add(web.reading_from_group(group(seq, T0 + seq / 10, gas="Argon" if seq > 20 else "Helium"), ("G1", "G2")))
		texts, mark2, reset = live.after(mark)
		self.assertTrue(reset)  # fell behind the rows kept: all of them again
		self.assertEqual([json.loads(t)[0] for t in texts], [round(T0 + s / 10, 3) for s in range(14, 25)])
		self.assertEqual([json.loads(t)[1] for t in texts][-5:], [0, 1, 1, 1, 1])
		live.add(web.reading_from_group(group(25, T0 + 2.5), ("G1", "G2")))
		texts, mark3, reset = live.after(mark2)
		self.assertEqual((len(texts), reset), (1, False))
		self.assertEqual(live.after(mark3)[:3:2], ([], False))
		live.add(web.reading_from_group(group(1, T0 + 1.0), ("G1", "G2")))  # a clock set back: a new generation
		texts, _, reset = live.after(mark3)
		self.assertEqual((len(texts), reset), (1, True))

	def test_status_has_no_nan_and_says_when_it_waits(self):
		now = [T0 + 10.0]
		hub = web.Hub(layout(), clock=lambda: now[0])
		status = json.loads(hub.status())
		self.assertEqual((status["waiting"], status["ioc"], status["connected"], status["sample"]), (True, False, None, None))
		self.assertEqual(status["gauges"]["G1"], {"model": None, "device_gas": None})
		hub.publish(web.reading_from_group(group(7, T0 + 9.5, g2=(NAN, 1.0, 4, 4, NAN)), ("G1", "G2")))
		hub.set_pv(("gas", "species"), "Helium")
		hub.set_pv(("gas", "since"), T0)
		hub.set_pv(("gas", "by"), "operator")
		hub.set_pv(("source", "connected"), "yes")
		hub.set_pv(("G1", "model"), "PKR")
		hub.set_ioc(True)
		status = json.loads(hub.status())
		self.assertEqual((status["waiting"], status["ioc"], status["connected"], status["age_s"]), (False, True, True, 0.5))
		self.assertEqual(status["gas"], {"species": "Helium", "since": T0, "by": "operator"})
		self.assertEqual(status["gauges"]["G1"]["model"], "PKR")
		self.assertEqual(status["gauges"]["G2"], {"model": None, "device_gas": None, "p": None, "p_raw": None, "gas_factor": 1.0, "status": 4,
		                                          "raw_status": 4, "base": None})
		now[0] = T0 + 9.5 + web.WAITING_AFTER_S
		self.assertTrue(json.loads(hub.status())["waiting"])

	def test_the_page_info_holds_no_address(self):
		info = web.page_info(layout(address="10.1.2.3:8000"))
		self.assertNotIn("10.1.2.3", json.dumps(info))
		self.assertEqual([(g["key"], g["warning"], g["alarm"], g["multipliers"]["Helium"]) for g in info["gauges"]],
		                 [("G1", 5e-6, 2e-5, 5.9), ("G2", None, None, 1)])
		self.assertEqual((info["species"], info["status_text"]["8"], info["live_s"]), (["Helium", "Hydrogen", "Argon", "Nitrogen"], "no reply", 300.0))

	def test_limits(self):
		"""The interferometer page's (D18, J 2026-11-06)."""
		self.assertEqual((web.MAX_VIEWERS, web.MAX_VIEWERS_PER_ADDRESS, web.TRY_AGAIN_LATER), (20, 3, 1013))
		self.assertEqual((web.MAX_CONNECTIONS, web.KEEP_ALIVE_S, web.SEND_TIMEOUT_S), (80, 30, 30))
		self.assertEqual((web.WS_MAX_SIZE, web.PING_INTERVAL_S, web.PING_TIMEOUT_S), (4096, 30, 60))

	def test_timed_h11_cancels_keep_alive_once_headers_complete(self):
		idle, send_response = object(), object()
		cancelled = []

		class BaseH11:
			def __init__(self):
				self.timeout_keep_alive = 30
				self.timeout_keep_alive_handler = lambda: None
				self.timeout_keep_alive_task = None
				self.loop = types.SimpleNamespace(call_later=lambda delay, cb: types.SimpleNamespace(cancel=lambda: cancelled.append(delay)))
				self.buf = b""
				self.conn = types.SimpleNamespace(our_state=idle, receive_data=lambda d: setattr(self, "buf", self.buf + d))

			def connection_made(self, transport):
				pass

			def handle_events(self):
				if b"\r\n\r\n" in self.buf:
					self.conn.our_state = send_response

		modules = {name: types.ModuleType(name) for name in ("h11", "uvicorn", "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.h11_impl")}
		modules["h11"].IDLE = idle
		modules["uvicorn.protocols.http.h11_impl"].H11Protocol = BaseH11
		with mock.patch.dict(sys.modules, modules):
			proto = web._timed_h11_protocol()()
		proto.connection_made(types.SimpleNamespace(get_extra_info=lambda _: None))
		self.assertIsNotNone(proto.timeout_keep_alive_task)
		proto.data_received(b"GET / ")  # incomplete headers: timer stays armed
		self.assertEqual(cancelled, [])
		self.assertIsNotNone(proto.timeout_keep_alive_task)
		proto.data_received(b"HTTP/1.1\r\nHost: localhost\r\n\r\n")  # complete headers: timer cancelled
		self.assertEqual(cancelled, [30])
		self.assertIsNone(proto.timeout_keep_alive_task)


#===================================================================================================================================================
# The history: what api/history returns is what the store's reader returns

def store_files(directory, start, seconds, rate=2.0, argon_after=None):
	"""Day files written by the store from made-up updates: G1 a ramp, G2 constant, `rate` a second."""
	store = Store(directory, layout(), clock=lambda: start)
	for i in range(int(seconds * rate)):
		t = start + i / rate
		gas = "Argon" if argon_after is not None and i / rate >= argon_after else "Helium"
		factor = 0.8 if gas == "Argon" else 5.9
		store.on_group(group(i + 1, t, gas=gas, g1=(1e-6 * (1 + i / (seconds * rate)), factor, 0, 0, NAN)))
	store.on_pv("TEST:GAS:SPECIES", {"value": {"index": 0, "choices": ["Helium", "Hydrogen", "Argon", "Nitrogen"]}})
	assert store.write()
	store.close()


class HistoryTests(unittest.TestCase):
	def setUp(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.dir = Path(tmp.name)

	def check(self, start, end, points, corrected):
		answer = json.loads(web.history_json(self.dir, "TPG1", start, end, points, corrected))
		expected = pressure_db.history(self.dir, "TPG1", start, end, points, corrected=corrected)
		np.testing.assert_allclose(answer["t"], expected["t"], atol=1e-3)
		self.assertAlmostEqual(answer["width"], expected["width"])
		self.assertEqual(sorted(answer["gauges"]), sorted(expected["gauges"]))
		for key, values in expected["gauges"].items():
			for name, array in values.items():
				got = np.array([np.nan if v is None else v for v in answer["gauges"][key][name]], dtype=float)
				np.testing.assert_allclose(got, array, rtol=1e-12, equal_nan=True, err_msg=f"{key} {name}")
		self.assertEqual(answer["gas"], [list(entry) for entry in expected["gas"]])
		return answer

	def test_the_answer_is_pressure_db_history(self):
		store_files(self.dir, T0 + 3600, 3 * 3600, rate=0.5, argon_after=2 * 3600)
		short = self.check(T0 + 3600, T0 + 7200, 120, True)  # 1 h: from the readings
		self.assertEqual((short["from"], len(short["t"])), ("readings", 120))
		long = self.check(T0, T0 + 5 * 3600, 300, True)  # 5 h: from the minute summaries, with buckets left out
		self.assertEqual(long["from"], "minutes")
		self.assertEqual(len(long["t"]), 180)  # 3 h of 1-minute buckets
		self.assertEqual([g[1] for g in long["gas"]], ["Helium", "Argon"])
		eight_hours = self.check(T0, T0 + 8 * 3600, 1400, True)  # 8 h on a wide screen: still 60 s buckets, so no false gaps
		self.assertEqual(eight_hours["width"], 60.0)
		self.assertTrue(np.all(np.diff(eight_hours["t"]) <= 1.5 * eight_hours["width"]))
		raw = self.check(T0, T0 + 5 * 3600, 300, False)
		self.assertAlmostEqual(raw["gauges"]["G1"]["mean"][0], long["gauges"]["G1"]["mean"][0] / 5.9)

	def test_no_files_is_an_empty_answer(self):
		self.assertEqual(json.loads(web.history_json(self.dir, "TPG1", T0, T0 + 60, 10, True)),
		                 {"t": [], "width": 6.0, "gauges": {}, "gas": [], "from": "readings"})


#===================================================================================================================================================
# The server, on loopback

@contextlib.contextmanager
def running(store_dir, the_layout=None, **limits):
	"""pressure_web on a free loopback port, with `limits` (module constants) patched and the Feed handed to the test in
	place of p4p; yields (port, feed)."""
	feeds = []

	def monitor(feed):
		feeds.append(feed)
		return lambda: None

	with contextlib.ExitStack() as stack:
		if limits:
			stack.enter_context(mock.patch.multiple(web, **limits))
		port = free_port()
		app = web.create_app(the_layout or layout(), store_dir, monitor=monitor)
		server = uvicorn.Server(uvicorn.Config(app, **web.server_settings(HOST, port)))
		thread = threading.Thread(target=server.run, name="pressure_web test server", daemon=True)
		thread.start()
		try:
			wait_for(lambda: server.started, 15, "the server to start")
			yield port, feeds[0]
		finally:
			server.should_exit = True
			thread.join(20)
			if thread.is_alive():
				raise AssertionError("the server did not stop within 20 s")


def request(port, method="GET", path="/"):
	"""(status, body) of one request on a new connection."""
	connection = http.client.HTTPConnection(HOST, port, timeout=10)
	try:
		connection.request(method, path)
		response = connection.getresponse()
		return response.status, response.read()
	finally:
		connection.close()


def frames(socket, until, timeout=10):
	"""Frames received until `until(frame)` is true; AssertionError after `timeout` s."""
	got, deadline = [], time.monotonic() + timeout
	while True:
		left = deadline - time.monotonic()
		if left <= 0:
			raise AssertionError(f"no such frame within {timeout} s; last: {got[-1:]}")
		got.append(json.loads(socket.recv(timeout=left)))
		if until(got[-1]):
			return got


@unittest.skipUnless(WEB, "needs fastapi, uvicorn and websockets")
class ServerTests(unittest.TestCase):
	def setUp(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		self.dir = Path(tmp.name)

	def test_page_info_and_read_only(self):
		with running(self.dir) as (port, feed):
			status, body = request(port)
			self.assertEqual(status, 200)
			self.assertIn(b"<title>Chamber pressure</title>", body)
			for path in ("/static/session.js", "/static/uPlot.iife.min.js", "/static/uPlot.min.css"):
				self.assertEqual(request(port, path=path)[0], 200, path)
			status, body = request(port, path="/api/info")
			self.assertEqual((status, json.loads(body)["source"]), (200, "TPG1"))
			for path in ("/", "/api/info", "/api/history", "/static/index.html"):
				for method in ("POST", "PUT", "DELETE"):
					self.assertEqual(request(port, method, path)[0], 405, (method, path))
			for path in ("/docs", "/redoc", "/openapi.json"):
				self.assertEqual(request(port, path=path)[0], 404, path)

	def test_history_requests_are_checked_and_limited(self):
		store_files(self.dir, T0, 600)
		with running(self.dir, HISTORY_RATE_PER_S=0.01, HISTORY_BURST=3) as (port, feed):
			status, body = request(port, path=f"/api/history?start={T0}&end={T0 + 600}&points=60")
			self.assertEqual(status, 200)
			self.assertEqual(len(json.loads(body)["t"]), 60)
			self.assertEqual(request(port, path=f"/api/history?start={T0}&end={T0 + 600}&points=60&raw=1")[0], 200)
			self.assertEqual(request(port, path=f"/api/history?start={T0}&end={T0}&points=60")[0], 422)  # counted
			self.assertEqual(request(port, path=f"/api/history?start={T0}&end={T0 + 1}&points={web.HISTORY_POINTS_MAX + 1}")[0], 422)  # not counted
			self.assertEqual(request(port, path=f"/api/history?start={T0}&end={T0 + 600}&points=60")[0], 429)
		with running(self.dir) as (port, feed):
			status, body = request(port, path=f"/api/history?start=0&end={web.HISTORY_RANGE_MAX_S + 1}&points=60")
			self.assertEqual(status, 422)
			self.assertIn(b"at most 400 days", body)

	def test_a_page_gets_the_last_five_minutes_then_each_new_reading(self):
		with running(self.dir) as (port, feed):
			now = time.time()
			for seq in range(1, 4):
				feed.on_group(group(seq, now - 3 + seq * 0.1))
			feed.on_pv("TEST:GAS:SPECIES", {"value": {"index": 0, "choices": ["Helium", "Hydrogen", "Argon", "Nitrogen"]}})
			feed.on_pv("P:TPG1:CONNECTED", {"value": {"index": 1, "choices": ["no", "yes"]}})
			wait_for(lambda: feed.hub.live.n == 3 and ("source", "connected") in feed.hub.pvs, 5, "the readings")
			with connect(f"ws://{HOST}:{port}/ws") as socket:
				first = json.loads(socket.recv(timeout=10))
				self.assertEqual((first["kind"], len(first["rows"])), ("live", 3))
				self.assertEqual(first["status"]["gas"]["species"], "Helium")
				self.assertTrue(first["status"]["ioc"] and first["status"]["connected"])
				feed.on_group(group(3, now - 2.7))  # the same reading again, after a reconnect: not shown twice
				for seq in range(4, 10):
					feed.on_group(group(seq, time.time()))
					time.sleep(0.05)
				got = frames(socket, lambda f: f["status"]["sample"]["seq"] == 9)
				rows = [row for f in got for row in f["rows"]]
				self.assertEqual([f["kind"] for f in got], ["rows"] * len(got))
				self.assertEqual(len(rows), 6)
				self.assertLessEqual(len(got), 6)
				self.assertEqual(rows[-1][2:], [round(2e-6 * 5.9, 10), 0.001])
				with self.assertLogs("pressure_web", "WARNING"):
					feed.on_group(RuntimeError("Disconnected"))
				frames(socket, lambda f: not f["status"]["ioc"])

	def test_viewers_beyond_the_caps_are_refused_with_a_reason(self):
		with running(self.dir, MAX_VIEWERS_PER_ADDRESS=1) as (port, feed):
			with connect(f"ws://{HOST}:{port}/ws") as first:
				first.recv(timeout=10)
				with connect(f"ws://{HOST}:{port}/ws") as second:
					with self.assertRaises(ConnectionClosed) as closed:
						second.recv(timeout=10)
					self.assertEqual(closed.exception.rcvd.code, web.TRY_AGAIN_LATER)
					self.assertIn("1 pages are already open from this address", closed.exception.rcvd.reason)
			wait_for(lambda: feed.hub.viewers.total() == 0, 10, "the viewer slot back")


#===================================================================================================================================================
# The IOC module on the fake controller, its published readings handed to the page's feed

@unittest.skipUnless(WEB, "needs fastapi, uvicorn and websockets")
class FakeControllerTests(unittest.TestCase):
	def test_what_the_fake_controller_sends_is_what_the_page_shows(self):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		fake = FakeTpg366().start()
		self.addCleanup(fake.stop)
		fake.pressures_torr = [2.5e-6, 0.0, 4.0e-4, 0.0, 0.0, 0.0]  # G1 is channel 1, G2 channel 3
		data = options(str(Path(tmp.name) / "gas_species"), f"{fake.host}:{fake.port}", period_s=0.05)
		data["source"][0]["timeout_s"] = 0.5
		module = PressureModule("pressure", data)
		builder = FakeBuilder("P")
		module.create_records(builder)
		records = builder.records
		the_layout = Layout("P", module.config, "c" * 64)

		with running(Path(tmp.name), the_layout) as (port, feed):
			def last(name):
				return records[name].last[0]

			def published(result, publish=module.publish):
				"""After each publish(): the update a monitor of the group P:TPG1:SAMPLE gets, and the single PVs."""
				publish(result)
				update = {"seq": last("P:TPG1:SAMPLE:SEQ"), "time": last("P:TPG1:SAMPLE:TIME"), "gas": last("P:TPG1:SAMPLE:GAS"),
				          "read_s": last("P:TPG1:READ_S")}
				for key in ("G1", "G2"):
					update[key.lower()] = {name.lower(): last(f"P:{key}:{name}") for name in ("P_RAW", "GAS_FACTOR", "P", "STATUS", "RAW_STATUS", "BASE")}
				feed.on_group(update)
				for name in feed.pvs:
					value = last(name)
					if name.endswith(":SPECIES") or name.endswith(":CONNECTED"):
						states = module.config.gas.species if name.endswith(":SPECIES") else ("no", "yes")
						value = {"index": value, "choices": list(states)}
					if value is not None:
						feed.on_pv(name, {"value": value})

			module.publish = published
			pipeline = ShotPipeline(module, depth=2, timing_log=False)
			module.start(types.SimpleNamespace(pipeline=pipeline))
			self.addCleanup(pipeline.close)
			self.addCleanup(module.stop)
			with connect(f"ws://{HOST}:{port}/ws") as socket:
				got = frames(socket, lambda f: f["status"]["sample"] is not None and f["status"]["gauges"]["G1"].get("model"))
				status = got[-1]["status"]
				self.assertEqual(status["gas"]["species"], "Helium")
				self.assertEqual(status["gas"]["by"], pressure_gas.DEFAULT_BY)
				self.assertEqual((status["gauges"]["G1"]["p_raw"], status["gauges"]["G1"]["gas_factor"]), (2.5e-6, 5.9))
				self.assertEqual((status["gauges"]["G1"]["model"], status["gauges"]["G1"]["device_gas"]), ("PKR", "Nitrogen"))
				self.assertTrue(status["connected"])
				rows = [row for f in got for row in f["rows"]]
				self.assertTrue(rows)
				self.assertEqual({tuple(row[2:]) for row in rows}, {(round(2.5e-6 * 5.9, 10), 4e-4)})

				pressure_gas.write_selection(module.config.gas, "argon")
				got = frames(socket, lambda f: f["status"]["gas"]["species"] == "Argon" and f["rows"] and f["rows"][-1][1] == 2)
				self.assertEqual(got[-1]["rows"][-1][2], 2e-6)  # 2.5e-6 x 0.8
				self.assertEqual(got[-1]["status"]["gauges"]["G1"]["gas_factor"], 0.8)

				with self.assertLogs("pressure_ioc", "WARNING"):
					fake.drop_client()
					got = frames(socket, lambda f: f["status"]["connected"] is False)
				self.assertIn("connection closed", got[-1]["status"]["error"])
				frames(socket, lambda f: f["status"]["connected"] is True and f["status"]["gauges"]["G1"]["status"] == 0)


#===================================================================================================================================================
# main(), the units and the nginx site

class DeployTests(unittest.TestCase):
	def test_a_bad_config_stops_the_start(self):
		with tempfile.TemporaryDirectory() as tmp:
			with contextlib.redirect_stderr(__import__("io").StringIO()) as stderr:
				self.assertEqual(web.main(["--config", str(Path(tmp) / "missing.toml"), "--dir", tmp]), 2)
			self.assertIn("missing.toml", stderr.getvalue())
			config = Path(tmp) / "pressure.toml"
			config.write_text(ioc_config("P", types.SimpleNamespace(host="127.0.0.1", port=8000), Path(tmp) / "gas_species"), encoding="utf-8")
			for bad_port in ("abc", "0", "70000"):
				with mock.patch.dict(os.environ, {"PRESSURE_WEB_PORT": bad_port}), contextlib.redirect_stderr(__import__("io").StringIO()) as stderr:
					self.assertEqual(web.main(["--config", str(config), "--dir", tmp]), 2)
				self.assertTrue(stderr.getvalue())

	def test_the_example_config_is_what_the_page_reads(self):
		"""The page takes everything from the IOC's own config; the test config is the example's shape."""
		with tempfile.TemporaryDirectory() as tmp:
			config = Path(tmp) / "pressure.toml"
			config.write_text(ioc_config("P", types.SimpleNamespace(host="127.0.0.1", port=8000), Path(tmp) / "gas_species"), encoding="utf-8")
			from pressure_store import load_layout
			info = web.page_info(load_layout(config))
		self.assertEqual([g["key"] for g in info["gauges"]], ["G1", "G2"])

	def test_units(self):
		def unit(name):
			return [line for line in (DEPLOY / "systemd" / f"{name}.service").read_text(encoding="utf-8").splitlines() if not line.startswith("#")]

		for name in ("diag-ioc@", "pressure-store", "pressure-web"):
			lines = unit(name)
			for line in ("User=diag-ioc", "Group=diag-ioc", "WorkingDirectory=/opt/bapsf_gaspuff", "Environment=PYTHONPATH=/opt/bapsf_gaspuff/pfeiffer"):
				self.assertIn(line, lines, name)
		web_unit = unit("pressure-web")
		for line in ("Environment=EPICS_PVA_ADDR_LIST=127.0.0.1", "Environment=EPICS_PVA_AUTO_ADDR_LIST=NO", "IPAddressDeny=any",
		             "IPAddressAllow=localhost", "ProtectSystem=strict", "NoNewPrivileges=yes", "CapabilityBoundingSet="):
			self.assertIn(line, web_unit)
		self.assertEqual([line for line in web_unit if "PRESSURE_WEB_HOST" in line], [])  # 127.0.0.1, the code's default
		exec_start = next(line for line in web_unit if line.startswith("ExecStart="))
		self.assertIn("pfeiffer/pressure_web.py --config /etc/diag-ioc/pressure.toml --dir /var/lib/pressure", exec_start)
		store = unit("pressure-store")
		for line in ("StateDirectory=pressure", "Environment=OPENBLAS_NUM_THREADS=1", "NoNewPrivileges=yes", "ProtectSystem=strict"):
			self.assertIn(line, store)

	def test_nginx_site(self):
		text = (DEPLOY / "nginx" / "pressure.nginx.example").read_text(encoding="utf-8")
		lines = [line.split("#", 1)[0].strip() for line in text.splitlines()]
		self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", lines)
		self.assertIn("proxy_pass http://127.0.0.1:8081/;", lines)  # the code's default port; the slash strips the prefix
		self.assertIn("location /diagnostic/pressure/ {", lines)
		self.assertIn("proxy_set_header Upgrade $http_upgrade;", lines)
		listens = [line[len("listen"):].strip().rstrip(";") for line in lines if line.startswith("listen ")]
		self.assertTrue(listens)
		for target in listens:  # never every interface
			self.assertNotIn(target.rsplit(":", 1)[0] if ":" in target else "", ("", "0.0.0.0", "*", "[::]"), target)


#===================================================================================================================================================
# The page's session.js in node, with fake timers and a fake socket

_SESSION_HARNESS = r"""
const S = require(process.env.SESSION_JS);
let now = 0, nextId = 1;
const timers = [];
const fake = {
  setTimeout(f, ms) { const id = nextId++; timers.push({id, at: now + ms, f}); return id; },
  setInterval(f, ms) { const id = nextId++; timers.push({id, at: now + ms, f, every: ms}); return id; },
  clearTimeout(id) { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); },
  clearInterval(id) { fake.clearTimeout(id); },
};
function advance(ms) {
  const end = now + ms;
  for (;;) {
    let next = null;
    for (const t of timers) if (next === null || t.at < next.at) next = t;
    if (next === null || next.at > end) break;
    now = next.at;
    if (next.every) next.at += next.every; else timers.splice(timers.indexOf(next), 1);
    next.f();
  }
  now = end;
}
const sockets = [];
class FakeSocket {
  constructor(url) { this.url = url; this.closed = false; sockets.push(this); }
  close() { this.closedBy("page", {code: 1000, reason: ""}); }
  closedBy(who, event) { if (this.closed) return; this.closed = who; this.onclose(event); }
}
const listeners = new Set();
const document = {hidden: false, addEventListener(type, f) { listeners.add(f); },
                  removeEventListener(type, f) { listeners.delete(f); }};
function setHidden(hidden) { document.hidden = hidden; for (const f of [...listeners]) f(); }
const closes = [], received = [];
S.openSession({url: "ws://test/ws", WebSocket: FakeSocket, document, timers: fake, now: () => now,
               onFrame: frame => received.push(frame), onClosed: (note, reason) => closes.push([note, reason])});
const socket = sockets[0];
const feedFrames = () => fake.setInterval(() => { if (!socket.closed) socket.onmessage({data: '{"kind": "rows"}'}); }, 1000);
const report = extra => console.log(JSON.stringify({sockets: sockets.length, closed: socket.closed, closes, frames: received.length, ...extra}));
const HOUR = 3600 * 1000;
"""

DISCONNECTED = "Disconnected - reload the page to reconnect"
PAUSED = "Paused - reload to resume"


@unittest.skipUnless(shutil.which("node"), "needs node")
class PageSessionTests(unittest.TestCase):
	"""session.js: one socket for the life of the page, the hidden-tab pause, and no timeout for a visible tab."""

	def run_session(self, steps):
		completed = subprocess.run(["node", "-e", _SESSION_HARNESS + steps],
		                           env={**os.environ, "SESSION_JS": str(PFEIFFER / "pressure_web_static" / "session.js")},
		                           capture_output=True, text=True, timeout=120, check=True)
		return json.loads(completed.stdout.strip().splitlines()[-1])

	def test_frames_reach_the_page(self):
		result = self.run_session("feedFrames(); advance(5500); report();")
		self.assertEqual((result["frames"], result["closed"]), (5, False))

	def test_no_reconnect_after_the_server_closes(self):
		result = self.run_session("feedFrames(); socket.closedBy('server', {code: 1013, reason: 'the page already has 20 viewers; try again later'});"
		                          "advance(24 * HOUR); report();")
		self.assertEqual((result["sockets"], result["closes"]), (1, [[DISCONNECTED, "the page already has 20 viewers; try again later"]]))

	def test_no_reconnect_after_a_dead_link(self):
		result = self.run_session("advance(S.FRAME_TIMEOUT_MS + 2000); advance(24 * HOUR); report();")
		self.assertEqual((result["sockets"], result["closed"], result["closes"]), (1, "page", [[DISCONNECTED, ""]]))

	def test_hidden_tab_pauses_after_the_limit(self):
		result = self.run_session("feedFrames(); setHidden(true); advance(S.HIDDEN_PAUSE_MS - 1000);"
		                          "const early = socket.closed; advance(2000); report({early});")
		self.assertFalse(result["early"])
		self.assertEqual((result["sockets"], result["closed"], result["closes"]), (1, "page", [[PAUSED, ""]]))

	def test_visible_tab_stays_for_days(self):
		result = self.run_session("feedFrames(); advance(3 * 24 * HOUR); report();")
		self.assertEqual((result["sockets"], result["closed"], result["closes"]), (1, False, []))

	def test_index_script_history_data_keeps_minute_series_connected(self):
		html = (PFEIFFER / "pressure_web_static" / "index.html").read_text(encoding="utf-8")
		script = html.split("<script>\n", 1)[1].rsplit("\nstart();\n</script>", 1)[0]
		probe = (
			"const vm = require('vm'), fs = require('fs');\n"
			"const code = fs.readFileSync(process.env.INDEX_JS, 'utf8');\n"
			"new vm.Script(code + '\\nstart();');\n"
			"const ctx = vm.createContext({Math, Number, String, Object, Array, Date});\n"
			"vm.runInContext(code + `\n"
			"  info = {gauges: [{key: 'G1'}]};\n"
			"  history = {t: [0, 60, 120, 420], width: 60, range: 28800, points: 1400, gauges: {G1: {mean: [1e-6, 2e-6, 3e-6, 4e-6]}}};\n"
			"  globalThis.out = historyData();\n`, ctx);\n"
			"console.log(JSON.stringify(ctx.out));\n"
		)
		with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
			f.write(script)
			js_path = Path(f.name)
		self.addCleanup(js_path.unlink)
		completed = subprocess.run(["node", "-e", probe], env={**os.environ, "INDEX_JS": str(js_path)},
		                           capture_output=True, text=True, timeout=30, check=True)
		times, values = json.loads(completed.stdout.strip())
		self.assertEqual(times, [30, 90, 150, 180, 450])  # one gap only across 120..420, none between 0, 60, 120
		self.assertEqual(values, [1e-6, 2e-6, 3e-6, None, 4e-6])


if __name__ == "__main__":
	unittest.main()
