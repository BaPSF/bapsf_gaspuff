"""Tests of the pressure page's server (pressure_web.py, web_shell.py) and the page's session.js. No instrument needed.

	python -m unittest discover -s pfeiffer/tests

They need the diag_ioc package (bapsf_epics_core) and are skipped without it. The server tests run it on loopback with
a stand-in for the p4p monitors, and need fastapi, uvicorn and websockets. The end-to-end test starts the fake
controller and a real IOC, and needs softioc and pyepics: where PVA cannot run (no kernel IPv6) it feeds the server
from the IOC over CA, so the page's use of p4p itself is only tested where PVA runs. The session.js tests need node.
"""
import contextlib
import http.client
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PFEIFFER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PFEIFFER))

try:
	from diag_ioc.testing import IocProcess, epics_client, free_port, have, isolated_epics_env, monitor_all, pva_available, read_pv, wait_for
except ImportError:
	raise unittest.SkipTest("needs the diag_ioc package (bapsf_epics_core)") from None

import pressure_gas  # noqa: E402
import pressure_web as web  # noqa: E402
import web_shell  # noqa: E402
from fake_tpg366 import FakeTpg366  # noqa: E402
from pressure_ioc import STATUS_NO_REPLY, PressureModule  # noqa: E402
from test_pressure_config import options  # noqa: E402
from test_pressure_ioc import FakeBuilder, ioc_config  # noqa: E402

WEB = have("fastapi", "uvicorn", "websockets")
if WEB:
	import uvicorn
	from websockets.exceptions import ConnectionClosed
	from websockets.sync.client import connect

HOST = "127.0.0.1"
NAMES = web.Names("TEST:PRESSURE", "TPG1", "TEST:GAS")
DEPLOY = PFEIFFER / "deploy"
SESSION_JS = PFEIFFER / "pressure_web_static" / "session.js"
PAUSED = "Paused - reload to resume"
DISCONNECTED = "Disconnected - reload the page to reconnect"


def gauge(p_raw, factor=1.0, status=0, base=math.nan):
	return {"p_raw": p_raw, "gas_factor": factor, "p": p_raw * factor, "status": status, "raw_status": status, "base": base}


def sample(seq, when, gas="Helium", **gauges):
	"""A value of the group <P>:<S>:SAMPLE as nested dicts; the default gauges are G1 and G2."""
	gauges = gauges or {"g1": gauge(2e-6, 5.9, base=1.9e-6), "g2": gauge(1e-3)}
	return {"seq": seq, "time": when, "gas": gas, "read_s": 0.004, **gauges,
	        "alarm": {"severity": 0, "status": 0, "message": ""}, "timeStamp": {"secondsPastEpoch": 1, "nanoseconds": 0}}


def nt(value, severity=0, **more):
	"""The value of one record as p4p delivers it with nt=False, as nested dicts."""
	return {"value": value, "alarm": {"severity": severity, "status": 0, "message": ""}, **more}


def hub_with(*samples, history=False):
	hub = web.Hub(NAMES, history)
	for value in samples:
		hub.add(web.parse_sample(value))
	return hub


def frame(hub, state=None):
	text, state = hub.frame(state)
	return json.loads(text), state


