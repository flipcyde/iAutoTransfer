# apple_sanity.py — lightweight checks for Apple Mobile Device / drivers on Windows
import os
import sys

def _file_exists_any(paths):
    for p in paths:
        try:
            if p and os.path.isfile(p):
                return True
        except Exception:
            pass
    return False

def _registry_has_amds():
    """Check a few common registry locations for Apple Mobile Device / iTunes bits."""
    if sys.platform != "win32":
        return True  # non-Windows: don't block UI
    try:
        import winreg
    except Exception:
        return False

    keys_to_try = [
        r"SOFTWARE\Apple Inc.\Apple Mobile Device Support",
        r"SOFTWARE\Apple Inc.\Apple Application Support",
        r"SOFTWARE\WOW6432Node\Apple Inc.\Apple Mobile Device Support",
        r"SOFTWARE\WOW6432Node\Apple Inc.\Apple Application Support",
        r"SOFTWARE\Apple Inc.\iTunes",
        r"SOFTWARE\WOW6432Node\Apple Inc.\iTunes",
    ]
    for hive in (getattr(__import__("winreg"), "HKEY_LOCAL_MACHINE"),):
        for subkey in keys_to_try:
            try:
                with __import__("winreg").OpenKey(hive, subkey, 0, __import__("winreg").KEY_READ):
                    return True
            except FileNotFoundError:
                continue
            except Exception:
                continue
    return False

def _common_driver_files_present():
    """Look for MobileDevice.dll and related Apple support DLLs in common locations."""
    if sys.platform != "win32":
        return True
    candidates = [
        r"C:\Program Files\Common Files\Apple\Mobile Device Support\MobileDevice.dll",
        r"C:\Program Files (x86)\Common Files\Apple\Mobile Device Support\MobileDevice.dll",
        r"C:\Program Files\Common Files\Apple\Apple Application Support\CoreFoundation.dll",
        r"C:\Program Files (x86)\Common Files\Apple\Apple Application Support\CoreFoundation.dll",
    ]
    return _file_exists_any(candidates)

def sanity_check_apple_drivers() -> bool:
    """
    Return True if Apple Mobile Device / iTunes runtime appears installed.

    Test overrides:
      - Set ITRANSFER_SIMULATE_NO_APPLE=1  -> always return False
      - Set ITRANSFER_SIMULATE_APPLE=1     -> always return True
    """
    # ---- TEST OVERRIDES ----
    if os.environ.get("ITRANSFER_SIMULATE_NO_APPLE") == "1":
        return False
    if os.environ.get("ITRANSFER_SIMULATE_APPLE") == "1":
        return True
    # ------------------------

    if sys.platform != "win32":
        return True
    return _registry_has_amds() or _common_driver_files_present()

__all__ = ["sanity_check_apple_drivers"]
