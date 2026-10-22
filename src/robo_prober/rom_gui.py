"""Range-of-motion window (tkinter + ttk).

One row per servo channel: a confirmed start pulse with an Enable button, a
slider the servo follows slowly, "Set ROM 1" and "Set ROM 2", Clear and
Widen. All logic lives in rom.py and api.py; this file only wires widgets to
them.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from . import __version__, theme
from .api import EMULATOR_PORT, Controller, connect, discover, load_map, save_map
from .rom import (
    ABS_MAX_US,
    ABS_MIN_US,
    DEFAULT_BAND_HALF_US,
    DEFAULT_HOST_SLEW_US_S,
    RomChannel,
    RomError,
    RomSession,
    apply_rom_to_map,
    channels_from_map,
    set_last_positions,
)

TICK_MS = 50
STRIP_H = 16
ESTOP_BG = "#d50000"
ESTOP_ACTIVE = "#ff1744"
EMULATOR_LABEL = "Built-in emulator (no hardware)"
SOURCE_TEXT = {"last_position": "last saved position", "rom_midpoint": "middle of the saved range",
               "operator": "your value", "none": "no saved position - check this value"}


class RomApp:
    def __init__(self, root: tk.Tk, *, map_path: Path | None = None, out_path: Path | None = None,
                 port: str | None = None, channels: list[int] | None = None, operator: str = "",
                 band_half_us: int = DEFAULT_BAND_HALF_US, host_slew_us_s: int = DEFAULT_HOST_SLEW_US_S,
                 connect_fn: Callable[..., Controller] = connect, run_timer: bool = True):
        self.root = root
        root.title(f"robo-prober {__version__} - range of motion")
        root.minsize(560, 420)
        root.geometry("1000x760")
        self.connect_fn = connect_fn
        self.band_half_us = band_half_us
        self.host_slew_us_s = host_slew_us_s
        self.selected_channels = channels
        self.controller: Controller | None = None
        self.session: RomSession | None = None
        self.hardware_map: dict[str, Any] | None = None
        self.map_path: Path | None = None
        self.out_path = out_path
        self.roms: dict[int, RomChannel] = {}
        self.rows: dict[int, dict[str, Any]] = {}
        self.last_error: str | None = None
        self.estop_latched = False
        self._closed = False

        self.port_var = tk.StringVar(value=EMULATOR_LABEL if port == EMULATOR_PORT else (port or ""))
        self.cutoff_var = tk.BooleanVar(value=False)
        self.operator_var = tk.StringVar(value=operator)
        self.map_var = tk.StringVar(value="No hardware map loaded (channels 0-15)")
        self.status = tk.StringVar(value="Pick a port, confirm the power cutoff, then Connect & arm.")

        self._build()
        self.refresh_ports()
        if map_path is not None:
            self.load_map_file(Path(map_path))
        else:
            self._set_channels([RomChannel(ch, band_half_us=band_half_us) for ch in (channels or range(16))])
        if run_timer:
            root.after(TICK_MS, self._timer)

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        root = self.root
        theme.apply(root)

        # window controls (tiling desktops often draw no title bar)
        header = ttk.Frame(root, padding=(12, 10, 12, 0))
        header.pack(fill="x")
        ttk.Label(header, text="robo-prober - range of motion", style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="✕", style="Close.TButton", width=3,
                   command=self.request_close).pack(side="right")
        ttk.Button(header, text="▢", style="Chrome.TButton", width=3,
                   command=self.toggle_fullscreen).pack(side="right", padx=(0, 6))
        ttk.Button(header, text="–", style="Chrome.TButton", width=3,
                   command=self.root.iconify).pack(side="right", padx=(0, 6))
        root.bind("<F11>", lambda e: self.toggle_fullscreen())
        # Esc is the keyboard E-STOP (it also leaves fullscreen).
        root.bind("<Escape>", lambda e: (self.toggle_fullscreen(False), self.estop()))
        root.bind("<Control-q>", lambda e: self.request_close())
        root.protocol("WM_DELETE_WINDOW", self.request_close)

        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        # 1. connection + always-visible E-STOP
        top = ttk.Frame(outer)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Port", style="Big.TLabel").grid(row=0, column=0, sticky="w")
        self.port_box = ttk.Combobox(top, textvariable=self.port_var, width=30)
        self.port_box.grid(row=0, column=1, sticky="ew", padx=(6, 4))
        ttk.Button(top, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=(0, 12))
        ttk.Checkbutton(top, text="Physical power cutoff is within reach",
                        variable=self.cutoff_var).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(top)
        buttons.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.arm_btn = ttk.Button(buttons, text="Connect & arm", style="Accent.TButton", command=self.connect_and_arm)
        self.arm_btn.pack(side="left")
        self.disarm_btn = ttk.Button(buttons, text="Disarm", command=self.disarm)
        self.disarm_btn.pack(side="left", padx=6)
        self.clear_estop_btn = ttk.Button(buttons, text="Clear e-stop", command=self.clear_estop)
        self.clear_estop_btn.pack(side="left")
        self.estop_btn = tk.Button(top, text="E-STOP", command=self.estop, bg=ESTOP_BG, fg="white",
                                   activebackground=ESTOP_ACTIVE, activeforeground="white",
                                   highlightthickness=2, highlightbackground="white",
                                   font=("TkDefaultFont", 18, "bold"), width=8, height=2, relief="raised", bd=4)
        self.estop_btn.grid(row=0, column=3, rowspan=3, sticky="ne", padx=(12, 0))

        # 2. hardware map
        maprow = ttk.LabelFrame(outer, text="Hardware map", padding=(10, 6))
        maprow.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        maprow.columnconfigure(1, weight=1)
        ttk.Button(maprow, text="Open map...", command=self.browse_map).grid(row=0, column=0, sticky="w")
        ttk.Label(maprow, textvariable=self.map_var, style="Muted.TLabel").grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(maprow, text="Operator").grid(row=0, column=2, sticky="e")
        ttk.Entry(maprow, textvariable=self.operator_var, width=14).grid(row=0, column=3, padx=6)
        self.save_btn = ttk.Button(maprow, text="Save ROM to map", command=self.save)
        self.save_btn.grid(row=0, column=4)

        # 3. channel rows (scrollable, so they stay usable in a small tiled window)
        box = ttk.LabelFrame(outer, text="Channels", padding=(6, 4))
        box.grid(row=2, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(box, highlightthickness=0, borderwidth=0, background=theme.BG)
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.rows_frame = ttk.Frame(self.canvas)
        self._rows_window = self.canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.rows_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._rows_window, width=e.width))
        root.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        root.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        root.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

        self.status_label = ttk.Label(outer, textvariable=self.status, wraplength=900)
        self.status_label.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        outer.bind("<Configure>", lambda e: self.status_label.configure(wraplength=max(e.width - 24, 200)))

    def _set_channels(self, roms: list[RomChannel]) -> None:
        for row in self.rows.values():
            row["frame"].destroy()
        self.roms = {rom.channel: rom for rom in roms}
        self.rows = {}
        for index, rom in enumerate(roms):
            self.rows[rom.channel] = self._build_row(index, rom)
        self._refresh_all()

    def _build_row(self, index: int, rom: RomChannel) -> dict[str, Any]:
        frame = ttk.LabelFrame(self.rows_frame, text=f"ch {rom.channel}  {rom.label}", padding=(10, 6))
        frame.grid(row=index, column=0, sticky="ew", padx=2, pady=4)
        self.rows_frame.columnconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        start_row = ttk.Frame(frame)
        start_row.grid(row=0, column=0, sticky="ew")
        ttk.Label(start_row, text="Start at").pack(side="left")
        start_var = tk.StringVar(value=str(rom.center))
        start_entry = ttk.Spinbox(start_row, from_=ABS_MIN_US, to=ABS_MAX_US, increment=10, width=7,
                                  textvariable=start_var)
        start_entry.pack(side="left", padx=4)
        ttk.Label(start_row, text="us").pack(side="left")
        enable = ttk.Button(start_row, text="Enable", style="Accent.TButton",
                            command=lambda ch=rom.channel: self.enable(ch))
        enable.pack(side="left", padx=(10, 0))
        warning = ttk.Label(frame, style="Error.TLabel")
        warning.grid(row=1, column=0, sticky="w", pady=(2, 0))

        low, high = rom.slider_range()
        var = tk.DoubleVar(value=rom.center)
        scale = tk.Scale(frame, from_=low, to=high, orient="horizontal", resolution=1, showvalue=True, variable=var,
                         background=theme.BG, foreground=theme.TEXT, troughcolor=theme.SURFACE_HI,
                         activebackground=theme.ACCENT, highlightthickness=0, borderwidth=0, sliderrelief="flat",
                         font=theme.FONT)
        scale.grid(row=2, column=0, sticky="ew")
        strip = tk.Canvas(frame, height=STRIP_H, highlightthickness=0, background=theme.SURFACE)
        strip.grid(row=3, column=0, sticky="ew", padx=12)
        info = ttk.Label(frame)
        info.grid(row=4, column=0, sticky="w", pady=(4, 0))
        actions = ttk.Frame(frame)
        actions.grid(row=5, column=0, sticky="w", pady=(4, 0))
        rom1 = ttk.Button(actions, text="Set ROM 1", command=lambda ch=rom.channel: self.mark(ch, 1))
        rom2 = ttk.Button(actions, text="Set ROM 2", command=lambda ch=rom.channel: self.mark(ch, 2))
        clear = ttk.Button(actions, text="Clear", command=lambda ch=rom.channel: self.clear(ch))
        widen = ttk.Button(actions, text="Widen range", command=lambda ch=rom.channel: self.widen(ch))
        for widget in (rom1, rom2, clear, widen):
            widget.pack(side="left", padx=(0, 6))
        row = {"frame": frame, "var": var, "scale": scale, "strip": strip, "info": info, "start_var": start_var,
               "start_entry": start_entry, "enable": enable, "warning": warning,
               "rom1": rom1, "rom2": rom2, "clear": clear, "widen": widen}
        # Bind after the row exists so the callbacks can find it.
        start_var.trace_add("write", lambda *_, ch=rom.channel: self._refresh_row(ch))
        var.trace_add("write", lambda *_, ch=rom.channel, v=var: self._var_changed(ch, v))
        strip.bind("<Configure>", lambda _e, ch=rom.channel: self._draw_strip(ch))
        return row

    # ---------------------------------------------------------------- state
    @property
    def armed(self) -> bool:
        return self.session is not None and self.session.started and self.controller is not None and self.controller.armed

    def _enabled(self, channel: int) -> bool:
        return self.armed and channel in self.session.enabled

    def _say(self, text: str, error: bool = False) -> None:
        self.status.set(text)
        self.status_label.configure(style="Error.TLabel" if error else "TLabel")
        self.last_error = text if error else None

    def _start_value(self, channel: int) -> int | None:
        try:
            return int(self.rows[channel]["start_var"].get())
        except (ValueError, tk.TclError):
            return None

    def _refresh_all(self) -> None:
        armed = self.armed
        for channel in self.rows:
            self._refresh_row(channel)
        self.arm_btn.state(["disabled"] if armed or self.estop_latched else ["!disabled"])
        self.disarm_btn.state(["!disabled"] if armed else ["disabled"])
        self.clear_estop_btn.state(["!disabled"] if self.estop_latched else ["disabled"])
        self.save_btn.state(["!disabled"] if self.hardware_map is not None else ["disabled"])

    def _refresh_row(self, channel: int) -> None:
        if channel not in self.rows:
            return
        row, rom = self.rows[channel], self.roms[channel]
        low, high = rom.slider_range()
        if int(float(row["scale"].cget("from"))) != low or int(float(row["scale"].cget("to"))) != high:
            row["scale"].configure(from_=low, to=high)
        enabled = self._enabled(channel)
        row["scale"].configure(state="normal" if enabled else "disabled")
        row["start_entry"].state(["disabled"] if enabled else ["!disabled"])
        row["enable"].state(["!disabled"] if self.armed and not enabled else ["disabled"])
        if enabled:
            row["warning"].configure(text="")
        else:
            value = self._start_value(channel)
            shown = f"{value} us" if value is not None else "the start value"
            source = SOURCE_TEXT.get(rom.start_source if value == rom.start_us else "operator", "")
            row["warning"].configure(
                text=f"Servo will move to {shown} from an unknown position - make sure it's clear. ({source})")
        for key in ("rom1", "rom2"):
            row[key].state(["!disabled"] if enabled else ["disabled"])
        row["clear"].state(["!disabled"] if rom.rom1_us is not None or rom.rom2_us is not None else ["disabled"])
        row["widen"].state(["!disabled"] if rom.can_widen() else ["disabled"])
        position = self.controller.position(channel) if enabled else None
        marks = f"ROM 1 {rom.rom1_us if rom.rom1_us is not None else '-'} / ROM 2 {rom.rom2_us if rom.rom2_us is not None else '-'}"
        where = f"at {position} us" if position is not None else "no output"
        span = f"safe range {low}..{high} us" if rom.complete else f"allowed {low}..{high} us (ROM not set)"
        row["info"].configure(text=f"{where}   {span}   {marks}",
                              foreground=theme.ACCENT if rom.complete else theme.TEXT)
        self._draw_strip(channel)

    def _draw_strip(self, channel: int) -> None:
        row, rom = self.rows[channel], self.roms[channel]
        strip = row["strip"]
        width = max(strip.winfo_width(), 50)
        strip.delete("all")

        def x(pulse: int) -> float:
            return (pulse - ABS_MIN_US) / (ABS_MAX_US - ABS_MIN_US) * (width - 1)

        low, high = rom.band
        strip.create_rectangle(x(low), 3, x(high), STRIP_H - 3, fill=theme.BORDER, outline="")
        if rom.complete:
            low, high = rom.range
            strip.create_rectangle(x(low), 1, x(high), STRIP_H - 1, fill=theme.ACCENT_LO, outline=theme.ACCENT)
        for mark in (rom.rom1_us, rom.rom2_us):
            if mark is not None:
                strip.create_line(x(mark), 0, x(mark), STRIP_H, fill=theme.TEXT, width=3)
        position = self.controller.position(channel) if self._enabled(channel) else None
        if position is not None:
            strip.create_polygon(x(position) - 5, 0, x(position) + 5, 0, x(position), STRIP_H,
                                 fill=theme.TEXT, outline=theme.BG)

    # ---------------------------------------------------------------- actions
    def refresh_ports(self) -> None:
        try:
            ports = [row["device"] for row in discover()]
        except Exception as error:  # noqa: BLE001 - discovery is best effort here
            ports = []
            self._say(f"Port discovery failed: {error}", error=True)
        self.port_box.configure(values=ports + [EMULATOR_LABEL])
        if not self.port_var.get():
            self.port_var.set(ports[0] if ports else EMULATOR_LABEL)

    def browse_map(self) -> None:
        path = filedialog.askopenfilename(title="Open hardware map",
                                          filetypes=[("Hardware map", "*.json"), ("All files", "*")])
        if path:
            self.load_map_file(Path(path))

    def load_map_file(self, path: Path) -> None:
        if self.armed:
            self._say("Disarm before loading another map.", error=True)
            return
        try:
            hardware_map = load_map(path)
            roms = channels_from_map(hardware_map, self.selected_channels, band_half_us=self.band_half_us)
        except Exception as error:  # noqa: BLE001 - shown to the operator
            self._say(f"Could not load {path}: {error}", error=True)
            return
        self.hardware_map, self.map_path = hardware_map, path
        self.map_var.set(str(path))
        self._set_channels(roms)
        self._say(f"Loaded {len(roms)} channel(s) from {path.name}.")

    def connect_and_arm(self) -> None:
        if self.armed:
            return
        if not self.cutoff_var.get():
            self._say("Confirm that a physical power cutoff is within reach first.", error=True)
            return
        port = self.port_var.get().strip()
        if not port:
            self._say("Pick a serial port (or the built-in emulator).", error=True)
            return
        try:
            if self.controller is None:
                self.controller = self.connect_fn(EMULATOR_PORT if port == EMULATOR_LABEL else port)
            # Heartbeats come from the window's own timer: if the window freezes,
            # the firmware watchdog releases everything.
            self.session = RomSession(self.controller, self.roms.values(), host_slew_us_s=self.host_slew_us_s,
                                      heartbeat="manual")
            self.session.start(power_cutoff_confirmed=True)
        except Exception as error:  # noqa: BLE001 - shown to the operator
            self._drop_connection()
            self._say(f"Could not arm: {error}", error=True)
            self._refresh_all()
            return
        self._say("Armed. No channel outputs anything yet: check each start value, then press Enable.")
        self._refresh_all()

    def enable(self, channel: int) -> None:
        """First output on a channel, at the start value shown in its row."""
        if not self.armed:
            return
        start = self._start_value(channel)
        if start is None:
            self._say(f"ch {channel}: enter a start pulse in us.", error=True)
            return
        rom = self.roms[channel]
        try:
            self.session.enable(channel, None if start == rom.start_us else start)
        except Exception as error:  # noqa: BLE001
            self._fault_or_say(error)
            return
        self.rows[channel]["var"].set(start)
        self._say(f"ch {channel}: output on at {start} us. Drag the slider; the servo follows slowly.")
        self._refresh_row(channel)

    def _var_changed(self, channel: int, var: tk.DoubleVar) -> None:
        try:
            value = var.get()
        except tk.TclError:  # transient text while Tk updates the variable
            return
        self.slider_moved(channel, value)

    def slider_moved(self, channel: int, value: Any) -> None:
        if not self._enabled(channel):
            return
        self.session.set_target(channel, float(value))

    def mark(self, channel: int, which: int) -> None:
        if not self._enabled(channel):
            return
        try:
            pulse = self.session.mark(channel, which)
        except Exception as error:  # noqa: BLE001
            self._fault_or_say(error)
            return
        self.rows[channel]["var"].set(pulse)
        rom = self.roms[channel]
        if rom.complete:
            low, high = rom.range
            self._say(f"ch {channel}: safe range {low}..{high} us; the firmware now clamps it there.")
        else:
            self._say(f"ch {channel}: ROM {which} = {pulse} us. Move to the other end and set ROM {3 - which}.")
        self._refresh_row(channel)

    def clear(self, channel: int) -> None:
        try:
            if self.session is not None and self.session.started:
                self.session.clear(channel)
            else:
                self.roms[channel].clear()
        except Exception as error:  # noqa: BLE001
            self._fault_or_say(error)
            return
        self._say(f"ch {channel}: ROM cleared; limited to the start band again.")
        self._refresh_row(channel)

    def widen(self, channel: int) -> None:
        try:
            if self.session is not None and self.session.started:
                low, high = self.session.widen(channel)
            else:
                low, high = self.roms[channel].widen()
        except Exception as error:  # noqa: BLE001
            self._fault_or_say(error)
            return
        self._say(f"ch {channel}: allowed band widened to {low}..{high} us. Move carefully.")
        self._refresh_row(channel)

    def estop(self) -> None:
        controller = self.controller
        if controller is None:
            self._say("E-STOP: nothing connected.")
            return
        try:
            if self.session is not None:
                self.session.estop()
            else:
                controller.estop()
            self.estop_latched = True
            self._say("E-STOP latched: all outputs released. Clear e-stop, then Connect & arm to continue.", error=True)
        except Exception as error:  # noqa: BLE001
            self._say(f"E-STOP could not be confirmed ({error}). CUT POWER NOW.", error=True)
            self._drop_connection()
        self._persist_positions()
        self._refresh_all()

    def clear_estop(self) -> None:
        if self.controller is None or not self.cutoff_var.get():
            self._say("Confirm the power cutoff to clear the e-stop.", error=True)
            return
        try:
            self.controller.clear_estop(power_cutoff_confirmed=True)
            self.controller.hello()
            self.estop_latched = False
            self._say("E-stop cleared. Nothing is armed; press Connect & arm to continue.")
        except Exception as error:  # noqa: BLE001
            self._say(f"Could not clear the e-stop: {error}", error=True)
        self._refresh_all()

    def disarm(self) -> None:
        if self.session is not None:
            try:
                self.session.close()
            except Exception as error:  # noqa: BLE001
                self._say(f"Disarm failed ({error}). CUT POWER NOW.", error=True)
                self._drop_connection()
                self._refresh_all()
                return
        saved = self._persist_positions()
        self._say("Disarmed: all outputs released." + (f" Last positions saved to {saved.name}." if saved else ""))
        self._refresh_all()

    def _persist_positions(self) -> Path | None:
        """Show each channel's last commanded pulse as its next start value and,
        with a map loaded, write it into the map so the next session starts there."""
        if self.session is None or not self.session.last_positions:
            return None
        # Always refresh the shown start values, map or not: Enable starts at the
        # shown value, and a stale one makes the servo jump on the next Enable.
        if not self._closed:
            for channel in self.rows:
                self.rows[channel]["start_var"].set(str(self.roms[channel].center))
        if self.hardware_map is None:
            return None
        target = self.out_path or self.map_path
        try:
            updated = set_last_positions(self.hardware_map, self.session.last_positions)
            save_map(updated, target, overwrite=True)
        except Exception as error:  # noqa: BLE001
            self._say(f"Could not save last positions: {error}", error=True)
            return None
        self.hardware_map = updated
        return target

    def save(self) -> Path | None:
        if self.hardware_map is None:
            self._say("Open a hardware map first.", error=True)
            return None
        operator = self.operator_var.get().strip()
        if not operator:
            self._say("Enter the operator name before saving.", error=True)
            return None
        complete = [rom for rom in self.roms.values() if rom.complete]
        if not complete:
            self._say("No channel has both ROM marks yet.", error=True)
            return None
        target = self.out_path or self.map_path
        try:
            updated = apply_rom_to_map(self.hardware_map, complete, set_by=operator)
            save_map(updated, target, overwrite=True)
        except Exception as error:  # noqa: BLE001
            self._say(f"Save failed: {error}", error=True)
            return None
        self.hardware_map = updated
        self._say(f"Saved ROM for {len(complete)} channel(s) to {target}.")
        return target

    def _fault_or_say(self, error: Exception) -> None:
        """Protocol problems stop everything; operator mistakes only show a message."""
        if isinstance(error, RomError):
            self._say(str(error), error=True)
            return
        self._say(f"Controller error: {error}. Stopping.", error=True)
        try:
            if self.session is not None:
                self.session._remember_positions()
            if self.controller is not None:
                self.controller.stop()
        except Exception:  # noqa: BLE001
            self._say(f"Controller error: {error}. STOP not confirmed - CUT POWER NOW.", error=True)
            self._drop_connection()
        if self.session is not None:
            self.session.started = False
            self.session.enabled.clear()
        self._persist_positions()
        self._refresh_all()

    def _drop_connection(self) -> None:
        controller, self.controller = self.controller, None
        if self.session is not None:
            self.session.started = False
            self.session.enabled.clear()
        if controller is not None:
            try:
                controller.close()
            except Exception:  # noqa: BLE001
                pass

    def tick(self, dt_s: float = TICK_MS / 1000) -> None:
        """One slew step for every moving channel (called by the timer)."""
        if not self.armed:
            return
        try:
            moved = self.session.step(dt_s)
        except Exception as error:  # noqa: BLE001
            self._fault_or_say(error)
            return
        for channel in moved:
            self._refresh_row(channel)

    def _timer(self) -> None:
        if self._closed:
            return
        self.tick()
        self.root.after(TICK_MS, self._timer)

    def toggle_fullscreen(self, state: bool | None = None) -> None:
        if state is None:
            state = not bool(self.root.attributes("-fullscreen"))
        self.root.attributes("-fullscreen", state)

    def request_close(self) -> None:
        """Exit button, window close and Ctrl+Q: confirm while armed, then close."""
        if self.armed:
            moving = bool(self.session.moving())
            question = ("A servo is still moving. " if moving else "Servos are powered. ") + \
                "Disarm (release every output) and quit?"
            if not messagebox.askyesno("Quit robo-prober", question, parent=self.root):
                return
        self.close()

    def close(self) -> None:
        """Always DISARM (releasing every output) and save last positions, then exit."""
        if self._closed:
            return
        self._closed = True
        try:
            if self.session is not None and self.armed:
                self.session.close()
        except Exception:  # noqa: BLE001 - closing anyway; the firmware watchdog releases too
            pass
        self._persist_positions()
        self._drop_connection()
        self.root.destroy()


def run(**kwargs) -> int:
    from .platform_utils import enable_hidpi

    enable_hidpi()
    root = tk.Tk()
    RomApp(root, **kwargs)
    root.mainloop()
    return 0
