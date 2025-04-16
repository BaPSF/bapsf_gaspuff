# file used for local debugging

import time
import random

class MockGauge:
    def __init__(self, ip_addr=None):
        self.connected = False

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def get_device_id(self):
        return ['PKR', 'PKR', 'PKR', 'PKR', 'PKR', 'PKR']

    def get_gas_type(self):
        return [0, 0, 0, 0, 0, 0]  # Nitrogen

    def get_all_pressure_reading(self):
        # Generate dummy status + random pressure values
        statuses = [0 for _ in range(6)]
        pressures = [10 ** (-random.uniform(3, 5)) for _ in range(6)]
        return statuses, pressures