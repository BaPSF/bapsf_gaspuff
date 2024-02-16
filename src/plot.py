import numpy as np
import matplotlib.pyplot as plt
import matplotlib

#matplotlib.rcParams['backend'] = 'agg'

data = np.loadtxt('/home/pi/flow_meter/data/output_0.csv')
plt.plot(data)
plt.show()