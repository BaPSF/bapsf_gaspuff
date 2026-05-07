"""Unit tests for FlowMeterCommunication.FlowMeter and flowmeter_main.read_flowmeter.

Runs without the Sensirion SDK or any real hardware: the SDK modules are
faked into ``sys.modules`` before the production modules are imported.
"""

import io
import os
import queue
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock


# --- Fake the Sensirion SDK before importing production code ---------------

def _make_fake_sensirion():
    """Inject minimal stand-ins for the two Sensirion packages."""

    driver = types.ModuleType("sensirion_shdlc_driver")
    driver.ShdlcSerialPort = mock.MagicMock(name="ShdlcSerialPort")
    driver.ShdlcConnection = mock.MagicMock(name="ShdlcConnection")
    sys.modules["sensirion_shdlc_driver"] = driver

    sfc = types.ModuleType("sensirion_shdlc_sfc5xxx")

    class Sfc5xxxScaling:
        USER_DEFINED = "USER_DEFINED"

    class _Enum:
        def __init__(self, name):
            self.name = name

        def __eq__(self, other):
            return isinstance(other, _Enum) and self.name == other.name

        def __hash__(self):
            return hash(self.name)

    class Sfc5xxxUnitPrefix:
        ONE = _Enum("ONE")

    class Sfc5xxxUnit:
        STANDARD_LITER = _Enum("STANDARD_LITER")

    class Sfc5xxxUnitTimeBase:
        MINUTE = _Enum("MINUTE")

    class Sfc5xxxMediumUnit:
        def __init__(self, prefix, unit, timebase):
            self.prefix = prefix
            self.unit = unit
            self.timebase = timebase

        def __eq__(self, other):
            if not isinstance(other, Sfc5xxxMediumUnit):
                return NotImplemented
            return (self.prefix == other.prefix
                    and self.unit == other.unit
                    and self.timebase == other.timebase)

        def __hash__(self):
            return hash((self.prefix, self.unit, self.timebase))

    class Sfc5xxxValveInputSource:
        pass

    class Sfc5xxxShdlcDevice:
        # Replaced per-test by a MagicMock; this stub exists only so the
        # production-code import succeeds.
        def __init__(self, *a, **kw):
            raise NotImplementedError("patch me in tests")

    sfc.Sfc5xxxScaling = Sfc5xxxScaling
    sfc.Sfc5xxxUnitPrefix = Sfc5xxxUnitPrefix
    sfc.Sfc5xxxUnit = Sfc5xxxUnit
    sfc.Sfc5xxxUnitTimeBase = Sfc5xxxUnitTimeBase
    sfc.Sfc5xxxMediumUnit = Sfc5xxxMediumUnit
    sfc.Sfc5xxxValveInputSource = Sfc5xxxValveInputSource
    sfc.Sfc5xxxShdlcDevice = Sfc5xxxShdlcDevice
    sys.modules["sensirion_shdlc_sfc5xxx"] = sfc

    return driver, sfc


_FAKE_DRIVER, _FAKE_SFC = _make_fake_sensirion()


# Make the source directory importable.
_SRC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


import FlowMeterCommunication as fmc  # noqa: E402


# --- Helpers ---------------------------------------------------------------

def _fake_buffer(values, sampling_time=0.001, lost_values=0, remaining_values=0):
    buf = mock.Mock()
    buf.values = list(values)
    buf.sampling_time = sampling_time
    buf.lost_values = lost_values
    buf.remaining_values = remaining_values
    return buf


def _fake_version(major, minor):
    fw = mock.Mock()
    fw.major = major
    fw.minor = minor
    version = mock.Mock()
    version.firmware = fw
    return version


