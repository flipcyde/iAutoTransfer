# gui.py  — iAutoTransfer (AFC Edition, gradient UI + glow + controls + storage/filters + telemetry/retry/manifest)
# Adds per-worker FILES count and sticky MBPS display + tooltips on controls.
# Also greys out the entire UI with an overlay if Apple drivers are missing.

import os
import time
import math
import csv
import threading
import queue
import subprocess
import shutil
import tempfile
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

from scan_afc import scan_media_afc, make_filter
from transfer_afc import TransferController
from apple_sanity import sanity_check_apple_drivers

# ---------------------- Config for driver workflow ----------------------
# If you ship driver MSIs with your app, place them here:
BUNDLED_DRIVER_DIR = Path("extras/apple_drivers")
# Typical filenames you may include:
#   AppleApplicationSupport.msi
#   AppleApplicationSupport64.msi
#   AppleMobileDeviceSupport.msi
#   AppleMobileDeviceSupport64.msi

# Download links if user prefers getting iTunes:
ITUNES_DOWNLOAD_URL = "https://www.apple.com/itunes/download/"
ITUNES_MICROSOFT_STORE_URL = "https://apps.microsoft.com/detail/itunes/9pb2mz1zmb1s"


# ---------------------- Tooltip helper (no deps) ----------------------

class ToolTip:
    def __init__(self, widget, text, delay_ms=350, wrap=60):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.wrap = wrap
        self._id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _schedule(self, _):
        self._unschedule()
        self._id = self.widget.after(self.delay_ms, self._show)

    def _unschedule(self):
        if self._id:
            try:
                self.widget.after_cancel(self._id)
            except Exception:
                pass
            self._id = None

    def _show(self):
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        frame = tk.Frame(self._tip, bg="#1e2530", bd=1, highlightthickness=0)
        frame.pack()
        lbl = tk.Label(
            frame,
            text=self.text,
            justify="left",
            bg="#1e2530",
            fg="#e8eef7",
            font=("Segoe UI", 9),
            wraplength=self.wrap * 7,
            padx=8,
            pady=6,
        )
        lbl.pack()

    def _hide(self, _=None):
        self._unschedule()
        if self._tip:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


def bind_tooltip(widget, text):
    ToolTip(widget, text)


# ---------------------- Gradient Progress Widget ----------------------

