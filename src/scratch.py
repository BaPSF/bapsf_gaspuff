import numpy as np

n = 40
ind = np.zeros(n)
for i in range(n):
    data = np.loadtxt(f'/home/pi/flow_meter/data/output_delay_{i}.csv')
    #ind[i] = len(data)
    crossings = np.diff(data > 1, prepend=False)
    cross_ind = np.argwhere(crossings)[0][0]
    ind[i] = cross_ind
    #print(cross_ind)
print('mean: ', np.mean(ind))
print('std: ', np.std(ind))