# scan_afc.py
import os
import time
from typing import Callable, Dict, List, Optional, Tuple
from contextlib import contextmanager

# Robust imports across pymobiledevice3 versions
try: from pymobiledevice3.lockdown import create_using_usbmux
except Exception: create_using_usbmux = None
try: from pymobiledevice3.lockdown import LockdownClient
except Exception: LockdownClient = None
try: from pymobiledevice3.services.afc import AfcService
except Exception: AfcService = None
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

MEDIA_EXT = tuple({
    '.jpg','.jpeg','.heic','.png','.raw','.dng',
    '.mov','.mp4','.avi','.hevc','.m4v','.3gp'
})

PhotoExt = {'.jpg','.jpeg','.heic','.png','.raw','.dng'}
VideoExt = {'.mov','.mp4','.avi','.hevc','.m4v','.3gp'}

def _is_media(name: str) -> bool:
    return name.lower().endswith(MEDIA_EXT)

def _is_photo(name: str) -> bool:
    return name.lower().endswith(tuple(PhotoExt))

def _is_video(name: str) -> bool:
    return name.lower().endswith(tuple(VideoExt))

def _safe(cb: Optional[Callable], *args, **kwargs):
    if cb:
        try: cb(*args, **kwargs)
        except Exception: pass

def _pick_first_device():
    if mux_select_device:
        dev = mux_select_device()
        if not dev: raise RuntimeError("No iOS device via usbmux (unlock & Trust).")
        return dev
    if mux_list_devices:
        devs = mux_list_devices()
        if not devs: raise RuntimeError("No iOS device via usbmux (unlock & Trust).")
        return devs[0]
    if UsbmuxClass:
        mux = UsbmuxClass()
        if getattr(mux, "devices", None):
            return mux.devices[0]
        raise RuntimeError("No iOS device via usbmux (unlock & Trust).")
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
                raise RuntimeError("Lockdown client API not found.")
            lockdown = LockdownClient(device=dev)
        if not AfcService:
            raise RuntimeError("AfcService not available.")
        svc = AfcService(lockdown=lockdown)
        yield lockdown, svc
    finally:
        try:
            if svc: svc.close()
        except Exception: pass
        try:
            if lockdown and hasattr(lockdown, "close"): lockdown.close()
        except Exception: pass

def _afc_listdir(svc, path: str):
    try: return [p for p in svc.listdir(path) if p not in ('.','..')]
    except Exception: return []

def _afc_stat(svc, path: str):
    try: return svc.stat(path)
    except Exception: return None

def _walk_dcim(svc, root="/DCIM"):
    """
    Yield (remote_path, size_bytes) for media files under /DCIM (depth-first).
    """
    stack = [root]
    while stack:
        cur = stack.pop()
        for name in _afc_listdir(svc, cur):
            full = f"{cur.rstrip('/')}/{name}"
            st = _afc_stat(svc, full)
            if not st: 
                continue
            if st.get('st_ifmt') == 'S_IFDIR':
                stack.append(full)
            else:
                if _is_media(name):
                    yield full, int(st.get('st_size', 0) or 0)

def get_device_info(lockdown) -> Dict[str, str]:
    """
    Pull a safe subset of device details from Lockdown.
    """
    info = {}
    # newer pymobiledevice3 exposes .all_values or .get_value
    for key in ("DeviceName","ProductVersion","ProductType","SerialNumber","UniqueDeviceID"):
        try:
            # Lockdown.get_value(domain=None, key=...)
            val = lockdown.get_value(None, key) if hasattr(lockdown, "get_value") else None
            if val is not None:
                info[key] = str(val)
        except Exception:
            pass
    # friendly aliases
    info["Name"] = info.get("DeviceName","iPhone")
    info["iOS"] = info.get("ProductVersion","")
    info["UDID"] = info.get("UniqueDeviceID","")
    return info

def summarize_counts(paths_sizes: List[Tuple[str,int]]) -> Dict[str,int]:
    photos = sum(1 for p,_ in paths_sizes if _is_photo(os.path.basename(p)))
    videos = sum(1 for p,_ in paths_sizes if _is_video(os.path.basename(p)))
    total  = len(paths_sizes)
    return {"total": total, "photos": photos, "videos": videos}

def scan_media_afc(
    progress_callback: Optional[Callable[[float], None]],
    log_callback: Optional[Callable[[str], None]],
    stats_callback: Optional[Callable[[Dict[str,int], int], None]] = None,
    filter_pred: Optional[Callable[[str], bool]] = None,
):
    """
    Enumerate iPhone /DCIM via AFC.
    Returns (device_info: dict, items: List[(remote_path, size_bytes)], totals: dict)

    - progress_callback(percent_float)
    - log_callback(str)
    - stats_callback(totals_dict, bytes_total)
    - filter_pred(remote_path) -> bool to filter items (optional)
    """
    t0 = time.time()
    items: List[Tuple[str,int]] = []
    bytes_total = 0

    with _afc_session() as (lockdown, svc):
        dev = get_device_info(lockdown)
        _safe(log_callback, f"Device: {dev.get('Name','iPhone')} — iOS {dev.get('iOS','?')} — UDID {dev.get('UDID','')[:8]}…")

        # verify DCIM
        if "DCIM" not in _afc_listdir(svc, "/"):
            _safe(log_callback, "No /DCIM via AFC. On iPhone: Settings > Photos > Transfer to Mac or PC > Keep Originals.")
            return dev, items, {"total":0,"photos":0,"videos":0}

        # quick pass to count folders (for progress pacing)
        # we’ll update progress by item count as we go
        count_est = 0
        for path, sz in _walk_dcim(svc, "/DCIM"):
            if (filter_pred is None) or filter_pred(path):
                items.append((path, sz))
                bytes_total += max(0, sz)
                count_est += 1
                if count_est % 250 == 0 and progress_callback:
                    pct = (count_est / max(1, count_est)) * 100.0  # keeps spinner feel
                    progress_callback(min(99.0, pct))

        totals = summarize_counts(items)
        if progress_callback:
            progress_callback(100.0)

        _safe(log_callback, f"Scan complete: {totals['total']} files ({totals['photos']} photos, {totals['videos']} videos)")
        if stats_callback:
            _safe(stats_callback, totals, bytes_total)

        return dev, items, totals
