import numpy
from wavegen_control import wavegen_control

def init_norm(freq, duty_cycle):
    '''
    Generate initial powered and unpowered phase
    Number of points are 1 step every 5 us
    Return power and unpowered phase with level normalized to 1
    Manual adjustment when neccessary
    '''

    NPh = int(1/freq * duty_cycle / 5e-6 + .5)   # High level number of points
    NPl = int(NPh / duty_cycle * (1-duty_cycle)) # Low level number of points
    pwf = numpy.ones(NPh)
    lwf = numpy.zeros(NPl)

    return pwf, lwf

 

def generate_pulse(pwf, lwf):
    '''
    Generate pulse ready to send to Agilent waveform generator
    See wavegen_control.send_dac_data for requirement
    '''

    n = int(len(lwf)/4)
    pulse = numpy.concatenate((lwf[:n], pwf, lwf[n:]))
   
    a = pulse.copy() * 4096
    a[pulse>8191] = 8191
   
    return a
 

def send_pulse(agilent:wavegen_control, a, level):
# Send array to agilent waveform generator VOLATILE
# agilent -– class object been called upon
# level – output voltage level  
    agilent.send_dac_data(a.astype('>i2'))
    agilent.function = 'USER' # updates waveform. see user manual
    agilent.voltage_level = level[0], level[1]
    print('Waveform sent to agilent')


'''
The script looks for a setup.txt file in the directory. The file should be formatted as (excluding dashed lines):
---
flow_rate=[ENTER FLOW RATE HERE]
trigger=[ENTER TRIGGER TIME HERE]
length=[ENTER PULSE LENGTH HERE]
---
units are sccm for flow rate and 
Currently it only supports a single pulse but that can be extended to custom pulse configurations.
'''
wavegen = wavegen_control(server_ip_addr='192.168.1.12')
voltage_level = [0, 1]
freq = 1
duty_cycle = .2
pwf, lwf = init_norm(freq, duty_cycle)
data = generate_pulse(pwf, lwf)
send_pulse(wavegen, data, voltage_level)