class SampleTests(unittest.TestCase):
	def test_a_sample_is_read_gauge_by_gauge(self):
		reading = web.parse_sample(sample(7, 1000.5, "Argon", g1=gauge(2e-6, 0.8, base=1e-6), pump=gauge(math.nan, 1.0, status=4)))
		self.assertEqual((reading.keys, reading.time, reading.seq, reading.gas), (("G1", "PUMP"), 1000.5, 7, "Argon"))
		self.assertEqual(reading.gauges[0], (2e-6 * 0.8, 2e-6, 0.8, 0, 1e-6))
		self.assertTrue(math.isnan(reading.gauges[1][0]))
		self.assertEqual(reading.gauges[1][3], 4)

	def test_the_ioc_before_its_first_reading_gives_none(self):
		self.assertIsNone(web.parse_sample(sample(0, 0.0)))
		self.assertIsNone(web.parse_sample(sample(0, math.nan)))

	def test_a_group_without_gauges_is_refused(self):
		with self.assertRaisesRegex(ValueError, "no gauge"):
			web.parse_sample({"seq": 1, "time": 5.0, "gas": "Helium", "alarm": {"severity": 0}})

	def test_the_fields_read_are_the_ones_the_ioc_module_puts_in_its_group(self):
		module = PressureModule("pressure", options())
		builder = FakeBuilder("TEST:PRESSURE")
		module.create_records(builder)
		fields = {}
		for record in builder.records.values():
			for key, groups in record.infos:
				self.assertEqual((key, list(groups)), ("Q:group", [NAMES.group]))
				fields.update(groups[NAMES.group])
		fields.pop("")  # the group's own alarm and time stamp
		value = {"alarm": {"severity": 0}, "timeStamp": {"secondsPastEpoch": 1}}
		for number, name in enumerate(fields, start=1):
			branch = value
			*parents, leaf = name.split(".")
			for parent in parents:
				branch = branch.setdefault(parent, {})
			branch[leaf] = "Helium" if name == "gas" else number
		reading = web.parse_sample(value)
		self.assertEqual(sorted(reading.keys), [g.key for g in module.config.gauges])  # the group's field order is PVXS's
		self.assertEqual((reading.time, reading.seq, reading.gas), (value["time"], value["seq"], "Helium"))
		for key, values in zip(reading.keys, reading.gauges):
			branch = value[key.lower()]
			self.assertEqual(values, tuple(branch[name] for name in ("p", "p_raw", "gas_factor", "status", "base")))
		# The records beside the group that the page monitors all exist.
		for name in [*NAMES.machine_pvs(), *NAMES.gauge_pvs("G1")]:
			self.assertIn(name, builder.records)

	@unittest.skipUnless(have("p4p"), "needs p4p")
	def test_p4p_values_are_read(self):
		from p4p import Type, Value
		from p4p.nt import NTEnum, NTScalar
		gauge_type = ("S", None, [("p_raw", "d"), ("gas_factor", "d"), ("p", "d"), ("status", "i"), ("raw_status", "i"), ("base", "d")])
		group = Value(Type([("seq", "i"), ("time", "d"), ("gas", "s"), ("read_s", "d"), ("g1", gauge_type),
		                    ("alarm", ("S", "alarm_t", [("severity", "i")])), ("timeStamp", ("S", "time_t", [("secondsPastEpoch", "l")]))]),
		              {"seq": 3, "time": 12.5, "gas": "Helium", "g1": gauge(2e-6, 5.9)})
		reading = web.parse_sample(web._plain(group))
		self.assertEqual((reading.keys, reading.seq, reading.gauges[0][1:4]), (("G1",), 3, (2e-6, 5.9, 0)))

		hub = hub_with(sample(1, 100.0))
		base = NTScalar("d", valueAlarm=True).wrap(1e-6)
		base["alarm.severity"] = 1
		base["valueAlarm.highWarningLimit"], base["valueAlarm.highAlarmLimit"] = 5e-6, 2e-5
		hub.set_pv("G1", "base", web._plain(base))
		hub.set_pv(None, "species", web._plain(NTEnum().wrap({"index": 1, "choices": ["Helium", "Argon"]})))
		meta = hub.meta()
		self.assertEqual([meta["gauges"][0][name] for name in ("base_level", "warn", "alarm")], ["warn", 5e-6, 2e-5])
		self.assertEqual(meta["species"], ["Helium", "Argon"])


