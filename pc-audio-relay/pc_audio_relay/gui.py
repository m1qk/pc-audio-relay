"""Tk window: device, Start/Stop, status, QR code and address."""
from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Optional

from .app import RelayApp, StartError
from .audio import ProbeResult
from .config import APP_NAME, VERSION, Settings
from .diagnostics import fix_firewall_elevated, run_diagnostics
from .netinfo import list_lan_addresses
from .qr import qr_matrix
from .status import compute_status

FOLLOW_DEFAULT = "Follow Windows default output"
COLORS = {"idle": "#8b94a3", "waiting": "#3b82f6", "connecting": "#f59e0b", "ok": "#22c55e",
          "warn": "#f59e0b", "error": "#ef4444"}


class MainWindow:
    def __init__(self, root: tk.Tk, settings: Settings, backend=None, tone: Optional[str] = None):
        self.root = root
        self.settings = settings
        self.backend = backend
        self.tone = tone
        self.app: Optional[RelayApp] = None
        self.started_at = 0.0
        self.addresses = list_lan_addresses()
        self._devices: list[str] = []
        self._default_name: Optional[str] = None
        self._qr_text: Optional[str] = None
        # Tk is not thread-safe: worker threads hand work to the main thread through this queue.
        self._ui_calls: "queue.Queue[Callable[[], None]]" = queue.Queue()

        root.title(f"{APP_NAME}")
        root.minsize(560, 640)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self.refresh_devices()
        self._refresh_address_choices()
        self._tick()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        pad = {"padx": 14}
        f = ttk.Frame(self.root, padding=(0, 12))
        f.pack(fill="both", expand=True)
        f.columnconfigure(0, weight=1)

        # device
        ttk.Label(f, text="Output device to share", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w", **pad)
        row = ttk.Frame(f)
        row.grid(row=1, column=0, sticky="ew", padx=14, pady=(2, 10))
        row.columnconfigure(0, weight=1)
        self.device_var = tk.StringVar(value=FOLLOW_DEFAULT)
        self.device_box = ttk.Combobox(row, textvariable=self.device_var, state="readonly")
        self.device_box.grid(row=0, column=0, sticky="ew")
        self.device_box.bind("<<ComboboxSelected>>", self.on_device_selected)
        ttk.Button(row, text="Refresh", command=self.refresh_devices).grid(row=0, column=1, padx=(8, 0))
        self.device_note = ttk.Label(f, text="", foreground="#8b94a3")
        self.device_note.grid(row=2, column=0, sticky="w", padx=14)

        # start/stop + status
        self.toggle_btn = ttk.Button(f, text="Start", command=self.toggle)
        self.toggle_btn.grid(row=3, column=0, sticky="ew", padx=14, pady=(12, 8), ipady=8)
        st = ttk.Frame(f)
        st.grid(row=4, column=0, sticky="ew", padx=14)
        self.dot = tk.Canvas(st, width=16, height=16, highlightthickness=0)
        self.dot.grid(row=0, column=0, padx=(0, 8))
        self.dot_item = self.dot.create_oval(2, 2, 14, 14, fill=COLORS["idle"], outline="")
        self.status_var = tk.StringVar(value="Stopped")
        ttk.Label(st, textvariable=self.status_var, font=("Segoe UI", 11, "bold")).grid(row=0, column=1, sticky="w")
        self.detail_var = tk.StringVar(value="")
        self.detail_lbl = ttk.Label(f, textvariable=self.detail_var, wraplength=500, foreground="#6b7280")
        self.detail_lbl.grid(row=5, column=0, sticky="w", padx=14, pady=(2, 8))

        # QR + address
        box = ttk.LabelFrame(f, text="Connect your iPhone", padding=10)
        box.grid(row=6, column=0, sticky="ew", padx=14, pady=6)
        box.columnconfigure(1, weight=1)
        self.qr = tk.Canvas(box, width=190, height=190, bg="white", highlightthickness=1, highlightbackground="#d1d5db")
        self.qr.grid(row=0, column=0, rowspan=4, padx=(0, 12))
        ttk.Label(box, text="Scan with the iPhone Camera app,\nor type this address in Safari:", justify="left").grid(row=0, column=1, sticky="w")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(box, textvariable=self.url_var, state="readonly")
        self.url_entry.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(box, text="Copy address", command=self.copy_url).grid(row=2, column=1, sticky="w")
        self.addr_var = tk.StringVar()
        self.addr_box = ttk.Combobox(box, textvariable=self.addr_var, state="readonly")
        self.addr_box.grid(row=3, column=1, sticky="ew", pady=(8, 0))
        self.addr_box.bind("<<ComboboxSelected>>", self.on_address_selected)

        # live numbers
        meter = ttk.Frame(f)
        meter.grid(row=7, column=0, sticky="ew", padx=14, pady=(8, 0))
        meter.columnconfigure(1, weight=1)
        ttk.Label(meter, text="Level").grid(row=0, column=0, padx=(0, 8))
        self.level = ttk.Progressbar(meter, maximum=100)
        self.level.grid(row=0, column=1, sticky="ew")
        self.stats_var = tk.StringVar(value="")
        ttk.Label(meter, textvariable=self.stats_var, foreground="#6b7280").grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # settings
        adv = ttk.LabelFrame(f, text="Settings (applied on Start)", padding=8)
        adv.grid(row=8, column=0, sticky="ew", padx=14, pady=10)
        self.buf_var = tk.IntVar(value=self.settings.capture_buffer_ms)
        self.br_var = tk.IntVar(value=self.settings.bitrate_kbps)
        self.port_var = tk.IntVar(value=self.settings.port)
        self.key_var = tk.BooleanVar(value=self.settings.require_key)
        self._adv_widgets = []
        for i, (label, var, lo, hi, step) in enumerate([
                ("Buffer (ms)", self.buf_var, 10, 300, 10),
                ("Bitrate (kbps)", self.br_var, 32, 320, 16),
                ("Port", self.port_var, 1024, 65535, 1)]):
            ttk.Label(adv, text=label).grid(row=0, column=i * 2, padx=(0 if i == 0 else 12, 4))
            sb = ttk.Spinbox(adv, from_=lo, to=hi, increment=step, textvariable=var, width=7)
            sb.grid(row=0, column=i * 2 + 1)
            self._adv_widgets.append(sb)
        kc = ttk.Checkbutton(adv, text="Require pairing key (recommended)", variable=self.key_var)
        kc.grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))
        self._adv_widgets.append(kc)

        # tools + log
        tools = ttk.Frame(f)
        tools.grid(row=9, column=0, sticky="ew", padx=14)
        ttk.Button(tools, text="Diagnose", command=self.diagnose).pack(side="left")
        if sys.platform == "win32":
            ttk.Button(tools, text="Fix firewall", command=self.fix_firewall).pack(side="left", padx=8)
        ttk.Button(tools, text="Copy log", command=self.copy_log).pack(side="left")
        self.log = tk.Text(f, height=8, wrap="word", state="disabled", font=("Consolas", 9) if sys.platform == "win32" else None)
        self.log.grid(row=10, column=0, sticky="nsew", padx=14, pady=(8, 0))
        f.rowconfigure(10, weight=1)

    # ------------------------------------------------------------------ #
    def log_line(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def copy_url(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url_var.get())
        self.log_line("address copied")

    def copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end"))

    # ------------------------------ devices ----------------------------- #
    def refresh_devices(self) -> None:
        self.device_note.configure(text="Looking for playback devices…")

        def work():
            try:
                if self.backend is None:
                    from .wasapi import WasapiBackend
                    backend = WasapiBackend()
                else:
                    backend = self.backend
                res: ProbeResult = backend.probe()
                self._ui_calls.put(lambda: self._devices_loaded(res, None))
            except Exception as e:
                self._ui_calls.put(lambda: self._devices_loaded(None, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _devices_loaded(self, res: Optional[ProbeResult], error: Optional[str]) -> None:
        if res is None:
            self.device_note.configure(text="Could not list devices." + (" (Windows only.)" if sys.platform != "win32" else ""))
            self.log_line(f"device list failed: {error}")
            names: list[str] = []
        else:
            names = [d.name for d in res.devices]
            self._default_name = res.default_name
            self.device_note.configure(text=f"Windows default right now: {res.default_name or 'none'}")
        self._devices = names
        values = [FOLLOW_DEFAULT] + names
        saved = self.settings.device
        if saved and saved not in names:
            values.append(saved)  # keep a saved-but-unplugged device selectable
        self.device_box.configure(values=values)
        self.device_var.set(saved if saved else FOLLOW_DEFAULT)

    def on_device_selected(self, _evt=None) -> None:
        choice = self.device_var.get()
        dev = None if choice == FOLLOW_DEFAULT else choice
        self.settings.device = dev
        self.settings.save()
        if self.app and self.app.running:
            self.app.set_device(dev)
            self.log_line(f"output device: {choice}")

    # ------------------------------ address / QR ------------------------ #
    def _refresh_address_choices(self) -> None:
        self.addresses = list_lan_addresses()
        labels = [a.label for a in self.addresses]
        self.addr_box.configure(values=labels)
        if not labels:
            self.addr_var.set("No network connection found")
            return
        chosen = next((a for a in self.addresses if a.ip == self.settings.address), self.addresses[0])
        self.addr_var.set(chosen.label)
        self._update_qr()

    def on_address_selected(self, _evt=None) -> None:
        idx = self.addr_box.current()
        if 0 <= idx < len(self.addresses):
            self.settings.address = self.addresses[idx].ip
            self.settings.save()
            if self.app and self.app.running:
                messagebox.showinfo(APP_NAME, "Stop and Start again to use the new address.")
            self._update_qr()

    def _current_url(self) -> Optional[str]:
        from .netinfo import build_url
        idx = self.addr_box.current()
        a = self.addresses[idx] if 0 <= idx < len(self.addresses) else (self.addresses[0] if self.addresses else None)
        if a is None:
            return None
        return build_url(a.ip, int(self.port_var.get() or self.settings.port), self.settings.key if self.key_var.get() else None)

    def _update_qr(self) -> None:
        url = self._current_url()
        self.url_var.set(url or "")
        if url == self._qr_text:
            return
        self._qr_text = url
        self.qr.delete("all")
        if not url:
            return
        m = qr_matrix(url)
        n = len(m)
        quiet = 2
        cell = max(2, 190 // (n + 2 * quiet))
        size = cell * (n + 2 * quiet)
        off = (190 - size) // 2
        for y, row in enumerate(m):
            for x, v in enumerate(row):
                if v:
                    x0 = off + (x + quiet) * cell
                    y0 = off + (y + quiet) * cell
                    self.qr.create_rectangle(x0, y0, x0 + cell, y0 + cell, fill="black", outline="")

    # ------------------------------ start / stop ------------------------ #
    def _collect_settings(self) -> None:
        for var, attr in ((self.buf_var, "capture_buffer_ms"), (self.br_var, "bitrate_kbps"), (self.port_var, "port")):
            try:
                setattr(self.settings, attr, int(var.get()))
            except (tk.TclError, ValueError):
                pass
        self.settings.require_key = bool(self.key_var.get())
        idx = self.addr_box.current()
        if 0 <= idx < len(self.addresses):
            self.settings.address = self.addresses[idx].ip
        self.settings.clamp()
        self.buf_var.set(self.settings.capture_buffer_ms)
        self.br_var.set(self.settings.bitrate_kbps)
        self.port_var.set(self.settings.port)

    def toggle(self) -> None:
        if self.app and self.app.running:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        self._collect_settings()
        self.settings.save()
        self.app = RelayApp(self.settings, tone_mode=self.tone, backend=self.backend)
        try:
            self.app.start()
        except StartError as e:
            self.app = None
            messagebox.showerror(APP_NAME, str(e))
            self.log_line(f"could not start: {e}")
            return
        self.started_at = time.time()
        self.toggle_btn.configure(text="Stop")
        for w in self._adv_widgets + [self.addr_box]:
            w.configure(state="disabled")
        self._update_qr()
        self.log_line(f"started on {self.app.url()}")
        self._refresh_status()

    def stop(self) -> None:
        if self.app:
            self.toggle_btn.configure(state="disabled")
            self.root.update_idletasks()
            self.app.stop()
            self._drain_events()
        self.toggle_btn.configure(text="Start", state="normal")
        for w in self._adv_widgets:
            w.configure(state="normal")
        self.addr_box.configure(state="readonly")
        self.log_line("stopped")
        self._refresh_status()

    # ------------------------------ tools ------------------------------- #
    def diagnose(self) -> None:
        self._collect_settings()
        idx = self.addr_box.current()
        addr = self.addresses[idx] if 0 <= idx < len(self.addresses) else None
        self.log_line("--- diagnostics ---")

        def work():
            checks = run_diagnostics(addr, self.settings.port, bool(self.app and self.app.running))
            self._ui_calls.put(lambda: [self.log_line(str(c)) for c in checks])

        threading.Thread(target=work, daemon=True).start()

    def fix_firewall(self) -> None:
        ok, msg = fix_firewall_elevated()
        self.log_line(msg)
        if not ok:
            messagebox.showwarning(APP_NAME, msg)

    # ------------------------------ periodic ---------------------------- #
    def _drain_events(self) -> None:
        if not self.app:
            return
        try:
            while True:
                kind, msg = self.app.events.get_nowait()
                if kind != "state":
                    self.log_line(f"{kind}: {msg}")
        except Exception:
            pass

    def _run_ui_calls(self) -> None:
        try:
            while True:
                self._ui_calls.get_nowait()()
        except queue.Empty:
            pass

    def _tick(self) -> None:
        self._run_ui_calls()
        self._refresh_status()
        self._drain_events()
        self.root.after(200, self._tick)

    def _refresh_status(self) -> None:
        running = bool(self.app and self.app.running)
        snap = self.app.snapshot() if running else {}
        st = compute_status(running, snap, time.time() - self.started_at if running else 0)
        self.status_var.set(st.text)
        self.detail_var.set(st.detail)
        self.dot.itemconfigure(self.dot_item, fill=COLORS.get(st.level, "#8b94a3"))
        if running:
            cap = snap.get("capture", {})
            self.level.configure(value=min(100, float(cap.get("level", 0)) * 140))
            self.stats_var.set(f"Queued {cap.get('queuedMs', 0):.0f} ms   Dropped {cap.get('droppedMs', 0)} ms")
        else:
            self.level.configure(value=0)
            self.stats_var.set("")

    def on_close(self) -> None:
        try:
            self._collect_settings()
            self.settings.save()
            if self.app and self.app.running:
                self.app.stop()
        finally:
            self.root.destroy()


def _dpi_aware() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def run_gui(settings: Settings, tone: Optional[str] = None) -> int:
    _dpi_aware()
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    MainWindow(root, settings, tone=tone)
    root.mainloop()
    return 0
