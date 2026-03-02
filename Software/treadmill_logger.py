"""
Treadmill Logger
----------------
Reads serial data from up to 6 treadmill devices and writes CSV logs.
Supports combined or per-device logs, ID read/set, and live UI status.
"""

import csv
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Optional dependency: pyserial. If missing, the UI will still load,
# but serial features will show an error until installed.
try:
    import serial
    from serial.tools import list_ports
except Exception as exc:  # pragma: no cover
    serial = None
    list_ports = None


# ===== Configuration =====
DEFAULT_BAUD = 192000
MAX_DEVICES = 6
RECONNECT_DELAY_SEC = 2.0
READ_TIMEOUT_SEC = 0.5
ROW_LIMIT = 524_288
# If no data arrives within this window, show RX as idle.
RX_TIMEOUT_SEC = 1.0


@dataclass
class DeviceConfig:
    # One device row from the GUI.
    enabled: bool
    port: str
    device_id: str
    device_label: str


@dataclass
class LogRecord:
    # One parsed serial line with timestamps added.
    timestamp_iso: str
    elapsed_s: float
    device_id: str
    device_label: str
    port: str
    distance_mm: Optional[float]
    speed_mms: Optional[float]
    device_micros: Optional[int]


# Background thread that reads one treadmill's serial stream.
# Background thread that reads one treadmill's serial stream.
class SerialWorker(threading.Thread):
    def __init__(self, cfg: DeviceConfig, record_queue_ref, status_cb, ui_queue: queue.Queue, start_mono_ref):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.record_queue_ref = record_queue_ref
        self.status_cb = status_cb
        self.ui_queue = ui_queue
        self.start_mono_ref = start_mono_ref
        self._stop_event = threading.Event()

    def stop(self):
        # Tell the thread to exit its loop.
        self._stop_event.set()

    def stop_and_wait(self, timeout: float = 1.0):
        # Request stop and wait briefly for thread to exit.
        self._stop_event.set()
        self.join(timeout=timeout)

    def run(self):
        # Keep trying to connect and read until stopped.
        while not self._stop_event.is_set():
            if not self.cfg.enabled or not self.cfg.port:
                # Disabled or no port selected.
                self.status_cb(self.cfg.port, "disabled")
                time.sleep(0.5)
                continue

            if serial is None:
                # pyserial isn't installed.
                self.status_cb(self.cfg.port, "pyserial missing")
                time.sleep(1.0)
                continue

            try:
                self.status_cb(self.cfg.port, "connecting")
                # Open the serial port and read line-by-line.
                with serial.Serial(
                    self.cfg.port,
                    DEFAULT_BAUD,
                    timeout=READ_TIMEOUT_SEC,
                    write_timeout=READ_TIMEOUT_SEC,
                ) as ser:
                    self.status_cb(self.cfg.port, "connected")
                    while not self._stop_event.is_set():
                        # Read one line at a time from the device.
                        line = ser.readline()
                        if not line:
                            continue
                        try:
                            text = line.decode("utf-8", errors="ignore").strip()
                        except Exception:
                            continue
                        if not text:
                            continue
                        rec = self._parse_line(text)
                        if rec:
                            # Send to file writer (if enabled) and UI updater.
                            q = self.record_queue_ref.get("queue")
                            if q is not None:
                                q.put(rec)
                            self.ui_queue.put(rec)
            except Exception:
                # If the port drops, wait and then retry.
                self.status_cb(self.cfg.port, "disconnected")
                time.sleep(RECONNECT_DELAY_SEC)

    def _parse_line(self, text: str) -> Optional[LogRecord]:
        # Accept:
        #   distance,speed
        # or
        #   micros,distance,speed
        parts = [p.strip() for p in text.split(",") if p.strip() != ""]
        device_micros = None
        distance = None
        speed = None

        if len(parts) == 2:
            try:
                distance = float(parts[0])
                speed = float(parts[1])
            except Exception:
                return None
        elif len(parts) >= 3:
            try:
                device_micros = int(float(parts[0]))
                distance = float(parts[1])
                speed = float(parts[2])
            except Exception:
                return None
        else:
            return None

        # Timestamp the record at receipt time.
        timestamp_iso = datetime.now(timezone.utc).isoformat()
        # Elapsed seconds since Start was pressed.
        elapsed_s = 0.0
        t0 = self.start_mono_ref.get("t0")
        if t0 is not None:
            elapsed_s = time.monotonic() - t0
        return LogRecord(
            timestamp_iso=timestamp_iso,
            elapsed_s=elapsed_s,
            device_id=self.cfg.device_id or "",
            device_label=self.cfg.device_label,
            port=self.cfg.port,
            distance_mm=distance,
            speed_mms=speed,
            device_micros=device_micros,
        )


