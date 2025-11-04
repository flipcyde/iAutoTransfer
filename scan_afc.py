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
import threading
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


def _safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default


def _read_storage_from_lockdown(ld):
    """
    Read com.apple.disk_usage and return a storage dict matching the GUI expectation:
      {"total": <bytes>, "used": <bytes>, "avail": <bytes>,
       "data_total": <bytes>, "sys_total": <bytes>, "percent_used": <float>}
    """
    disk = {}
    try:
        # Modern domain (fast)
        disk = ld.get_value(domain="com.apple.disk_usage") or {}
    except Exception:
        disk = {}

    # Fallback: some stacks expose a 'DiskUsage' root dict (older/alt naming)
    if not disk:
        try:
            du = ld.get_value(None, "DiskUsage")
            if isinstance(du, dict):
                disk = {
                    "TotalDiskCapacity": du.get("TotalDiskCapacity") or du.get("TotalDataCapacity"),
                    "AmountDataAvailable": du.get("AmountDataAvailable") or du.get("TotalDataAvailable"),
                    "TotalDataCapacity": du.get("TotalDataCapacity"),
                    "TotalSystemCapacity": du.get("TotalSystemCapacity"),
                }
        except Exception:
            pass

    total = _safe_int(disk.get("TotalDiskCapacity"))
    data_total = _safe_int(disk.get("TotalDataCapacity"))
    sys_total = _safe_int(disk.get("TotalSystemCapacity"))
    avail = _safe_int(disk.get("AmountDataAvailable") or disk.get("FreeDiskCapacity"))
    used = total - avail if total and avail >= 0 else 0

    out = {}
    if total > 0:
        out["total"] = total
    if used >= 0:
        out["used"] = used
    if avail >= 0:
        out["avail"] = avail
    if data_total > 0:
        out["data_total"] = data_total
    if sys_total > 0:
        out["sys_total"] = sys_total
    if out.get("total"):
        t = float(out["total"]); u = float(out.get("used") or 0)
        out["percent_used"] = (u / t * 100.0) if t > 0 else 0.0
    return out


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
            if svc:
                svc.close()
        except Exception:
            pass
        try:
            if lockdown and hasattr(lockdown, "close"):
                lockdown.close()
        except Exception:
            pass


# ---- public helpers ----
def get_device_info(lockdown) -> Dict[str, str]:
    """Best-effort Lockdown fields + storage/battery (legacy)."""
    info: Dict[str, str] = {}
    for key in ("DeviceName", "ProductVersion", "ProductType", "SerialNumber", "UniqueDeviceID"):
        try:
            val = lockdown.get_value(None, key) if hasattr(lockdown, "get_value") else None
            if val is not None:
                info[key] = str(val)
        except Exception:
            pass

    # Legacy DiskUsage (some stacks expose this)
    try:
        du = lockdown.get_value(None, "DiskUsage")
        if isinstance(du, dict):
            total = float(du.get("TotalDataCapacity") or 0.0)
            free = float(du.get("TotalDataAvailable") or 0.0)
            used = max(0.0, total - free)
            info["StorageTotalBytes"] = total
            info["StorageFreeBytes"] = free
            info["StorageUsedBytes"] = used
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
    info["iOS"] = info.get("ProductVersion", "")
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

        # ---------- FAST PATH: start storage read in the background (non-blocking) ----------
        storage_ready = {"done": False}

        def _bg_storage():
            try:
                storage = _read_storage_from_lockdown(lockdown)  # fast Lockdown call
                if storage:
                    di["storage"] = storage
                    # legacy fallbacks for GUI versions that use these
                    di.setdefault("StorageTotalBytes", float(storage.get("total") or 0))
                    di.setdefault("StorageUsedBytes", float(storage.get("used") or 0))
                    di.setdefault("StorageFreeBytes", float(storage.get("avail") or 0))
                    if log_callback:
                        try:
                            gb = 1024 * 1024 * 1024
                            log_callback(
                                f"[storage] total={storage.get('total',0)/gb:.2f}GB "
                                f"used={storage.get('used',0)/gb:.2f}GB "
                                f"free={storage.get('avail',0)/gb:.2f}GB "
                                f"({storage.get('percent_used',0):.0f}%)"
                            )
                        except Exception:
                            pass
            finally:
                storage_ready["done"] = True

        t = threading.Thread(target=_bg_storage, daemon=True)
        t.start()

        # Always log connection immediately (keeps UI snappy)
        if log_callback:
            try:
                log_callback(f"Connected: {di.get('Name','iPhone')} (iOS {di.get('iOS','')})")
            except Exception:
                pass

        # ---------- Enumerate DCIM; tick progress lightly every ~100 files ----------
        for i, (path, size) in enumerate(_walk_dcim(svc)):
            total_seen += 1
            if filter_pred and not filter_pred(path):
                continue
            items.append((path, size))
            total_bytes += size
            if i % 100 == 0 and progress_callback:
                try:
                    progress_callback((i % 100))  # heartbeat 0..100 (not overall %)
                except Exception:
                    pass

        if progress_callback:
            try:
                progress_callback(100.0)
            except Exception:
                pass

        # Give the storage thread a tiny window to finish if it's already done (do not block UX)
        try:
            t.join(timeout=0.05)
        except Exception:
            pass

    totals = summarize_counts(items)
    if stats_callback:
        try:
            stats_callback(totals, total_bytes)
        except Exception:
            pass
    return di, items, totals