class HubTests(unittest.TestCase):
	def test_each_socket_gets_every_reading_once(self):
		hub = hub_with(sample(1, 100.0), sample(2, 100.1))
		first, state = frame(hub)
		self.assertTrue(first["reset"])
		self.assertEqual((first["keys"], first["t"], first["p"]), (["G1", "G2"], [100.0, 100.1], [[2e-6 * 5.9] * 2, [1e-3] * 2]))
		self.assertEqual([g["key"] for g in first["meta"]["gauges"]], ["G1", "G2"])
		self.assertEqual(first["latest"]["gauges"]["G1"], {"p": 2e-6 * 5.9, "p_raw": 2e-6, "gas_factor": 5.9, "status": 0, "base": 1.9e-6})
		self.assertEqual(first["gas"], "Helium")

		hub.add(web.parse_sample(sample(3, 100.2)))
		second, state = frame(hub, state)
		self.assertEqual((second["reset"], second["t"], "meta" in second), (False, [100.2], False))
		idle, state = frame(hub, state)
		self.assertEqual((idle["t"], idle["p"]), ([], [[], []]))
		# A socket that was blocked for a while gets what it missed in one frame.
		for seq in range(4, 9):
			hub.add(web.parse_sample(sample(seq, 100.0 + seq / 10)))
		late, state = frame(hub, state)
		self.assertEqual(len(late["t"]), 5)

	def test_no_reading_is_json_null(self):
		hub = hub_with(sample(1, 100.0, g1=gauge(math.nan, 5.9, status=STATUS_NO_REPLY), g2=gauge(math.inf)))
		text, _ = hub.frame()
		message = json.loads(text, parse_constant=self.fail)  # no NaN or Infinity in the text
		self.assertEqual(message["p"], [[None], [None]])
		self.assertEqual(message["latest"]["gauges"]["G1"], {"p": None, "p_raw": None, "gas_factor": 5.9, "status": 8, "base": None})

	def test_the_same_reading_again_is_passed_over(self):
		hub = hub_with(sample(1, 100.0), sample(2, 100.1), sample(2, 100.1))
		self.assertEqual(frame(hub)[0]["t"], [100.0, 100.1])

	def test_a_clock_set_back_or_other_gauges_start_the_chart_again(self):
		hub = hub_with(sample(1, 100.0), sample(2, 100.1))
		_, state = frame(hub)
		hub.add(web.parse_sample(sample(3, 50.0)))
		message, state = frame(hub, state)
		self.assertEqual((message["reset"], message["t"]), (True, [50.0]))

		watched = []
		hub.watch_gauge = watched.append
		hub.add(web.parse_sample(sample(4, 50.1, g2=gauge(1e-3), g7=gauge(1e-4))))
		message, state = frame(hub, state)
		self.assertEqual((message["reset"], message["keys"], message["t"]), (True, ["G2", "G7"], [50.1]))
		self.assertEqual([g["key"] for g in message["meta"]["gauges"]], ["G2", "G7"])
		self.assertEqual(watched, ["G7"])  # G2 was in the group before

	def test_only_the_live_window_is_kept(self):
		start = 1000.0
		hub = hub_with(sample(1, start), sample(2, start + 1, "Argon"), sample(3, start + web.LIVE_SECONDS + 0.5, "Argon"))
		message, _ = frame(hub)
		self.assertEqual(message["t"], [start + 1, start + web.LIVE_SECONDS + 0.5])
		self.assertEqual(message["gas_changes"], [[start + 1, "Argon"]])
		hub.add(web.parse_sample(sample(4, start + web.LIVE_SECONDS + 2, "Argon")))
		self.assertEqual(frame(hub)[0]["gas_changes"], [])  # the change left the window with its reading
		with mock.patch.object(web, "LIVE_ROWS_MAX", 3):
			for seq in range(5, 10):
				hub.add(web.parse_sample(sample(seq, start + web.LIVE_SECONDS + seq)))
		self.assertEqual(len(frame(hub)[0]["t"]), 3)

	def test_waiting_follows_the_age_of_the_last_reading(self):
		hub = web.Hub(NAMES, False)
		self.assertEqual([frame(hub)[0][name] for name in ("waiting", "age_s", "latest", "gas")], [True, None, None, None])
		hub.add(web.parse_sample(sample(1, 100.0)))
		with mock.patch.object(web.time, "time", return_value=100.0 + web.WAITING_AFTER_S - 0.1):
			self.assertFalse(frame(hub)[0]["waiting"])
		with mock.patch.object(web.time, "time", return_value=100.0 + web.WAITING_AFTER_S):
			message, _ = frame(hub)
		self.assertEqual((message["waiting"], message["age_s"]), (True, web.WAITING_AFTER_S))

	def test_the_records_beside_the_group_fill_the_meta(self):
		hub = hub_with(sample(1, 100.0), history=True)
		_, state = frame(hub)
		for key, channel, name in (("G1", 3, "Test port"), ("G2", 1, "<b>Foreline</b>")):
			hub.set_pv(key, "channel", nt(channel))
			hub.set_pv(key, "name", nt(name))
		hub.set_pv("G1", "model", nt("IKR "))
		hub.set_pv("G1", "device_gas", nt("Argon", severity=1))
		hub.set_pv("G1", "base", nt(1.5e-5, severity=2, valueAlarm={"highWarningLimit": 5e-6, "highAlarmLimit": 1e-5}))
		hub.set_pv("G2", "base", nt(math.nan, severity=3, valueAlarm={"highWarningLimit": 0.0, "highAlarmLimit": math.nan}))
		hub.set_pv(None, "species", nt({"index": 2, "choices": ["Helium", "Hydrogen", "Argon"]}))
		hub.set_pv(None, "gas_since", nt(90.0))
		hub.set_pv(None, "base_window_h", nt(12.0))
		hub.set_pv(None, "period_s", nt(0.1))
		message, state = frame(hub, state)
		meta = message["meta"]
		self.assertEqual([g["key"] for g in meta["gauges"]], ["G2", "G1"])  # in the order of the controller channels
		self.assertEqual(meta["gauges"][1], {"key": "G1", "name": "Test port", "channel": 3, "model": "IKR", "device_gas": "Argon",
		                                     "device_gas_alarm": True, "base_level": "alarm", "warn": 5e-6, "alarm": 1e-5})
		self.assertEqual([meta["gauges"][0][name] for name in ("base_level", "warn", "alarm")], ["", None, None])
		self.assertEqual([meta[name] for name in ("species", "gas_since", "base_window_h", "period_s", "history", "zone")],
		                 [["Helium", "Hydrogen", "Argon"], 90.0, 12.0, 0.1, True, "America/Los_Angeles"])
		# The same values again, or one that cannot be read, send no new meta.
		hub.set_pv("G1", "model", nt("IKR"))
		with self.assertLogs(web.log, "WARNING"):
			hub.set_pv("G1", "channel", nt("not a number"))
		self.assertNotIn("meta", frame(hub, state)[0])

	def test_before_the_first_reading_the_gas_is_the_selection_record(self):
		hub = web.Hub(NAMES, False)
		hub.set_pv(None, "species", nt({"index": 1, "choices": ["Helium", "Hydrogen"]}))
		self.assertEqual(frame(hub)[0]["gas"], "Hydrogen")


