"""A fake Pfeiffer TPG 366 controller: a small TCP server that speaks the controller's protocol, for tests and
for running the pressure IOC with no instrument.

It answers the read commands AYT, UNI, TID, GAS, CID, PRX and ERR, refuses everything else (and every attempt to
change a setting) with <NAK>, and serves one client at a time like the real controller: a second connection is
closed at once. All values are made up.

	python fake_tpg366.py --port 8000

Standard library only. It does not import tpg366, so the tests do not check the driver against itself.
"""
import argparse
import math
import socket
import threading
import time

_ETX = 0x03
_ENQ = 0x05
_ACK = b"\x06\r\n"
_NAK = b"\x15\r\n"
_END = b"\r\n"
_READ_COMMANDS = ("AYT", "UNI", "TID", "GAS", "CID", "PRX", "ERR")
# One Torr in each unit the controller can be set to (UNI code): mbar, Torr, Pa, Micron, hPa. 5 is Volt.
_UNITS_PER_TORR = {0: 1.3332236842, 1: 1.0, 2: 133.32236842, 3: 1000.0, 4: 1.3332236842}


class FakeTpg366:
	"""Start with start() or as a context manager. Tests change the public attributes at any time."""

	def __init__(self, host="127.0.0.1", port=0):
		# What the controller reports; channel 1 first.
		self.pressures_torr = [2.0e-6, 5.0e-5, 1.0e-3, 7.6e2, 1.0e-7, 3.0e-4]
		self.statuses = [0, 0, 0, 0, 0, 0]
		self.unit = 1  # UNI code; the pressures are sent in this unit
		self.gases = [0, 0, 0, 0, 0, 0]  # GAS codes
		self.models = ["PKR", "PKR", "TPR/PCR", "CMR/APR", "IKR", "PBR"]
		self.names = ["FAKE_1", "FAKE_2", "FAKE_3", "FAKE_4", "FAKE_5", "FAKE_6"]
		self.identity = "TPG366,FAKE0000,00000000,010100,010100"
		# Faults.
		self.silent = False  # take commands, send nothing
		self.refuse = set()  # mnemonics answered with <NAK>
		self.replies = {}  # mnemonic -> sent instead of the real data: a str as a line, bytes exactly as given
		self.streaming = False  # a new client is sent a continuous output until its first byte arrives
		# What happened.
		self.commands = []  # every command line received, e.g. "PRX"
		self.connections = 0
		self.turned_away = 0  # connections closed because a client was already connected

		self._lock = threading.Lock()
		self._client = None
		self._stopping = False
		self._threads = []
		self._listener = socket.create_server((host, port))
		self.host, self.port = self._listener.getsockname()[:2]

	def start(self):
		self._spawn(self._accept)
		return self

	def stop(self):
		self._stopping = True
		self.drop_client()
		for thread in self._threads:
			thread.join(2.0)
		self._listener.close()

	def drop_client(self):
		"""Close the current client's connection, as a power cut or a pulled cable would."""
		with self._lock:
			client = self._client
		if client is not None:
			try:
				client.shutdown(socket.SHUT_RDWR)
			except OSError:
				pass

	@property
	def busy(self):
		"""True while a client is connected."""
		with self._lock:
			return self._client is not None

	def __enter__(self):
		return self.start()

	def __exit__(self, *exc):
		self.stop()

	def _spawn(self, target, *args):
		thread = threading.Thread(target=target, args=args, daemon=True)
		self._threads.append(thread)
		thread.start()

	def _accept(self):
		self._listener.settimeout(0.1)  # so stop() is noticed: closing a socket does not wake accept() everywhere
		while not self._stopping:
			try:
				conn, _ = self._listener.accept()
			except TimeoutError:
				continue
			except OSError:
				return
			with self._lock:
				busy = self._client is not None
				if busy:
					self.turned_away += 1
				else:
					self._client = conn
					self.connections += 1
			if busy:
				conn.close()
			else:
				self._spawn(self._serve, conn)

	def _serve(self, conn):
		pending = None  # the mnemonic whose data <ENQ> returns
		error_word = "0000"
		line = bytearray()
		streaming = self.streaming
		conn.settimeout(0.1)
		try:
			if streaming:
				conn.sendall(self._data("PRX"))
			while not self._stopping:
				try:
					data = conn.recv(4096)
				except TimeoutError:
					if streaming:
						conn.sendall(self._data("PRX"))
					continue
				if not data:
					return
				streaming = False
				for byte in data:
					if byte == _ETX:
						line.clear()
					elif byte == _ENQ:
						if self.silent:
							continue
						if pending is None:
							conn.sendall(error_word.encode() + _END)
							error_word = "0000"
						else:
							conn.sendall(self._data(pending))
					elif byte == 0x0D:
						command = line.decode("ascii", "replace").upper()
						line.clear()
						self.commands.append(command)
						if self.silent:
							continue
						mnemonic, parameters = command[:3], command[3:]
						if mnemonic not in _READ_COMMANDS or mnemonic in self.refuse:
							pending, error_word = None, "0001"
						elif parameters:
							pending, error_word = None, "0010"  # the fake does not take settings
						else:
							pending = mnemonic
						conn.sendall(_NAK if pending is None else _ACK)
					elif byte not in (0x0A, 0x20):
						line.append(byte)
		except OSError:
			pass
		finally:
			conn.close()
			with self._lock:
				self._client = None

	def _data(self, mnemonic):
		if mnemonic in self.replies:
			text = self.replies[mnemonic]
			if isinstance(text, bytes):
				return text
		elif mnemonic == "PRX":
			scale = _UNITS_PER_TORR.get(self.unit, 1.0)
			text = ",".join(f"{s},{p * scale:+.4E}" for s, p in zip(self.statuses, self.pressures_torr))
		elif mnemonic == "AYT":
			text = self.identity
		elif mnemonic == "UNI":
			text = str(self.unit)
		elif mnemonic == "TID":
			text = ",".join(self.models)
		elif mnemonic == "GAS":
			text = ",".join(str(g) for g in self.gases)
		elif mnemonic == "CID":
			text = ",".join(self.names)
		else:  # ERR
			text = "0000"
		return text.encode("ascii") + _END


def main():
	parser = argparse.ArgumentParser(description="Fake Pfeiffer TPG 366 controller with made-up, slowly moving pressures.")
	parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this machine only)")
	parser.add_argument("--port", type=int, default=8000)
	args = parser.parse_args()
	with FakeTpg366(args.host, args.port) as fake:
		base = list(fake.pressures_torr)
		print(f"fake TPG 366 listening on {fake.host}:{fake.port}; Ctrl-C stops it", flush=True)
		try:
			while True:
				t = time.time()
				fake.pressures_torr = [p * (1.0 + 0.05 * math.sin(t / (20.0 + 7.0 * i))) for i, p in enumerate(base)]
				time.sleep(0.05)
		except KeyboardInterrupt:
			pass


if __name__ == "__main__":
	main()