# Background thread that writes incoming records to CSV files.
class LogWriter(threading.Thread):
    def __init__(self, record_queue: queue.Queue, base_path: str, combined: bool, combined_labels: Optional[List[str]] = None):
        super().__init__(daemon=True)
        # Queue of LogRecord entries to write.
        self.record_queue = record_queue
        # Base output path without extension/suffix.
        self.base_path = base_path
        # Combined = one wide CSV, otherwise one CSV per device.
        self.combined = combined
        self._stop_event = threading.Event()
        self._files = {}
        self._writers = {}
        self._combined_header = None
        self._row_count = {}
        self._file_index = {}
        if combined_labels:
            # Build the combined header up-front so the first row has all columns.
            cols = ["timestamp_iso", "elapsed_s"]
            for label in combined_labels:
                cols.extend(
                    [f"{label}_position_mm", f"{label}_speed_mm_s", f"{label}_micros"]
                )
            self._combined_header = cols

    def stop(self):
        # Signal the writer to stop once the queue is drained.
        self._stop_event.set()

    def run(self):
        # Drain the queue and write all records to disk.
        while not self._stop_event.is_set() or not self.record_queue.empty():
            try:
                rec = self.record_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            # Write each record to the appropriate file.
            self._write_record(rec)

        # Close any open files.
        for f in self._files.values():
            try:
                f.flush()
                f.close()
            except Exception:
                pass

    def _path_for(self, key: str, index: int) -> str:
        # Map device key + rotation index to a file path.
        if self.combined:
            if index <= 1:
                return self.base_path + ".csv"
            return f"{self.base_path}_{index}.csv"
        if index <= 1:
            return f"{self.base_path}_{key}.csv"
        return f"{self.base_path}_{key}_{index}.csv"

    def _open_writer(self, key: str, header: List[str]):
        # Open a CSV writer for this key (combined or per-device).
        if key in self._writers:
            return self._writers[key]
        os.makedirs(os.path.dirname(self.base_path), exist_ok=True)
        index = self._file_index.get(key, 1)
        path = self._path_for(key, index)
        # Append if file exists; create if not.
        f = open(path, "a", newline="")
        writer = csv.writer(f)
        if f.tell() == 0:
            # New file: write header and start row count at 1.
            writer.writerow(header)
            self._row_count[key] = 1
        else:
            try:
                # Count rows when appending to an existing file so rotation stays correct.
                with open(path, "r", newline="") as count_f:
                    self._row_count[key] = sum(1 for _ in count_f)
            except Exception:
                self._row_count[key] = 0
        self._files[key] = f
        self._writers[key] = writer
        return writer

    def _rotate_if_needed(self, key: str, header: List[str]):
        # If the current file hit the row limit, close it and open a new one.
        if self._row_count.get(key, 0) < ROW_LIMIT:
            return
        f = self._files.get(key)
        if f:
            try:
                f.flush()
                f.close()
            except Exception:
                pass
        self._files.pop(key, None)
        self._writers.pop(key, None)
        self._file_index[key] = self._file_index.get(key, 1) + 1
        self._row_count[key] = 0
        self._open_writer(key, header)

    def _write_record(self, rec: LogRecord):
        # Write one record to the correct file layout.
        if self.combined:
            # Combined mode: one wide row with per-device columns.
            label = rec.device_label
            col_pos = f"{label}_position_mm"
            col_spd = f"{label}_speed_mm_s"
            col_mic = f"{label}_micros"
            if self._combined_header is None:
                self._combined_header = ["timestamp_iso", "elapsed_s", col_pos, col_spd, col_mic]
            else:
                for col in (col_pos, col_spd, col_mic):
                    if col not in self._combined_header:
                        self._combined_header.append(col)

            # Ensure file is open and below the row limit.
            writer = self._open_writer("combined", self._combined_header)
            self._rotate_if_needed("combined", self._combined_header)
            writer = self._open_writer("combined", self._combined_header)
            row = [""] * len(self._combined_header)
            row[0] = rec.timestamp_iso
            row[1] = f"{rec.elapsed_s:.6f}"
            # Place values into the device-specific columns.
            row[self._combined_header.index(col_pos)] = rec.distance_mm
            row[self._combined_header.index(col_spd)] = rec.speed_mms
            row[self._combined_header.index(col_mic)] = (
                rec.device_micros if rec.device_micros is not None else ""
            )
            writer.writerow(row)
            self._row_count["combined"] = self._row_count.get("combined", 0) + 1
        else:
            # Per-device mode: each device gets its own file.
            key = rec.device_id if rec.device_id else rec.port.replace("/", "_")
            header = ["timestamp_iso", "elapsed_s", "distance_mm", "speed_mm_s", "device_micros"]
            writer = self._open_writer(key, header)
            self._rotate_if_needed(key, header)
            writer = self._open_writer(key, header)
            writer.writerow(
                [
                    rec.timestamp_iso,
                    f"{rec.elapsed_s:.6f}",
                    rec.distance_mm,
                    rec.speed_mms,
                    rec.device_micros if rec.device_micros is not None else "",
                ]
            )
            self._row_count[key] = self._row_count.get(key, 0) + 1