class HistoryMessageTests(unittest.TestCase):
	def test_arrays_become_json(self):
		import numpy as np
		result = {"t": np.array([10.0, 20.0]), "gas": [[5.0, "Helium"], [15.0, "Argon"]],
		          "gauges": {"G1": {"mean": np.array([1e-6, np.nan]), "min": np.array([9e-7, np.nan]), "max": [2e-6, None], "count": [1, 0]}}}
		message = web.history_message(result)
		self.assertEqual(message, {"t": [10.0, 20.0], "gas": [[5.0, "Helium"], [15.0, "Argon"]],
		                           "gauges": {"G1": {"mean": [1e-6, None], "min": [9e-7, None], "max": [2e-6, None]}}})
		json.dumps(message, allow_nan=False)

	def test_a_series_of_another_length_is_refused(self):
		with self.assertRaisesRegex(ValueError, "G1 mean: 1 values for 2 times"):
			web.history_message({"t": [1.0, 2.0], "gauges": {"G1": {"mean": [1e-6]}}})


#===============================================================================================================================================
# The server on loopback

@contextlib.contextmanager
def running(history=None, monitor=None, names=NAMES, **limits):
	"""The page server on a free loopback port, with `limits` (module constants) patched; yields (port, feed, watched).
	Without `monitor`, a stand-in for p4p: `watched` is {PV name: callback}, to be called as p4p would."""
	feeds, watched = [], {}

	def stand_in(feed):
		feed.start(watched.__setitem__)
		return feed.stop

	def start(feed):
		feeds.append(feed)
		return (monitor or stand_in)(feed)

	with contextlib.ExitStack() as stack:
		for module in (web, web_shell):
			patch = {name: value for name, value in limits.items() if hasattr(module, name)}
			if patch:
				stack.enter_context(mock.patch.multiple(module, **patch))
		port = free_port()
		app = web.create_app(names, "/var/lib/pressure" if history else None, history, monitor=start)
		server = uvicorn.Server(uvicorn.Config(app, **web_shell.server_settings(HOST, port)))
		thread = threading.Thread(target=server.run, name="pressure_web test server", daemon=True)
		thread.start()
		try:
			wait_for(lambda: server.started, 15, "the server to start")
			yield port, feeds[0], watched
		finally:
			server.should_exit = True
			thread.join(20)
			if thread.is_alive():
				raise AssertionError("the server did not stop within 20 s")


def request(port, path="/", method="GET"):
	"""(status, body) of one request on a new connection."""
	connection = http.client.HTTPConnection(HOST, port, timeout=10)
	try:
		connection.request(method, path)
		response = connection.getresponse()
		return response.status, response.read()
	finally:
		connection.close()


def frames(socket, until, timeout=10.0):
	"""Frames from `socket` up to the first for which until(frame) holds; returns that one."""
	deadline = time.monotonic() + timeout
	while True:
		message = json.loads(socket.recv(timeout=max(0.1, deadline - time.monotonic())))
		if until(message):
			return message


