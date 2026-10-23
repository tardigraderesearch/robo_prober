"""The robo-prober window (tkinter + ttk), in four steps:

1. Discover: read-only survey of the controller's pins and I2C bus, and a
   button watch.
2. Identify: one pin (or PCA9685 channel) at a time, wiggle it as a servo
   or blink it, say what happened and name it; save the hardware map.
3. Bench: drag the identified servos (tiles on the right) onto the bench and
   move them together or one by one, each inside its start +/-300 us; pick
   each one's start pulse for step 4.
4. Range of motion: one row per named servo: a confirmed start pulse with an
   Enable button, a slider the servo follows slowly, "Set ROM 1" and
   "Set ROM 2", Clear and Widen.

The connection bar and the E-STOP stay visible above all four. All logic
lives in find.py, rom.py and api.py; this file only wires widgets to them.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from datetime import datetime
import re
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from . import __version__, find, flasher, servo_db, theme
from .api import EMULATOR_PORT, Controller, connect, discover, load_map, save_map
from .transport import FirmwareOutdated, ProtocolError
from .rom import (
    ABS_MAX_US,
    ABS_MIN_US,
    DEFAULT_BAND_HALF_US,
    DEFAULT_FIRMWARE_SLEW_US_S,
    DEFAULT_HOST_SLEW_US_S,
    RomChannel,
    RomError,
    RomSession,
    apply_rom_to_map,
    channel_name,
    channels_from_map,
    faulty_reason,
    faulty_servos,
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
KIND_TEXT = {"servo": "servo", "faulty": "FAULTY servo (one way only)", "led": "light / sound", "button": "button",
             "unused": "nothing"}
SOURCE_TEXT = {"last_position": "last saved position", "rom_midpoint": "middle of the saved range",
               "operator": "your value", "none": "no saved position - check this value"}


class RomApp:
    def __init__(self, root: tk.Tk, *, map_path: Path | None = None, out_path: Path | None = None,
                 port: str | None = None, channels: list[int] | None = None, operator: str = "",
                 band_half_us: int = DEFAULT_BAND_HALF_US, host_slew_us_s: int = DEFAULT_HOST_SLEW_US_S,
                 connect_fn: Callable[..., Controller] = connect, run_timer: bool = True,
                 firmware_check_fn: Callable[[str], flasher.FirmwareStatus] | None = None,
                 install_fn: Callable[..., flasher.InstallResult] | None = None,
                 restore_fn: Callable[..., Any] | None = None,
                 identify_fn: Callable[[str], flasher.ChipInfo] | None = None,
                 backup_dir: Path | None = None, run_async: bool = True):
        self.root = root
        root.title(f"robo-prober {__version__}")
        root.minsize(560, 420)
        root.geometry("1000x800")
        self.connect_fn = connect_fn
        # Firmware: never rely on what is on the board. A real serial port is
        # checked with HELLO first; anything else gets the backup-and-install offer.
        real = connect_fn is connect
        self.firmware_check_fn = firmware_check_fn or (flasher.check_firmware if real else None)
        self.install_fn = install_fn or flasher.install
        self.restore_fn = restore_fn or flasher.restore
        self.identify_fn = identify_fn or flasher.identify
        self.backup_dir = Path(backup_dir) if backup_dir else flasher.BACKUP_DIR
        self.run_async = run_async
        self.verified_ports: set[str] = set()
        self.flashing = False
        self._just_installed: str | None = None
        self._jobs: queue.Queue = queue.Queue()
        self.ask_fn: Callable[[str, str, list[str]], str | None] = self._ask
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
        self.focus_key: str | None = None  # a target picked by Back or from a table (redo it)
        self.pressed: set[int] = set()
        self.wiggle: find.Wiggle | None = None
        self.last_action = ""
        self.pending_kind = ""
        self._faulty_disarmed = ""
        self._watch_ticks = 0
        self._found_path: Path | None = None
        self.wiggle_slew_us_s = find.DEFAULT_WIGGLE_SLEW_US_S
        # 3. bench: servos (found keys) the operator put on the bench
        self.bench: list[str] = []
        self.bench_roms: dict[str, RomChannel] = {}   # each servo's start and allowed band (before step 4)
        self.bench_rows: dict[str, dict[str, Any]] = {}
        self.bench_tiles: dict[str, dict[str, Any]] = {}
        self.bench_invert: dict[str, bool] = {}
        self.bench_pending: dict[int, int] = {}       # channel -> target, sent by the next tick
        self.bench_buzz_queue: list[str] = []
        self._bench_buzz: str | None = None
        self._drag: dict[str, Any] | None = None
        self._ghost: tk.Label | None = None
        self._group_last = 0.0

        self.port_var = tk.StringVar(value=EMULATOR_LABEL if port == EMULATOR_PORT else (port or ""))
        self.cutoff_var = tk.BooleanVar(value=False)
        self.operator_var = tk.StringVar(value=operator)
        self.map_var = tk.StringVar(value="No hardware map loaded (channels 0-15)")
        self.status = tk.StringVar(value="Pick a port and press Discover (nothing moves), or open a saved map.")
        self.watch_var = tk.BooleanVar(value=False)
        self.start_var = tk.StringVar(value=str(find.DEFAULT_START_US))
        self.delta_var = tk.StringVar(value=str(find.DEFAULT_DELTA_US))
        self.name_var = tk.StringVar()
        self.servo_var = tk.StringVar()                       # servo type: type-ahead text
        self.servo_choice: dict[str, Any] | None = None       # the servo_db entry picked for this target
        self._servo_hits: list[dict[str, Any]] = []
        self.identifier: servo_db.Identifier | None = None    # "Don't know? Identify it" dialog state
        self.ident_dialog: tk.Toplevel | None = None
        self.ident_current: servo_db.Question | None = None
        self.survey_var = tk.StringVar(value="Not discovered yet. Press Discover: it only reads the pins.")
        self.target_var = tk.StringVar(value="Discover first.")
        self.group_var = tk.DoubleVar(value=0)

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
        self.restore_btn = ttk.Button(top, text="Restore original firmware", command=self.restore_firmware)
        self.restore_btn.grid(row=1, column=2, sticky="e", padx=(0, 12), pady=(6, 0))
        self.update_btn = ttk.Button(top, text="Update firmware", command=self.update_firmware)
        self.update_btn.grid(row=1, column=1, sticky="e", padx=(0, 6), pady=(6, 0))
        self.flash_bar = ttk.Progressbar(top, mode="determinate", maximum=100)
        self.flash_bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.flash_bar.grid_remove()
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

        # steps: 1 discover, 2 identify, 3 bench, 4 range of motion
        self.steps = ttk.Notebook(outer)
        self.steps.grid(row=1, column=0, rowspan=2, sticky="nsew", pady=(10, 0))
        self._build_discover(ttk.Frame(self.steps, padding=10))
        self._build_identify(ttk.Frame(self.steps, padding=10))
        self._build_bench(ttk.Frame(self.steps, padding=10))
        self.steps.bind("<<NotebookTabChanged>>", lambda e: self._refresh_bench())
        rom_tab = ttk.Frame(self.steps, padding=(4, 8))
        self.steps.add(rom_tab, text="4. Range of motion")
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
        self.faulty_rows = ttk.Label(rom_tab, style="Error.TLabel", wraplength=900, justify="left")
        self.faulty_rows.grid(row=3, column=0, sticky="w", padx=6, pady=(6, 0))
        self.faulty_rows.grid_remove()

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
        root.bind_all("<MouseWheel>", lambda e: self._wheel_canvas().yview_scroll(-1 if e.delta > 0 else 1, "units"))
        root.bind_all("<Button-4>", lambda e: self._wheel_canvas().yview_scroll(-1, "units"))
        root.bind_all("<Button-5>", lambda e: self._wheel_canvas().yview_scroll(1, "units"))

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
        self.back_btn = ttk.Button(actions, text="Back", command=self.back_target)
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop_identify)
        self.zero_btn = ttk.Button(actions, text="Set all servos to 0", command=self.zero_all_servos)
        for widget in (self.wiggle_btn, self.blink_btn, self.skip_btn, self.back_btn, self.stop_btn, self.zero_btn):
            widget.pack(side="left", padx=(0, 6))
        self.auto_next_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(actions, text="Buzz the next pin after No", variable=self.auto_next_var).pack(side="left", padx=(12, 0))
        ttk.Label(actions, text="Keys: Space buzz · Y servo · O one way only · L light · N no · S skip · B back",
                  style="Muted.TLabel").pack(side="left", padx=(12, 0))

        def key(action):
            def handler(event):
                # typing in a field (name, servo type, start) never answers
                if self.steps.index("current") != 1 or isinstance(event.widget, (ttk.Entry, tk.Entry)):
                    return None
                action()
                return "break"
            return handler
        for keys, action in (("<space>", self.do_wiggle), ("yY", lambda: self.answer("servo")),
                             ("oO", lambda: self.answer("faulty")),
                             ("lL", lambda: self.answer("led")), ("nN", lambda: self.answer("unused")),
                             ("sS", self.skip_target), ("bB", self.back_target)):
            for k in ([keys] if keys.startswith("<") else list(keys)):
                self.root.bind(k, key(action))

        self.answer_row = ttk.Frame(tab)
        self.answer_row.grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Label(self.answer_row, text="What happened?", style="Big.TLabel").pack(side="left", padx=(0, 8))
        for text, kind in (("Servo moved", "servo"), ("Moved one way only / spins", "faulty"),
                           ("Light / sound", "led"), ("Nothing", "unused"), ("Try again", "again")):
            ttk.Button(self.answer_row, text=text, command=lambda k=kind: self.answer(k)).pack(side="left", padx=(0, 6))
        self.name_row = ttk.Frame(tab)
        self.name_row.grid(row=4, column=0, sticky="w", pady=(8, 0))
        line = ttk.Frame(self.name_row)
        line.pack(anchor="w")
        ttk.Label(line, text="What is it?", style="Big.TLabel").pack(side="left", padx=(0, 8))
        self.name_entry = ttk.Entry(line, textvariable=self.name_var, width=24)
        self.name_entry.pack(side="left")
        self.name_entry.bind("<Return>", lambda e: self.save_name())
        ttk.Button(line, text="Save", style="Accent.TButton", command=self.save_name).pack(side="left", padx=6)
        # after Y: the servo's type (optional): sets its starting ROM band
        self.type_line = ttk.Frame(self.name_row)
        ttk.Label(self.type_line, text="Servo type (optional)").pack(side="left", padx=(0, 8))
        self.servo_box = ttk.Combobox(self.type_line, textvariable=self.servo_var, width=34)
        self.servo_box.pack(side="left")
        self.servo_box.bind("<KeyRelease>", lambda e: self._servo_typed(e))
        self.servo_box.bind("<<ComboboxSelected>>", lambda e: self._servo_selected())
        self.servo_box.bind("<Return>", lambda e: self.save_name())
        ttk.Button(self.type_line, text="Don't know? Identify it",
                   command=self.open_servo_identifier).pack(side="left", padx=6)
        self.servo_info = ttk.Label(self.type_line, style="Muted.TLabel")
        self.servo_info.pack(side="left", padx=(6, 0))

        self.found_table = ttk.Treeview(tab, columns=("target", "what", "name"), show="headings", height=8)
        for column, title, width in (("target", "Pin / channel", 160), ("what", "What", 140), ("name", "Name", 300)):
            self.found_table.heading(column, text=title, anchor="w")
            self.found_table.column(column, width=width, anchor="w", stretch=column == "name")
        self.found_table.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        # click (or double-click) a row: jump to that pin and redo it (Discover's table: double-click)
        self.found_table.bind("<ButtonRelease-1>", lambda e: self._table_clicked(self.found_table, e))
        for table in (self.found_table, self.pin_table):
            table.bind("<Double-1>", lambda e, t=table: self._table_clicked(t, e))
        bottom = ttk.Frame(tab)
        bottom.grid(row=6, column=0, sticky="w", pady=(8, 0))
        self.save_map_btn = ttk.Button(bottom, text="Save hardware map", command=self.save_found_map)
        self.save_map_btn.pack(side="left")
        self.to_rom_btn = ttk.Button(bottom, text="Go to range of motion", style="Accent.TButton",
                                     command=self.go_to_rom)
        self.to_rom_btn.pack(side="left", padx=6)
        self.to_bench_btn = ttk.Button(bottom, text="Go to bench", command=lambda: self.steps.select(2))
        self.to_bench_btn.pack(side="left")

    def _build_bench(self, tab: ttk.Frame) -> None:
        self.steps.add(tab, text="3. Bench")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        ttk.Label(tab, text="Drag servos from the right onto the bench (or click them). Each one moves only "
                            "inside its start +/-300 us (or its servo type's safe band), slowly.", style="Muted.TLabel", wraplength=900).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.bench_zone = ttk.LabelFrame(tab, text="Bench", padding=(10, 6))
        self.bench_zone.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        self.bench_zone.columnconfigure(0, weight=1)
        self.bench_zone.rowconfigure(2, weight=1)
        group = ttk.Frame(self.bench_zone)
        group.grid(row=0, column=0, sticky="ew")
        group.columnconfigure(0, weight=1)
        self.group_scale = tk.Scale(group, from_=-DEFAULT_BAND_HALF_US, to=DEFAULT_BAND_HALF_US, orient="horizontal",
                                    resolution=1, showvalue=True, variable=self.group_var,
                                    label="All together: us from start (Invert flips a servo)",
                                    background=theme.BG, foreground=theme.TEXT, troughcolor=theme.SURFACE_HI,
                                    activebackground=theme.ACCENT, highlightthickness=0, borderwidth=0,
                                    sliderrelief="flat", font=theme.FONT)
        self.group_scale.grid(row=0, column=0, sticky="ew")
        self.group_var.trace_add("write", lambda *_: self._group_moved())
        bar = ttk.Frame(group)
        bar.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.bench_power_btn = ttk.Button(bar, text="Power up", style="Accent.TButton",
                                          command=lambda: self.bench_power_up())
        self.bench_buzz_all_btn = ttk.Button(bar, text="Buzz all", command=self.bench_buzz_all)
        self.bench_zero_btn = ttk.Button(bar, text="Back to 0", command=self.bench_back_to_zero)
        for widget in (self.bench_power_btn, self.bench_buzz_all_btn, self.bench_zero_btn):
            widget.pack(side="left", padx=(0, 6))
        self.bench_empty = ttk.Label(self.bench_zone, text="Drop servos here (or click a tile on the right).",
                                     style="Big.TLabel")
        self.bench_empty.grid(row=1, column=0, sticky="w", pady=(10, 0))

        holder = ttk.Frame(self.bench_zone)
        holder.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        self.bench_canvas = tk.Canvas(holder, highlightthickness=0, borderwidth=0, background=theme.BG)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.bench_canvas.yview)
        self.bench_canvas.configure(yscrollcommand=scroll.set)
        self.bench_canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.bench_frame = ttk.Frame(self.bench_canvas)
        self.bench_frame.columnconfigure(0, weight=1)
        window = self.bench_canvas.create_window((0, 0), window=self.bench_frame, anchor="nw")
        self.bench_frame.bind("<Configure>", lambda e: self.bench_canvas.configure(
            scrollregion=self.bench_canvas.bbox("all")))
        self.bench_canvas.bind("<Configure>", lambda e: self.bench_canvas.itemconfigure(window, width=e.width))

        self.tiles_box = ttk.LabelFrame(tab, text="Servos", padding=(8, 6))
        self.tiles_box.grid(row=1, column=1, sticky="nsew")
        self.no_tiles = ttk.Label(self.tiles_box, text="No servos yet:\nidentify them in step 2.",
                                  style="Muted.TLabel", justify="left")
        self.no_tiles.pack(anchor="w")
        bottom = ttk.Frame(tab)
        bottom.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.bench_to_rom_btn = ttk.Button(bottom, text="Go to range of motion", style="Accent.TButton",
                                           command=self.go_to_rom)
        self.bench_to_rom_btn.pack(side="left")

    def _wheel_canvas(self) -> tk.Canvas:
        return self.bench_canvas if self.steps.index("current") == 2 else self.canvas

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
        zero = rom.start_us if rom.start_us is not None else rom.center
        var = tk.DoubleVar(value=0)  # the slider reads 0 at the start position; left/right are -/+ us
        scale = tk.Scale(frame, from_=low - zero, to=high - zero, orient="horizontal", resolution=1, showvalue=True,
                         variable=var, label=f"us from start ({zero} us)",
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
        narrow = ttk.Button(actions, text="Narrow range", command=lambda ch=rom.channel: self.narrow(ch))
        for widget in (rom1, rom2, clear, widen, narrow):
            widget.pack(side="left", padx=(0, 6))
        row = {"frame": frame, "var": var, "scale": scale, "strip": strip, "info": info, "start_var": start_var,
               "start_entry": start_entry, "enable": enable, "warning": warning, "zero": zero,
               "rom1": rom1, "rom2": rom2, "clear": clear, "widen": widen, "narrow": narrow}
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
        self._refresh_bench()

    def _refresh_row(self, channel: int) -> None:
        if channel not in self.rows:
            return
        row, rom = self.rows[channel], self.roms[channel]
        low, high = rom.slider_range()
        zero = row["zero"]
        if int(float(row["scale"].cget("from"))) != low - zero or int(float(row["scale"].cget("to"))) != high - zero:
            row["scale"].configure(from_=low - zero, to=high - zero)
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
        row["narrow"].state(["!disabled"] if rom.can_narrow() else ["disabled"])
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
        faulty = faulty_servos(hardware_map)
        if faulty:
            self.faulty_rows.configure(text="Not offered: " + " ".join(
                faulty_reason(name, ch, note) for ch, (name, note) in sorted(faulty.items())))
            self.faulty_rows.grid()
        else:
            self.faulty_rows.grid_remove()
        self._say(f"Loaded {len(roms)} channel(s) from {path.name}."
                  + (f" Skipped {len(faulty)} faulty servo(s)." if faulty else ""))

    def _port(self) -> str | None:
        port = self.port_var.get().strip()
        if not port:
            self._say("Pick a serial port (or the built-in emulator).", error=True)
            return None
        return EMULATOR_PORT if port == EMULATOR_LABEL else port

    # ------------------------------------------------------------ firmware
    def _ask(self, title: str, text: str, choices: list[str]) -> str | None:
        """A plain dialog with one button per choice; returns the choice or None."""
        result: dict[str, str | None] = {"choice": None}
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=text, wraplength=520, justify="left").pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(anchor="e", pady=(14, 0))
        for index, choice in enumerate(choices):
            def pick(c=choice):
                result["choice"] = None if c == "Cancel" else c
                dialog.destroy()
            ttk.Button(row, text=choice, style="Accent.TButton" if index == 0 else "TButton",
                       command=pick).pack(side="left", padx=(6, 0))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        self.root.wait_window(dialog)
        return result["choice"]

    def _run_job(self, work: Callable[[Callable[[str, int, int], None]], Any],
                 done: Callable[[Any], None], failed: Callable[[Exception], None]) -> None:
        """Run a flashing job off the Tk thread; progress and results come back via a queue."""
        self.flashing = True
        self.flash_bar.grid()
        self.flash_bar["value"] = 0

        def progress(stage: str, current: int, total: int) -> None:
            self._jobs.put(("progress", stage, current, total))

        def worker() -> None:
            try:
                self._jobs.put(("done", work(progress)))
            except Exception as error:  # noqa: BLE001 - shown to the operator
                self._jobs.put(("failed", error))

        self._job_handlers = (done, failed)
        if self.run_async:
            threading.Thread(target=worker, daemon=True).start()
            self.root.after(100, self._poll_jobs)
        else:
            worker()
            self._poll_jobs()

    def _poll_jobs(self) -> None:
        while True:
            try:
                item = self._jobs.get_nowait()
            except queue.Empty:
                break
            if item[0] == "progress":
                _, stage, current, total = item
                pct = int(100 * current / total) if total else 0
                self.flash_bar["value"] = pct
                self._say(f"{stage}... {pct}%" if total > 1 else f"{stage}...")
                continue
            self.flashing = False
            self.flash_bar.grid_remove()
            done, failed = self._job_handlers
            if item[0] == "done":
                done(item[1])
            else:
                failed(item[1])
            return
        if self.flashing and self.run_async and not self._closed:
            self.root.after(100, self._poll_jobs)

    def _firmware_ready(self, port: str, then: Callable[[], None] | None = None) -> bool:
        """True when the board on ``port`` is known to run current robo-prober firmware (or needs none).

        Otherwise the HELLO check runs off the Tk thread (a silent board takes
        seconds to time out; the window must not freeze) and this returns
        False; when the board answers with current firmware, ``then`` runs
        (the action that asked, retried). Anything else gets the install offer.
        """
        if port == EMULATOR_PORT or port in self.verified_ports or self.firmware_check_fn is None:
            return True
        check = self.firmware_check_fn

        def work(progress):
            progress(f"Checking the firmware on {port} (HELLO, nothing moves)", 0, 1)
            return check(port)

        def done(status: flasher.FirmwareStatus) -> None:
            if status.needs_install:
                self.offer_install(port, status)
                return
            self.verified_ports.add(port)
            if status.state == "sbc":
                self._say(status.message)
            elif status.update_available:  # works as is: no popup, just say an update exists
                self._say(f"{status.message} It works as is; 'Update firmware' installs the new one.")
            if then is not None and not self._closed:
                then()

        def failed(error: Exception) -> None:
            self._say(f"Could not check the firmware: {error}", error=True)

        self._say(f"Checking the firmware on {port} (HELLO, nothing moves)...")
        self._run_job(work, done, failed)
        return False

    def offer_install(self, port: str, status: flasher.FirmwareStatus | None = None) -> None:
        """Ask once; only the explicit click backs up and flashes. Then continue to Discover."""
        if self.flashing:
            return
        if self.armed:
            self._say("Disarm before installing firmware.", error=True)
            return
        lead = status.message if status is not None else "This board isn't running current robo-prober firmware."
        if "isn't running robo-prober" not in lead:
            lead += " It needs the current robo-prober firmware."
        text = (f"{lead} Back up everything on it to {self.backup_dir} and install the robo-prober test "
                "firmware? Unplug servo power first.\n\nThe backup reads the whole flash (about 100 s for 8 MB); "
                "'Restore original firmware' writes it back later.")
        choice = self.ask_fn("Install robo-prober firmware", text,
                             ["Back up and install", "Install without backup", "Cancel"])
        if choice not in ("Back up and install", "Install without backup"):
            self._say("Nothing was flashed. robo-prober needs its own firmware on the board to continue.")
            return
        backup = choice == "Back up and install"
        self._drop_connection()

        def work(progress):
            return self.install_fn(port, out_dir=self.backup_dir, progress=progress, force=True, backup=backup)

        def done(result: flasher.InstallResult) -> None:
            self.verified_ports.add(port)
            self._say(result.message + " Continuing to Discover.")
            self.steps.select(0)
            self._just_installed = port
            try:
                self.discover()
            finally:
                self._just_installed = None

        def failed(error: Exception) -> None:
            self._say(f"Firmware install stopped: {error}", error=True)

        self._run_job(work, done, failed)

    def update_firmware(self) -> None:
        """Install the bundled firmware on request (same choices: back up first, or skip the backup)."""
        port = self._port()
        if port is None:
            return
        if port == EMULATOR_PORT:
            self._say("The built-in emulator has no firmware to update.", error=True)
            return
        self.verified_ports.discard(port)
        self.offer_install(port)

    def restore_firmware(self) -> None:
        if self.flashing:
            return
        port = self._port()
        if port is None:
            return
        if port == EMULATOR_PORT:
            self._say("The built-in emulator has no firmware to restore.", error=True)
            return
        if self.armed:
            self._say("Disarm before restoring firmware.", error=True)
            return
        self._drop_connection()
        try:
            info = self.identify_fn(port)
        except Exception as error:  # noqa: BLE001 - shown to the operator
            self._say(f"Could not identify the board on {port}: {error}", error=True)
            return
        path = flasher.newest_backup(info.mac, self.backup_dir)
        while True:
            if path is None:
                text = (f"No backup for this board ({info.name}, {info.mac}) in {self.backup_dir}. "
                        "Choose a backup file?")
                choice = self.ask_fn("Restore original firmware", text, ["Choose a file...", "Cancel"])
            else:
                text = (f"Write {path} back to this board ({info.name}, {info.mac})? This replaces the "
                        "robo-prober firmware with the original. Unplug servo power first.")
                choice = self.ask_fn("Restore original firmware", text, ["Restore", "Choose a file...", "Cancel"])
            if choice == "Choose a file...":
                picked = filedialog.askopenfilename(title="Backup to restore", initialdir=str(self.backup_dir),
                                                    filetypes=[("Flash backup", "*.bin"), ("All files", "*")])
                path = Path(picked) if picked else path
                continue
            if choice != "Restore":
                self._say("Nothing was restored.")
                return
            break

        def work(progress):
            return self.restore_fn(port, path, progress=progress)

        def done(_result) -> None:
            self.verified_ports.discard(port)
            self._say(f"Restored the original firmware from {path.name}. robo-prober will offer to "
                      "install its own firmware again on the next Connect.")

        def failed(error: Exception) -> None:
            self._say(f"Restore stopped: {error}", error=True)

        self._run_job(work, done, failed)

    def _ensure_connected(self, then: Callable[[], None] | None = None) -> bool:
        """Connect if needed. False while a firmware check runs in the background; ``then`` runs after it."""
        if self.controller is not None:
            return True
        if self.flashing:
            self._say("Wait for the firmware check or job to finish.", error=True)
            return False
        port = self._port()
        if port is None:
            return False
        try:
            if not self._firmware_ready(port, then):
                return False
        except Exception as error:  # noqa: BLE001 - shown to the operator
            self._say(f"Could not check the firmware: {error}", error=True)
            return False
        try:
            self.controller = self.connect_fn(port)
        except Exception as error:  # noqa: BLE001 - shown to the operator
            self._say(f"Could not connect: {error}", error=True)
            return False
        if (getattr(self.controller, "info", None) or {}).get("mode") == "estop":
            self.estop_latched = True  # latched before this window opened
            self._refresh_all()
        return True

    def connect_and_arm(self) -> None:
        if self.armed:
            return
        if not self.cutoff_var.get():
            self._say("Confirm that a physical power cutoff is within reach first.", error=True)
            return
        if not self._ensure_connected(self.connect_and_arm):
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
            if "estop_latched" in str(error) or "e-stop is latched" in str(error):
                # The latch lives on the board and survives a restart of this window:
                # keep the connection so Clear e-stop can reach it.
                self.session = None
                self.estop_latched = True
                self._say("The board's e-stop is still latched. Press Clear e-stop, then Connect & arm.", error=True)
                self._refresh_all()
                return
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
        if not self._ensure_connected(self.discover):
            return
        try:
            self.survey = find.discover(self.controller)
        except FirmwareOutdated as error:
            # robo-prober firmware, but older: same offer as foreign firmware, not an error
            port = self._port()
            if port == self._just_installed:
                self._say(f"The new firmware still reports: {error}", error=True)
                return
            self.verified_ports.discard(port)
            self._drop_connection()
            self.offer_install(port, flasher.FirmwareStatus(
                "outdated", f"This board runs older robo-prober firmware ({error})."))
            return
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
        if on and not self._ensure_connected(lambda: (self.watch_var.set(True), self.toggle_watch())):
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
        targets = self._identify_targets()
        if self.focus_key is not None:
            focused = next((t for t in targets if t.key == self.focus_key), None)
            if focused is not None:
                return focused
            self.focus_key = None
        return next((t for t in targets if t.key not in self.found and t.key not in self.skipped), None)

    def jump_to(self, key: str) -> bool:
        """Make ``key`` the current target; answering it again replaces its old result."""
        if self.wiggle is not None:
            self._say("Wait for the wiggle to finish (or press Stop).", error=True)
            return False
        if not any(t.key == key for t in self._identify_targets()):
            self._say(f"{key} is not offered for identifying (driven, reserved or a bus pin).", error=True)
            return False
        self.focus_key = key
        self.skipped.discard(key)
        self.last_action = ""
        self.pending_kind = ""
        self.name_var.set("")
        self.steps.select(1)
        self._refresh_identify()
        old = self.found.get(key)
        self._say(f"Redo {key}" + (f" (was: {old.kind}{' ' + old.name if old.name else ''})." if old else "."))
        return True

    def back_target(self) -> None:
        """Go to the target before the current one (in walk order)."""
        targets = self._identify_targets()
        current = self.current_target()
        index = len(targets) if current is None else next(i for i, t in enumerate(targets) if t.key == current.key)
        if index == 0:
            self._say("This is the first pin.", error=True)
            return
        self.jump_to(targets[index - 1].key)

    def _table_clicked(self, table: ttk.Treeview, event: Any = None) -> None:
        row = table.identify_row(event.y) if event is not None else ""
        row = row or (table.selection() or ("",))[0]
        if not row:
            return
        key = str(table.item(row, "values")[0])
        if key.startswith(("GPIO ", "PCA9685 ch ")):
            self.jump_to(key)

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
            text = (f"{target.key}: it will go to {start} us, then buzz +/-{delta} us. "
                    "Make sure it's clear." + ("  (boot-strapping pin)" if target.last else ""))
        self.target_var.set(text)
        can_probe = target is not None and target.cls != "button" and self.armed and not busy
        self.wiggle_btn.state(["!disabled"] if can_probe else ["disabled"])
        self.blink_btn.state(["!disabled"] if can_probe and target.pin is not None else ["disabled"])
        self.skip_btn.state(["!disabled"] if target is not None and not busy else ["disabled"])
        first = self._identify_targets()[:1]
        at_first = target is not None and first and first[0].key == target.key
        self.back_btn.state(["!disabled"] if first and not at_first and not busy else ["disabled"])
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
        if self.pending_kind == "servo":
            self.type_line.pack(anchor="w", pady=(6, 0))
        else:
            self.type_line.pack_forget()
        self.save_map_btn.state(["!disabled"] if self._named_servos() else ["disabled"])
        for button in (self.to_rom_btn, self.to_bench_btn, self.bench_to_rom_btn):
            button.state(["!disabled"] if self._named_servos() and not busy else ["disabled"])

    def _named_servos(self) -> bool:
        return any(item.kind == "servo" for item in self.found.values())

    def _refresh_found(self) -> None:
        table = self.found_table
        table.delete(*table.get_children())
        for item in self.found.values():
            what = KIND_TEXT.get(item.kind, item.kind)
            if item.servo_id:
                typed = find.servo_type(item.servo_id)
                what += f" ({typed['model'] if typed else item.servo_id})"
            table.insert("", "end", values=(item.target.key, what, item.name or "-"))
        self._refresh_bench()

    def _probe_error(self, error: Exception) -> None:
        """Refusals (driven pin, no PWM slot, ...) are messages; anything else stops everything."""
        self._bench_stop_activity()
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
        self._say(f"{target.key}: buzzing. Did it move? Y / N (L for a light).")
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
            if self.auto_next_var.get():
                self.root.after(150, self.do_wiggle)  # next pin buzzes right away: just keep pressing N
            return
        self.pending_kind = kind
        self.name_var.set("")
        self.choose_servo(None)
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
        note = find.FAULTY_NOTE if kind == "faulty" else ""
        servo_id = self.servo_choice["id"] if kind == "servo" and self.servo_choice else ""
        self.found[target.key] = find.Found(target, kind, name, start, note, servo_id)
        self._close_identifier()
        self.bench_roms.pop(target.key, None)
        if kind == "faulty":
            self._release_faulty(target, name)
        self._next_target()
        if kind == "faulty":
            disarmed, self._faulty_disarmed = self._faulty_disarmed, ""
            self._say(f"{name} ({target.key}) saved as FAULTY: {note}. The bench and range of motion skip it. "
                      + (disarmed or (f"Next: {self.current_target().key}." if self.current_target()
                                      else "All pins done.")), error=bool(disarmed))

    # -- servo type (servo_db)
    @staticmethod
    def _servo_label(entry: dict[str, Any]) -> str:
        return f"{entry['brand']} {entry['model']}"

    def _servo_typed(self, event: Any = None) -> None:
        if event is not None and getattr(event, "keysym", "") in ("Return", "Down", "Up", "Escape", "Tab"):
            return
        text = self.servo_var.get().strip()
        if self.servo_choice is not None and text != self._servo_label(self.servo_choice):
            self.servo_choice = None
            self.servo_info.configure(text="")
        self._servo_hits = servo_db.search(text, limit=12) if text else []
        self.servo_box.configure(values=[self._servo_label(e) for e in self._servo_hits])

    def _servo_selected(self) -> None:
        label = self.servo_var.get()
        entry = next((e for e in self._servo_hits if self._servo_label(e) == label), None)
        self.choose_servo(entry)

    def choose_servo(self, entry: dict[str, Any] | str | None) -> None:
        """The servo type for the target being named (None: unknown, the default band)."""
        if isinstance(entry, str):
            entry = servo_db.get(entry)
        self.servo_choice = entry
        if entry is None:
            self.servo_var.set("")
            self.servo_info.configure(text="")
            return
        self.servo_var.set(self._servo_label(entry))
        low, high = servo_db.safe_pulse_range(entry)
        clone = "  (clone: specs vary)" if entry.get("clone_of") else ""
        self.servo_info.configure(text=f"starting ROM band {low}..{high} us{clone}")

    def open_servo_identifier(self) -> None:
        """20 questions in a small dialog: answer what you can see, pick the match."""
        self._close_identifier()
        self.identifier = servo_db.Identifier()
        dialog = tk.Toplevel(self.root)
        dialog.title("Identify the servo")
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", self._close_identifier)
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)
        self.ident_question = ttk.Label(frame, style="Big.TLabel", wraplength=460, justify="left")
        self.ident_question.pack(anchor="w")
        self.ident_options = ttk.Frame(frame)
        self.ident_options.pack(anchor="w", pady=(8, 0), fill="x")
        ttk.Label(frame, text="Best matches (click one to use it):", style="Muted.TLabel").pack(anchor="w", pady=(12, 0))
        self.ident_matches = ttk.Frame(frame)
        self.ident_matches.pack(anchor="w", fill="x")
        ttk.Button(frame, text="Close", command=self._close_identifier).pack(anchor="e", pady=(12, 0))
        self.ident_dialog = dialog
        self._ident_show()

    def _ident_show(self) -> None:
        ident = self.identifier
        if ident is None or self.ident_dialog is None:
            return
        for holder in (self.ident_options, self.ident_matches):
            for child in holder.winfo_children():
                child.destroy()
        question = ident.next_question()
        self.ident_current = question
        if question is None:
            self.ident_question.configure(text=f"{len(ident.candidates)} match(es) left: pick it below.")
        else:
            self.ident_question.configure(text=question.text)
            for value in list(question.options[:8]) + [servo_db.UNKNOWN]:
                text = "Don't know" if value == servo_db.UNKNOWN else str(value)
                ttk.Button(self.ident_options, text=text,
                           command=lambda v=value: self.ident_answer(v)).pack(side="left", padx=(0, 4), pady=2)
        for entry in ident.ranked()[:6]:
            ttk.Button(self.ident_matches, text=self._servo_label(entry),
                       command=lambda e=entry: self.ident_pick(e)).pack(anchor="w", pady=1)

    def ident_answer(self, value: Any) -> None:
        if self.identifier is None or self.ident_current is None:
            return
        self.identifier.answer(self.ident_current.key, value)
        self._ident_show()

    def ident_pick(self, entry: dict[str, Any]) -> None:
        self.choose_servo(entry)
        self._close_identifier()

    def _close_identifier(self) -> None:
        dialog, self.ident_dialog = self.ident_dialog, None
        self.identifier = None
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:
                pass

    def _release_faulty(self, target: find.Target, name: str) -> None:
        """A servo with broken feedback can keep pushing while it is held: offer to let go of it."""
        if not self.armed or self.controller.position(target.channel) is None:
            return
        if target.pin is not None:
            try:
                self.controller.release_pin(target.pin)
            except Exception as error:  # noqa: BLE001
                self._probe_error(error)
            return
        # PCA9685 channels are held until DISARM and one with broken feedback keeps pushing:
        # fail safe, no question in the main flow.
        self.disarm()
        self._faulty_disarmed = f"{name} was still driven (a PCA9685 channel is held until Disarm), so everything " \
                                "was disarmed. Connect & arm to continue."

    def skip_target(self) -> None:
        target = self.current_target()
        if target is not None and self.wiggle is None:
            if target.key not in self.found:  # skipping a redo keeps its old result
                self.skipped.add(target.key)
            self._next_target()

    def _next_target(self) -> None:
        self.focus_key = None
        self.last_action = ""
        self.pending_kind = ""
        self.name_var.set("")
        self.choose_servo(None)
        self._refresh_found()
        self._refresh_identify()
        target = self.current_target()
        self._say(f"Next: {target.key}." if target else "All pins done. Save the hardware map.")

    ZERO_US = 1500
    ZERO_WARNING = ("Ensure servo horns are mounted at middle range or disconnect them before zeroing; "
                    "then re-mount servo horns before continuing.")

    def zero_all_servos(self) -> None:
        """Drive every identified servo to the middle pulse (slider 0) and hold it there."""
        if not self.armed or self.controller is None:
            self._say("Connect & arm first.", error=True)
            return
        if self.wiggle is not None:
            self._say("Wait for the buzz to finish.", error=True)
            return
        servos = [f for f in self.found.values() if f.kind == "servo"]
        if not servos:
            self._say("No servos identified yet: buzz the pins and answer Y first.", error=True)
            return
        names = ", ".join(f.name or f.target.key for f in servos)
        text = f"{self.ZERO_WARNING}\n\nThis moves {names} to {self.ZERO_US} us and holds them there."
        if self.ask_fn("Set all servos to 0", text, ["Set all servos to 0", "Cancel"]) != "Set all servos to 0":
            self._say("Nothing moved.")
            return
        zero, done = self.ZERO_US, []
        try:
            for found in servos:
                ch = found.target.channel
                held = self.controller.position(ch)
                low, high = zero - 50, zero + 50
                if held is not None:
                    low, high = min(low, held), max(high, held)
                self.controller.set_limits(ch, low, high, neutral_us=zero)
                if held is None:
                    self.controller.start(ch, zero)
                else:
                    self.controller.move(ch, zero)
                found.start_us = zero
                done.append(found.name or found.target.key)
        except Exception as error:  # noqa: BLE001
            self._probe_error(error)
            return
        for found in servos:
            self.bench_roms.pop(found.target.key, None)  # re-created around the new start
        self._bench_back_to_zero()
        self._refresh_bench()
        self._say(f"Holding {', '.join(done)} at {zero} us (0). Re-mount the horns at the middle of each joint's "
                  "travel, then continue. Disarm or E-STOP releases them.")
        self._refresh_identify()

    def stop_identify(self) -> None:
        """End the walk through the pins. A wiggle in progress is stopped (all outputs released)."""
        if self.wiggle is not None and self.controller is not None:
            self.wiggle = None
            try:
                self.controller.stop()
            except Exception as error:  # noqa: BLE001
                self._say(f"STOP not confirmed ({error}). CUT POWER NOW.", error=True)
                self._drop_connection()
        self.focus_key = None
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

    def _default_map_path(self) -> Path:
        """Maps are saved without a dialog: ~/robo_prober_maps/<board>-<date-time>.map.json."""
        board = re.sub(r"[^A-Za-z0-9_-]+", "_", str((self.survey and getattr(self.survey, "board", None)) or "robot"))
        folder = Path.home() / "robo_prober_maps"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{board}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.map.json"

    # ------------------------------------------------------------ 3. bench
    def _bench_servos(self) -> dict[str, find.Found]:
        return {key: item for key, item in self.found.items() if item.kind == "servo"}

    def _faulty_servos(self) -> dict[str, find.Found]:
        return {key: item for key, item in self.found.items() if item.kind == "faulty"}

    def _bench_name(self, key: str) -> str:
        found = self.found.get(key)
        return (found.name if found is not None and found.name else key)

    def _bench_in_session(self, channel: int) -> bool:
        """With step 4's session running, its channels are driven through it (same band, same targets)."""
        return self._rom_started() and channel in self.session.channels

    def _bench_powered(self, key: str) -> bool:
        channel = self.found[key].target.channel
        if self._bench_in_session(channel):
            return self._enabled(channel)
        return self.armed and self.controller.position(channel) is not None

    def _bench_rom(self, key: str) -> RomChannel:
        """The servo's start pulse and allowed band (RomChannel does the band arithmetic)."""
        found = self.found[key]
        channel = found.target.channel
        if self._bench_in_session(channel):
            return self.roms[channel]
        start = found.start_us or find.DEFAULT_START_US
        model = self.bench_roms.get(key)
        if model is None or model.channel != channel:
            typed = find.servo_type(found.servo_id)
            model = RomChannel(channel, self._bench_name(key), start, self.band_half_us,
                               safe_us=tuple(typed["safe_start_us"]) if typed else None)
            self.bench_roms[key] = model
        elif model.start_us != start and not self._bench_powered(key):
            model.set_start(start)
        return model

    def _bench_target(self, key: str) -> int:
        model = self._bench_rom(key)
        try:
            offset = float(self.bench_rows[key]["var"].get())
        except (KeyError, ValueError, tk.TclError):
            offset = 0.0
        return model.clamp(model.center + offset)

    def _bench_stop_activity(self) -> None:
        self.bench_pending.clear()
        self.bench_buzz_queue.clear()
        self._bench_buzz = None

    def _bench_ready(self) -> bool:
        if not self.armed:
            self._say("Confirm the power cutoff and Connect & arm first.", error=True)
            return False
        if self.wiggle is not None:
            self._say("Wait for the buzz to finish.", error=True)
            return False
        return True

    def _bench_error(self, key: str, error: Exception) -> None:
        """No free PWM slot and other refusals are messages; anything else stops everything."""
        self._bench_stop_activity()
        name = f"{self._bench_name(key)} ({self.found[key].target.key})"
        if "no_pwm_slot" in str(error):
            self._say(f"Refused: no free PWM slot for {name}. The board can drive only a few servo pins at once. "
                      "Disarm releases them all (taking a servo off the bench does not).", error=True)
        elif isinstance(error, RomError) or (isinstance(error, ProtocolError) and "returned error" in str(error)):
            self._say(f"Refused for {name}: {error}", error=True)
        else:
            self._fault_or_say(error)
        self._refresh_bench()

    # -- tiles, drag and drop
    def toggle_bench(self, key: str) -> None:
        if key in self.bench:
            self.remove_from_bench(key)
        else:
            self.add_to_bench(key)

    def add_to_bench(self, key: str) -> None:
        faulty = self._faulty_servos().get(key)
        if faulty is not None:
            self._say(f"{self._bench_name(key)} ({key}) is marked faulty: {faulty.note or find.FAULTY_NOTE}. "
                      "It can't go on the bench; redo it in step 2 if that was a mistake.", error=True)
            return
        if key not in self._bench_servos() or key in self.bench:
            return
        self.bench.append(key)
        self._refresh_bench()
        model = self._bench_rom(key)
        if self._bench_powered(key):
            self._say(f"{self._bench_name(key)} is on the bench (holding at "
                      f"{self.controller.position(model.channel)} us).")
        else:
            self._say(f"{self._bench_name(key)} is on the bench. Power up starts it at {model.center} us.")

    def remove_from_bench(self, key: str) -> None:
        if key not in self.bench:
            return
        self.bench.remove(key)
        self.bench_pending.pop(self.found[key].target.channel, None)
        if key in self.bench_buzz_queue:
            self.bench_buzz_queue.remove(key)
        held = self._bench_powered(key)
        self._refresh_bench()
        self._say(f"{self._bench_name(key)} is off the bench" + (
            " (still holding where it is; Disarm releases it)." if held else "."))

    def _drag_press(self, key: str, source: str, event: Any) -> None:
        self._drag = {"key": key, "source": source, "x": event.x_root, "y": event.y_root, "moved": False}

    def _drag_motion(self, event: Any) -> None:
        drag = self._drag
        if drag is None:
            return
        if not drag["moved"] and abs(event.x_root - drag["x"]) + abs(event.y_root - drag["y"]) < 8:
            return
        drag["moved"] = True
        if self._ghost is None:
            self._ghost = tk.Label(self.root, text=self._bench_name(drag["key"]), background=theme.ACCENT,
                                   foreground=theme.ON_ACCENT, font=theme.FONT_BOLD, padx=10, pady=4)
        self._ghost.place(x=event.x_root - self.root.winfo_rootx() + 12,
                          y=event.y_root - self.root.winfo_rooty() + 12)
        self._ghost.lift()

    def _drag_release(self, event: Any) -> None:
        drag, self._drag = self._drag, None
        if self._ghost is not None:
            self._ghost.destroy()
            self._ghost = None
        if drag is None:
            return
        if not drag["moved"]:
            if drag["source"] == "tile":
                self.toggle_bench(drag["key"])  # a plain click works without dragging
            return
        over = self._over_bench(event.x_root, event.y_root)
        if drag["source"] == "tile" and over:
            self.add_to_bench(drag["key"])
        elif drag["source"] == "bench" and not over:
            self.remove_from_bench(drag["key"])

    def _over_bench(self, x_root: int, y_root: int) -> bool:
        zone = self.bench_zone
        x, y = x_root - zone.winfo_rootx(), y_root - zone.winfo_rooty()
        return 0 <= x < max(zone.winfo_width(), 1) and 0 <= y < max(zone.winfo_height(), 1)

    def _draggable(self, widgets: list[tk.Misc], key: str, source: str) -> None:
        for widget in widgets:
            widget.bind("<ButtonPress-1>", lambda e, k=key, s=source: self._drag_press(k, s, e))
            widget.bind("<B1-Motion>", self._drag_motion)
            widget.bind("<ButtonRelease-1>", self._drag_release)

    def _build_tile(self, key: str, faulty: bool = False) -> dict[str, Any]:
        cursor = "X_cursor" if faulty else "hand2"
        frame = tk.Frame(self.tiles_box, background=theme.SURFACE, highlightthickness=2,
                         highlightbackground=theme.BORDER, padx=10, pady=6, cursor=cursor)
        frame.pack(fill="x", pady=(0, 6))
        name = tk.Label(frame, text=self._bench_name(key), font=theme.FONT_BOLD, background=theme.SURFACE,
                        foreground=theme.MUTED if faulty else theme.TEXT, anchor="w", cursor=cursor)
        name.pack(fill="x")
        where = tk.Label(frame, background=theme.SURFACE, foreground=theme.MUTED, anchor="w", cursor=cursor,
                         font=theme.FONT, justify="left")
        where.pack(fill="x")
        if faulty:
            # not draggable: a click explains why it can't go on the bench
            where.configure(text=f"{self.found[key].target.key}  ·  FAULTY: moves one way only")
            for widget in (frame, name, where):
                widget.bind("<ButtonRelease-1>", lambda e, k=key: self.add_to_bench(k))
        else:
            self._draggable([frame, name, where], key, "tile")
        return {"frame": frame, "name": name, "where": where, "faulty": faulty}

    def _build_bench_row(self, key: str) -> dict[str, Any]:
        found = self.found[key]
        frame = ttk.LabelFrame(self.bench_frame, text=f"{self._bench_name(key)}  ({found.target.key})",
                               padding=(10, 6))
        frame.columnconfigure(0, weight=1)
        top = ttk.Frame(frame)
        top.grid(row=0, column=0, sticky="ew")
        handle = tk.Label(top, text="⠿ drag off to remove", background=theme.BG, foreground=theme.MUTED,
                          font=theme.FONT, cursor="fleur")
        handle.pack(side="left", padx=(0, 10))
        self._draggable([handle], key, "bench")
        invert = tk.BooleanVar(value=self.bench_invert.get(key, False))
        invert.trace_add("write", lambda *_, k=key, v=invert: self.bench_invert.__setitem__(k, bool(v.get())))
        ttk.Checkbutton(top, text="Invert", variable=invert).pack(side="left", padx=(0, 8))
        buzz = ttk.Button(top, text="Buzz", command=lambda k=key: self.bench_buzz(k))
        use = ttk.Button(top, text="Use as start", command=lambda k=key: self.bench_use_as_start(k))
        widen = ttk.Button(top, text="Widen", command=lambda k=key: self.bench_widen(k))
        narrow = ttk.Button(top, text="Narrow", command=lambda k=key: self.bench_narrow(k))
        remove = ttk.Button(top, text="✕", width=3, command=lambda k=key: self.remove_from_bench(k))
        for widget in (buzz, use, widen, narrow, remove):
            widget.pack(side="left", padx=(0, 6))
        model = self._bench_rom(key)
        low, high = model.slider_range()
        var = tk.DoubleVar(value=0)  # 0 at the start pulse, like step 4
        scale = tk.Scale(frame, from_=low - model.center, to=high - model.center, orient="horizontal", resolution=1,
                         showvalue=True, variable=var, label=f"us from start ({model.center} us)",
                         background=theme.BG, foreground=theme.TEXT, troughcolor=theme.SURFACE_HI,
                         activebackground=theme.ACCENT, highlightthickness=0, borderwidth=0, sliderrelief="flat",
                         font=theme.FONT)
        scale.grid(row=1, column=0, sticky="ew")
        info = ttk.Label(frame)
        info.grid(row=2, column=0, sticky="w", pady=(2, 0))
        row = {"frame": frame, "var": var, "scale": scale, "info": info, "invert": invert, "buzz": buzz,
               "use": use, "widen": widen, "narrow": narrow}
        var.trace_add("write", lambda *_, k=key: self._bench_slider(k))
        return row

    def _refresh_bench(self) -> None:
        if not hasattr(self, "bench_zone") or self._closed:
            return
        servos = self._bench_servos()
        faulty = self._faulty_servos()
        self.bench = [key for key in self.bench if key in servos]
        if list(servos) + list(faulty) != list(self.bench_tiles):
            for tile in self.bench_tiles.values():
                tile["frame"].destroy()
            self.bench_tiles = {key: self._build_tile(key) for key in servos}
            self.bench_tiles.update({key: self._build_tile(key, faulty=True) for key in faulty})
        for key in [k for k in self.bench_rows if k not in self.bench]:
            self.bench_rows.pop(key)["frame"].destroy()
        for index, key in enumerate(self.bench):
            if key not in self.bench_rows:
                self.bench_rows[key] = self._build_bench_row(key)
            self.bench_rows[key]["frame"].grid(row=index, column=0, sticky="ew", padx=2, pady=4)
        if servos or faulty:
            self.no_tiles.pack_forget()
        else:
            self.no_tiles.pack(anchor="w")
        if self.bench:
            self.bench_empty.grid_remove()
        else:
            self.bench_empty.grid()

        ready = self.armed and self.wiggle is None
        powered = {key: self._bench_powered(key) for key in servos}
        for key, tile in self.bench_tiles.items():
            if tile["faulty"]:
                continue
            channel = servos[key].target.channel
            pulse = self.controller.position(channel) if powered[key] else None
            where = f"{pulse} us" if pulse is not None else "off"
            on = key in self.bench
            tile["name"].configure(text=self._bench_name(key), foreground=theme.ACCENT if on else theme.TEXT)
            tile["where"].configure(text=f"{servos[key].target.key}  ·  {where}" + ("  ·  on bench" if on else ""))
            tile["frame"].configure(highlightbackground=theme.ACCENT if on else theme.BORDER)
        for key, row in self.bench_rows.items():
            model = self._bench_rom(key)
            low, high = model.slider_range()
            start = model.center
            if not powered[key] and float(row["var"].get() or 0) != 0:
                row["var"].set(0)  # released: it starts at its start pulse again
            if int(float(row["scale"].cget("from"))) != low - start or int(float(row["scale"].cget("to"))) != high - start:
                row["scale"].configure(from_=low - start, to=high - start)
            row["scale"].configure(label=f"us from start ({start} us)",
                                   state="normal" if powered[key] and ready else "disabled")
            if powered[key]:
                pulse = self.controller.position(model.channel)
                text = f"at {pulse} us   target {self._bench_target(key)} us   allowed {low}..{high} us"
            else:
                text = f"off: Power up starts it at {start} us   allowed {low}..{high} us"
            row["info"].configure(text=text)
            row["buzz"].state(["!disabled"] if ready else ["disabled"])
            row["use"].state(["!disabled"] if ready and powered[key] else ["disabled"])
            row["widen"].state(["!disabled"] if model.can_widen() else ["disabled"])
            row["narrow"].state(["!disabled"] if model.can_narrow() else ["disabled"])
        any_on = any(powered[key] for key in self.bench)
        if not any_on and self.group_var.get() != 0:
            self._group_last = 0.0
            self.group_var.set(0)
        half = max([self._bench_rom(key).half_span for key in self.bench] + [DEFAULT_BAND_HALF_US])
        if int(float(self.group_scale.cget("to"))) != half:
            self.group_scale.configure(from_=-half, to=half)
        self.group_scale.configure(state="normal" if any_on and ready else "disabled")
        self.bench_power_btn.state(["!disabled"] if ready and any(not powered[k] for k in self.bench)
                                   else ["disabled"])
        self.bench_buzz_all_btn.state(["!disabled"] if ready and self.bench else ["disabled"])
        self.bench_zero_btn.state(["!disabled"] if ready and any_on else ["disabled"])

    # -- output
    def _bench_push_band(self, key: str) -> None:
        """LIMIT: start +/- band (never excluding where it is held), firmware slew cap."""
        model = self._bench_rom(key)
        if self._bench_in_session(model.channel):
            self.session._push_band(model)
            return
        low, high = model.slider_range()
        held = self.controller.position(model.channel)
        if held is not None:
            low, high = min(low, held), max(high, held)
        self.controller.set_limits(model.channel, low, high, neutral_us=min(max(model.center, low), high),
                                   slew_us_s=DEFAULT_FIRMWARE_SLEW_US_S)

    def bench_power_up(self, keys: list[str] | None = None) -> bool:
        """First output on bench servos, at their start pulse, after one confirmation."""
        if not self._bench_ready():
            return False
        keys = list(self.bench if keys is None else keys)
        off = [key for key in keys if not self._bench_powered(key)]
        if not off:
            return True
        names = ", ".join(f"{self._bench_name(k)} at {self._bench_rom(k).center} us" for k in off)
        text = f"{self.ZERO_WARNING}\n\nThis powers up {names} and holds them there."
        if self.ask_fn("Power up bench servos", text, ["Power up", "Cancel"]) != "Power up":
            self._say("Nothing moved.")
            return False
        for key in off:
            model = self._bench_rom(key)
            if key in self.bench_rows:
                self.bench_rows[key]["var"].set(0)
            try:
                if self._bench_in_session(model.channel):
                    self.rows[model.channel]["start_var"].set(str(model.center))
                    self._enable_channel(model.channel, model.center)
                else:
                    self._bench_push_band(key)
                    self.controller.start(model.channel, model.center)
            except Exception as error:  # noqa: BLE001
                self._bench_error(key, error)
                return False
        self._refresh_bench()
        self._say(f"Holding {', '.join(self._bench_name(k) for k in off)}. Drag a slider; it follows slowly. "
                  "Disarm or E-STOP releases them.")
        return True

    def _bench_slider(self, key: str) -> None:
        if key not in self.bench_rows or not self._bench_powered(key):
            return
        target = self._bench_target(key)
        channel = self.found[key].target.channel
        if self._bench_in_session(channel):
            row = self.rows[channel]
            row["var"].set(target - row["zero"])  # step 4's row follows; its session slews the servo
        else:
            self.bench_pending[channel] = target  # sent by the next tick; the firmware slews to it
        self._refresh_bench()

    def _group_moved(self) -> None:
        try:
            value = float(self.group_var.get())
        except (ValueError, tk.TclError):
            return
        delta, self._group_last = value - self._group_last, value
        if not delta:
            return
        for key in self.bench:
            if key not in self.bench_rows or not self._bench_powered(key):
                continue
            model = self._bench_rom(key)
            low, high = model.slider_range()
            var = self.bench_rows[key]["var"]
            sign = -1 if self.bench_invert.get(key) else 1
            var.set(min(max(float(var.get()) + sign * delta, low - model.center), high - model.center))

    def _bench_back_to_zero(self) -> None:
        self._group_last = 0.0
        self.group_var.set(0)
        for key, row in self.bench_rows.items():
            row["var"].set(0)

    def bench_back_to_zero(self) -> None:
        """Every bench servo back to its start pulse (slowly)."""
        if not self._bench_ready():
            return
        self._bench_back_to_zero()
        self._say("Moving the bench servos back to their start (0).")

    def _bench_send(self) -> None:
        pending, self.bench_pending = self.bench_pending, {}
        for channel, target in pending.items():
            position = self.controller.position(channel)
            if position is not None and position != target:
                self.controller.move(channel, target)

    def bench_use_as_start(self, key: str) -> None:
        """This servo's start pulse becomes where it is now; step 4 starts it here."""
        if not self._bench_ready() or not self._bench_powered(key):
            return
        found = self.found[key]
        channel = found.target.channel
        in_session = self._bench_in_session(channel)
        pulse = (self.session.targets[channel] if in_session
                 else self.bench_pending.pop(channel, None) or self.controller.position(channel))
        try:
            if not in_session and self.controller.position(channel) != pulse:
                self.controller.move(channel, pulse)
            if channel in self.roms:
                rom = self.roms[channel]
                rom.set_start(pulse, "operator")
                self.rows[channel]["start_var"].set(str(pulse))
                if self._enabled(channel):
                    self.session._push_band(rom)
                    row = self.rows[channel]
                    row["zero"] = pulse
                    row["scale"].configure(label=f"us from start ({pulse} us)")
                    self._refresh_row(channel)
                    row["var"].set(0)
            found.start_us = pulse
            if not in_session:
                self._bench_rom(key).set_start(pulse)
                self._bench_push_band(key)  # band re-centred on the new start
        except Exception as error:  # noqa: BLE001
            self._bench_error(key, error)
            return
        self.bench_rows[key]["var"].set(0)
        self._refresh_bench()
        self._say(f"{self._bench_name(key)}: start is now {pulse} us (slider 0). Range of motion starts it here.")

    def bench_widen(self, key: str) -> None:
        self._bench_band(key, widen=True)

    def bench_narrow(self, key: str) -> None:
        self._bench_band(key, widen=False)

    def _bench_band(self, key: str, *, widen: bool) -> None:
        channel = self.found[key].target.channel
        if self._bench_in_session(channel):
            (self.widen if widen else self.narrow)(channel)
            self._refresh_bench()
            return
        model = self._bench_rom(key)
        try:
            held = self.controller.position(channel) if self._bench_powered(key) else None
            low, high = model.widen() if widen else model.narrow(keep_inside=held)
            if held is not None:
                self._bench_push_band(key)
        except Exception as error:  # noqa: BLE001
            self._bench_error(key, error)
            return
        self._refresh_bench()
        self._say(f"{self._bench_name(key)}: allowed band {'widened' if widen else 'narrowed'} to {low}..{high} us.")

    def bench_buzz(self, key: str) -> None:
        if not self._bench_ready():
            return
        if not self._bench_powered(key) and not self.bench_power_up([key]):
            return
        self.bench_buzz_queue = [key]
        self._bench_try_buzz()

    def bench_buzz_all(self) -> None:
        if not self._bench_ready():
            return
        if not self.bench:
            self._say("Put servos on the bench first.", error=True)
            return
        if not self.bench_power_up(self.bench):
            return
        self.bench_buzz_queue = list(self.bench)
        self._bench_try_buzz()

    def _bench_try_buzz(self) -> None:
        try:
            self._bench_next_buzz()
        except Exception as error:  # noqa: BLE001
            key = self._bench_buzz or (self.bench_buzz_queue[0] if self.bench_buzz_queue else None)
            if key is None:
                self._fault_or_say(error)
            else:
                self._bench_error(key, error)

    def _bench_next_buzz(self) -> None:
        """Start the next queued buzz once that servo has stopped moving (one at a time)."""
        while self.bench_buzz_queue and self.wiggle is None:
            key = self.bench_buzz_queue[0]
            if key not in self.bench or not self._bench_powered(key):
                self.bench_buzz_queue.pop(0)
                continue
            found = self.found[key]
            channel = found.target.channel
            if channel in self.bench_pending:
                self._bench_send()
                return
            state = self.controller.status(channel)
            pulse = self.controller.position(channel)
            if state["current_us"] != pulse or state["target_us"] != pulse:
                return  # still moving: the next tick tries again
            self.bench_buzz_queue.pop(0)
            low, high = self._bench_rom(key).slider_range()
            delta = min(find.DEFAULT_DELTA_US, pulse - low, high - pulse)
            if delta < 10:
                self._say(f"{self._bench_name(key)} is at the edge of its band: move it inward to buzz it.",
                          error=True)
                continue
            self.wiggle = find.Wiggle(self.controller, found.target, pulse, delta, self.wiggle_slew_us_s,
                                      release=False)
            self._bench_buzz = key
            self.wiggle.begin()
            self._say(f"{self._bench_name(key)}: buzzing.")
            self._refresh_identify()
            self._refresh_bench()
            return

    def _bench_buzz_done(self) -> None:
        key, self._bench_buzz = self._bench_buzz, None
        if key in self.found and self.armed:
            self._bench_push_band(key)  # back to its own band and slew after the quick buzz
        self._say(f"{self._bench_name(key)} buzzed." + (" Next..." if self.bench_buzz_queue else ""))
        self._refresh_identify()
        self._refresh_bench()

    def go_to_rom(self, path: Path | None = None) -> None:
        """Save the map, release everything and show only the named servos for range of motion."""
        if self.wiggle is not None:
            return
        # A servo that is holding (bench, Set all to 0) starts step 4 where it is: no snap on Enable.
        if self.armed:
            for found in self.found.values():
                held = self.controller.position(found.target.channel) if found.kind == "servo" else None
                if held is not None:
                    found.start_us = held
        target = path or self._found_path or self._default_map_path()
        if self.save_found_map(target) is None:
            return
        saved = self._found_path
        if self.armed:
            self.disarm()
        self.load_map_file(saved)
        self.steps.select(3)
        if not self.roms:
            self._say(f"Saved to {saved}. No servos were named, so there is nothing to range.", error=True)
            return
        self.connect_and_arm()  # same session, same power-cutoff confirmation
        if self.armed:
            self._say(f"Saved to {saved}. Armed: press Enable on a servo (it starts where the buzz left it), "
                      "then drag its slider.")

    def enable(self, channel: int) -> None:
        """First output on a channel, at the start value shown in its row."""
        if not self.armed:
            return
        start = self._start_value(channel)
        if start is None:
            self._say(f"ch {channel}: enter a start pulse in us.", error=True)
            return
        try:
            self._enable_channel(channel, start)
        except Exception as error:  # noqa: BLE001
            self._fault_or_say(error)
            return
        self._say(f"ch {channel}: output on at {start} us (slider 0). Drag the slider; the servo follows slowly.")

    def _enable_channel(self, channel: int, start: int) -> None:
        rom = self.roms[channel]
        self.session.enable(channel, None if start == rom.start_us else start)
        row = self.rows[channel]
        row["zero"] = start
        row["scale"].configure(label=f"us from start ({start} us)")
        self._refresh_row(channel)
        row["var"].set(0)

    def _var_changed(self, channel: int, var: tk.DoubleVar) -> None:
        try:
            value = var.get()
        except tk.TclError:  # transient text while Tk updates the variable
            return
        self.slider_moved(channel, value)

    def slider_moved(self, channel: int, value: Any) -> None:
        if not self._enabled(channel):
            return
        self.session.set_target(channel, self.rows[channel]["zero"] + float(value))

    def mark(self, channel: int, which: int) -> None:
        if not self._enabled(channel):
            return
        try:
            pulse = self.session.mark(channel, which)
        except Exception as error:  # noqa: BLE001
            self._fault_or_say(error)
            return
        self.rows[channel]["var"].set(pulse - self.rows[channel]["zero"])
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

    def narrow(self, channel: int) -> None:
        try:
            if self.session is not None and self.session.started:
                low, high = self.session.narrow(channel)
            else:
                low, high = self.roms[channel].narrow()
        except Exception as error:  # noqa: BLE001
            self._fault_or_say(error)
            return
        self._say(f"ch {channel}: allowed band narrowed to {low}..{high} us.")
        self._refresh_row(channel)

    def estop(self) -> None:
        controller = self.controller
        if controller is None:
            self._say("E-STOP: nothing connected.")
            return
        self.wiggle = None
        self.last_action = ""
        self._bench_stop_activity()
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
        if not self.cutoff_var.get():
            self._say("Confirm the power cutoff to clear the e-stop.", error=True)
            return
        if self.controller is None and not self._ensure_connected(self.clear_estop):
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
        self._bench_stop_activity()
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
        self._bench_stop_activity()
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
        self._bench_stop_activity()
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
            # a buzz drives its channel itself: the ROM session waits for it
            moved = self.session.step(dt_s) if self._rom_started() and self.wiggle is None else {}
            if self.wiggle is not None:
                if self.wiggle.step():
                    self.wiggle = None
                    if self._bench_buzz is not None:
                        self._bench_buzz_done()
                    else:
                        self._say("What happened? Pick one below.")
                        self._refresh_identify()
            else:
                self._bench_send()
                if self.bench_buzz_queue:
                    self._bench_next_buzz()
                if not self._rom_started():
                    self.controller.heartbeat()
        except Exception as error:  # noqa: BLE001
            if self.wiggle is not None:
                self._probe_error(error)
            else:
                self._fault_or_say(error)
            return
        for channel in moved:
            self._refresh_row(channel)
        if self._watch_ticks % 4 == 0 and self.bench_rows:
            self._refresh_bench()

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
        self._close_identifier()
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
