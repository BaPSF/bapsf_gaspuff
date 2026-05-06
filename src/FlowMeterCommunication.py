"""Thin wrapper over the Sensirion SHDLC SDK for SFC5xxx mass-flow controllers.

Exposes a single :class:`FlowMeter` that opens a serial connection, configures
the user-defined unit (standard liter / minute), and provides convenience
read methods for buffered and single-sample acquisition.
"""

from sensirion_shdlc_driver import ShdlcSerialPort, ShdlcConnection
from sensirion_shdlc_sfc5xxx import Sfc5xxxShdlcDevice, Sfc5xxxScaling, \
    Sfc5xxxValveInputSource, Sfc5xxxUnitPrefix, Sfc5xxxUnit, \
    Sfc5xxxUnitTimeBase, Sfc5xxxMediumUnit
import time
import numpy as np

# Internal sample rate of the SFC5xxx flow-meter buffer.
SAMPLE_RATE_HZ = 1000


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

            self.device.activate_calibration(3)
            self.unit = Sfc5xxxMediumUnit(
                Sfc5xxxUnitPrefix.ONE,
                Sfc5xxxUnit.STANDARD_LITER,
                Sfc5xxxUnitTimeBase.MINUTE
            )
            self.device.set_user_defined_medium_unit(self.unit)
        except Exception as e:
            if hasattr(self, 'port'):
                self.port.close()
            raise RuntimeError(f"Failed to initialize flow meter: {e}")

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


    def get_reading(self, duration: float) -> list:
        """
        Retrieve flow readings from the flow meter for a fixed duration
        by repeatedly reading from its buffer. Duration in unit of seconds.
        """
        reading = []
        target_samples = int(duration * SAMPLE_RATE_HZ)
        while len(reading) <= target_samples:
            buffer = self.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED)
            reading.extend(buffer.values)
        return reading

    def get_single_buffer(self) -> list:
        """
        Read one buffer chunk from the flow meter and return its samples.

        Equivalent to a single :py:meth:`read_measured_value_buffer` call with
        ``max_reads=1``; useful for low-latency polling.
        """
        buffer = self.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED, max_reads=1)
        return buffer.values

    def get_reading_single_cycle(self, duration: float) -> list:
        """
        Acquire ``duration`` seconds of flow data using single-sample reads.

        Unlike :py:meth:`get_reading`, this does not pull from the device's
        buffer — it issues one ``read_measured_value`` call per sample at the
        nominal :data:`SAMPLE_RATE_HZ`. Slower, but the timing of each sample
        is controlled by the host loop.
        """
        reading = []
        n = int(duration * SAMPLE_RATE_HZ)
        for i in range(n):
            val = self.device.read_measured_value(Sfc5xxxScaling.USER_DEFINED)
            reading.append(val)
        return reading

    def get_pre_and_post_trigger_samples(self, pretrigger_samples: int = 10, posttrigger_samples: int = 90) -> list:
        """
        Retrieve pre-trigger and post-trigger samples from the flow meter buffer.

        TODO: verify which end of ``buffer.values`` is the most recent — the
        slice below assumes the tail holds the newest samples.
        """
        samples = []
        buffer = self.device.read_measured_value_buffer(Sfc5xxxScaling.USER_DEFINED)
        # Take the most-recent ``pretrigger_samples`` already in the buffer.
        samples.extend(buffer.values[-pretrigger_samples:])
        # Then poll one sample at a time for the post-trigger window.
        for i in range(posttrigger_samples):
            samples.append(self.device.read_measured_value(Sfc5xxxScaling.USER_DEFINED))

        return samples

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self, 'port'):
            self.port.close()

    def close(self):
        """Explicitly close the port connection"""
        if hasattr(self, 'port'):
            self.port.close()