@unittest.skipUnless(WEB, "needs fastapi, uvicorn and websockets")
class ServerTests(unittest.TestCase):
	def test_readings_reach_a_browser(self):
		with running(FRAME_PERIOD_S=0.05) as (port, feed, watched):
			self.assertEqual(set(watched), {NAMES.group, *NAMES.machine_pvs()})
			with connect(f"ws://{HOST}:{port}/ws") as socket:
				first = json.loads(socket.recv(timeout=5))
				self.assertEqual((first["waiting"], first["latest"], first["meta"]["gauges"]), (True, None, []))
				now = time.time()
				watched[NAMES.group](sample(1, now - 0.1))  # as p4p calls it, from another thread
				watched[NAMES.group](sample(2, now))
				message = frames(socket, lambda m: m["latest"] is not None)
				self.assertEqual((message["keys"], message["waiting"], len(message["t"])), (["G1", "G2"], False, 2))
				self.assertEqual([g["key"] for g in message["meta"]["gauges"]], ["G1", "G2"])
				# The first sight of a gauge starts the monitors of its records.
				self.assertLessEqual({*NAMES.gauge_pvs("G1"), *NAMES.gauge_pvs("G2")}, set(watched))
				watched[f"{NAMES.prefix}:G2:NAME"](nt("Foreline"))
				watched[f"{NAMES.prefix}:G2:NAME"](RuntimeError("disconnected"))  # changes nothing
				message = frames(socket, lambda m: "meta" in m)
				self.assertEqual(message["meta"]["gauges"][1]["name"], "Foreline")
				with self.assertLogs(web.log, "WARNING") as logged:
					watched[NAMES.group](RuntimeError("disconnected"))
					watched[NAMES.group]({"seq": 3})  # another layout: logged, not shown
				self.assertEqual(len(logged.records), 2)
				watched[NAMES.group](sample(3, now + 0.1))
				self.assertEqual(frames(socket, lambda m: m["t"])["t"], [now + 0.1])
			wait_for(lambda: feed.hub.viewers.total() == 0, 5, "the viewer to be counted out")

	def test_the_page_and_its_files_are_served(self):
		with running() as (port, feed, watched):
			status, body = request(port)
			self.assertEqual(status, 200)
			for name in re.findall(rb'(?:src|href)="(static/[^"]+)"', body):
				self.assertEqual(request(port, "/" + name.decode())[0], 200, name)
			self.assertIn(b"static/pressure_page.js", body)

	def test_viewer_caps(self):
		with running(MAX_VIEWERS=3, MAX_VIEWERS_PER_ADDRESS=2) as (port, feed, watched):
			with connect(f"ws://{HOST}:{port}/ws") as one, connect(f"ws://{HOST}:{port}/ws") as two:
				for socket in (one, two):
					socket.recv(timeout=5)
				with connect(f"ws://{HOST}:{port}/ws") as third, self.assertRaises(ConnectionClosed) as closed:
					third.recv(timeout=5)
				self.assertEqual(closed.exception.rcvd.code, web_shell.TRY_AGAIN_LATER)
				self.assertIn("2 pages are already open from this address", closed.exception.rcvd.reason)
				self.assertEqual(feed.hub.viewers.total(), 2)
			wait_for(lambda: feed.hub.viewers.total() == 0, 5, "both viewers to be counted out")

	def test_history_comes_from_the_store_s_reader(self):
		calls = []

		def history(directory, source, start, end, buckets, corrected=True):
			calls.append((directory, source, start, end, buckets, corrected))
			if start < 0:
				raise OSError("disk trouble")
			return {"t": [start], "gauges": {"G1": {"mean": [math.nan], "min": [1e-6], "max": [2e-6]}}, "gas": [[start, "Helium"]]}

		with running(history, HISTORY_BURST=100) as (port, feed, watched):
			status, body = request(port, "/api/history?start=100&end=200&points=480")
			self.assertEqual((status, json.loads(body)), (200, {"t": [100.0], "gas": [[100.0, "Helium"]],
			                                                 "gauges": {"G1": {"mean": [None], "min": [1e-6], "max": [2e-6]}}}))
			self.assertEqual(request(port, "/api/history?start=100&end=200&points=10&raw=1")[0], 200)
			self.assertEqual(calls, [("/var/lib/pressure", "TPG1", 100.0, 200.0, 480, True), ("/var/lib/pressure", "TPG1", 100.0, 200.0, 10, False)])
			with self.assertLogs(web.log, "WARNING"):
				self.assertEqual(request(port, "/api/history?start=-5&end=200&points=10")[0], 500)
			for query in ("start=200&end=100&points=10", "start=0&end=1e9&points=10", "start=100&end=200&points=0",
			              f"start=100&end=200&points={web.HISTORY_POINTS_MAX + 1}", "start=nan&end=200&points=10"):
				self.assertEqual(request(port, f"/api/history?{query}")[0], 422, query)
			frame_meta = json.loads(feed.hub.frame()[0])["meta"]
			self.assertTrue(frame_meta["history"])

	def test_history_limits(self):
		with running(lambda *a, **k: {"t": [], "gauges": {}}, HISTORY_BURST=2, HISTORY_RATE_PER_S=0.01) as (port, feed, watched):
			statuses = [request(port, "/api/history?start=100&end=200&points=10")[0] for _ in range(3)]
			self.assertEqual(statuses, [200, 200, 429])
		with running() as (port, feed, watched):  # no store
			self.assertEqual(request(port, "/api/history?start=100&end=200&points=10")[0], 404)
			self.assertFalse(json.loads(feed.hub.frame()[0])["meta"]["history"])

	def test_read_only(self):
		with running() as (port, feed, watched):
			for path in ("/", "/api/history?start=100&end=200&points=10", "/ws", "/static/pressure_page.js"):
				for method in ("POST", "PUT", "DELETE"):
					self.assertIn(request(port, path, method)[0], (404, 405), (method, path))
		for name in ("pressure_web.py", "web_shell.py"):  # the server has no PV write in it
			self.assertNotRegex((PFEIFFER / name).read_text(encoding="utf-8"), r"\.put\(|caput|rpc\(")


