# transfer_afc.py — parallel AFC transfer with pause/resume/stop, flatten, HEIC->JPEG, worker telemetry
# Hooks emitted here:
#   worker_callback(wid, info_dict)                     # info: {"status","last_file","mbps","active","files"}
#   success_callback(worker_id, remote_path, local_path, bytes)
#   failed_callback(remote_path, reason)                # (GUI marshals to main thread)
#   manifest_writer(remote_path, local_path, size_bytes, ok, worker_id, error_message)
#
# Telemetry "status" values: "idle", "copying", "ok", "failed", "skipped"

import os
import time
import threading
import queue
from typing import List, Tuple, Optional, Dict
from contextlib import contextmanager
from collections import defaultdict

# ---- optional: HEIC conversion
try:
    import pillow_heif  # HEIF decoder
    from PIL import Image
    pillow_heif.register_heif_opener()
    _HEIC_OK = True
except Exception:
    _HEIC_OK = False

# ---- robust imports across pymobiledevice3 versions ----
try:
    from pymobiledevice3.lockdown import create_using_usbmux
except Exception:
    create_using_usbmux = None
try:
    from pymobiledevice3.lockdown import LockdownClient
except Exception:
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


def _safe(cb, *args, **kwargs):
    if cb:
        try:
            cb(*args, **kwargs)
        except Exception:
            pass


def _pick_first_device():
    if mux_select_device:
        dev = mux_select_device()
        if not dev:
            raise RuntimeError("No iOS device via usbmux (unlock & Trust).")
        return dev
    if mux_list_devices:
        devs = mux_list_devices()
        if not devs:
            raise RuntimeError("No iOS device via usbmux (unlock & Trust).")
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
        yield svc
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


def _dedupe_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    n = 1
    while True:
        cand = f"{root} ({n}){ext}"
        if not os.path.exists(cand):
            return cand
        n += 1


def _compute_local_path(dest_root: str, remote: str, flatten: bool) -> str:
    base = os.path.basename(remote)
    if flatten:
        return os.path.join(dest_root, base)
    rel_under_dcim = os.path.dirname(remote).replace("/DCIM", "").strip("/\\")
    local_dir = os.path.join(dest_root, "DCIM", rel_under_dcim) if rel_under_dcim else os.path.join(dest_root, "DCIM")
    return os.path.join(local_dir, base)


def _pull_file(svc, remote_path: str, local_path: str, log_callback, retries=3, chunk=1024 * 1024) -> (bool, Optional[str]):
    """
    Copy one file phone->PC using first available AFC API.
    Tries: pull -> file_open/read/close -> get_file_contents -> read_file
    Returns (ok, error_message_if_any)
    """
    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    # A) pull()
    if hasattr(svc, "pull"):
        for t in range(retries):
            try:
                _safe(log_callback, f"[afc] using pull(): {remote_path}")
                svc.pull(remote_path, local_path)
                return True, None
            except Exception as e:
                msg = f"[pull] retry {t+1}: {e}"
                _safe(log_callback, msg)
                if t + 1 == retries:
                    return False, str(e)
                time.sleep(0.25 * (t + 1))

    # B) file_* API
    if hasattr(svc, "file_open") and hasattr(svc, "file_read") and hasattr(svc, "file_close"):
        for t in range(retries):
            try:
                _safe(log_callback, f"[afc] using file_open/read/close: {remote_path}")
                with open(local_path, "wb") as out_f:
                    h = svc.file_open(remote_path, mode='r')
                    try:
                        while True:
                            data = svc.file_read(h, chunk)
                            if not data:
                                break
                            out_f.write(data)
                    finally:
                        svc.file_close(h)
                return True, None
            except Exception as e:
                msg = f"[stream] retry {t+1}: {e}"
                _safe(log_callback, msg)
                try:
                    os.remove(local_path)
                except Exception:
                    pass
                if t + 1 == retries:
                    return False, str(e)
                time.sleep(0.25 * (t + 1))

    # C) get_file_contents()
    if hasattr(svc, "get_file_contents"):
        for t in range(retries):
            try:
                _safe(log_callback, f"[afc] using get_file_contents(): {remote_path}")
                data = svc.get_file_contents(remote_path)
                with open(local_path, "wb") as f:
                    f.write(data)
                return True, None
            except Exception as e:
                msg = f"[get_file_contents] retry {t+1}: {e}"
                _safe(log_callback, msg)
                if t + 1 == retries:
                    return False, str(e)
                time.sleep(0.25 * (t + 1))

    # D) read_file()
    if hasattr(svc, "read_file"):
        for t in range(retries):
            try:
                _safe(log_callback, f"[afc] using read_file(): {remote_path}")
                data = svc.read_file(remote_path)
                with open(local_path, "wb") as f:
                    f.write(data)
                return True, None
            except Exception as e:
                msg = f"[read_file] retry {t+1}: {e}"
                _safe(log_callback, msg)
                if t + 1 == retries:
                    return False, str(e)
                time.sleep(0.25 * (t + 1))

    return False, "No usable AFC method (need pull, file_open/read/close, get_file_contents, or read_file)."


