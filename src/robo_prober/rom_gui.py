"""The robo-prober window (tkinter + ttk), in three steps:

1. Discover: read-only survey of the controller's pins and I2C bus, and a
   button watch.
2. Identify: one pin (or PCA9685 channel) at a time, wiggle it as a servo
   or blink it, say what happened and name it; save the hardware map.
3. Range of motion: one row per named servo: a confirmed start pulse with an
   Enable button, a slider the servo follows slowly, "Set ROM 1" and
   "Set ROM 2", Clear and Widen.

The connection bar and the E-STOP stay visible above all three. All logic
lives in find.py, rom.py and api.py; this file only wires widgets to them.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from . import __version__, find, theme
from .api import EMULATOR_PORT, Controller, connect, discover, load_map, save_map
from .transport import ProtocolError
from .rom import (
    ABS_MAX_US,
    ABS_MIN_US,
    DEFAULT_BAND_HALF_US,
    DEFAULT_HOST_SLEW_US_S,
    RomChannel,
    RomError,
    RomSession,
    apply_rom_to_map,
    channel_name,
    channels_from_map,
    set_last_positions,
)

TICK_MS = 50
STRIP_H = 16
ESTOP_BG = "#d50000"
ESTOP_ACTIVE = "#ff1744"
EMULATOR_LABEL = "Built-in emulator (no hardware)"
NO_ROWS_TEXT = "Find servos first (steps 1 and 2), or open a saved map."
CLASS_TEXT = {"floating": "floating (nothing, or a servo/LED input)", "pulled_up": "pulled up (resistor: button, I2C)",
              "pulled_down": "pulled down (resistor, LED)", "driven_high": "DRIVEN high by something: never driven",
              "driven_low": "DRIVEN low by something: never driven", "reserved": "reserved"}
SOURCE_TEXT = {"last_position": "last saved position", "rom_midpoint": "middle of the saved range",
               "operator": "your value", "none": "no saved position - check this value"}


class RomApp:
    def __init__(self, root: tk.Tk, *, map_path: Path | None = None, out_path: Path | None = None,
                 port: str | None = None, channels: list[int] | None = None, operator: str = "",
                 band_half_us: int = DEFAULT_BAND_HALF_US, host_slew_us_s: int = DEFAULT_HOST_SLEW_US_S,
                 connect_fn: Callable[..., Controller] = connect, run_timer: bool = True):
        self.root = root
        root.title(f"robo-prober {__version__}")
        root.minsize(560, 420)
        root.geometry("1000x800")
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
        # find servos (steps 1 and 2)
        self.survey: find.Survey | None = None
        self.found: dict[str, find.Found] = {}
        self.skipped: set[str] = set()
        self.pressed: set[int] = set()
        self.wiggle: find.Wiggle | None = None
        self.last_action = ""
        self.pending_kind = ""
        self._watch_ticks = 0
        self._found_path: Path | None = None
        self.wiggle_slew_us_s = find.DEFAULT_WIGGLE_SLEW_US_S

        self.port_var = tk.StringVar(value=EMULATOR_LABEL if port == EMULATOR_PORT else (port or ""))
        self.cutoff_var = tk.BooleanVar(value=False)
        self.operator_var = tk.StringVar(value=operator)
        self.map_var = tk.StringVar(value="No hardware map loaded (channels 0-15)")
        self.status = tk.StringVar(value="Pick a port and press Discover (nothing moves), or open a saved map.")
        self.watch_var = tk.BooleanVar(value=False)
        self.start_var = tk.StringVar(value=str(find.DEFAULT_START_US))
        self.delta_var = tk.StringVar(value=str(find.DEFAULT_DELTA_US))
        self.name_var = tk.StringVar()
        self.survey_var = tk.StringVar(value="Not discovered yet. Press Discover: it only reads the pins.")
        self.target_var = tk.StringVar(value="Discover first.")

        self._build()
        self.refresh_ports()
        if map_path is not None:
            self.load_map_file(Path(map_path))
        else:
            self._set_channels([RomChannel(ch, band_half_us=band_half_us) for ch in (channels or [])])
        if run_timer:
            root.after(TICK_MS, self._timer)

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        root = self.root
        theme.apply(root)

        # window controls (tiling desktops often draw no title bar)
        header = ttk.Frame(root, padding=(12, 10, 12, 0))
        header.pack(fill="x")
        ttk.Label(header, text="robo-prober", style="Title.TLabel").pack(side="left")
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

        # steps: 1 discover, 2 identify, 3 range of motion
        self.steps = ttk.Notebook(outer)
        self.steps.grid(row=1, column=0, rowspan=2, sticky="nsew", pady=(10, 0))
        self._build_discover(ttk.Frame(self.steps, padding=10))
        self._build_identify(ttk.Frame(self.steps, padding=10))
        rom_tab = ttk.Frame(self.steps, padding=(4, 8))
        self.steps.add(rom_tab, text="3. Range of motion")
        rom_tab.columnconfigure(0, weight=1)
        rom_tab.rowconfigure(2, weight=1)

        maprow = ttk.LabelFrame(rom_tab, text="Hardware map", padding=(10, 6))
        maprow.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        maprow.columnconfigure(1, weight=1)
        ttk.Button(maprow, text="Open map...", command=self.browse_map).grid(row=0, column=0, sticky="w")
        ttk.Label(maprow, textvariable=self.map_var, style="Muted.TLabel").grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(maprow, text="Operator").grid(row=0, column=2, sticky="e")
        ttk.Entry(maprow, textvariable=self.operator_var, width=14).grid(row=0, column=3, padx=6)
        self.save_btn = ttk.Button(maprow, text="Save ROM to map", command=self.save)
        self.save_btn.grid(row=0, column=4)
        self.no_rows = ttk.Label(rom_tab, text=NO_ROWS_TEXT, style="Big.TLabel")
        self.no_rows.grid(row=1, column=0, sticky="w", padx=6, pady=(0, 6))

        # channel rows (scrollable, so they stay usable in a small tiled window)
        box = ttk.LabelFrame(rom_tab, text="Servos", padding=(6, 4))
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
        self._refresh_identify()
        outer.bind("<Configure>", lambda e: self.status_label.configure(wraplength=max(e.width - 24, 200)))

    def _build_discover(self, tab: ttk.Frame) -> None:
        self.steps.add(tab, text="1. Discover")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew")
        self.discover_btn = ttk.Button(bar, text="Discover", style="Accent.TButton", command=self.discover)
        self.discover_btn.pack(side="left")
        ttk.Checkbutton(bar, text="Watch buttons (press them now)", variable=self.watch_var,
                        command=self.toggle_watch).pack(side="left", padx=12)
        ttk.Label(tab, textvariable=self.survey_var, wraplength=900).grid(row=1, column=0, sticky="w", pady=8)
        self.pin_table = ttk.Treeview(tab, columns=("pin", "class", "adc", "note"), show="headings", height=12)
        for column, title, width in (("pin", "Pin", 70), ("class", "What the survey saw", 330),
                                     ("adc", "ADC mV", 80), ("note", "Note", 380)):
            self.pin_table.heading(column, text=title, anchor="w")
            self.pin_table.column(column, width=width, anchor="w", stretch=column == "note")
        self.pin_table.grid(row=2, column=0, sticky="nsew")

    def _build_identify(self, tab: ttk.Frame) -> None:
        self.steps.add(tab, text="2. Identify")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(5, weight=1)
        ttk.Label(tab, textvariable=self.target_var, style="Big.TLabel", wraplength=900).grid(
            row=0, column=0, sticky="w")
        values = ttk.Frame(tab)
        values.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(values, text="Start").pack(side="left")
        ttk.Spinbox(values, from_=ABS_MIN_US, to=ABS_MAX_US, increment=10, width=7,
                    textvariable=self.start_var).pack(side="left", padx=4)
        ttk.Label(values, text="us   wiggle +/-").pack(side="left")
        ttk.Spinbox(values, from_=10, to=400, increment=10, width=5, textvariable=self.delta_var).pack(
            side="left", padx=4)
        ttk.Label(values, text="us").pack(side="left")
        self.start_var.trace_add("write", lambda *_: self._refresh_identify())
        self.delta_var.trace_add("write", lambda *_: self._refresh_identify())

        actions = ttk.Frame(tab)
        actions.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.wiggle_btn = ttk.Button(actions, text="Wiggle as servo", style="Accent.TButton", command=self.do_wiggle)
        self.blink_btn = ttk.Button(actions, text="Blink", command=self.do_blink)
        self.skip_btn = ttk.Button(actions, text="Skip", command=self.skip_target)
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop_identify)
        for widget in (self.wiggle_btn, self.blink_btn, self.skip_btn, self.stop_btn):
            widget.pack(side="left", padx=(0, 6))

        self.answer_row = ttk.Frame(tab)
        self.answer_row.grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Label(self.answer_row, text="What happened?", style="Big.TLabel").pack(side="left", padx=(0, 8))
        for text, kind in (("Servo moved", "servo"), ("Light / sound", "led"), ("Nothing", "unused"),
                           ("Try again", "again")):
            ttk.Button(self.answer_row, text=text, command=lambda k=kind: self.answer(k)).pack(side="left", padx=(0, 6))
        self.name_row = ttk.Frame(tab)
        self.name_row.grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Label(self.name_row, text="What is it?", style="Big.TLabel").pack(side="left", padx=(0, 8))
        self.name_entry = ttk.Entry(self.name_row, textvariable=self.name_var, width=24)
        self.name_entry.pack(side="left")
        self.name_entry.bind("<Return>", lambda e: self.save_name())
        ttk.Button(self.name_row, text="Save", style="Accent.TButton", command=self.save_name).pack(side="left", padx=6)

        self.found_table = ttk.Treeview(tab, columns=("target", "what", "name"), show="headings", height=8)
        for column, title, width in (("target", "Pin / channel", 160), ("what", "What", 140), ("name", "Name", 300)):
            self.found_table.heading(column, text=title, anchor="w")
            self.found_table.column(column, width=width, anchor="w", stretch=column == "name")
        self.found_table.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        bottom = ttk.Frame(tab)
        bottom.grid(row=6, column=0, sticky="w", pady=(8, 0))
        self.save_map_btn = ttk.Button(bottom, text="Save hardware map", command=self.save_found_map)
        self.save_map_btn.pack(side="left")
        self.to_rom_btn = ttk.Button(bottom, text="Go to range of motion", style="Accent.TButton",
                                     command=self.go_to_rom)
        self.to_rom_btn.pack(side="left", padx=6)

    def _set_channels(self, roms: list[RomChannel]) -> None:
        for row in self.rows.values():
            row["frame"].destroy()
        self.roms = {rom.channel: rom for rom in roms}
        self.rows = {}
        for index, rom in enumerate(roms):
            self.rows[rom.channel] = self._build_row(index, rom)
        if roms:
            self.no_rows.grid_remove()
        else:
            self.no_rows.grid()
        self._refresh_all()

    def _build_row(self, index: int, rom: RomChannel) -> dict[str, Any]:
        title = rom.label if rom.label == channel_name(rom.channel) else f"{rom.label}  ({channel_name(rom.channel)})"
        frame = ttk.LabelFrame(self.rows_frame, text=title, padding=(10, 6))
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
        return self.controller is not None and self.controller.armed

    def _rom_started(self) -> bool:
        return self.armed and self.session is not None and self.session.started

    def _enabled(self, channel: int) -> bool:
        return self._rom_started() and channel in self.session.enabled

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
        if hasattr(self, "wiggle_btn"):
            self._refresh_identify()

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
        row["enable"].state(["!disabled"] if self._rom_started() and not enabled else ["disabled"])
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

    def _port(self) -> str | None:
        port = self.port_var.get().strip()
        if not port:
            self._say("Pick a serial port (or the built-in emulator).", error=True)
            return None
        return EMULATOR_PORT if port == EMULATOR_LABEL else port

    def _ensure_connected(self) -> bool:
        if self.controller is not None:
            return True
        port = self._port()
        if port is None:
            return False
        try:
            self.controller = self.connect_fn(port)
        except Exception as error:  # noqa: BLE001 - shown to the operator
            self._say(f"Could not connect: {error}", error=True)
            return False
        return True

    def connect_and_arm(self) -> None:
        if self.armed:
            return
        if not self.cutoff_var.get():
            self._say("Confirm that a physical power cutoff is within reach first.", error=True)
            return
        if not self._ensure_connected():
            return
        try:
            # Heartbeats come from the window's own timer: if the window freezes,
            # the firmware watchdog releases everything.
            if self.roms:
                self.session = RomSession(self.controller, self.roms.values(), host_slew_us_s=self.host_slew_us_s,
                                          heartbeat="manual")
                self.session.start(power_cutoff_confirmed=True)
            else:
                self.session = None
                self.controller.arm(power_cutoff_confirmed=True, heartbeat="manual")
        except Exception as error:  # noqa: BLE001 - shown to the operator
            self._drop_connection()
            self._say(f"Could not arm: {error}", error=True)
            self._refresh_all()
            return
        if self.roms:
            self._say("Armed. No servo outputs anything yet: check each start value, then press Enable.")
        else:
            self._say("Armed. Nothing outputs yet. In step 2, wiggle or blink one pin at a time.")
        self._refresh_all()

    # ------------------------------------------------------------ 1. discover
    def discover(self) -> None:
        if not self._ensure_connected():
            return
        try:
            self.survey = find.discover(self.controller)
        except Exception as error:  # noqa: BLE001
            self._say(f"Discover failed: {error}", error=True)
            return
        self.survey_var.set(self.survey.summary())
        self._refresh_pin_table()
        self._refresh_identify()
        self._say("Discovered (nothing was driven). Tick 'Watch buttons' and press any buttons, "
                  "or confirm the power cutoff, Connect & arm, and go to step 2.")

    def _refresh_pin_table(self) -> None:
        table = self.pin_table
        table.delete(*table.get_children())
        if self.survey is None:
            return
        i2c_notes: dict[int, str] = {}
        for (sda, scl), addresses in self.survey.i2c.items():
            text = f"I2C {', '.join(addresses)} (SDA {sda} / SCL {scl})"
            i2c_notes[sda] = i2c_notes[scl] = text
        if self.survey.pca_bus:
            table.insert("", "end", values=(f"{self.survey.pca_bus[0]}, {self.survey.pca_bus[1]}", "I2C bus in use",
                                            "", i2c_notes.get(self.survey.pca_bus[0], "PCA9685")))
        for info in self.survey.pins:
            notes = []
            if info.pin in self.pressed:
                notes.append("BUTTON: changed while watching")
            if info.pin in i2c_notes:
                notes.append(i2c_notes[info.pin])
            if info.last:
                notes.append("boot strapping / JTAG: probed last")
            if info.input_only:
                notes.append("input only")
            table.insert("", "end", values=(f"GPIO {info.pin}", CLASS_TEXT.get(info.cls, info.cls),
                                            "" if info.adc_mv is None else info.adc_mv, "; ".join(notes)))

    def toggle_watch(self) -> None:
        on = self.watch_var.get()
        if on and not self._ensure_connected():
            self.watch_var.set(False)
            return
        try:
            if self.controller is not None:
                self.controller.watch(on)
        except Exception as error:  # noqa: BLE001
            self.watch_var.set(False)
            self._say(f"Watch failed: {error}", error=True)
            return
        self._say("Watching: press each button now; the pins that change are listed." if on else "Watch off.")

    def poll_watch(self) -> None:
        if not self.watch_var.get() or self.controller is None:
            return
        try:
            events = self.controller.events()
        except Exception as error:  # noqa: BLE001
            self.watch_var.set(False)
            self._say(f"Watch stopped: {error}", error=True)
            return
        new = {pin for pin, _ in events} - self.pressed
        if new:
            self.pressed |= new
            self._refresh_pin_table()
            self._refresh_identify()
            self._say("Pin(s) changed: " + ", ".join(f"GPIO {pin}" for pin in sorted(new))
                      + ". Name them in step 2.")

    # ------------------------------------------------------------ 2. identify
    def _identify_targets(self) -> list[find.Target]:
        buttons = [find.Target(pin, cls="button") for pin in sorted(self.pressed)]
        if self.survey is None:
            return buttons
        return buttons + [t for t in find.targets(self.survey) if t.pin not in self.pressed]

    def current_target(self) -> find.Target | None:
        return next((t for t in self._identify_targets() if t.key not in self.found and t.key not in self.skipped),
                    None)

    def _wiggle_values(self) -> tuple[int, int] | None:
        try:
            return int(self.start_var.get()), int(self.delta_var.get())
        except (ValueError, tk.TclError):
            return None

    def _refresh_identify(self) -> None:
        target = self.current_target()
        values = self._wiggle_values()
        start, delta = values if values else ("?", "?")
        busy = self.wiggle is not None
        if self.survey is None and not self.pressed:
            text = "Discover first (step 1)."
        elif target is None:
            text = "All pins done. Save the hardware map, then go to range of motion."
        elif target.cls == "button":
            text = f"{target.key} changed while you pressed buttons. What is it?"
        else:
            text = (f"{target.key}: it will go to {start} us, then wiggle +/-{delta} us slowly. "
                    "Make sure it's clear." + ("  (boot-strapping pin)" if target.last else ""))
        self.target_var.set(text)
        can_probe = target is not None and target.cls != "button" and self.armed and not busy
        self.wiggle_btn.state(["!disabled"] if can_probe else ["disabled"])
        self.blink_btn.state(["!disabled"] if can_probe and target.pin is not None else ["disabled"])
        self.skip_btn.state(["!disabled"] if target is not None and not busy else ["disabled"])
        self.stop_btn.state(["!disabled"] if target is not None else ["disabled"])
        asking = self.last_action and not busy and not self.pending_kind
        if asking:
            self.answer_row.grid()
        else:
            self.answer_row.grid_remove()
        if self.pending_kind or (target is not None and target.cls == "button"):
            self.name_row.grid()
        else:
            self.name_row.grid_remove()
        self.save_map_btn.state(["!disabled"] if self._named_servos() else ["disabled"])
        self.to_rom_btn.state(["!disabled"] if self._named_servos() else ["disabled"])

    def _named_servos(self) -> bool:
        return any(item.kind == "servo" for item in self.found.values())

    def _refresh_found(self) -> None:
        table = self.found_table
        table.delete(*table.get_children())
        for item in self.found.values():
            what = {"servo": "servo", "led": "light / sound", "button": "button", "unused": "nothing"}[item.kind]
            table.insert("", "end", values=(item.target.key, what, item.name or "-"))

    def _probe_error(self, error: Exception) -> None:
        """Refusals (driven pin, no PWM slot, ...) are messages; anything else stops everything."""
        if isinstance(error, ProtocolError) and "returned error" in str(error):
            self.wiggle = None
            self.last_action = ""
            self._say(f"Refused: {error}", error=True)
            self._refresh_identify()
            return
        self.wiggle = None
        self.last_action = ""
        self._fault_or_say(error)

    def _confirm_strapping(self, target: find.Target) -> bool:
        if not target.last:
            return True
        return messagebox.askyesno(
            "Boot-strapping pin", f"{target.key} is a boot-strapping / JTAG pin. Driving it is safe while the "
            "board runs, but it can matter at the next reset. Probe it?", parent=self.root)

    def do_wiggle(self) -> None:
        target = self.current_target()
        values = self._wiggle_values()
        if target is None or not self.armed or self.wiggle is not None:
            return
        if values is None:
            self._say("Enter the start pulse and the wiggle size in us.", error=True)
            return
        if not self._confirm_strapping(target):
            return
        try:
            self.wiggle = find.Wiggle(self.controller, target, values[0], values[1], self.wiggle_slew_us_s)
            self.wiggle.begin()
        except ValueError as error:
            self.wiggle = None
            self._say(str(error), error=True)
            return
        except Exception as error:  # noqa: BLE001
            self._probe_error(error)
            return
        self.last_action = "wiggle"
        self._say(f"{target.key}: wiggling slowly. Watch the robot.")
        self._refresh_identify()

    def do_blink(self) -> None:
        target = self.current_target()
        if target is None or target.pin is None or not self.armed or self.wiggle is not None:
            return
        if not self._confirm_strapping(target):
            return
        try:
            find.blink(self.controller, target)
        except Exception as error:  # noqa: BLE001
            self._probe_error(error)
            return
        self.last_action = "blink"
        self._say(f"{target.key}: blinking 3 times (on/off, 2 Hz).")
        self._refresh_identify()

    def answer(self, kind: str) -> None:
        target = self.current_target()
        if target is None:
            return
        if kind == "again":
            (self.do_blink if self.last_action == "blink" else self.do_wiggle)()
            return
        if kind == "unused":
            self.found[target.key] = find.Found(target, "unused")
            self._next_target()
            return
        self.pending_kind = kind
        self.name_var.set("")
        self._refresh_identify()
        self.name_entry.focus_set()

    def save_name(self) -> None:
        target = self.current_target()
        name = self.name_var.get().strip()
        if target is None:
            return
        if not name:
            self._say("Type what it is (e.g. shoulder, status LED).", error=True)
            return
        kind = "button" if target.cls == "button" else self.pending_kind
        if not kind:
            return
        start = self._wiggle_values()[0] if kind == "servo" and self._wiggle_values() else None
        self.found[target.key] = find.Found(target, kind, name, start)
        self._next_target()

    def skip_target(self) -> None:
        target = self.current_target()
        if target is not None and self.wiggle is None:
            self.skipped.add(target.key)
            self._next_target()

    def _next_target(self) -> None:
        self.last_action = ""
        self.pending_kind = ""
        self.name_var.set("")
        self._refresh_found()
        self._refresh_identify()
        target = self.current_target()
        self._say(f"Next: {target.key}." if target else "All pins done. Save the hardware map.")

    def stop_identify(self) -> None:
        """End the walk through the pins. A wiggle in progress is stopped (all outputs released)."""
        if self.wiggle is not None and self.controller is not None:
            self.wiggle = None
            try:
                self.controller.stop()
            except Exception as error:  # noqa: BLE001
                self._say(f"STOP not confirmed ({error}). CUT POWER NOW.", error=True)
                self._drop_connection()
        target = self.current_target()
        while target is not None:
            self.skipped.add(target.key)
            target = self.current_target()
        self._next_target()
        self._refresh_all()

    def _found_map(self) -> dict[str, Any]:
        survey = self.survey or find.Survey("", "?", [])
        return find.build_map(self.controller.info if self.controller else {}, survey, list(self.found.values()))

    def save_found_map(self, path: Path | None = None) -> Path | None:
        try:
            hardware_map = self._found_map()
        except Exception as error:  # noqa: BLE001
            self._say(str(error), error=True)
            return None
        if path is None:
            chosen = filedialog.asksaveasfilename(title="Save hardware map", defaultextension=".json",
                                                  initialfile="robot.map.json",
                                                  filetypes=[("Hardware map", "*.json")])
            if not chosen:
                return None
            path = Path(chosen)
        try:
            save_map(hardware_map, path, overwrite=True)
        except Exception as error:  # noqa: BLE001
            self._say(f"Save failed: {error}", error=True)
            return None
        self.hardware_map, self.map_path, self._found_path = hardware_map, path, path
        self.map_var.set(str(path))
        self._say(f"Saved {len(hardware_map['joints'])} servo(s) and {len(hardware_map['devices'])} other part(s) "
                  f"to {path}.")
        return path

    def go_to_rom(self, path: Path | None = None) -> None:
        """Save the map, release everything and show only the named servos for range of motion."""
        if self.wiggle is not None:
            return
        target = path or self._found_path
        if self.save_found_map(target) is None:
            return
        if self.armed:
            self.disarm()
        self.load_map_file(self._found_path)
        self.steps.select(2)
        self._say("All outputs released. Connect & arm, then Enable each servo: it starts where the wiggle "
                  "left it.")

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
        self.wiggle = None
        self.last_action = ""
        try:
            if self.session is not None and self.session.started:
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
        self.wiggle = None
        self.last_action = ""
        if self.session is not None or self.armed:
            try:
                if self.session is not None:
                    self.session.close()
                if self.armed:
                    self.controller.disarm()
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
        self.wiggle = None
        self.last_action = ""
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
        self._watch_ticks += 1
        if self._watch_ticks % 4 == 0:
            self.poll_watch()
        if not self.armed:
            return
        try:
            moved = self.session.step(dt_s) if self._rom_started() else {}
            if self.wiggle is not None:
                if self.wiggle.step():
                    self.wiggle = None
                    self._say("What happened? Pick one below.")
                    self._refresh_identify()
            elif not self._rom_started():
                self.controller.heartbeat()
        except Exception as error:  # noqa: BLE001
            if self.wiggle is not None:
                self._probe_error(error)
            else:
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
            moving = self.wiggle is not None or (self._rom_started() and bool(self.session.moving()))
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
            if self.watch_var.get() and self.controller is not None:
                self.controller.watch(False)
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
