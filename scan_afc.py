# scan_afc.py — AFC/DCIM scan with filters + Lockdown device info (storage/battery)
#
# Public surface used by gui.py:
#   make_filter(year:int|None, month:int|None, kind:str) -> Callable[path->bool]
#   scan_media_afc(progress_callback, log_callback, stats_callback=None, filter_pred=None)
#       -> (device_info:dict, items:list[(remote_path,size)], totals:dict)
#
# Notes:
# - Requires iPhone unlocked + "Trust this computer".
# - Each scan uses a single AFC session.
# - Storage/battery fields are best-effort (not all builds expose all keys).

import os
import re
from typing import Callable, Dict, List, Optional, Tuple
from contextlib import contextmanager

# ---- pymobiledevice3 imports (robust across versions) ----
try:
    from pymobiledevice3.lockdown import create_using_usbmux, LockdownClient
except Exception:
    create_using_usbmux = None
    LockdownClient = None

try:
    from pymobiledevice3.services.afc import AfcService
except Exception:
    AfcService = None

try:
    from pymobiledevice3.usbmux import select_device as mux_select_device, list_devices as mux_list_devices
except Exception:
    mux_select_device = mux_list_devices = None
    try:
        from pymobiledevice3.usbmux import Usbmux as UsbmuxClass
    except Exception:
        try:
            from pymobiledevice3.usbmux import USBMux as UsbmuxClass
        except Exception:
            UsbmuxClass = None

# ---- media extensions ----
MEDIA_EXT = tuple({
    ".jpg", ".jpeg", ".heic", ".png", ".raw", ".dng",
    ".mov", ".mp4", ".avi", ".hevc", ".m4v", ".3gp"
})
PHOTO_EXT = {".jpg", ".jpeg", ".heic", ".png", ".raw", ".dng"}
VIDEO_EXT = {".mov", ".mp4", ".avi", ".hevc", ".m4v", ".3gp"}

def _is_media(name: str) -> bool:
    return name.lower().endswith(MEDIA_EXT)

def _is_photo(name: str) -> bool:
    return name.lower().endswith(tuple(PHOTO_EXT))

def _is_video(name: str) -> bool:
    return name.lower().endswith(tuple(VIDEO_EXT))

# filename-based YYYY/MM heuristics (IMG_20250118, VID_20241005, PXL_20240908, etc.)
_DATE_RE = re.compile(r"(?:IMG|VID|PXL|MOV|DSC|CIMG)?[_-]?([12]\d{3})(\d{2})?(\d{2})?")

def _guess_ym_from_name(name: str):
    m = _DATE_RE.search(name.upper())
    if not m:
        return None, None
    yyyy = m.group(1)
    mm = m.group(2)
    if yyyy and mm and mm.isdigit():
        try:
            mmi = int(mm)
            if 1 <= mmi <= 12:
                return int(yyyy), mmi
        except Exception:
            pass
    return int(yyyy) if yyyy else None, None

# ---- usbmux helpers ----
def _pick_first_device():
    if mux_select_device:
        dev = mux_select_device()
        if not dev:
            raise RuntimeError("No iOS device via usbmux (unlock & Trust this computer).")
        return dev
    if mux_list_devices:
        devs = mux_list_devices()
        if not devs:
            raise RuntimeError("No iOS device via usbmux (unlock & Trust this computer).")
        return devs[0]
    if 'UsbmuxClass' in globals() and UsbmuxClass:
        mux = UsbmuxClass()
        if getattr(mux, "devices", None):
            return mux.devices[0]
        raise RuntimeError("No iOS device via usbmux (unlock & Trust this computer).")
    raise RuntimeError("pymobiledevice3 usbmux API not available in this build.")

@contextmanager
def _afc_session():
    lockdown = svc = None
    try:
        dev = _pick_first_device()
        if create_using_usbmux:
            ident = getattr(dev, "serial", None) or getattr(dev, "identifier", None) or getattr(dev, "udid", None)
            lockdown = create_using_usbmux(identifier=ident)
        else:
            if not LockdownClient:
                raise RuntimeError("Lockdown client API not found in this build.")
            lockdown = LockdownClient(device=dev)
        if not AfcService:
            raise RuntimeError("AfcService not available; install/upgrade pymobiledevice3.")
        svc = AfcService(lockdown=lockdown)
        yield lockdown, svc
    finally:
        try:
            if svc: svc.close()
        except Exception:
            pass
        try:
            if lockdown and hasattr(lockdown, "close"): lockdown.close()
        except Exception:
            pass

# ---- public helpers ----
def get_device_info(lockdown) -> Dict[str, str]:
    """Best-effort Lockdown fields + storage/battery."""
    info: Dict[str, str] = {}
    for key in ("DeviceName", "ProductVersion", "ProductType", "SerialNumber", "UniqueDeviceID"):
        try:
            val = lockdown.get_value(None, key) if hasattr(lockdown, "get_value") else None
            if val is not None:
                info[key] = str(val)
        except Exception:
            pass

    # DiskUsage (data partition) from Lockdown
    try:
        du = lockdown.get_value(None, "DiskUsage")
        if isinstance(du, dict):
            total = float(du.get("TotalDataCapacity") or 0.0)
            free  = float(du.get("TotalDataAvailable") or 0.0)
            used  = max(0.0, total - free)
            info["StorageTotalBytes"] = total
            info["StorageFreeBytes"]  = free
            info["StorageUsedBytes"]  = used
    except Exception:
        pass

    # Battery
    try:
        cap = lockdown.get_value(None, "BatteryCurrentCapacity")
        if cap is not None:
            info["BatteryPercent"] = int(cap)
    except Exception:
        pass
    try:
        chg = lockdown.get_value(None, "BatteryIsCharging")
        if chg is not None:
            info["BatteryCharging"] = bool(chg)
    except Exception:
        pass

    # Friendly aliases
    info["Name"] = info.get("DeviceName", "iPhone")
    info["iOS"]  = info.get("ProductVersion", "")
    info["UDID"] = info.get("UniqueDeviceID", "")
    return info

