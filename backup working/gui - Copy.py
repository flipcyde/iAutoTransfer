# gui.py  — iAutoTransfer (AFC Edition, gradient UI + glow + controls)
#
# Requires:
#   scan_afc.py      -> scan_media_afc(progress_cb, log_cb, stats_cb, filter_pred)
#   transfer_afc.py  -> TransferController(...)
#
# Optional for HEIC->JPEG in transfer:
#   py -3 -m pip install --upgrade pillow-heif pillow
#
# Tested on Python 3.12 + Tkinter (Windows).

import os
import time
import math
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from scan_afc import scan_media_afc
from transfer_afc import TransferController


# ---------------------- Gradient Progress Widget ----------------------

class GradientProgress(tk.Canvas):
    """
    Canvas-based progress bar with multi-color gradient (red->orange->yellow->chartreuse->green)
    and optional animated glow while transferring.
    Use:
      set(value: 0..100)
      start_glow(), stop_glow()
    """
    def __init__(self, master, height=26, corner=10, **kwargs):
        super().__init__(master, height=height, highlightthickness=0, bd=0, **kwargs)
        self._value = 0.0
        self._corner = corner
        self._bg_color = "#1b1f23"
        self._border_color = "#2a3138"
        self._text_color = "#d7dee7"
        self._font = ("Segoe UI", 10, "bold")

        self._stops = [
            "#ff3b30",  # red
            "#ff9500",  # orange
            "#ffcc00",  # yellow
            "#7cfc00",  # chartreuse-ish
            "#34c759"   # green
        ]

        # Glow state
        self._glow = False
        self._glow_phase = 0.0
        self._glow_job = None

        self.bind("<Configure>", lambda e: self._redraw())

    def set(self, value: float):
        self._value = max(0.0, min(100.0, float(value)))
        self._redraw()

    # ---- Glow control ----
    def start_glow(self):
        if self._glow:
            return
        self._glow = True
        self._animate_glow()

    def stop_glow(self):
        self._glow = False
        if self._glow_job:
            self.after_cancel(self._glow_job)
            self._glow_job = None
        self._redraw()

    def _animate_glow(self):
        if not self._glow:
            return
        self._glow_phase += 0.18  # speed
        self._redraw()
        self._glow_job = self.after(40, self._animate_glow)  # ~25fps

    # ---- Drawing helpers ----
    def _hex_to_rgb(self, hx):
        hx = hx.lstrip("#")
        return tuple(int(hx[i:i+2], 16) for i in (0, 2, 4))

    def _rgb_to_hex(self, rgb):
        return "#%02x%02x%02x" % rgb

    def _lerp(self, a, b, t):
        return a + (b - a) * t

    def _color_lerp(self, c1, c2, t):
        r1, g1, b1 = self._hex_to_rgb(c1)
        r2, g2, b2 = self._hex_to_rgb(c2)
        r = int(self._lerp(r1, r2, t))
        g = int(self._lerp(g1, g2, t))
        b = int(self._lerp(b1, b2, t))
        return self._rgb_to_hex((r, g, b))

    def _rounded_rect(self, x1, y1, x2, y2, r, **kwargs):
        # simple rounded rectangle
        self.create_polygon(
            x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r, x2, y2, x2-r, y2, x1+r, y2, x1, y2, x1, y2-r,
            x1, y1+r, x1, y1, smooth=True, **kwargs
        )

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        r = min(self._corner, h // 2)

        # background & border
        self._rounded_rect(1, 1, w-1, h-1, r, fill=self._bg_color, outline=self._border_color, width=2)

        # progress width
        pct = self._value / 100.0
        prog_w = max(0, int((w-4) * pct))  # inside border padding
        left = 2
        top = 2
        right = 2 + prog_w
        bottom = h - 2

        # draw gradient segments
        if prog_w > 0:
            segments = len(self._stops) - 1
            for i in range(segments):
                seg_start = i / segments
                seg_end = (i+1) / segments
                seg_left = left + int((right - left) * seg_start)
                seg_right = left + int((right - left) * seg_end)
                if seg_right <= seg_left:
                    continue
                steps = max(1, seg_right - seg_left)
                for s in range(steps):
                    t = s / max(1, steps - 1)
                    col = self._color_lerp(self._stops[i], self._stops[i+1], t)
                    x1 = seg_left + s
                    self.create_line(x1, top, x1, bottom, fill=col)

            # rounded cap clean-up
            self._rounded_rect(left, top, right, bottom, r, outline="", fill="")

            # glow halo
            if self._glow:
                phase = (math.sin(self._glow_phase) + 1.0) * 0.5  # 0..1
                alpha = int(40 + phase * 70)  # 40..110
                # fake glow via lighter overlay lines around the bar (no alpha in Tk)
                glow_color = "#%02x%02x%02x" % (60+alpha, 120+alpha//2, 255)
                for off in range(2, 7):
                    self.create_rectangle(left-off, top-off, right+off, bottom+off, outline=glow_color)

        # text label
        txt = f"{int(round(self._value))}%"
        self.create_text(w//2, h//2, text=txt, fill=self._text_color, font=self._font)


# ---------------------- Main Application Window ----------------------

class AppWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("iAutoTransfer — Apple Stack (AFC)")
        self.geometry("1180x760")
        self.minsize(980, 660)

        # state
        self.last_scan_items = []   # list[(remote_path, size)]
        self.last_device_info = {}  # dict
        self.bytes_total = 0
        self.log_q = queue.Queue()
        self._scan_thread = None
        self._xfer_thread = None
        self._xfer_controller = None
        self._closing = False

        self._build_styles()
        self._build_ui()
        self._pump_log()

        # default destination
        pictures = os.path.join(os.path.expanduser("~"), "Pictures", "iAutoTransfer")
        if not os.path.isdir(pictures):
            try:
                os.makedirs(pictures, exist_ok=True)
            except Exception:
                pictures = os.path.join(os.path.expanduser("~"), "Desktop", "iAutoTransfer")
                os.makedirs(pictures, exist_ok=True)
        self.dest_var.set(pictures)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- Styles / Theme ----------
    def _build_styles(self):
        self["bg"] = "#0f1317"
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("TFrame", background="#0f1317")
        style.configure("Header.TFrame", background="#101720")
        style.configure("Card.TFrame", background="#151b22", relief="flat")
        style.configure("TLabel", background="#0f1317", foreground="#d7dee7", font=("Segoe UI", 10))
        style.configure("Header.TLabel", background="#101720", foreground="#e8eef7", font=("Segoe UI", 12, "bold"))
        style.configure("Title.TLabel", background="#101720", foreground="#7cd1ff", font=("Segoe UI Semibold", 16))
        style.configure("Dim.TLabel", background="#0f1317", foreground="#9aa7b2")
        style.configure("TButton", font=("Segoe UI", 10), relief="flat")
        style.map("TButton",
                  background=[("!disabled", "#1f2833"), ("active", "#263344")],
                  foreground=[("!disabled", "#e8eef7"), ("active", "#ffffff")])
        style.configure("Accent.TButton", background="#2c7be5", foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", "#1f6bd1")])
        style.configure("Danger.TButton", background="#e55353", foreground="#ffffff")
        style.map("Danger.TButton", background=[("active", "#d03c3c")])

        style.configure("TEntry", fieldbackground="#0e141a", background="#0e141a", foreground="#d7dee7")
        style.map("TEntry", fieldbackground=[("active", "#121b22"), ("focus", "#121b22")])

    # ---------- UI Layout ----------
    def _build_ui(self):
        # Header
        header = ttk.Frame(self, style="Header.TFrame")
        header.pack(side="top", fill="x")
        self.title_lbl = ttk.Label(header, text="iAutoTransfer (AFC)", style="Title.TLabel")
        self.title_lbl.pack(side="left", padx=16, pady=10)
        self.device_lbl = ttk.Label(header, text="Device: —", style="Header.TLabel")
        self.device_lbl.pack(side="right", padx=16, pady=10)

        # Controls Card
        controls = ttk.Frame(self, style="Card.TFrame")
        controls.pack(side="top", fill="x", padx=16, pady=(16, 10))

        # Destination
        ttk.Label(controls, text="Destination Folder:", style="TLabel").grid(row=0, column=0, sticky="w", padx=10, pady=10)
        self.dest_var = tk.StringVar()
        self.dest_entry = ttk.Entry(controls, textvariable=self.dest_var, width=72)
        self.dest_entry.grid(row=0, column=1, sticky="we", padx=(0, 8), pady=10)
        browse_btn = ttk.Button(controls, text="Browse…", command=self._on_browse, style="TButton")
        browse_btn.grid(row=0, column=2, sticky="w", padx=(0, 8), pady=10)

        # Action Buttons
        self.scan_btn = ttk.Button(controls, text="Scan (AFC)", command=self._on_scan, style="Accent.TButton")
        self.scan_btn.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="w")

        self.transfer_btn = ttk.Button(controls, text="Transfer", command=self._on_transfer, style="Accent.TButton")
        self.transfer_btn.grid(row=1, column=1, padx=(0, 8), pady=(0, 10), sticky="w")

        self.pause_btn = ttk.Button(controls, text="Pause", command=self._on_pause, style="TButton")
        self.pause_btn.grid(row=1, column=3, padx=(0, 8), pady=(0, 10), sticky="w")

        self.stop_btn = ttk.Button(controls, text="Stop", command=self._on_stop, style="Danger.TButton")
        self.stop_btn.grid(row=1, column=4, padx=(0, 8), pady=(0, 10), sticky="w")

        # Options
        self.flatten_var = tk.BooleanVar(value=False)
        self.chk_flatten = ttk.Checkbutton(controls, text="Flatten output", variable=self.flatten_var)
        self.chk_flatten.grid(row=1, column=5, padx=(8, 0), pady=(0, 10), sticky="w")

        self.heic_var = tk.BooleanVar(value=False)
        self.chk_heic = ttk.Checkbutton(controls, text="Convert HEIC → JPEG", variable=self.heic_var)
        self.chk_heic.grid(row=1, column=6, padx=(8, 0), pady=(0, 10), sticky="w")

        self.del_heic_var = tk.BooleanVar(value=False)
        self.chk_del_heic = ttk.Checkbutton(controls, text="Delete HEIC after convert", variable=self.del_heic_var)
        self.chk_del_heic.grid(row=1, column=7, padx=(8, 0), pady=(0, 10), sticky="w")

        controls.columnconfigure(1, weight=1)

        # Stats & Progress Card
        stats_card = ttk.Frame(self, style="Card.TFrame")
        stats_card.pack(side="top", fill="x", padx=16, pady=(0, 10))

        self.stats_lbl = ttk.Label(stats_card, text="Idle — 0 files", style="TLabel")
        self.stats_lbl.pack(side="left", padx=12, pady=12)

        self.rate_lbl = ttk.Label(stats_card, text="0.00 files/s | 0.00 MB/s | ETA —", style="Dim.TLabel")
        self.rate_lbl.pack(side="right", padx=12, pady=12)

        # Gradient Progress
        prog_card = ttk.Frame(self, style="Card.TFrame")
        prog_card.pack(side="top", fill="x", padx=16, pady=(0, 10))
        self.progress = GradientProgress(prog_card, height=28, bg="#151b22")
        self.progress.pack(side="top", fill="x", padx=12, pady=12)

        # Log Card
        log_card = ttk.Frame(self, style="Card.TFrame")
        log_card.pack(side="top", fill="both", expand=True, padx=16, pady=(0, 16))

        self.log = tk.Text(log_card, height=18, bg="#0e141a", fg="#d7dee7",
                           insertbackground="#ffffff", relief="flat", wrap="word")
        self.log.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        self.log.tag_configure("info", foreground="#9bd0ff")
        self.log.tag_configure("good", foreground="#8df7a8")
        self.log.tag_configure("warn", foreground="#ffd166")
        self.log.tag_configure("err", foreground="#ff6b6b")
        self.log.tag_configure("dim", foreground="#9aa7b2")

        log_scroll = ttk.Scrollbar(log_card, orient="vertical", command=self.log.yview)
        log_scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=log_scroll.set)

        # initial control states
        self.pause_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")

    # ---------- Helpers ----------
    def _append_log(self, msg, tag="info"):
        if self._closing:
            return
        self.log_q.put((msg, tag))

    def _pump_log(self):
        try:
            while True:
                msg, tag = self.log_q.get_nowait()
                self.log.insert("end", time.strftime("[%H:%M:%S] ") + str(msg) + "\n", tag)
                self.log.see("end")
        except queue.Empty:
            pass
        if not self._closing:
            self.after(40, self._pump_log)

    def _set_status(self, text):
        self.stats_lbl.config(text=text)

    def _update_header_device(self):
        di = getattr(self, "last_device_info", {}) or {}
        name = di.get("Name", "iPhone")
        ios = di.get("iOS", "?")
        udid = di.get("UDID", "")
        if udid:
            udid = udid[:8] + "…"
        self.device_lbl.config(text=f"Device: {name} • iOS {ios} • {udid}")

    def _on_progress(self, pct):
        try:
            self.progress.set(float(pct))
        except Exception:
            pass

    def _calc_eta(self, bytes_done, bytes_total, bps):
        if bytes_total <= 0 or bps <= 0:
            return "—"
        remain = max(0, bytes_total - bytes_done) / bps
        m = int(remain // 60)
        s = int(remain % 60)
        return f"{m}m{s:02d}s"

    # ---------- Callbacks for scan/transfer ----------
    def _scan_stats_cb(self, totals, bytes_total):
        self.bytes_total = bytes_total
        self._set_status(f"Scanned {totals['total']} files • {totals['photos']} photos, {totals['videos']} videos • {bytes_total/1024/1024:.1f} MB total")

    def _xfer_stats_cb(self, copied, total, fps, bytes_done=0, bytes_total=0, bps=0):
        eta = self._calc_eta(bytes_done, bytes_total, bps)
        self.rate_lbl.config(text=f"{fps:.2f} files/s | {bps/1024/1024:.2f} MB/s | ETA {eta}")
        self._set_status(f"Copying {copied}/{total} files")

    # ---------- Button Handlers ----------
    def _on_browse(self):
        path = filedialog.askdirectory(title="Select destination folder")
        if path:
            self.dest_var.set(path)

    def _on_clear_log(self):
        self.log.delete("1.0", "end")

    def _disable_buttons(self):
        self.scan_btn.config(state="disabled")
        self.transfer_btn.config(state="disabled")

    def _enable_buttons(self):
        self.scan_btn.config(state="normal")
        self.transfer_btn.config(state="normal")
        # Pause/Stop are enabled only during transfer

    def _on_scan(self):
        if self._scan_thread and self._scan_thread.is_alive():
            return
        self._disable_buttons()
        self.pause_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.progress.stop_glow()
        self.progress.set(0)
        self.rate_lbl.config(text="0.00 files/s | 0.00 MB/s | ETA —")
        self._append_log("==== SCAN START (AFC) ====", "dim")

        def worker():
            try:
                di, items, totals = scan_media_afc(
                    progress_callback=self._on_progress,
                    log_callback=lambda m: self._append_log(m, "info"),
                    stats_callback=self._scan_stats_cb,
                    filter_pred=None
                )
                self.last_scan_items = items or []
                self.last_device_info = di or {}
                self._update_header_device()

                self._append_log(f"Scan complete: {totals.get('total',0)} files", "good")
                self._append_log("==== SCAN END ====", "dim")
            except Exception as e:
                self._append_log(f"Scan error: {e}", "err")
            finally:
                self._enable_buttons()

        self._scan_thread = threading.Thread(target=worker, daemon=True)
        self._scan_thread.start()

    def _on_transfer(self):
        if self._xfer_thread and self._xfer_thread.is_alive():
            return
        items = getattr(self, "last_scan_items", [])
        if not items:
            messagebox.showinfo("iAutoTransfer", "Nothing to transfer. Please run Scan first.")
            return

        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showinfo("iAutoTransfer", "Please select a destination folder.")
            return

        try:
            os.makedirs(dest, exist_ok=True)
        except Exception as e:
            messagebox.showerror("iAutoTransfer", f"Cannot create destination:\n{e}")
            return

        self._disable_buttons()
        self.pause_btn.config(state="normal", text="Pause")
        self.stop_btn.config(state="normal")
        self.progress.set(0)
        self.progress.start_glow()
        self.rate_lbl.config(text="0.00 files/s | 0.00 MB/s | ETA —")
        self._append_log(f"Starting transfer to {dest}", "info")

        total = len(items)
        self._set_status(f"Copying 0/{total} files")

        def stats(copied, total, fps, bytes_done=0, bytes_total=0, bps=0):
            eta = self._calc_eta(bytes_done, bytes_total, bps)
            self.rate_lbl.config(text=f"{fps:.2f} files/s | {bps/1024/1024:.2f} MB/s | ETA {eta}")
            self._set_status(f"Copying {copied}/{total} files")

        self._xfer_controller = TransferController(
            items=items,
            dest_root=dest,
            progress_callback=self._on_progress,
            log_callback=lambda m: self._append_log(m, "info"),
            stats_callback=stats,
            num_workers=3,  # Tune 2-4; often saturates around 3 on iPhone USB
            flatten=self.flatten_var.get(),
            convert_heic=self.heic_var.get(),
            delete_heic_after_convert=self.del_heic_var.get(),
        )

        def runner():
            try:
                self._xfer_controller.run()
                self._append_log("Transfer complete.", "good")
            except Exception as e:
                self._append_log(f"Transfer error: {e}", "err")
            finally:
                self.progress.stop_glow()
                self._enable_buttons()
                self.pause_btn.config(state="disabled", text="Pause")
                self.stop_btn.config(state="disabled")
                self._xfer_controller = None

        self._xfer_thread = threading.Thread(target=runner, daemon=True)
        self._xfer_thread.start()

    def _on_pause(self):
        if not self._xfer_controller:
            return
        # toggle pause/resume
        if self.pause_btn.cget("text") == "Pause":
            self._xfer_controller.pause()
            self.pause_btn.config(text="Resume")
            # optional: stop glow when paused
            self.progress.stop_glow()
        else:
            self._xfer_controller.resume()
            self.pause_btn.config(text="Pause")
            self.progress.start_glow()

    def _on_stop(self):
        if self._xfer_controller:
            self._xfer_controller.stop()
            self._append_log("Stop requested by user.", "warn")

    # ---------- Lifecycle ----------
    def _on_close(self):
        self._closing = True
        try:
            if self._xfer_controller and self._xfer_controller.is_running():
                self._xfer_controller.stop()
        except Exception:
            pass
        self.progress.stop_glow()
        self.destroy()

    # Compatibility with main.py expecting app.run()
    def run(self):
        self.mainloop()


# Local launch
if __name__ == "__main__":
    app = AppWindow()
    app.run()
