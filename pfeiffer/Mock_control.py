from PfeifferVacuumCommunication import MaxiGauge
import time

def test_communication_loop(ip_addr="192.168.7.44"):
    gauge = MaxiGauge(ip_addr, verbose=True)
    count = 0

    while True:
        try:
            gauge.connect()
            stat_ls, pres_ls = gauge.get_all_pressure_reading()
            gauge_id = gauge.get_device_id()
            gas_ls = gauge.get_gas_type()
            gauge.disconnect()

            print(f"[{count}] Pressure: {pres_ls} | Status: {stat_ls} | ID: {gauge_id} | Gas: {gas_ls}")
            count += 1
            time.sleep(0.1)

        except Exception as e:
            print(f"Error communicating with gauge: {e}")
            time.sleep(0.5)

if __name__ == "__main__":
    test_communication_loop()