def _build_flowmeter(*, current_unit_equal=True, fw=(1, 40)):
    """Build a FlowMeter with all device and serial calls mocked.

    Patches the SDK constructors so __init__ runs end-to-end without I/O.
    Returns (flow_meter, device_mock).
    """
    device = mock.MagicMock()
    device.get_version.return_value = _fake_version(*fw)
    device.get_number_of_calibrations.return_value = 0  # short-circuit info loop
    device.get_product_name.return_value = "MOCK"
    device.get_article_code.return_value = "MOCK"
    device.get_serial_number.return_value = "MOCK"
    device.slave_address = 2

    configured_unit = fmc.Sfc5xxxMediumUnit(
        fmc.Sfc5xxxUnitPrefix.ONE,
        fmc.Sfc5xxxUnit.STANDARD_LITER,
        fmc.Sfc5xxxUnitTimeBase.MINUTE,
    )
    if current_unit_equal:
        device.get_user_defined_medium_unit.return_value = configured_unit
    else:
        # Different timebase → __eq__ returns False
        other_tb = mock.MagicMock()
        device.get_user_defined_medium_unit.return_value = fmc.Sfc5xxxMediumUnit(
            fmc.Sfc5xxxUnitPrefix.ONE, fmc.Sfc5xxxUnit.STANDARD_LITER, other_tb,
        )

    serial_port = mock.MagicMock()
    serial_port.port = "COMTEST"

    with mock.patch.object(fmc, "ShdlcSerialPort", return_value=serial_port), \
         mock.patch.object(fmc, "ShdlcConnection"), \
         mock.patch.object(fmc, "Sfc5xxxShdlcDevice", return_value=device):
        fm = fmc.FlowMeter(port="COMTEST", baudrate=460800, slave_address=2)

    return fm, device


# --- FlowMeter __init__ guards ---------------------------------------------

