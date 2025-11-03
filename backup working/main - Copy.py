"""
main.py — iAutoTransfer Entry Point
Launches GUI immediately and monitors for iPhone connections.
"""

import threading
import time
from pymobiledevice3.usbmux import list_devices, select_device
from pymobiledevice3.lockdown import LockdownClient
from gui import AppWindow


def device_watcher(app_window, poll_interval=3):
    """
    Background thread that periodically checks for iPhone connection
    and updates the GUI status.
    Works around Windows usbmux caching issues by comparing device sets.
    """
    last_devices = set()

    while True:
        try:
            # Re-import each iteration to force fresh usbmux socket connect
            from pymobiledevice3.usbmux import list_devices, select_device

            current_devices = {d.serial for d in list_devices()}
            connected = bool(current_devices)

            # detect new connection
            if current_devices and current_devices != last_devices:
                try:
                    mux = select_device()
                    app_window.set_device_connected(mux)
                    app_window.update_log("iPhone connected (basic mode).")
                except Exception as e:
                    app_window.update_log(f"Device connect error: {e}")
                    app_window.set_device_disconnected()

            # detect disconnect
            if not current_devices and last_devices:
                app_window.set_device_disconnected()
                app_window.update_log("iPhone disconnected.")

            last_devices = current_devices

        except Exception as e:
            app_window.update_log(f"Device check error: {e}")

        time.sleep(poll_interval)




def main():
    print("iAutoTransfer starting... Launching GUI.")
    app = AppWindow()
    app.mainloop()

    # Start background watcher thread
    watcher = threading.Thread(target=device_watcher, args=(app,), daemon=True)
    watcher.start()

    app.run()


if __name__ == "__main__":
    main()
