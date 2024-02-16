from kernel import GasPuffController
import numpy as np

gpc = GasPuffController(gpio_channel=5)
gpc.acquire(path='/home/pi/flow_meter/data/0213', duration=0.3)