def summarize_counts(paths_sizes: List[Tuple[str, int]]):
    photos = sum(1 for p, _ in paths_sizes if _is_photo(os.path.basename(p)))
    videos = sum(1 for p, _ in paths_sizes if _is_video(os.path.basename(p)))
    return {"total": len(paths_sizes), "photos": photos, "videos": videos}

def make_filter(year: Optional[int], month: Optional[int], kind: str):
    """Return a predicate(path)->bool enforcing kind/year/month."""
    kind = (kind or "all").lower()
    want_photo = (kind == "photos")
    want_video = (kind == "videos")

    def pred(path: str) -> bool:
        name = os.path.basename(path)
        if want_photo and not _is_photo(name):
            return False
        if want_video and not _is_video(name):
            return False
        if year is None and month is None:
            return True
        y, m = _guess_ym_from_name(name)
        if year is not None:
            if y is None or y != year:
                return False
        if month is not None:
            if m is None or m != month:
                return False
        return True

    return pred

# ---- AFC/DCIM scan ----
def _afc_listdir(svc, path: str):
    try:
        return [p for p in svc.listdir(path) if p not in (".", "..")]
    except Exception:
        return []

def _afc_stat(svc, path: str):
    try:
        return svc.stat(path)
    except Exception:
        return None

def _walk_dcim(svc, root="/DCIM"):
    """Iterative walk of /DCIM returning (remote_path, size)."""
    stack = [root]
    while stack:
        cur = stack.pop()
        for name in _afc_listdir(svc, cur):
            full = f"{cur.rstrip('/')}/{name}"
            st = _afc_stat(svc, full)
            if not st:
                continue
            if st.get("st_ifmt") == "S_IFDIR":
                stack.append(full)
            else:
                if _is_media(name):
                    size = int(st.get("st_size", 0) or 0)
                    yield full, size

# ---- Public API ----
def scan_media_afc(
    progress_callback: Optional[Callable[[float], None]],
    log_callback: Optional[Callable[[str], None]],
    stats_callback: Optional[Callable[[dict, int], None]] = None,
    filter_pred: Optional[Callable[[str], bool]] = None,
):
    """
    Enumerate DCIM via AFC, filter files, and return:
      (device_info, items, totals)
    items = list[(remote_path, size_bytes)]
    totals = {'total':N, 'photos':P, 'videos':V}
    """
    items: List[Tuple[str, int]] = []
    total_seen = 0
    total_bytes = 0

    with _afc_session() as (lockdown, svc):
        di = get_device_info(lockdown)

        # --- NEW: AFC filesystem usage (more direct than Lockdown) ---
        # Some builds provide AfcService.get_fs_info() with FSTotalBytes/FSFreeBytes.
        # We merge this into device_info["storage"] for the GUI.
        try:
            if hasattr(svc, "get_fs_info"):
                fsinfo = svc.get_fs_info() or {}
                fs_total = int(fsinfo.get("FSTotalBytes") or 0)
                fs_free  = int(fsinfo.get("FSFreeBytes") or 0)
            else:
                fs_total = fs_free = 0
        except Exception:
            fs_total = fs_free = 0

        # Fallback to Lockdown DiskUsage if AFC not available
        if fs_total <= 0 and ("StorageTotalBytes" in di):
            try:
                fs_total = int(float(di.get("StorageTotalBytes") or 0))
                fs_free  = int(float(di.get("StorageFreeBytes")  or 0))
            except Exception:
                pass

        fs_used = max(0, fs_total - fs_free)
        percent_used = (fs_used / fs_total * 100.0) if fs_total else 0.0
        di["storage"] = {
            "total": fs_total,
            "free":  fs_free,
            "used":  fs_used,
            "percent_used": percent_used,
        }

        if log_callback:
            try:
                log_callback(f"Connected: {di.get('Name','iPhone')} (iOS {di.get('iOS','')})")
                if fs_total > 0:
                    mb = 1024 * 1024
                    log_callback(
                        f"[storage] used={fs_used/mb:.1f}MB / total={fs_total/mb:.1f}MB ({percent_used:.1f}%)"
                    )
                elif "StorageTotalBytes" in di:
                    mb = 1024 * 1024
                    log_callback(
                        f"[storage] (Lockdown) used={float(di.get('StorageUsedBytes',0))/mb:.1f}MB "
                        f"/ total={float(di.get('StorageTotalBytes',0))/mb:.1f}MB"
                    )
                else:
                    log_callback("[storage] unable to query filesystem usage")
            except Exception:
                pass

        # Enumerate quickly; tick progress lightly every ~100 files
        for i, (path, size) in enumerate(_walk_dcim(svc)):
            total_seen += 1
            if filter_pred and not filter_pred(path):
                continue
            items.append((path, size))
            total_bytes += size
            if i % 100 == 0 and progress_callback:
                try:
                    # lightweight heartbeat (not overall % of total device DCIM)
                    progress_callback((i % 100))  # 0..100 visual tick
                except Exception:
                    pass

        if progress_callback:
            try:
                progress_callback(100.0)
            except Exception:
                pass

    totals = summarize_counts(items)
    if stats_callback:
        try:
            stats_callback(totals, total_bytes)
        except Exception:
            pass
    return di, items, totals