# Main GUI application.
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Treadmill Logger")
        self.geometry("980x520")

        # Queues for cross-thread communication.
        self.record_queue = queue.Queue()
        self.record_queue_ref = {"queue": None}
        self.ui_queue = queue.Queue()
        self.monitor_workers = {}
        self.writer: Optional[LogWriter] = None
        # State flags.
        self.logging = False
        self.start_mono = None
        self.start_mono_ref = {"t0": None}
        self.last_rx = {}
        self.monitor_paused = False
        self.recording_blink = False

        # Build UI and populate port list.
        self._build_ui()
        self._refresh_ports()
        self._ensure_monitor_workers()

    def _build_ui(self):
        # Top bar with file settings and control buttons.
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=10)

        ttk.Label(top, text="Base File Name").grid(row=0, column=0, sticky="w")
        self.base_name_var = tk.StringVar(value="treadmill_log")
        ttk.Entry(top, textvariable=self.base_name_var, width=28).grid(
            row=0, column=1, sticky="w", padx=(8, 16)
        )

        ttk.Label(top, text="Directory").grid(row=0, column=2, sticky="w")
        self.dir_var = tk.StringVar(value=os.getcwd())
        ttk.Entry(top, textvariable=self.dir_var, width=40).grid(
            row=0, column=3, sticky="w", padx=(8, 8)
        )
        ttk.Button(top, text="Browse", command=self._browse_dir).grid(
            row=0, column=4, sticky="w"
        )

        ttk.Label(top, text="Log Mode").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.combined_var = tk.BooleanVar(value=False)
        ttk.Radiobutton(top, text="Combined", variable=self.combined_var, value=True).grid(
            row=1, column=1, sticky="w", pady=(8, 0)
        )
        ttk.Radiobutton(
            top, text="Per Device", variable=self.combined_var, value=False
        ).grid(row=1, column=1, sticky="e", pady=(8, 0))

        ttk.Button(top, text="Rescan Ports", command=self._refresh_ports).grid(
            row=1, column=2, sticky="w", padx=(8, 0), pady=(8, 0)
        )

        ttk.Button(top, text="Auto-Assign ESP32-S2", command=self._auto_assign_esp32s2).grid(
            row=1, column=3, sticky="w", padx=(8, 0), pady=(8, 0)
        )

        self.start_button = ttk.Button(top, text="Start", command=self.start_logging)
        self.start_button.grid(row=1, column=4, sticky="w", pady=(8, 0))
        self.stop_button = ttk.Button(top, text="Stop", command=self.stop_logging, state="disabled")
        self.stop_button.grid(row=1, column=5, sticky="w", pady=(8, 0))

        # Recording indicator (blinks red while logging).
        ttk.Label(top, text="Recording").grid(row=1, column=6, sticky="w", padx=(12, 4), pady=(8, 0))
        self.recording_dot = ttk.Label(top, text="●", font=("Segoe UI", 12, "bold"), foreground="gray40")
        self.recording_dot.grid(row=1, column=7, sticky="w", pady=(8, 0))

        sep = ttk.Separator(self, orient="horizontal")
        sep.pack(fill="x", padx=12, pady=6)

        # Table of devices and their live status.
        self.rows_frame = ttk.Frame(self)
        self.rows_frame.pack(fill="both", expand=True, padx=12, pady=6)

        headers = ["Enable", "Port", "Device ID", "Position (mm)", "Status", "Receiving", ""]
        for col, title in enumerate(headers):
            ttk.Label(self.rows_frame, text=title).grid(row=0, column=col, sticky="w", padx=6)

        self.device_rows = []
        for i in range(MAX_DEVICES):
            enabled = tk.BooleanVar(value=False)
            port = tk.StringVar(value="")
            device_id = tk.StringVar(value="")
            position = tk.StringVar(value="")
            rx = tk.StringVar(value="●")
            status = tk.StringVar(value="idle")

            chk = ttk.Checkbutton(self.rows_frame, variable=enabled)
            chk.grid(row=i + 1, column=0, sticky="w", padx=6, pady=4)

            port_combo = ttk.Combobox(
                self.rows_frame, textvariable=port, values=[], width=16
            )
            port_combo.grid(row=i + 1, column=1, sticky="w", padx=6, pady=4)

            ttk.Entry(self.rows_frame, textvariable=device_id, width=18).grid(
                row=i + 1, column=2, sticky="w", padx=6, pady=4
            )

            ttk.Label(self.rows_frame, textvariable=position).grid(
                row=i + 1, column=3, sticky="w", padx=6, pady=4
            )

            ttk.Label(self.rows_frame, textvariable=status).grid(
                row=i + 1, column=4, sticky="w", padx=6, pady=4
            )

            rx_label = ttk.Label(self.rows_frame, textvariable=rx, font=("Segoe UI", 12, "bold"))
            rx_label.grid(row=i + 1, column=5, sticky="w", padx=6, pady=4)

            id_btn = ttk.Button(
                self.rows_frame, text="Read/Set ID", command=lambda idx=i: self._id_dialog(idx)
            )
            id_btn.grid(row=i + 1, column=6, sticky="w", padx=6, pady=4)

            self.device_rows.append(
                {
                    "enabled": enabled,
                    "port": port,
                    "device_id": device_id,
                    "position": position,
                    "rx": rx,
                    "status": status,
                    "port_combo": port_combo,
                    "rx_label": rx_label,
                    "id_btn": id_btn,
                }
            )

        footer = ttk.Frame(self)
        footer.pack(fill="x", padx=12, pady=6)
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(footer, textvariable=self.status_var).pack(anchor="w")

        if serial is None:
            self.status_var.set("pyserial not installed; run 'pip install pyserial'")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._poll_ui_queue)
        self.after(500, self._update_recording_indicator)

    def _browse_dir(self):
        # Let the user pick a folder for log files.
        path = filedialog.askdirectory()
        if path:
            self.dir_var.set(path)

    def _refresh_ports(self):
        # Query the system for available serial ports.
        ports = []
        if list_ports is not None:
            ports = [p.device for p in list_ports.comports()]
        for row in self.device_rows:
            row["port_combo"].configure(values=ports)
        if not ports:
            self.status_var.set("No serial ports found")
        else:
            self.status_var.set(f"Found {len(ports)} ports")
        self._ensure_monitor_workers()

    def _auto_assign_esp32s2(self):
        # Auto-fill rows with ports matching the ESP32-S2 VID/PID.
        if list_ports is None:
            self.status_var.set("pyserial not installed")
            return
        ports = list_ports.comports()
        matches = []
        for p in ports:
            desc = (p.description or "").upper()
            manuf = (p.manufacturer or "").upper()
            hwid = (p.hwid or "").upper()
            # Primary match: known Adafruit VID/PID for this board.
            if "VID:PID=239A:80ED" in hwid:
                matches.append(p.device)
                continue
            # Fallback match: human-readable strings.
            if "ESP32-S2" in desc or "ESP32-S2" in manuf or "FEATHER" in desc:
                matches.append(p.device)

        if not matches:
            self.status_var.set("No ESP32-S2 devices found")
            return

        for i, row in enumerate(self.device_rows):
            if i < len(matches):
                row["port"].set(matches[i])
                row["enabled"].set(True)
            else:
                row["enabled"].set(False)
        self.status_var.set(f"Auto-assigned {min(len(matches), MAX_DEVICES)} device(s)")
        self._ensure_monitor_workers()

    def _set_row_status(self, port: str, status: str):
        # Update the connection status text for a given port.
        for row in self.device_rows:
            if row["port"].get() == port:
                row["status"].set(status)

    def _set_row_position(self, port: str, distance_mm: float):
        # Update the live position readout and RX status for a given port.
        for row in self.device_rows:
            if row["port"].get() == port:
                row["position"].set(f"{distance_mm:.1f}")
                row["rx"].set("RX")
                self.last_rx[port] = time.time()

    def _build_configs(self) -> List[DeviceConfig]:
        # Convert the GUI table rows into DeviceConfig objects.
        cfgs = []
        for idx, row in enumerate(self.device_rows):
            device_id = row["device_id"].get().strip()
            label = device_id if device_id else f"DEV{idx + 1}"
            cfgs.append(
                DeviceConfig(
                    enabled=row["enabled"].get(),
                    port=row["port"].get().strip(),
                    device_id=device_id,
                    device_label=label,
                )
            )
        return cfgs

    def start_logging(self):
        # Validate settings, then start writer and enable logging on readers.
        if self.writer is not None:
            return
        base_name = self.base_name_var.get().strip()
        directory = self.dir_var.get().strip()
        if not base_name:
            messagebox.showerror("Missing", "Base file name is required")
            return
        if not directory:
            messagebox.showerror("Missing", "Directory is required")
            return
        base_path = os.path.join(directory, base_name)

        cfgs = self._build_configs()
        if not any(c.enabled and c.port for c in cfgs):
            messagebox.showerror("Missing", "Enable at least one device with a port")
            return
        # Warn if we will append to existing files.
        existing = self._existing_log_paths(base_path, cfgs, self.combined_var.get())
        if existing:
            msg = "Existing log files will be appended:\n\n" + "\n".join(existing)
            if not messagebox.askyesno("File Exists", msg + "\n\nProceed?"):
                return

        combined_labels = None
        if self.combined_var.get():
            combined_labels = [c.device_label for c in cfgs if c.enabled and c.port]
        self.writer = LogWriter(self.record_queue, base_path, self.combined_var.get(), combined_labels)
        self.writer.start()

        # Start elapsed timer and enable file writing.
        self.start_mono = time.monotonic()
        self.start_mono_ref["t0"] = self.start_mono
        self.record_queue_ref["queue"] = self.record_queue
        # Ensure background readers are running.
        self._ensure_monitor_workers()

        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("Logging")
        self.logging = True
        for row in self.device_rows:
            row["id_btn"].configure(state="disabled")

    def stop_logging(self):
        # Stop file writing and leave readers running for UI updates.
        if self.writer is not None:
            self.writer.stop()
            self.writer = None
        self.start_mono = None
        self.start_mono_ref["t0"] = None
        self.record_queue_ref["queue"] = None

        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("Stopped")
        self.logging = False
        for row in self.device_rows:
            row["id_btn"].configure(state="normal")
        # Background readers keep running for UI updates.
        self._ensure_monitor_workers()

    def _on_close(self):
        # Ensure threads stop before window closes.
        self.stop_logging()
        self.destroy()

    def _poll_ui_queue(self):
        # Process UI updates coming from worker threads.
        while True:
            try:
                rec = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if rec and rec.distance_mm is not None:
                self._set_row_position(rec.port, rec.distance_mm)
        # Update RX indicators based on how recently data arrived.
        now = time.time()
        for row in self.device_rows:
            port = row["port"].get()
            last = self.last_rx.get(port)
            if last is None or now - last > RX_TIMEOUT_SEC:
                row["rx"].set("●")
                row["rx_label"].configure(foreground="gray40")
            else:
                row["rx"].set("●")
                row["rx_label"].configure(foreground="green")
        # Keep monitor workers in sync with the current UI selection.
        self._ensure_monitor_workers()
        self.after(200, self._poll_ui_queue)

    def _update_recording_indicator(self):
        # Blink the recording light while logging.
        if self.logging:
            self.recording_blink = not self.recording_blink
            if self.recording_blink:
                self.recording_dot.configure(foreground="red")
            else:
                self.recording_dot.configure(foreground="gray40")
        else:
            self.recording_dot.configure(foreground="gray40")
            self.recording_blink = False
        self.after(500, self._update_recording_indicator)

    def _ensure_monitor_workers(self):
        # Ensure background readers are running (used for UI and optional logging).
        if self.monitor_paused:
            self._stop_monitor_workers()
            return

        desired = {}
        for cfg in self._build_configs():
            if cfg.enabled and cfg.port:
                desired[cfg.port] = cfg

        # Stop monitors for ports that are no longer selected.
        for port in list(self.monitor_workers.keys()):
            if port not in desired:
                self.monitor_workers[port].stop()
                self.monitor_workers.pop(port, None)

        # Start monitors for new ports.
        for port, cfg in desired.items():
            if port in self.monitor_workers:
                continue
            worker = SerialWorker(cfg, self.record_queue_ref, self._set_row_status, self.ui_queue, self.start_mono_ref)
            self.monitor_workers[port] = worker
            worker.start()

    def _stop_monitor_workers(self):
        # Stop all monitor-only workers.
        for worker in self.monitor_workers.values():
            worker.stop_and_wait(timeout=1.0)
        self.monitor_workers = {}

    def _existing_log_paths(self, base_path: str, cfgs: List[DeviceConfig], combined: bool) -> List[str]:
        # List any files that already exist and would be appended.
        paths = []
        if combined:
            path = base_path + ".csv"
            if os.path.exists(path):
                paths.append(path)
            return paths

        for cfg in cfgs:
            if not cfg.enabled or not cfg.port:
                continue
            key = cfg.device_id if cfg.device_id else cfg.port.replace("/", "_")
            path = f"{base_path}_{key}.csv"
            if os.path.exists(path):
                paths.append(path)
        return paths

    def _id_dialog(self, idx: int):
        # Open a small dialog for reading/setting the device ID.
        if self.logging:
            messagebox.showinfo("Busy", "Stop logging before reading or setting IDs.")
            return
        row = self.device_rows[idx]
        port = row["port"].get().strip()
        if not port:
            messagebox.showerror("Missing", "Select a port first.")
            return
        current_id = row["device_id"].get().strip()

        dialog = tk.Toplevel(self)
        dialog.title(f"Device ID - {port}")
        dialog.geometry("360x160")
        ttk.Label(dialog, text="Device ID (uppercase, no spaces)").pack(pady=(12, 4))
        id_var = tk.StringVar(value=current_id)
        entry = ttk.Entry(dialog, textvariable=id_var, width=24)
        entry.pack()
        entry.focus_set()

        def read_id():
            # Query device for ID and update the row.
            self.monitor_paused = True
            self._stop_monitor_workers()
            try:
                time.sleep(0.2)
                device_id = self._serial_read_id(port)
                if device_id is None:
                    return
                row["device_id"].set(device_id)
                id_var.set(device_id)
                dialog.destroy()
            finally:
                self.monitor_paused = False
                self._ensure_monitor_workers()

        def set_id():
            # Send a new ID to the device.
            self.monitor_paused = True
            self._stop_monitor_workers()
            try:
                value = id_var.get().strip().upper().replace(" ", "")
                if not value:
                    messagebox.showerror("Missing", "ID cannot be empty.")
                    return
                time.sleep(0.2)
                ok = self._serial_set_id(port, value)
                if ok:
                    row["device_id"].set(value)
                    id_var.set(value)
                    dialog.destroy()
            finally:
                self.monitor_paused = False
                self._ensure_monitor_workers()

        btns = ttk.Frame(dialog)
        btns.pack(pady=12)
        ttk.Button(btns, text="Read ID", command=read_id).grid(row=0, column=0, padx=6)
        ttk.Button(btns, text="Set ID", command=set_id).grid(row=0, column=1, padx=6)
        ttk.Button(btns, text="Close", command=dialog.destroy).grid(row=0, column=2, padx=6)

    def _serial_read_id(self, port: str) -> Optional[str]:
        # Open a temporary serial connection and request the ID.
        if serial is None:
            messagebox.showerror("Missing", "pyserial is not installed.")
            return None
        try:
            with serial.Serial(
                port, DEFAULT_BAUD, timeout=0.2, write_timeout=1.0
            ) as ser:
                # Many ESP32 boards reboot when the port opens; wait briefly.
                time.sleep(1.2)
                ser.reset_input_buffer()
                ser.write(b"ID\r\n")
                ser.flush()

                deadline = time.time() + 3.0
                while time.time() < deadline:
                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    # Ignore streaming data lines.
                    if "," in line:
                        continue
                    if line == "?":
                        messagebox.showerror("Error", "Device returned '?' for ID query.")
                        return None
                    return line.upper().replace(" ", "")

                messagebox.showerror("Timeout", "No response to ID query.")
                return None
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to read ID: {exc}")
            return None

    def _serial_set_id(self, port: str, value: str) -> bool:
        # Open a temporary serial connection, write ID, then read it back.
        if serial is None:
            messagebox.showerror("Missing", "pyserial is not installed.")
            return False
        try:
            with serial.Serial(
                port, DEFAULT_BAUD, timeout=0.2, write_timeout=1.0
            ) as ser:
                # Allow reboot on open, then send command.
                time.sleep(1.2)
                ser.reset_input_buffer()
                ser.write(f"ID {value}\r\n".encode("utf-8"))
                ser.flush()
                # Query back to confirm.
                time.sleep(0.1)
                ser.reset_input_buffer()
                ser.write(b"ID\r\n")
                ser.flush()

                deadline = time.time() + 3.0
                while time.time() < deadline:
                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    # Ignore streaming data lines.
                    if "," in line:
                        continue
                    if line == "?":
                        messagebox.showerror("Error", "Device returned '?' for ID set.")
                        return False
                    return line.upper().replace(" ", "") == value

            messagebox.showerror("Timeout", "No response after setting ID.")
            return False
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to set ID: {exc}")
            return False


if __name__ == "__main__":
    app = App()
    app.mainloop()