class InitGuardsTests(unittest.TestCase):

    def test_medium_unit_skipped_when_already_correct(self):
        _, device = _build_flowmeter(current_unit_equal=True)
        device.set_user_defined_medium_unit.assert_not_called()

    def test_medium_unit_written_when_mismatched(self):
        _, device = _build_flowmeter(current_unit_equal=False)
        device.set_user_defined_medium_unit.assert_called_once()

    def test_activate_calibration_called_unconditionally(self):
        # Per manual §5.7.1 the device no-ops if already loaded; we still call.
        _, device = _build_flowmeter(current_unit_equal=True)
        device.activate_calibration.assert_called_once_with(3)

    def test_firmware_warning_below_140(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _build_flowmeter(fw=(1, 39))
        self.assertIn("below V1.40", buf.getvalue())

    def test_firmware_no_warning_at_140(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _build_flowmeter(fw=(1, 40))
        self.assertNotIn("below V1.40", buf.getvalue())


# --- get_reading / get_single_buffer ---------------------------------------

class AcquisitionTests(unittest.TestCase):

    def test_get_reading_uses_device_sampling_time(self):
        fm, device = _build_flowmeter()

        # Each buffer carries 60 samples at 1 ms; duration 0.05s → 50 samples.
        device.read_measured_value_buffer.side_effect = lambda *a, **kw: \
            _fake_buffer([0.0] * 60, sampling_time=0.001)

        result = fm.get_reading(duration=0.05)
        self.assertEqual(result.sampling_time, 0.001)
        self.assertGreaterEqual(len(result.values), 50)
        # We never accumulate more than one buffer past the target.
        self.assertLessEqual(len(result.values), 60)

        # And at half the sampling period, twice as many samples are needed.
        device.reset_mock()
        device.read_measured_value_buffer.side_effect = lambda *a, **kw: \
            _fake_buffer([0.0] * 60, sampling_time=0.0005)
        result2 = fm.get_reading(duration=0.05)
        self.assertEqual(result2.sampling_time, 0.0005)
        self.assertGreaterEqual(len(result2.values), 100)

    def test_get_reading_surfaces_lost_values(self):
        fm, device = _build_flowmeter()
        device.read_measured_value_buffer.return_value = _fake_buffer(
            [0.0] * 60, sampling_time=0.001, lost_values=7,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = fm.get_reading(duration=0.005)  # 5 samples — first buffer covers it
        self.assertEqual(result.lost_values, 7)
        self.assertIn("ring-buffer overrun", buf.getvalue())

    def test_get_single_buffer_returns_full_response(self):
        fm, device = _build_flowmeter()
        sentinel = _fake_buffer([1.0, 2.0, 3.0], sampling_time=0.002,
                                lost_values=0, remaining_values=4)
        device.read_measured_value_buffer.return_value = sentinel

        out = fm.get_single_buffer()
        self.assertIs(out, sentinel)
        self.assertEqual(out.sampling_time, 0.002)
        self.assertEqual(out.remaining_values, 4)


# --- read_flowmeter regression for the `with fm:` bug ----------------------

class ReadFlowmeterTests(unittest.TestCase):
    """Drive read_flowmeter with stdlib queues; assert the port is not
    closed between two consecutive 'TRIG' messages."""

    def setUp(self):
        # flowmeter_main imports h5py and numpy at module top.
        try:
            import h5py  # noqa: F401
            import numpy  # noqa: F401
        except ImportError as e:
            self.skipTest(f"flowmeter_main needs h5py/numpy: {e}")

        # Defer the import — we patched the SDK already.
        if "flowmeter_main" in sys.modules:
            del sys.modules["flowmeter_main"]
        import flowmeter_main  # noqa: E402
        self.flowmeter_main = flowmeter_main

    def test_two_consecutive_triggers_share_one_fm_instance(self):
        fm_instance = mock.MagicMock(name="FlowMeter")
        fm_instance.device.read_measured_value_buffer.return_value = _fake_buffer(
            [0.1, 0.2, 0.3], sampling_time=0.001, lost_values=0,
        )

        q_trig = queue.Queue()
        q_data = queue.Queue()
        q_trig.put("TRIG")
        q_trig.put("TRIG")
        q_trig.put("QUIT")

        with mock.patch.object(self.flowmeter_main, "FlowMeter",
                               return_value=fm_instance), \
             mock.patch.object(self.flowmeter_main, "time") as t:
            t.time.return_value = 0.0
            t.sleep = mock.Mock()
            self.flowmeter_main.read_flowmeter(q_trig, q_data, "COMTEST", 2,
                                               wait_time=0.0)

        # Both shots used the same fm; the buffer read happened twice.
        self.assertEqual(fm_instance.device.read_measured_value_buffer.call_count, 2)
        # `with fm:` regression: __enter__/__exit__ must NOT be invoked inside
        # the trigger loop. (FlowMeter.__exit__ closes the port; calling it
        # between shots is exactly the bug we're guarding against.)
        fm_instance.__enter__.assert_not_called()
        fm_instance.__exit__.assert_not_called()

        # And both MeterShot messages carry the device's sampling_time, not NaN.
        s1 = q_data.get_nowait()
        s2 = q_data.get_nowait()
        self.assertEqual(s1.sampling_time, 0.001)
        self.assertEqual(s2.sampling_time, 0.001)


# --- save_flow_data records sampling_time as a group attribute -------------

class HDF5SchemaTests(unittest.TestCase):

    def setUp(self):
        try:
            import h5py  # noqa: F401
            import numpy  # noqa: F401
        except ImportError as e:
            self.skipTest(f"needs h5py/numpy: {e}")
        if "flowmeter_main" in sys.modules:
            del sys.modules["flowmeter_main"]
        import flowmeter_main  # noqa: E402
        self.flowmeter_main = flowmeter_main

    def test_first_successful_shot_records_sampling_time(self):
        import h5py
        import numpy as np

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "flow_test.hdf5")
            self.flowmeter_main.init_hdf5_file(
                path, east_info=("port_e", 2), west_info=("port_w", 0),
            )

            with h5py.File(path, "a", libver="latest") as f:
                f.swmr_mode = True
                ok = self.flowmeter_main.save_flow_data(
                    f, np.array([1.0, 2.0, 3.0]), 1234.5,
                    "FlowMeter_East", sampling_time=0.0008,
                )
                self.assertTrue(ok)

            with h5py.File(path, "r") as f:
                self.assertAlmostEqual(
                    float(f["FlowMeter_East"].attrs["sampling_time"]), 0.0008,
                )
                # West group has no shot yet → no attr written.
                self.assertNotIn("sampling_time", f["FlowMeter_West"].attrs)

    def test_failure_shot_does_not_write_sampling_time(self):
        import h5py
        import numpy as np

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "flow_test.hdf5")
            self.flowmeter_main.init_hdf5_file(
                path, east_info=("port_e", 2), west_info=("port_w", 0),
            )
            # Establish data_length first with a good shot.
            with h5py.File(path, "a", libver="latest") as f:
                f.swmr_mode = True
                self.flowmeter_main.save_flow_data(
                    f, np.array([1.0, 2.0]), 1.0, "FlowMeter_East",
                    sampling_time=0.001,
                )
                # Now a failure on West should not establish any sampling_time
                # attribute on the West group.
                self.flowmeter_main.save_flow_data(
                    f, np.nan, 2.0, "FlowMeter_West",
                    sampling_time=float("nan"),
                )

            with h5py.File(path, "r") as f:
                self.assertNotIn("sampling_time", f["FlowMeter_West"].attrs)


if __name__ == "__main__":
    unittest.main()