#===============================================================================================================================================
# Fake controller -> real IOC -> page server

GAUGE_CHAIN = ("P_RAW", "GAS_FACTOR", "P", "STATUS", "RAW_STATUS", "BASE")


class CaFeed:
	"""A stand-in for monitor_ioc where PVA cannot run: it gives the page server the IOC's values read over CA, in the
	shape p4p delivers them. The chain posts SAMPLE:SEQ first and the rest of the reading right behind it, so one
	sample is every chain record's value SETTLE_S after a SEQ update (the read period is 0.1 s)."""
	SETTLE_S = 0.04

	def __init__(self, names, keys, ioc):
		self.names, self.keys, self.ioc = names, keys, ioc
		prefix, source = names.prefix, names.source
		self.seq = f"{prefix}:{source}:SAMPLE:SEQ"
		self.chain = {self.seq: "seq", f"{prefix}:{source}:SAMPLE:TIME": "time", f"{prefix}:{source}:SAMPLE:GAS": "gas"}
		for key in keys:
			self.chain.update({f"{prefix}:{key}:{record}": (key.lower(), record.lower()) for record in GAUGE_CHAIN})
		self.values, self.feed, self.watched = {}, None, {}
		self._lock = threading.Lock()
		self._stop = threading.Event()

	def __call__(self, feed):
		self.feed = feed
		feed.start(self.watched.__setitem__)
		self.values = {name: read_pv(name, as_string=name.endswith(":GAS"), ioc=self.ioc)["value"] for name in self.chain}
		pvs = monitor_all(list(self.chain), self.on_update, timeout=10)
		poll = threading.Thread(target=self.poll_records, name="CaFeed records", daemon=True)
		poll.start()

		def stop():
			self._stop.set()
			poll.join(10)
			for pv in pvs:
				pv.disconnect()
			feed.stop()

		return stop

	def on_update(self, name, value, stamp, severity):
		with self._lock:
			self.values[name] = value.decode() if isinstance(value, bytes) else value
		if name == self.seq:
			threading.Timer(self.SETTLE_S, self.hand_over).start()

	def hand_over(self):
		sample_now = {"alarm": {"severity": 0}}
		with self._lock:
			for record, field in self.chain.items():
				if isinstance(field, tuple):
					sample_now.setdefault(field[0], {})[field[1]] = self.values[record]
				else:
					sample_now[field] = self.values[record]
		if not self._stop.is_set():
			self.feed.on_sample(sample_now)

	def poll_records(self):
		"""The records beside the group, read once a second and handed over as p4p would."""
		while not self._stop.is_set():
			for name, callback in list(self.watched.items()):
				if name == self.names.group:
					continue
				try:
					callback(self.read(name))
				except AssertionError:
					pass  # the IOC is stopping
			self._stop.wait(1.0)

	def read(self, name):
		record = name.rsplit(":", 1)[1]
		data = read_pv(name, as_string=record in ("NAME", "MODEL", "DEVICE_GAS"), ioc=self.ioc)
		value, more = data["value"], {}
		if record == "SPECIES":
			choices = epics_client().get_pv(name, form="ctrl", connect=True, timeout=5).get_ctrlvars(timeout=5)["enum_strs"]
			value = {"index": int(value), "choices": list(choices)}
		elif record == "BASE":
			more = {"valueAlarm": {"highWarningLimit": read_pv(f"{name}.HIGH")["value"], "highAlarmLimit": read_pv(f"{name}.HIHI")["value"]}}
		return nt(value, data["severity"], **more)