def _maybe_convert_heic_to_jpeg(src_path: str, log_callback, delete_original: bool = False) -> Optional[str]:
    if not _HEIC_OK or not src_path.lower().endswith(".heic"):
        return None
    try:
        img = Image.open(src_path)
        rgb = img.convert("RGB")
        out_path = os.path.splitext(src_path)[0] + ".jpg"
        out_path = _dedupe_path(out_path)
        rgb.save(out_path, "JPEG", quality=92, optimize=True)
        if delete_original:
            try:
                os.remove(src_path)
            except Exception:
                pass
        _safe(log_callback, f"Converted HEIC -> {os.path.basename(out_path)}")
        return out_path
    except Exception as e:
        _safe(log_callback, f"HEIC convert error: {e}")
        return None


class TransferController:
    """
    Multi-thread AFC transfer controller.

    Options / Hooks:
      - flatten: put everything directly in dest_root
      - convert_heic: convert .heic to .jpg after copy (requires pillow-heif)
      - delete_heic_after_convert: remove original .heic after success (if convert_heic)
      - worker_callback(wid, info_dict)  # {"status","last_file","mbps","active","files"}
      - success_callback(worker_id, remote_path, local_path, bytes)
      - failed_callback(remote_path, reason)
      - manifest_writer(remote_path, local_path, size_bytes, ok, worker_id, error_message)
    """
    def __init__(
        self,
        items: List[Tuple[str, int]],
        dest_root: str,
        progress_callback,
        log_callback,
        stats_callback,
        num_workers: int = 3,
        flatten: bool = False,
        convert_heic: bool = False,
        delete_heic_after_convert: bool = False,
        worker_callback=None,
        success_callback=None,
        failed_callback=None,
        manifest_writer=None,
    ):
        self.items = items
        self.total = len(items)
        self.dest_root = os.path.normpath(os.path.abspath(dest_root))
        os.makedirs(self.dest_root, exist_ok=True)

        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.stats_callback = stats_callback

        self.num_workers = max(1, int(num_workers))
        self.flatten = bool(flatten)
        self.convert_heic = bool(convert_heic)
        self.delete_heic_after_convert = bool(delete_heic_after_convert)
        self.worker_callback = worker_callback
        self.success_callback = success_callback
        self.failed_callback = failed_callback
        self.manifest_writer = manifest_writer  # callable or None

        self.q: "queue.Queue[Tuple[str,int]]" = queue.Queue()
        for it in items:
            self.q.put(it)

        self._lock = threading.Lock()
        self._t0 = time.time()
        self._copied = 0
        self._bytes_total = sum(sz for _, sz in items if sz > 0)
        self._bytes_done = 0

        self._threads: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # when set => paused
        self._running = False

        # scaling / retry
        self._desired_workers = self.num_workers
        self._failed_items: List[Tuple[str, int]] = []

        # per-worker stats
        self._w_files: Dict[int, int] = defaultdict(int)     # attempts processed
        self._w_last_mbps: Dict[int, float] = defaultdict(float)

    # ---- control ----
    def pause(self):
        self._pause_event.set()
        _safe(self.log_callback, "Paused.")

    def resume(self):
        if self._pause_event.is_set():
            self._pause_event.clear()
            _safe(self.log_callback, "Resumed.")

    def stop(self):
        self._stop_event.set()
        _safe(self.log_callback, "Stopping…")

    def is_running(self) -> bool:
        return self._running

    def join(self):
        for t in self._threads:
            t.join()

    # ---- stats/emit ----
    def _emit_progress(self):
        if self.progress_callback:
            pct = (self._copied / max(1, self.total)) * 100.0
            self.progress_callback(pct)

    def _emit_stats(self):
        if self.stats_callback:
            elapsed = max(1e-6, time.time() - self._t0)
            fps = self._copied / elapsed
            bps = self._bytes_done / elapsed
            try:
                self.stats_callback(self._copied, self.total, fps, self._bytes_done, self._bytes_total, bps)
            except TypeError:
                self.stats_callback(self._copied, self.total, fps)

    # ---- manifest hook ----
    def _manifest(self, remote_path, local_path, size_bytes, ok, worker_id, error_message=None):
        if self.manifest_writer:
            _safe(self.manifest_writer, remote_path, local_path, size_bytes, ok, worker_id, error_message)

    # ---- scaling ----
    def scale_workers(self, new_count: int):
        """Increase/decrease worker threads (decrease is opportunistic when idle)."""
        new_count = max(1, int(new_count))
        if not self._running:
            self.num_workers = self._desired_workers = new_count
            return

        if new_count > self._desired_workers:
            need = new_count - self._desired_workers
            start_idx = len(self._threads)
            extra = [
                threading.Thread(target=self._worker, args=(start_idx + i + 1,), daemon=True)
                for i in range(need)
            ]
            for t in extra:
                t.start()
                self._threads.append(t)
        self._desired_workers = new_count

    # ---- retry support ----
    def retry_failed(self):
        """Requeue failed items during an active transfer."""
        with self._lock:
            for rp, sz in self._failed_items:
                self.q.put((rp, sz))
            n = len(self._failed_items)
            self._failed_items.clear()
        _safe(self.log_callback, f"Re-queued {n} failed items.")

    # ---- worker ----
    def _emit_worker(self, wid: int, status: str, last_file: str, mbps: Optional[float], active: bool):
        """Send telemetry. 'mbps' may be None when idle; GUI should keep last non-zero."""
        payload = {
            "status": status,
            "last_file": last_file,
            "active": bool(active),
            "files": int(self._w_files.get(wid, 0)),
        }
        if mbps is not None:
            payload["mbps"] = float(mbps)
        if self.worker_callback:
            _safe(self.worker_callback, wid, payload)

    def _worker(self, wid: int):
        # announce idle row with sticky (no mbps field => GUI keeps whatever it had; initial 0.00)
        self._emit_worker(wid, "idle", "", None, False)

        with _afc_session() as svc:
            while not self._stop_event.is_set():
                # opportunistic shrink when over target
                if self._desired_workers < len(self._threads):
                    if self.q.empty():
                        break

                if self._pause_event.is_set():
                    time.sleep(0.05)
                    continue

                try:
                    remote, sz = self.q.get_nowait()
                except queue.Empty:
                    break

                raw_path = _compute_local_path(self.dest_root, remote, self.flatten)
                local_path = _dedupe_path(raw_path)
                basename = os.path.basename(remote)

                # mark an attempt processed immediately (reflects queue throughput)
                self._w_files[wid] += 1
                self._emit_worker(wid, "copying", basename, None, True)

                # skip if exists (fast path)
                if os.path.exists(local_path):
                    got = os.path.getsize(local_path) if os.path.exists(local_path) else sz
                    t0 = time.time()
                    # emit a tiny synthetic speed to keep last_mbps meaningful (optional)
                    measured_mbps = (got / max(1e-6, time.time() - t0)) / (1024 * 1024) if got else None
                    if measured_mbps and measured_mbps > 0:
                        self._w_last_mbps[wid] = measured_mbps

                    with self._lock:
                        self._copied += 1
                        self._bytes_done += (got if sz == 0 else sz)
                        self._emit_worker(wid, "skipped", basename, measured_mbps, False)
                        if self.success_callback:
                            _safe(self.success_callback, worker_id=wid, remote_path=remote, local_path=local_path, bytes=sz)
                        self._manifest(remote, local_path, sz, True, wid, None)
                        self._emit_progress()
                        self._emit_stats()
                    self.q.task_done()
                    continue

                # copy via AFC
                t0 = time.time()
                ok, err = _pull_file(svc, remote, local_path, self.log_callback)
                elapsed = max(1e-6, time.time() - t0)
                measured_mbps = (os.path.getsize(local_path) / elapsed) / (1024 * 1024) if ok else None
                if measured_mbps and measured_mbps > 0:
                    self._w_last_mbps[wid] = measured_mbps

                with self._lock:
                    if ok:
                        self._copied += 1
                        add_bytes = (os.path.getsize(local_path) if sz == 0 else sz)
                        self._bytes_done += add_bytes
                        _safe(self.log_callback, f"[W{wid}] Copied {basename}")
                        if self.success_callback:
                            _safe(self.success_callback, worker_id=wid, remote_path=remote, local_path=local_path, bytes=sz)
                        self._manifest(remote, local_path, sz, True, wid, None)
                        self._emit_worker(wid, "ok", basename, measured_mbps, False)
                    else:
                        _safe(self.log_callback, f"[W{wid}] FAILED {basename} — {err}")
                        self._failed_items.append((remote, sz))
                        if self.failed_callback:
                            _safe(self.failed_callback, remote, err)
                        self._manifest(remote, local_path, sz, False, wid, err)
                        # on failure, do not zero out mbps; emit None so GUI keeps last value
                        self._emit_worker(wid, "failed", basename, None, False)

                    self._emit_progress()
                    self._emit_stats()

                self.q.task_done()

                # optional HEIC conversion (post-copy)
                if ok and self.convert_heic and local_path.lower().endswith(".heic"):
                    _maybe_convert_heic_to_jpeg(local_path, self.log_callback, delete_original=self.delete_heic_after_convert)

    # ---- runner ----
    def run(self):
        self._running = True
        flags = []
        if self.flatten: flags.append("flatten")
        if self.convert_heic: flags.append("heic->jpeg")
        if self.delete_heic_after_convert: flags.append("delete-heic")
        _safe(self.log_callback, f"AFC transfer: {self.total} files -> {self.dest_root} | workers={self.num_workers} | {'; '.join(flags) if flags else 'no-extra-flags'}")
        try:
            self._threads = [
                threading.Thread(target=self._worker, args=(i + 1,), daemon=True)
                for i in range(self.num_workers)
            ]
            for t in self._threads:
                t.start()
            self.q.join()
        finally:
            self._stop_event.set()
            for t in self._threads:
                try:
                    t.join(timeout=0.2)
                except Exception:
                    pass
            self._running = False
            _safe(self.log_callback, "AFC transfer complete.")
