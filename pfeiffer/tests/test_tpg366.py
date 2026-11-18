"""Tests of the TPG 366 driver against the fake controller. No instrument and no extra package needed:

	python -m unittest discover -s pfeiffer/tests
"""
import math
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tpg366  # noqa: E402
from fake_tpg366 import FakeTpg366  # noqa: E402

TIMEOUT_S = 0.5


class Tpg366Test(unittest.TestCase):
	def setUp(self):
		self.fake = FakeTpg366().start()
		self.addCleanup(self.fake.stop)
		self.driver = tpg366.Tpg366(self.fake.host, self.fake.port, timeout_s=TIMEOUT_S)
		self.addCleanup(self.driver.close)

	def connect(self):
		"""Connect once the fake has let go of the previous connection: it takes one client, as the controller does."""
		self.driver.close()
		deadline = time.monotonic() + 2.0
		while self.fake.busy and time.monotonic() < deadline:
			time.sleep(0.005)
		return self.driver.connect()

	def assert_fails_and_closes(self, call, text):
		with self.assertRaises(tpg366.Tpg366Error) as raised:
			call()
		self.assertNotIsInstance(raised.exception, tpg366.Tpg366Nak)
		self.assertIn(text, str(raised.exception))
		self.assertFalse(self.driver.connected)

	def test_describe(self):
		self.fake.gases = [0, 1, 2, 3, 0, 7]
		description = self.connect()
		self.assertEqual((description.driver, description.driver_version), ("tpg366", tpg366.DRIVER_VERSION))
		self.assertEqual(
			(description.model, description.part_number, description.serial_number, description.firmware, description.hardware),
			("TPG366", "FAKE0000", "00000000", "010100", "010100"),
		)
		self.assertEqual(description.unit, "Torr")
		self.assertEqual([c.model for c in description.channels], self.fake.models)
		self.assertEqual([c.name for c in description.channels], self.fake.names)
		self.assertEqual([c.gas for c in description.channels], ["Nitrogen", "Argon", "Hydrogen", "Helium", "Nitrogen", "Other"])
		self.assertEqual([c.gas_code for c in description.channels], self.fake.gases)

	def test_read(self):
		self.connect()
		before = time.time()
		reading = self.driver.read()
		self.assertEqual(len(reading.channels), 6)
		self.assertTrue(before <= reading.time <= time.time())
		self.assertTrue(0 < reading.read_s < TIMEOUT_S)
		for channel, expected in zip(reading.channels, self.fake.pressures_torr):
			self.assertEqual((channel.status, channel.raw_status), (tpg366.STATUS_OK, 0))
			self.assertAlmostEqual(channel.pressure_torr / expected, 1.0, places=4)  # sent with 5 digits

	def test_every_pressure_unit_is_converted_to_torr(self):
		for unit, name in ((0, "mbar"), (1, "Torr"), (2, "Pa"), (3, "Micron"), (4, "hPa")):
			with self.subTest(unit=name):
				self.fake.unit = unit
				self.assertEqual(self.connect().unit, name)
				for channel, expected in zip(self.driver.read().channels, self.fake.pressures_torr):
					self.assertAlmostEqual(channel.pressure_torr / expected, 1.0, places=4)
		# One value by hand: 1 hPa = 100 Pa = 100 * 760 / 101325 Torr.
		self.fake.replies["PRX"] = ",".join(["0,+1.0000E+00"] * 6)
		self.assertAlmostEqual(self.driver.read().channels[0].pressure_torr, 0.7500617, places=7)

	def test_volt_is_refused(self):
		self.fake.unit = 5
		self.assert_fails_and_closes(self.connect, "Volt")

	def test_unit_change_is_picked_up_by_describe(self):
		self.connect()
		self.fake.unit = 4
		self.assertEqual(self.driver.describe().unit, "hPa")
		self.assertAlmostEqual(self.driver.read().channels[3].pressure_torr / self.fake.pressures_torr[3], 1.0, places=4)

	def test_statuses(self):
		self.fake.statuses = [0, 1, 2, 3, 5, 9]
		self.connect()
		channels = self.driver.read().channels
		self.assertEqual([c.status for c in channels], [0, 1, 2, 3, 5, tpg366.STATUS_OTHER])
		self.assertEqual([c.raw_status for c in channels], [0, 1, 2, 3, 5, 9])
		# Underrange and overrange keep the end of the range; the others carry no pressure.
		self.assertEqual([math.isnan(c.pressure_torr) for c in channels], [False, False, False, True, True, True])

	def test_only_read_commands_are_sent(self):
		self.connect()
		self.driver.read()
		self.driver.describe()
		self.assertEqual(set(self.fake.commands), {"AYT", "UNI", "TID", "GAS", "CID", "PRX"})

	def test_no_reply_times_out_and_reconnect_works(self):
		self.connect()
		self.fake.silent = True
		t0 = time.monotonic()
		self.assert_fails_and_closes(self.driver.read, "no reply")
		self.assertLess(time.monotonic() - t0, TIMEOUT_S + 0.5)
		with self.assertRaises(tpg366.Tpg366Error):
			self.driver.read()  # not connected: fails at once, sends nothing
		self.fake.silent = False
		self.connect()
		self.assertEqual(len(self.driver.read().channels), 6)

	def test_dropped_connection_and_reconnect(self):
		self.connect()
		self.fake.drop_client()
		self.assert_fails_and_closes(self.driver.read, "")
		self.connect()
		self.assertEqual(len(self.driver.read().channels), 6)
		self.assertEqual(self.fake.connections, 2)

	def test_garbled_replies(self):
		good = "0,+1.0000E-06"
		for reply, text in (
			(",".join([good] * 5), "fields"),  # one channel missing
			(",".join([good] * 5 + ["x,+1.0000E-06"]), "status,pressure"),
			(",".join([good] * 5 + ["0,\x06"]), "status,pressure"),  # a stray ACK in the data, as the old logger saw
			(b"\xff\xff\r\n", "ASCII"),
			(b"9" * 5000, "without a line end"),
		):
			with self.subTest(reply=reply):
				self.fake.replies.clear()
				self.connect()
				self.fake.replies["PRX"] = reply
				self.assert_fails_and_closes(self.driver.read, text)

	def test_reply_that_is_not_ack(self):
		self.connect()
		line = ",".join(["0,+1.0000E-06"] * 6).encode() + b"\r\n"
		self.fake.replies["PRX"] = line + line  # the second line, which nobody asked for, is read as the next reply
		self.driver.read()
		self.assert_fails_and_closes(self.driver.read, "expected ACK")

	def test_refused_command_keeps_the_connection(self):
		self.connect()
		self.fake.refuse.add("PRX")
		with self.assertRaises(tpg366.Tpg366Nak) as raised:
			self.driver.read()
		self.assertIn("syntax error", str(raised.exception))
		self.assertTrue(self.driver.connected)
		self.fake.refuse.clear()
		self.assertEqual(len(self.driver.read().channels), 6)
		self.assertEqual(self.fake.connections, 1)

	def test_identity_is_blank_when_ayt_is_refused(self):
		self.fake.refuse.add("AYT")
		description = self.connect()
		self.assertEqual((description.model, description.serial_number), ("", ""))
		self.assertEqual(description.unit, "Torr")

	def test_controller_takes_one_client(self):
		self.connect()
		second = tpg366.Tpg366(self.fake.host, self.fake.port, timeout_s=TIMEOUT_S)
		with self.assertRaises(tpg366.Tpg366Error):
			second.connect()
		self.assertFalse(second.connected)
		self.assertEqual(self.fake.turned_away, 1)
		self.assertEqual(len(self.driver.read().channels), 6)  # the first client is not disturbed

	def test_leftover_continuous_output_is_discarded(self):
		self.fake.streaming = True  # measurement lines arrive before the first command is answered
		self.assertEqual(self.connect().unit, "Torr")
		self.assertEqual(len(self.driver.read().channels), 6)

	def test_close_from_another_thread_ends_a_waiting_read(self):
		driver = tpg366.Tpg366(self.fake.host, self.fake.port, timeout_s=10.0)
		driver.connect()
		self.fake.silent = True
		threading.Timer(0.2, driver.close).start()
		t0 = time.monotonic()
		with self.assertRaises(tpg366.Tpg366Error):
			driver.read()
		self.assertLess(time.monotonic() - t0, 2.0)

	def test_connect_refused(self):
		with socket.socket() as unused:
			unused.bind(("127.0.0.1", 0))
			port = unused.getsockname()[1]
		driver = tpg366.Tpg366("127.0.0.1", port, timeout_s=TIMEOUT_S)
		with self.assertRaises(tpg366.Tpg366Error) as raised:
			driver.connect()
		self.assertIn("connect failed", str(raised.exception))

	def test_a_reading_takes_far_less_than_the_read_period(self):
		self.connect()
		self.assertLess(max(self.driver.read().read_s for _ in range(50)), 0.1)  # 10 readings a second


class HelpersTest(unittest.TestCase):
	def test_reconnect_delay_grows_to_a_limit_and_resets(self):
		delay = tpg366.ReconnectDelay(first_s=1.0, factor=2.0, max_s=30.0)
		self.assertEqual([delay.next() for _ in range(7)], [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0])
		delay.reset()
		self.assertEqual(delay.next(), 1.0)

	def test_parse_address(self):
		self.assertEqual(tpg366.parse_address("controller.example"), ("controller.example", 8000))
		self.assertEqual(tpg366.parse_address(" controller.example:8001 "), ("controller.example", 8001))
		self.assertEqual(tpg366.parse_address("[::1]"), ("::1", 8000))
		self.assertEqual(tpg366.parse_address("[::1]:8001"), ("::1", 8001))
		for bad in ("", ":8000", "controller.example:", "controller.example:x", "controller.example:0", "a:b:8000", "[::1"):
			with self.subTest(address=bad), self.assertRaises(ValueError):
				tpg366.parse_address(bad)


if __name__ == "__main__":
	unittest.main()