class GradientProgress(tk.Canvas):
    def __init__(self, master, height=26, corner=10, **kwargs):
        super().__init__(master, height=height, highlightthickness=0, bd=0, **kwargs)
        self._value = 0.0
        self._corner = corner
        self._bg_color = "#1b1f23"
        self._border_color = "#2a3138"
        self._font = ("Segoe UI", 10, "bold")
        self._stops = ["#ff3b30", "#ff9500", "#ffcc00", "#7cfc00", "#34c759"]
        self._glow = False
        self._glow_phase = 0.0
        self._glow_job = None
        self.bind("<Configure>", lambda e: self._redraw())

    def set(self, value: float):
        self._value = max(0.0, min(100.0, float(value)))
        self._redraw()

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
        self._glow_phase += 0.18
        self._redraw()
        self._glow_job = self.after(40, self._animate_glow)

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
        self.create_polygon(
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
            smooth=True, **kwargs
        )

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        r = min(self._corner, h // 2)

        # background frame
        self._rounded_rect(1, 1, w - 1, h - 1, r, fill=self._bg_color, outline=self._border_color, width=2)

        # progress fill
        pct = self._value / 100.0
        prog_w = max(0, int((w - 4) * pct))
        left, top, right, bottom = 2, 2, 2 + prog_w, h - 2

        if prog_w > 0:
            segments = len(self._stops) - 1
            for i in range(segments):
                seg_start = i / segments
                seg_end = (i + 1) / segments
                seg_left = left + int((right - left) * seg_start)
                seg_right = left + int((right - left) * seg_end)
                if seg_right <= seg_left:
                    continue
                steps = max(1, seg_right - seg_left)
                for s in range(steps):
                    t = s / max(1, steps - 1)
                    col = self._color_lerp(self._stops[i], self._stops[i + 1], t)
                    x1 = seg_left + s
                    self.create_line(x1, top, x1, bottom, fill=col)

            self._rounded_rect(left, top, right, bottom, r, outline="", fill="")

            if self._glow:
                import math as _m
                phase = (_m.sin(self._glow_phase) + 1.0) * 0.5
                alpha = int(40 + phase * 70)
                glow_color = "#%02x%02x%02x" % (60 + alpha, 120 + alpha // 2, 255)
                for off in range(2, 7):
                    self.create_rectangle(left - off, top - off, right + off, bottom + off, outline=glow_color)

        # percentage label with dynamic contrast (white <50%, black ≥50%)
        text_color = "#000000" if pct >= 0.5 else "#ffffff"

        # optional soft shadow for readability
        self.create_text(w // 2 + 1, h // 2 + 1, text=f"{int(round(self._value))}%", fill="#000000", font=self._font)
        self.create_text(w // 2, h // 2, text=f"{int(round(self._value))}%", fill=text_color, font=self._font)


# ---------------------- Main Application Window ----------------------

class AppWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("iAutoTransfer — Apple Stack (AFC)")
        self.geometry("1180x840")
        self.minsize(980, 720)

        # state
        self.last_scan_items = []
        self.last_device_info = {}
        self.bytes_total = 0
        self.log_q = queue.Queue()
        self._scan_thread = None
        self._xfer_thread = None
        self._xfer_controller = None
        self._closing = False
        self._manifest_fp = None

        # per-worker sticky UI state
        self._w_last_mbps = {}     # wid -> last non-zero mbps
        self._w_files = {}         # wid -> files processed

        self._build_styles()
        self._build_ui()
        self._pump_log()

        pictures = os.path.join(os.path.expanduser("~"), "Pictures", "iAutoTransfer")
        if not os.path.isdir(pictures):
            try:
                os.makedirs(pictures, exist_ok=True)
            except Exception:
                pictures = os.path.join(os.path.expanduser("~"), "Desktop", "iAutoTransfer")
                os.makedirs(pictures, exist_ok=True)
        self.dest_var.set(pictures)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---- Apple drivers sanity check: if false => grey out and overlay
        try:
            ok = bool(sanity_check_apple_drivers())
        except Exception:
            ok = False
        if not ok:
            self._set_interactive_enabled(False)
            self._show_driver_overlay()

    # ---------- Grey-out / overlay helpers ----------
    def _set_interactive_enabled(self, enabled: bool):
        """Enable/disable most interactive widgets recursively."""
        target_state = "normal" if enabled else "disabled"

        def _walk(widget):
            for child in widget.winfo_children():
                try:
                    if isinstance(child, (ttk.Button, ttk.Entry, ttk.Checkbutton, ttk.Combobox, ttk.Spinbox)):
                        child.configure(state=target_state)
                except Exception:
                    pass
                _walk(child)
        _walk(self)

        # keep the log readable
        try:
            self.log.configure(state="normal")
        except Exception:
            pass

    def _show_driver_overlay(self):
        try:
            self._driver_overlay.destroy()
        except Exception:
            pass
        self._driver_overlay = tk.Frame(self, bg="#0f1317")
        self._driver_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)

        card = tk.Frame(self._driver_overlay, bg="#151b22", bd=0, highlightthickness=1, highlightbackground="#263344")
        card.place(relx=0.5, rely=0.5, anchor="center", width=640, height=260)

        lbl_title = tk.Label(card, text="Apple drivers not detected", bg="#151b22", fg="#e8eef7",
                             font=("Segoe UI Semibold", 16))
        lbl_title.pack(pady=(18, 6))

        msg = ("iAutoTransfer requires Apple Mobile Device Support (or iTunes) to talk to your iPhone.\n"
               "Install the drivers directly, or install iTunes (which includes them).")
        lbl_msg = tk.Label(card, text=msg, bg="#151b22", fg="#9aa7b2", font=("Segoe UI", 10), justify="center")
        lbl_msg.pack(pady=(0, 16))

        btn_row = tk.Frame(card, bg="#151b22")
        btn_row.pack()

        ttk.Button(btn_row, text="Install Drivers", style="Accent.TButton",
                   command=self._on_install_drivers).grid(row=0, column=0, padx=8, pady=6)
        ttk.Button(btn_row, text="Download iTunes",
                   command=self._on_download_itunes).grid(row=0, column=1, padx=8, pady=6)
        ttk.Button(btn_row, text="Exit",
                   command=self._on_exit_app).grid(row=0, column=2, padx=8, pady=6)

    def _hide_driver_overlay(self):
        try:
            self._driver_overlay.destroy()
            self._driver_overlay = None
        except Exception:
            pass
        self._set_interactive_enabled(True)

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

        # Dark Treeview
        style.configure("Treeview",
                        background="#0e141a",
                        fieldbackground="#0e141a",
                        foreground="#d7dee7",
                        rowheight=22,
                        bordercolor="#1f2833",
                        lightcolor="#1f2833",
                        darkcolor="#1f2833")
        style.configure("Treeview.Heading",
                        background="#1a2230",
                        foreground="#e8eef7",
                        relief="flat",
                        font=("Segoe UI Semibold", 10))
        style.map("Treeview",
                  background=[("selected", "#223349")],
                  foreground=[("selected", "#ffffff")])

    # ---------- UI Layout ----------
    def _build_ui(self):
        # Header
        header = ttk.Frame(self, style="Header.TFrame")
        header.pack(side="top", fill="x")
        self.title_lbl = ttk.Label(header, text="iAutoTransfer (AFC)", style="Title.TLabel")
        self.title_lbl.pack(side="left", padx=16, pady=8)
        self.device_lbl = ttk.Label(header, text="Device: —", style="Header.TLabel")
        self.device_lbl.pack(side="left", padx=(24, 8), pady=8)

        storage_frame = ttk.Frame(header, style="Header.TFrame")
        storage_frame.pack(side="right", padx=16, pady=8)
        self.storage_var = tk.StringVar(value="Storage: —")
        self.storage_lbl = ttk.Label(storage_frame, textvariable=self.storage_var, style="Header.TLabel")
        self.storage_lbl.pack(side="left", padx=(0, 8))
        self.storage_bar = ttk.Progressbar(storage_frame, orient="horizontal", mode="determinate",
                                           length=240, maximum=100.0)
        self.storage_bar.pack(side="left")

        # Controls Card
        controls = ttk.Frame(self, style="Card.TFrame")
        controls.pack(side="top", fill="x", padx=16, pady=(16, 10))

        ttk.Label(controls, text="Destination Folder:", style="TLabel").grid(row=0, column=0, sticky="w", padx=10, pady=10)
        self.dest_var = tk.StringVar()
        self.dest_entry = ttk.Entry(controls, textvariable=self.dest_var, width=72)
        self.dest_entry.grid(row=0, column=1, sticky="we", padx=(0, 8), pady=10)
        self.browse_btn = ttk.Button(controls, text="Browse…", command=self._on_browse, style="TButton")
        self.browse_btn.grid(row=0, column=2, sticky="w", padx=(0, 8), pady=10)
        bind_tooltip(self.browse_btn, "Choose where files will be saved on your computer.")

        self.scan_btn = ttk.Button(controls, text="Scan (AFC)", command=self._on_scan, style="Accent.TButton")
        self.scan_btn.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="w")
        bind_tooltip(self.scan_btn, "Scans your iPhone /DCIM via AFC and lists media with the filters below.")

        self.transfer_btn = ttk.Button(controls, text="Transfer", command=self._on_transfer, style="Accent.TButton")
        self.transfer_btn.grid(row=1, column=1, padx=(0, 8), pady=(0, 10), sticky="w")
        bind_tooltip(self.transfer_btn, "Start copying to the destination folder using worker threads.")

        self.pause_btn = ttk.Button(controls, text="Pause", command=self._on_pause, style="TButton")
        self.pause_btn.grid(row=1, column=3, padx=(0, 8), pady=(0, 10), sticky="w")
        bind_tooltip(self.pause_btn, "Pause/resume active workers without cancelling the run.")

        self.stop_btn = ttk.Button(controls, text="Stop", command=self._on_stop, style="Danger.TButton")
        self.stop_btn.grid(row=1, column=4, padx=(0, 8), pady=(0, 10), sticky="w")
        bind_tooltip(self.stop_btn, "Request a clean stop. Workers finish their current file, then exit.")

        # Define these BEFORE creating the checkboxes
        self.flatten_var = tk.BooleanVar(value=False)
        self.heic_var = tk.BooleanVar(value=False)
        self.del_heic_var = tk.BooleanVar(value=False)

        # Checkboxes
        self.flatten_cb = ttk.Checkbutton(controls, text="Flatten output", variable=self.flatten_var)
        self.flatten_cb.grid(row=1, column=5, padx=(8, 0), pady=(0, 10), sticky="w")
        bind_tooltip(self.flatten_cb, "Save all files directly in the destination folder (no DCIM subfolders).")

        self.heic_cb = ttk.Checkbutton(controls, text="Convert HEIC → JPEG", variable=self.heic_var)
        self.heic_cb.grid(row=1, column=6, padx=(8, 0), pady=(0, 10), sticky="w")
        bind_tooltip(self.heic_cb, "After copying, convert .HEIC photos to .JPG (requires pillow-heif).")

        self.del_heic_cb = ttk.Checkbutton(controls, text="Delete HEIC after convert", variable=self.del_heic_var)
        self.del_heic_cb.grid(row=1, column=7, padx=(8, 0), pady=(0, 10), sticky="w")
        bind_tooltip(self.del_heic_cb, "Delete the original .HEIC after a successful conversion to JPEG. Use with care.")

        controls.columnconfigure(1, weight=1)

        # Filters row
        filters = ttk.Frame(self, style="Card.TFrame")
        filters.pack(side="top", fill="x", padx=16, pady=(0, 10))
        ttk.Label(filters, text="Type:", style="TLabel").pack(side="left", padx=(12, 4))
        self.type_var = tk.StringVar(value="all")
        self.type_cb = ttk.Combobox(filters, textvariable=self.type_var, values=["all", "photos", "videos"], width=10, state="readonly")
        self.type_cb.pack(side="left")
        bind_tooltip(self.type_cb, "Media kind filter. ‘photos’ = JPG/HEIC/PNG/DNG. ‘videos’ = MOV/MP4/HEVC/M4V/AVI.")

        ttk.Label(filters, text="Year:", style="TLabel").pack(side="left", padx=(16, 4))
        self.year_var = tk.StringVar(value="All")
        years = ["All"] + [str(y) for y in range(2015, 2036)]
        self.year_cb = ttk.Combobox(filters, textvariable=self.year_var, values=years, width=8, state="readonly")
        self.year_cb.pack(side="left")
        bind_tooltip(self.year_cb, "Year filter from filename heuristics (e.g., IMG_YYYYMMDD). Choose ‘All’ for no filter.")

        ttk.Label(filters, text="Month:", style="TLabel").pack(side="left", padx=(16, 4))
        self.month_var = tk.StringVar(value="All")
        months = ["All"] + [f"{m:02d}" for m in range(1, 13)]
        self.month_cb = ttk.Combobox(filters, textvariable=self.month_var, values=months, width=6, state="readonly")
        self.month_cb.pack(side="left")
        bind_tooltip(self.month_cb, "Month filter (01–12) from filename heuristics. Choose ‘All’ for no filter.")

        ttk.Separator(filters, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(filters, text="Workers:", style="TLabel").pack(side="left", padx=(10, 4))

        self.workers_var = tk.IntVar(value=3)
        self.workers_spin = ttk.Spinbox(filters, from_=1, to=8, textvariable=self.workers_var, width=4, command=self._on_workers_changed)
        self.workers_spin.pack(side="left")
        bind_tooltip(self.workers_spin, "Parallel copy threads. Increase for speed; reduce if AFC errors appear. Can change during a transfer.")

        ttk.Separator(filters, orient="vertical").pack(side="left", fill="y", padx=10)
        self.manifest_var = tk.BooleanVar(value=True)
        self.manifest_cb = ttk.Checkbutton(filters, text="Write Manifest CSV", variable=self.manifest_var)
        self.manifest_cb.pack(side="left", padx=(4, 0))
        bind_tooltip(self.manifest_cb, "Write a CSV log (manifest_*.csv) with remote path, local path, size, and timestamp.")

        # Stats & Progress
        stats_card = ttk.Frame(self, style="Card.TFrame")
        stats_card.pack(side="top", fill="x", padx=16, pady=(0, 10))
        self.stats_lbl = ttk.Label(stats_card, text="Idle — 0 files", style="TLabel")
        self.stats_lbl.pack(side="left", padx=12, pady=12)
        self.rate_lbl = ttk.Label(stats_card, text="0.00 files/s | 0.00 MB/s | ETA —", style="Dim.TLabel")
        self.rate_lbl.pack(side="right", padx=12, pady=12)

        prog_card = ttk.Frame(self, style="Card.TFrame")
        prog_card.pack(side="top", fill="x", padx=16, pady=(0, 10))
        self.progress = GradientProgress(prog_card, height=28, bg="#151b22")
        self.progress.pack(side="top", fill="x", padx=12, pady=12)

        # Worker Telemetry
        tele_card = ttk.Frame(self, style="Card.TFrame")
        tele_card.pack(side="top", fill="x", padx=16, pady=(0, 10))
        ttk.Label(tele_card, text="Workers", style="Header.TLabel").pack(anchor="w", padx=12, pady=(10, 4))

        cols = ("id", "status", "files", "mbps", "last")
        self.workers_tv = ttk.Treeview(tele_card, columns=cols, show="headings", height=4)
        for c, w, anc, stretch in (
            ("id", 60, "center", False),
            ("status", 120, "w", False),
            ("files", 80, "e", False),
            ("mbps", 90, "e", False),
            ("last", 820, "w", True),
        ):
            self.workers_tv.heading(c, text=c.upper(), anchor=anc)
            self.workers_tv.column(c, width=w, anchor=anc, stretch=stretch)
        self.workers_tv.pack(fill="x", padx=12, pady=(0, 10))
        bind_tooltip(self.workers_tv,
                     "Per-worker telemetry:\n"
                     "ID — worker number\n"
                     "STATUS — idle/copying/ok/failed/skipped\n"
                     "FILES — attempts processed\n"
                     "MBPS — last measured throughput (sticks)\n"
                     "LAST — last file handled")

        # Failed Copies + Retry
        fail_card = ttk.Frame(self, style="Card.TFrame")
        fail_card.pack(side="top", fill="x", padx=16, pady=(0, 10))
        hdr = ttk.Frame(fail_card, style="Card.TFrame")
        hdr.pack(side="top", fill="x")
        ttk.Label(hdr, text="Failed Copies", style="Header.TLabel").pack(side="left", padx=12, pady=(8, 4))
        self.retry_btn = ttk.Button(hdr, text="Retry Failed", command=self._on_retry_failed)
        self.retry_btn.pack(side="right", padx=12, pady=(8, 4))
        self.failed_lb = tk.Listbox(fail_card, height=4, bg="#131621", fg="#ff6b6b")
        self.failed_lb.pack(fill="x", padx=12, pady=(0, 10))

        # Log
        log_card = ttk.Frame(self, style="Card.TFrame")
        log_card.pack(side="top", fill="both", expand=True, padx=16, pady=(0, 16))
        self.log = tk.Text(log_card, height=12, bg="#0e141a", fg="#d7dee7",
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
        bat = di.get("BatteryPercent")
        chg = di.get("BatteryCharging")
        battxt = f" • {bat}%{' (charging)' if chg else ''}" if bat is not None else ""
        self.device_lbl.config(text=f"{name} • iOS {ios}{battxt} • {udid}")

    def _update_storage_bar(self):
        di = getattr(self, "last_device_info", {}) or {}
        storage = di.get("storage") or {}
        used = float(storage.get("used") or di.get("StorageUsedBytes") or 0.0)
        total = float(storage.get("total") or di.get("StorageTotalBytes") or 0.0)
        if total > 0:
            pct = max(0.0, min(100.0, (used / total) * 100.0))
            self.storage_bar["value"] = pct
            self.storage_var.set(f"Storage: {used/1e9:.1f} GB / {total/1e9:.1f} GB ({pct:.0f}%)")
        else:
            self.storage_bar["value"] = 0
            self.storage_var.set("Storage: —")

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

    # ---------- Scan / Transfer ----------
    def _scan_stats_cb(self, totals, bytes_total):
        self.bytes_total = bytes_total
        self._set_status(f"Scanned {totals['total']} files • {totals['photos']} photos, {totals['videos']} videos • {bytes_total/1024/1024:.1f} MB total")

    def _xfer_stats_cb(self, copied, total, fps, bytes_done=0, bytes_total=0, bps=0):
        eta = self._calc_eta(bytes_done, bytes_total, bps)
        self.rate_lbl.config(text=f"{fps:.2f} files/s | {bps/1024/1024:.2f} MB/s | ETA {eta}")
        self._set_status(f"Copying {copied}/{total} files")

    def _on_browse(self):
        path = filedialog.askdirectory(title="Select destination folder")
        if path:
            self.dest_var.set(path)

    def _disable_buttons(self):
        self.scan_btn.config(state="disabled")
        self.transfer_btn.config(state="disabled")

    def _enable_buttons(self):
        self.scan_btn.config(state="normal")
        self.transfer_btn.config(state="normal")

    def _on_scan(self):
        # If UI is greyed due to drivers missing, do nothing
        if hasattr(self, "_driver_overlay"):
            return
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
                kind = (self.type_var.get() or "all").lower()
                year = None if self.year_var.get() == "All" else int(self.year_var.get())
                month = None if self.month_var.get() == "All" else int(self.month_var.get())
                pred = make_filter(year, month, kind)

                di, items, totals = scan_media_afc(
                    progress_callback=self._on_progress,
                    log_callback=lambda m: self._append_log(m, "info"),
                    stats_callback=self._scan_stats_cb,
                    filter_pred=pred
                )
                self.last_scan_items = items or []
                self.last_device_info = di or {}
                self._update_header_device()
                self._update_storage_bar()
                self._append_log(f"Scan complete: {totals.get('total', 0)} files", "good")
                self._append_log("==== SCAN END ====", "dim")
            except Exception as e:
                self._append_log(f"Scan error: {e}", "err")
            finally:
                self._enable_buttons()

        self._scan_thread = threading.Thread(target=worker, daemon=True)
        self._scan_thread.start()

    def _on_transfer(self):
        # If UI is greyed due to drivers missing, do nothing
        if hasattr(self, "_driver_overlay"):
            messagebox.showerror(
                "Apple Drivers Required",
                "Transfers require Apple Mobile Device Support. Please install drivers and relaunch.",
                parent=self
            )
            return

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

        # reset per-worker sticky state
        self._w_last_mbps.clear()
        self._w_files.clear()
        for iid in self.workers_tv.get_children():
            self.workers_tv.delete(iid)

        self._disable_buttons()
        self.pause_btn.config(state="normal", text="Pause")
        self.stop_btn.config(state="normal")
        self.progress.set(0)
        self.progress.start_glow()
        self.rate_lbl.config(text="0.00 files/s | 0.00 MB/s | ETA —")
        self._append_log(f"Starting transfer to {dest}", "info")
        total = len(items)
        self._set_status(f"Copying 0/{total} files")
        num_workers = max(1, int(self.workers_var.get() or 3))
        self.failed_lb.delete(0, tk.END)

        manifest_writer = None
        if self.manifest_var.get():
            ts = time.strftime("%Y%m%d_%H%M%S")
            manifest_path = os.path.join(dest, f"manifest_{ts}.csv")
            try:
                self._manifest_fp = open(manifest_path, "w", newline="", encoding="utf-8")
                manifest_writer = csv.writer(self._manifest_fp)
                manifest_writer.writerow(["remote_path", "local_path", "size_bytes", "copied_at", "was_converted_to_jpeg"])
                self._append_log(f"Writing manifest → {manifest_path}", "dim")
            except Exception as e:
                self._append_log(f"Manifest open failed: {e}", "warn")
                self._manifest_fp = None
                manifest_writer = None

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
            num_workers=num_workers,
            flatten=self.flatten_var.get(),
            convert_heic=self.heic_var.get(),
            delete_heic_after_convert=self.del_heic_var.get(),
            worker_callback=self._on_worker_update_from_thread,
            failed_callback=self._on_failed_from_thread,
            manifest_writer=manifest_writer,
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
                try:
                    if self._manifest_fp:
                        self._manifest_fp.close()
                except Exception:
                    pass
                self._manifest_fp = None
                self._xfer_controller = None

        self._xfer_thread = threading.Thread(target=runner, daemon=True)
        self._xfer_thread.start()

    def _on_pause(self):
        if not self._xfer_controller:
            return
        if self.pause_btn.cget("text") == "Pause":
            self._xfer_controller.pause()
            self.pause_btn.config(text="Resume")
            self.progress.stop_glow()
        else:
            self._xfer_controller.resume()
            self.pause_btn.config(text="Pause")
            self.progress.start_glow()

    def _on_stop(self):
        if self._xfer_controller:
            self._xfer_controller.stop()
            self._append_log("Stop requested by user.", "warn")

    # ---------- Live wiring from controller ----------
    def _on_workers_changed(self):
        try:
            new_count = int(self.workers_var.get())
        except Exception:
            return
        if self._xfer_controller and self._xfer_controller.is_running():
            try:
                self._xfer_controller.scale_workers(new_count)
                self._append_log(f"Scaled workers to {new_count}", "info")
            except Exception as e:
                self._append_log(f"Scaling error: {e}", "err")
        else:
            self._append_log(f"Workers set to {new_count} (next run)", "dim")

    def _on_worker_update_from_thread(self, wid, info: dict):
        self.after(0, self._on_worker_update, wid, info)

    def _on_worker_update(self, wid: int, info: dict):
        iid = f"w{wid}"
        if not self.workers_tv.exists(iid):
            self.workers_tv.insert("", "end", iid=iid, values=(wid, "—", 0, "0.00", "—"))
            self._w_last_mbps.setdefault(wid, 0.0)
            self._w_files.setdefault(wid, 0)

        status = info.get("status", "—")

        files = info.get("files")
        if isinstance(files, int):
            self._w_files[wid] = files
        files_disp = self._w_files.get(wid, 0)

        if "mbps" in info and info["mbps"] is not None:
            try:
                mb = float(info["mbps"])
                if mb > 0:
                    self._w_last_mbps[wid] = mb
            except Exception:
                pass
        mbps_disp = self._w_last_mbps.get(wid, 0.0)

        last = info.get("last_file", "—")
        self.workers_tv.item(iid, values=(wid, status, files_disp, f"{mbps_disp:.2f}", last))

    def _on_failed_from_thread(self, remote_path: str, err: str):
        def add():
            self.failed_lb.insert(tk.END, f"{os.path.basename(remote_path)} — {err}")
        self.after(0, add)

    def _on_retry_failed(self):
        if not self._xfer_controller or not hasattr(self._xfer_controller, "retry_failed"):
            self._append_log("Retry requires an active/new transfer.", "warn")
            return
        try:
            self._xfer_controller.retry_failed()
            self._append_log("Queued failed items for retry.", "info")
        except Exception as e:
            self._append_log(f"Retry error: {e}", "err")

    # ---------- Driver overlay actions & install helpers ----------
    def _open_url(self, url: str):
        try:
            webbrowser.open(url)
        except Exception as e:
            messagebox.showerror("Open URL", f"Could not open browser:\n{e}")

    def _on_download_itunes(self):
        # Prefer Microsoft Store on Windows
        self._open_url(ITUNES_MICROSOFT_STORE_URL or ITUNES_DOWNLOAD_URL)

    def _on_exit_app(self):
        self._on_close()

    def _run_msi(self, msi_path: Path):
        """Run an MSI with msiexec (use /passive for visible progress; /quiet for silent)."""
        try:
            subprocess.check_call(["msiexec", "/i", str(msi_path), "/passive"])
            return True
        except subprocess.CalledProcessError as e:
            messagebox.showerror("Driver Install", f"Installer failed:\n{e}")
        except Exception as e:
            messagebox.showerror("Driver Install", f"Could not start installer:\n{e}")
        return False

    def _try_install_bundled_msis(self):
        """
        If you ship driver MSIs in extras/apple_drivers, install them here.
        Typical files:
          - AppleApplicationSupport.msi / AppleApplicationSupport64.msi
          - AppleMobileDeviceSupport.msi / AppleMobileDeviceSupport64.msi
        """
        base = BUNDLED_DRIVER_DIR
        if not base.exists():
            return False

        candidates = [
            "AppleApplicationSupport64.msi",
            "AppleApplicationSupport.msi",
            "AppleMobileDeviceSupport64.msi",
            "AppleMobileDeviceSupport.msi",
        ]
        found = [base / name for name in candidates if (base / name).exists()]
        if not found:
            return False

        ok_any = False
        for msi in found:
            self._append_log(f"Launching installer: {msi}", "dim")
            if self._run_msi(msi):
                ok_any = True
        return ok_any

    def _find_7z(self):
        """Return a 7z/7za executable path if found on PATH, else None."""
        for exe in ("7z.exe", "7za.exe"):
            p = shutil.which(exe)
            if p:
                return p
        return None

    def _extract_and_install_from_itunes_exe(self, itunes_exe_path: Path):
        """
        Use 7-Zip to extract Apple MSIs out of iTunes*.exe and run them.
        Requires 7z/7za in PATH.
        """
        seven = self._find_7z()
        if not seven:
            messagebox.showinfo(
                "7-Zip required",
                "To extract drivers from the iTunes installer, install 7-Zip and ensure 7z.exe is on PATH.\n"
                "Alternatively, download iTunes from Microsoft Store."
            )
            return False

        self._append_log(f"Extracting from: {itunes_exe_path}", "dim")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            try:
                subprocess.check_call([seven, "x", "-y", str(itunes_exe_path)], cwd=td)
            except subprocess.CalledProcessError as e:
                messagebox.showerror("Extract", f"Failed to extract with 7-Zip:\n{e}")
                return False

            want = {
                "AppleApplicationSupport64.msi",
                "AppleApplicationSupport.msi",
                "AppleMobileDeviceSupport64.msi",
                "AppleMobileDeviceSupport.msi",
            }
            msis = []
            for root, _, files in os.walk(td):
                for f in files:
                    if f in want:
                        msis.append(Path(root) / f)

            if not msis:
                messagebox.showwarning("Extract", "No driver MSIs found inside the iTunes installer.")
                return False

            ok_any = False
            for msi in msis:
                self._append_log(f"Installing extracted MSI: {msi}", "dim")
                if self._run_msi(msi):
                    ok_any = True
            return ok_any

    def _on_install_drivers(self):
        """
        Try in order:
         1) Install bundled MSIs (if shipped in extras/apple_drivers)
         2) Ask user to pick an iTunes installer EXE and extract drivers with 7-Zip
        After install, re-check sanity and re-enable UI if OK.
        """
        # 1) Bundled route
        if self._try_install_bundled_msis():
            self._append_log("Installers completed. Rechecking driver sanity…", "info")
            self._post_install_recheck()
            return

        # 2) Let user select an iTunes EXE
        itunes_path = filedialog.askopenfilename(
            title="Select iTunes installer (EXE)",
            filetypes=[("iTunes Installer", "*.exe"), ("All files", "*.*")]
        )
        if not itunes_path:
            return
        itunes_path = Path(itunes_path)

        if self._extract_and_install_from_itunes_exe(itunes_path):
            self._append_log("Installers completed. Rechecking driver sanity…", "info")
            self._post_install_recheck()

    def _post_install_recheck(self):
        try:
            ok = bool(sanity_check_apple_drivers())
        except Exception:
            ok = False

        if ok:
            self._append_log("Apple drivers detected. Enabling UI.", "good")
            self._hide_driver_overlay()
        else:
            messagebox.showwarning(
                "Drivers not detected",
                "Still not seeing the Apple drivers. You may need to reboot, or install from the Microsoft Store."
            )

    # ---------- Lifecycle ----------
    def _on_close(self):
        self._closing = True
        try:
            if self._xfer_controller and self._xfer_controller.is_running():
                self._xfer_controller.stop()
        except Exception:
            pass
        self.progress.stop_glow()
        try:
            if self._manifest_fp:
                self._manifest_fp.close()
        except Exception:
            pass
        self.destroy()

    def run(self):
        self.mainloop()


if __name__ == "__main__":
    app = AppWindow()
    app.run()
