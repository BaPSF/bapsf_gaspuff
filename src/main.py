from kernel import GasPuffController
import numpy as np

gpc = GasPuffController(gpio_channel=5)
gpc.acquire(10)