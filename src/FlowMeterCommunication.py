"""Thin wrapper over the Sensirion SHDLC SDK for SFC5xxx mass-flow controllers.

Exposes a single :class:`FlowMeter` that opens a serial connection, configures
the user-defined unit (standard liter / minute), and provides convenience
methods for buffered acquisition.
"""

from typing import NamedTuple

from sensirion_shdlc_driver import ShdlcSerialPort, ShdlcConnection
from sensirion_shdlc_sfc5xxx import Sfc5xxxShdlcDevice, Sfc5xxxScaling, \
    Sfc5xxxUnitPrefix, Sfc5xxxUnit, Sfc5xxxUnitTimeBase, Sfc5xxxMediumUnit


class FlowReading(NamedTuple):
    """Result of a duration-bounded buffered acquisition."""
    values: list
    sampling_time: float  # seconds between samples, reported by the device
    lost_values: int      # ring-buffer overrun count, summed across reads


class FlowMeter(object):
    """
    This class represents the flowmeter device and handles its I/O with the APIs provided by manufacturer
    Sensirion.
    """
    def __init__(self, port: str = '/dev/ttyUSB0', baudrate: int = 460800, slave_address: int = 2):
        """
        Initialize connection to the flow meter and other configurations.

        Parameters
        ----------
        port : Address of the serial port on local device.
        baudrate : Baud rate for serial communication. Default is the current baud rate setup for the device.
        slave_address : Address of the device in the master-slave model of the device control model.
            Typically no need to change.
        """
        try:
            self.port = ShdlcSerialPort(port=port, baudrate=baudrate)
            self.device = Sfc5xxxShdlcDevice(ShdlcConnection(self.port), slave_address=slave_address)

            # Print device information upon initialization
            self._print_device_info()

            self._warn_if_firmware_too_old()

            # Manual §5.7.1: "Loading the same calibration again is not a problem
            # and will not cause a write operation." Safe to call unconditionally.
            self.device.activate_calibration(3)

            self.unit = Sfc5xxxMediumUnit(
                Sfc5xxxUnitPrefix.ONE,
                Sfc5xxxUnit.STANDARD_LITER,
                Sfc5xxxUnitTimeBase.MINUTE
            )
            # Manual §5.4.1: this command writes NV memory. Skip the write if
            # the device already holds the desired unit.
            if self.device.get_user_defined_medium_unit() != self.unit:
                self.device.set_user_defined_medium_unit(self.unit)
        except Exception as e:
            if hasattr(self, 'port'):
                self.port.close()
            raise RuntimeError(f"Failed to initialize flow meter: {e}")

    def _warn_if_firmware_too_old(self):
        """Warn when firmware predates V1.40 (USER_DEFINED scaling support)."""
        try:
            version = self.device.get_version()
            fw = version.firmware
            if (fw.major, fw.minor) < (1, 40):
                print(
                    f"Warning: flow meter firmware {fw.major}.{fw.minor:02d} is "
                    "below V1.40; Sfc5xxxScaling.USER_DEFINED is not supported."
                )
        except Exception as e:
            print(f"Warning: could not check firmware version: {e}")

    def _print_device_info(self):
        """Print device information and available calibration blocks."""
        try:
            print(f"\nFlow meter at {self.port.port} (slave address: {self.device.slave_address})")
            print("Version:", self.device.get_version())
            print("Product Name:", self.device.get_product_name())
            print("Article Code:", self.device.get_article_code())
            print("Serial Number:", self.device.get_serial_number())

            print("\nAvailable gas calibration blocks:")
            for i in range(self.device.get_number_of_calibrations()):
                if self.device.get_calibration_validity(i):
                    gas = self.device.get_calibration_gas_description(i)
                    fullscale = self.device.get_calibration_fullscale(i)
                    unit = self.device.get_calibration_gas_unit(i)
                    print(f" - {i}: {fullscale:.2f} {unit} {gas}")
            print()
        except Exception as e:
            print(f"Warning: Could not get device info: {e}")

    def set_baudrate(self, baudrate: int):
        """
        Set baudrate for serial communication.
        """
        self.device.set_baudrate(baudrate)


    def set_slave_address(self, slave_address: int):
        """
        Set slave address for the flow meter. Do not change unless necessary.
        """
        self.device.set_slave_address(slave_address)


    def get_reading(self, duration: float) -> FlowReading:
        """
        Retrieve flow readings for a fixed duration by repeatedly draining the
        device's ring buffer. ``duration`` in seconds.

        The target sample count is derived from the device-reported
        ``sampling_time`` on the first response, not assumed. Lost-value counts
        from the SHDLC ring buffer (manual §5.2.3) are accumulated and surfaced
        via the returned :class:`FlowReading`; a non-zero count is also printed.
        """
        first = self.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED)
        sampling_time = first.sampling_time
        lost = first.lost_values
        values = list(first.values)

        target_samples = int(duration / sampling_time) if sampling_time > 0 else 0
        while len(values) < target_samples:
            buf = self.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED)
            values.extend(buf.values)
            lost += buf.lost_values

        if lost > 0:
            print(f"Flow meter ring-buffer overrun: {lost} samples lost")

        return FlowReading(values=values, sampling_time=sampling_time, lost_values=lost)

    def get_single_buffer(self, max_reads: int = 100):
        """
        Read one buffered acquisition and return the full
        :class:`Sfc5xxxReadBufferResponse`.

        Callers that need just the samples should use ``.values``; the response
        also carries ``.sampling_time``, ``.lost_values`` and
        ``.remaining_values`` (manual §5.2.3) which the caller is expected to
        inspect.
        """
        return self.device.read_measured_value_buffer(
            Sfc5xxxScaling.USER_DEFINED, max_reads=max_reads
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self, 'port'):
            self.port.close()

    def close(self):
        """Explicitly close the port connection"""
        if hasattr(self, 'port'):
            self.port.close()
