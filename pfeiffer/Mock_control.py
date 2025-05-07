from PfeifferVacuumCommunication import MaxiGauge
import time
import traceback


def test_communication_loop(ip_addr="192.168.7.44"):
    gauge = MaxiGauge(ip_addr, verbose=False)
    count = 0

    while True:
        try:
            gauge.connect()
            stat_ls, pres_ls = gauge.get_all_pressure_reading()
            gauge_id = gauge.get_device_id()
            gas_ls = gauge.get_gas_type()
            gauge.disconnect()

            count += 1

            if count % 10000 == 0:
                print(f"{count} iterations completed")

            time.sleep(0.01)

        except Exception as e:
            error_message = f"[ERROR {time.strftime('%Y-%m-%d %H:%M:%S')}] {type(e).__name__}: {e}\n"
            print(error_message.strip())
            with open("gauge_errors.txt", "a") as log_file:
                log_file.write(error_message)
                log_file.write(traceback.format_exc() + "\n")
            time.sleep(0.5)

if __name__ == "__main__":
    test_communication_loop()
