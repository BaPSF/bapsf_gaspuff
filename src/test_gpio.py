from RPi import GPIO
import time

gpio_pin = 25
GPIO.setmode(GPIO.BCM)
GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

# for i in range(2000):
#     if (GPIO.input(pin)) != 0:
#         print("1")
#     time.sleep(0.001)

for i in range(5):
    print(i)
    result = GPIO.wait_for_edge(pin, GPIO.RISING, timeout=1500)
    if result is None:
        print('no trigger')
    else:
        print('Trigger at {}'.format(time.time()))


# GPIO.setmode(GPIO.BCM)
# GPIO.setup(pin, GPIO.OUT)

# for i in range(1000):
#     GPIO.output(pin, GPIO.HIGH)
#     time.sleep(0.005)
#     GPIO.output(pin, GPIO.LOW)
#     time.sleep(0.005)

#     if i % 100 == 0:
#         print(i)

GPIO.cleanup()