@unittest.skipUnless(WEB and have("softioc", "epics"), "needs fastapi, uvicorn, websockets, softioc and pyepics")
class EndToEndTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.prefix = f"WEB{os.getpid()}"
		cls.names = web.Names(cls.prefix, "TPG1", f"{cls.prefix}GAS")
		cls.fake = FakeTpg366().start()
		cls.addClassCleanup(cls.fake.stop)
		tmp = tempfile.TemporaryDirectory()
		cls.addClassCleanup(tmp.cleanup)
		cls.selection_file = Path(tmp.name) / "gas_species"
		config = ioc_config(cls.prefix, cls.fake, cls.selection_file)
		cls.ioc = IocProcess(config, f"{cls.prefix}:IOC:HEARTBEAT", python_path=[PFEIFFER])
		cls.enterClassContext(cls.ioc)
		wait_for(lambda: read_pv(f"{cls.prefix}:TPG1:CONNECTED", ioc=cls.ioc)["value"] == 1, 10, "the first reading")
		import tomllib
		cls.gas = pressure_gas.parse_gas(tomllib.loads(config)["module"][0]["options"]["gas"])

	def check_the_page(self, port):
		"""What a browser is sent: the fake controller's pressures through the IOC, a gas change, and an outage."""
		fake = self.fake
		with connect(f"ws://{HOST}:{port}/ws") as socket:
			meta = {}  # the newest one: a frame carries it only when it changed

			def complete(m):
				meta.update(m.get("meta", {}))
				gauges = meta.get("gauges", [])
				return (m["latest"] is not None and len(gauges) == 2 and all(g["name"] and g["model"] and g["channel"] for g in gauges)
				        and gauges[0]["warn"] is not None and meta["species"] and meta["period_s"] is not None
				        and meta["base_window_h"] is not None)

			message = frames(socket, complete, timeout=30)
			latest = message["latest"]["gauges"]
			self.assertEqual([(g["key"], g["channel"], g["name"], g["model"]) for g in meta["gauges"]],
			                 [("G1", 1, "Test port", fake.models[0]), ("G2", 3, "Foreline", fake.models[2])])
			self.assertEqual(latest["G1"]["p_raw"], fake.pressures_torr[0])
			self.assertAlmostEqual(latest["G1"]["p"], fake.pressures_torr[0] * 5.9)
			self.assertEqual((latest["G1"]["gas_factor"], latest["G1"]["status"], latest["G1"]["base"]), (5.9, 0, None))
			self.assertEqual(latest["G2"]["p"], fake.pressures_torr[2])
			self.assertEqual((message["waiting"], message["gas"]), (False, "Helium"))
			self.assertAlmostEqual(message["now"], time.time(), delta=2.0)
			self.assertEqual((meta["species"], meta["base_window_h"], meta["period_s"]), (list(self.gas.species), 12.0, 0.1))
			self.assertEqual([(g["warn"], g["alarm"], g["base_level"], g["device_gas_alarm"]) for g in meta["gauges"]],
			                 [(5e-6, 2e-5, "", False), (None, None, "", False)])
			# Ten readings a second arrive in order and without a gap.
			times = []
			while len(times) < 20:
				times += json.loads(socket.recv(timeout=5))["t"]
			steps = [b - a for a, b in zip(times, times[1:])]
			self.assertAlmostEqual(sum(steps) / len(steps), 0.1, delta=0.02)
			self.assertGreater(min(steps), 0)

			pressure_gas.write_selection(self.gas, "hydrogen")
			self.addCleanup(self.selection_file.unlink)
			message = frames(socket, lambda m: m["gas"] == "Hydrogen" and m["latest"]["gauges"]["G1"]["gas_factor"] == 2.4, timeout=10)
			self.assertEqual([species for _, species in message["gas_changes"]], ["Hydrogen"])

			fake.silent = True  # the controller stops answering: one reading with no value, then nothing
			message = frames(socket, lambda m: m["waiting"], timeout=15)
			self.assertEqual([g["status"] for g in message["latest"]["gauges"].values()], [STATUS_NO_REPLY] * 2)
			self.assertEqual(message["latest"]["gauges"]["G1"]["p"], None)
			fake.silent = False
			message = frames(socket, lambda m: not m["waiting"], timeout=40)
			self.assertEqual(message["latest"]["gauges"]["G1"]["status"], 0)

	def test_the_page_follows_the_ioc_over_ca(self):
		with running(monitor=CaFeed(self.names, ("G1", "G2"), self.ioc), names=self.names, FRAME_PERIOD_S=0.1) as (port, feed, watched):
			self.check_the_page(port)

	@unittest.skipUnless(pva_available(), "needs p4p and kernel IPv6")
	def test_the_page_follows_the_ioc_over_pva(self):
		os.environ.update(isolated_epics_env())
		with running(monitor=web.monitor_ioc, names=self.names, FRAME_PERIOD_S=0.1) as (port, feed, watched):
			self.check_the_page(port)


#===============================================================================================================================================
# Deploy examples

class DeployTests(unittest.TestCase):
	def read(self, name):
		return (DEPLOY / name).read_text(encoding="utf-8")

	def test_the_page_unit_and_the_proxy_agree_with_the_code(self):
		unit = self.read("systemd/pressure-web.service")
		self.assertIn("\nUser=diag-ioc\n", unit)
		self.assertIn("pressure_web.py --prefix LAPD:MACH:PRESSURE --source TPG1 --gas-prefix LAPD:MACH:GAS --store-dir /var/lib/pressure\n", unit)
		self.assertNotIn("--port", unit)  # the default, which the proxy names
		site = self.read("nginx/diagnostic.nginx.example")
		self.assertIn(f"proxy_pass http://127.0.0.1:{web.DEFAULT_PORT}/;", site)
		self.assertIn("location /diagnostic/pressure/ {", site)
		self.assertEqual(web_shell.server_settings("127.0.0.1", web.DEFAULT_PORT)["forwarded_allow_ips"], "127.0.0.1")
		example = (DEPLOY / "diag-ioc" / "pressure.toml.example").read_text(encoding="utf-8")
		for value in ('prefix = "LAPD:MACH:PRESSURE"', 'key = "TPG1"', 'prefix = "LAPD:MACH:GAS"'):
			self.assertIn(value, example)

	def test_no_real_address_in_the_examples(self):
		for path in sorted(p for p in DEPLOY.rglob("*") if p.is_file()):
			addresses = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", path.read_text(encoding="utf-8")))
			allowed = {a for a in addresses if a in ("127.0.0.1", "0.0.0.0") or a.startswith("192.0.2.")}  # loopback, any, documentation
			self.assertEqual(addresses - allowed, set(), path.name)


#===============================================================================================================================================
# session.js in node, with fake timers

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
const closes = [], got = [];
S.openSession({url: "ws://test/ws", WebSocket: FakeSocket, document, timers: fake, now: () => now,
               onFrame: frame => got.push(frame), onClosed: (note, reason) => closes.push([note, reason])});
const socket = sockets[0];
const feedFrames = () => fake.setInterval(() => { if (!socket.closed) socket.onmessage({data: '{"now": 1}'}); }, 500);
const report = extra => console.log(JSON.stringify({sockets: sockets.length, closed: socket.closed, closes, frames: got.length, ...extra}));
const HOUR = 3600 * 1000;
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class PageSessionTests(unittest.TestCase):
	"""session.js: one socket for the life of the page, the hidden-tab pause, and no timeout for a visible tab."""

	def run_session(self, steps):
		completed = subprocess.run(["node", "-e", _SESSION_HARNESS + steps], env={**os.environ, "SESSION_JS": str(SESSION_JS)},
		                           capture_output=True, text=True, timeout=120, check=True)
		return json.loads(completed.stdout.strip().splitlines()[-1])

	def test_no_reconnect_after_the_server_closes_or_the_link_dies(self):
		refused = '{code: 1013, reason: "the page already has 20 viewers; try again later"}'
		result = self.run_session(f"feedFrames(); advance(2000); socket.closedBy('server', {refused}); advance(24 * HOUR); report();")
		self.assertEqual((result["sockets"], result["frames"], len(result["closes"])), (1, 4, 1))
		self.assertEqual(result["closes"][0][0], DISCONNECTED)
		self.assertIn("20 viewers", result["closes"][0][1])
		result = self.run_session("advance(S.FRAME_TIMEOUT_MS + 2000); advance(24 * HOUR); report();")
		self.assertEqual((result["sockets"], result["closed"], result["closes"]), (1, "page", [[DISCONNECTED, ""]]))

	def test_a_hidden_tab_pauses_after_the_limit_and_not_before(self):
		result = self.run_session("feedFrames(); setHidden(true); advance(S.HIDDEN_PAUSE_MS - 1000);"
		                          "const early = socket.closed; advance(2000); report({early, limit: S.HIDDEN_PAUSE_MS});")
		self.assertEqual((result["limit"], result["early"]), (15 * 60 * 1000, False))
		self.assertEqual((result["sockets"], result["closed"], result["closes"]), (1, "page", [[PAUSED, ""]]))
		result = self.run_session("feedFrames(); setHidden(true); advance(S.HIDDEN_PAUSE_MS / 2); setHidden(false);"
		                          "advance(2 * S.HIDDEN_PAUSE_MS); report();")
		self.assertEqual((result["closed"], result["closes"]), (False, []))

	def test_a_visible_tab_stays_for_days(self):
		result = self.run_session("feedFrames(); advance(3 * 24 * HOUR); report();")
		self.assertEqual((result["sockets"], result["closed"], result["closes"]), (1, False, []))


if __name__ == "__main__":
	unittest.main()
