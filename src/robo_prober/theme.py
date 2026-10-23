"""Dark, high-contrast ttk theme: near-black surfaces, warm off-white text, green accent,
rounded fields and buttons (drawn as anti-aliased images, so it looks the same on every OS)."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

BG = "#0b0b0b"          # window background
SURFACE = "#151515"     # panels, entries
SURFACE_HI = "#1f1f1f"  # hover / raised
BORDER = "#2e2e2e"
TEXT = "#f7ece5"
MUTED = "#9a908b"
ACCENT = "#2ee67a"
ACCENT_HI = "#62f59c"
ACCENT_LO = "#1fae5a"
ON_ACCENT = "#0b0b0b"
ERROR = "#ff5c5c"
FOCUS = "#8affbd"

FONT = ("TkDefaultFont", 10)
FONT_BOLD = ("TkDefaultFont", 11, "bold")


def apply(root: tk.Misc) -> ttk.Style:
    """Install the theme on ``root`` and return the configured style."""
    style = ttk.Style(root)
    style.theme_use("clam")  # clam honours colour options on every platform

    root.configure(background=BG)
    # Classic tk widgets and popups (combobox dropdown, dialogs) follow these.
    root.option_add("*Background", BG)
    root.option_add("*Foreground", TEXT)
    root.option_add("*selectBackground", ACCENT)
    root.option_add("*selectForeground", ON_ACCENT)
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", ON_ACCENT)

    style.configure(".", background=BG, foreground=TEXT, fieldbackground=SURFACE,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    troughcolor=SURFACE, focuscolor=FOCUS, insertcolor=TEXT,
                    selectbackground=ACCENT, selectforeground=ON_ACCENT, font=FONT)

    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Big.TLabel", font=FONT_BOLD)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Error.TLabel", foreground=ERROR)
    style.configure("TLabelframe", background=BG, bordercolor=BORDER, relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=FONT_BOLD)

    # Buttons: flat dark with an orange edge on hover; accent button is solid orange.
    style.configure("TButton", background=SURFACE_HI, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=SURFACE_HI, darkcolor=SURFACE_HI, relief="flat",
                    padding=(12, 6), font=FONT_BOLD, focusthickness=1)
    style.map("TButton",
              background=[("disabled", SURFACE), ("pressed", ACCENT_LO), ("active", "#2a2a2a")],
              foreground=[("disabled", MUTED), ("pressed", ON_ACCENT)],
              bordercolor=[("active", ACCENT), ("focus", ACCENT)],
              lightcolor=[("active", "#2a2a2a")], darkcolor=[("active", "#2a2a2a")])

    style.configure("Accent.TButton", background=ACCENT, foreground=ON_ACCENT, bordercolor=ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, font=("TkDefaultFont", 12, "bold"),
                    padding=(22, 9))
    style.map("Accent.TButton",
              background=[("disabled", "#1d4a31"), ("pressed", ACCENT_LO), ("active", ACCENT_HI)],
              foreground=[("disabled", "#6f9a80")],
              bordercolor=[("active", ACCENT_HI), ("focus", FOCUS)],
              lightcolor=[("active", ACCENT_HI)], darkcolor=[("active", ACCENT_HI)])

    for widget in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(widget, fieldbackground=SURFACE, foreground=TEXT, background=SURFACE_HI,
                        bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE,
                        arrowcolor=ACCENT, insertcolor=ACCENT, padding=4)
        style.map(widget,
                  bordercolor=[("focus", ACCENT), ("hover", "#4a4a4a")],
                  lightcolor=[("focus", ACCENT)],
                  fieldbackground=[("readonly", SURFACE), ("disabled", BG)],
                  foreground=[("disabled", MUTED)],
                  arrowcolor=[("disabled", MUTED), ("active", ACCENT_HI)])

    style.configure("TCheckbutton", background=BG, foreground=TEXT, indicatorbackground=SURFACE,
                    indicatorforeground=ON_ACCENT, focuscolor=BG)
    style.map("TCheckbutton",
              background=[("active", BG)],
              indicatorbackground=[("selected", ACCENT), ("active", SURFACE_HI)])

    style.configure("Vertical.TScrollbar", background=SURFACE_HI, troughcolor=BG,
                    bordercolor=BG, arrowcolor=ACCENT, lightcolor=SURFACE_HI, darkcolor=SURFACE_HI)
    style.map("Vertical.TScrollbar", background=[("active", ACCENT_LO)])

    _rounded_elements(root, style)

    style.configure("TNotebook", background=BG, bordercolor=BORDER, tabmargins=(0, 4, 0, 0))
    style.configure("TNotebook.Tab", background=SURFACE, foreground=MUTED, bordercolor=BORDER,
                    lightcolor=SURFACE, padding=(14, 6), font=FONT_BOLD)
    style.map("TNotebook.Tab", background=[("selected", SURFACE_HI)], foreground=[("selected", ACCENT)],
              lightcolor=[("selected", ACCENT)])
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT,
                    bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE, rowheight=22)
    style.map("Treeview", background=[("selected", ACCENT_LO)], foreground=[("selected", ON_ACCENT)])
    style.configure("Treeview.Heading", background=SURFACE_HI, foreground=ACCENT, bordercolor=BORDER,
                    lightcolor=SURFACE_HI, darkcolor=SURFACE_HI, font=FONT_BOLD, relief="flat")
    style.map("Treeview.Heading", background=[("active", SURFACE_HI)])

    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=SURFACE,
                    bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT, thickness=10)
    return style


RADIUS = 8
_SIZE = 32  # source image size; the 9-slice border keeps corners crisp at any widget size


def _round_image(fill: str, outline: str, width: int = 1) -> Image.Image:
    """Anti-aliased rounded rectangle with transparent corners (drawn 4x, then downsampled)."""
    k = 4
    big = Image.new("RGBA", (_SIZE * k, _SIZE * k), (0, 0, 0, 0))
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, _SIZE * k - 1, _SIZE * k - 1), radius=RADIUS * k, fill=fill,
        outline=outline, width=width * k)
    return big.resize((_SIZE, _SIZE), Image.LANCZOS)


def _rounded_elements(root: tk.Misc, style: ttk.Style) -> None:
    """Replace the square field and button borders with rounded image elements."""
    keep = getattr(root, "_theme_images", {})
    root._theme_images = keep  # tk drops images that Python garbage-collects

    def img(name: str, fill: str, outline: str, width: int = 1) -> ImageTk.PhotoImage:
        keep[name] = ImageTk.PhotoImage(_round_image(fill, outline, width), master=root)
        return keep[name]

    border = RADIUS + 1
    if "RoundField" not in style.element_names():
        style.element_create(
            "RoundField", "image", img("field", SURFACE, BORDER),
            ("disabled", img("field_off", BG, BORDER)),
            ("focus", img("field_focus", SURFACE, ACCENT, 2)),
            ("hover", img("field_hover", SURFACE, "#4a4a4a")),
            border=border, sticky="nsew")
        style.element_create(
            "RoundButton", "image", img("btn", SURFACE_HI, BORDER),
            ("disabled", img("btn_off", SURFACE, SURFACE)),
            ("pressed", img("btn_down", ACCENT_LO, ACCENT_LO)),
            ("active", img("btn_hover", "#2a2a2a", ACCENT)),
            ("focus", img("btn_focus", SURFACE_HI, ACCENT)),
            border=border, sticky="nsew")
        style.element_create(
            "RoundAccent", "image", img("acc", ACCENT, ACCENT),
            ("disabled", img("acc_off", "#1d4a31", "#1d4a31")),
            ("pressed", img("acc_down", ACCENT_LO, ACCENT_LO)),
            ("active", img("acc_hover", ACCENT_HI, ACCENT_HI)),
            border=border, sticky="nsew")

        style.element_create(
            "RoundClose", "image", img("close", SURFACE_HI, BORDER),
            ("pressed", img("close_down", "#a51d1d", "#a51d1d")),
            ("active", img("close_hover", ERROR, ERROR)),
            border=border, sticky="nsew")
        style.element_create("RoundFrame", "image", img("frame", BG, BORDER),
                             border=border, sticky="nsew")

    style.layout("TLabelframe", [("RoundFrame", {"sticky": "nsew"})])

    style.configure("Chrome.TButton", padding=(10, 2), font=("TkDefaultFont", 11, "bold"))
    style.configure("Close.TButton", padding=(10, 2), font=("TkDefaultFont", 11, "bold"))
    style.configure("Title.TLabel", foreground=ACCENT, font=("TkDefaultFont", 13, "bold"))

    def button(element: str) -> list:
        return [(element, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [
                ("Button.label", {"sticky": "nsew"})]})]})]

    style.layout("TButton", button("RoundButton"))
    style.layout("Accent.TButton", button("RoundAccent"))
    style.layout("Chrome.TButton", button("RoundButton"))
    style.layout("Close.TButton", button("RoundClose"))
    style.layout("TEntry", [("RoundField", {"sticky": "nsew", "children": [
        ("Entry.padding", {"sticky": "nsew", "children": [
            ("Entry.textarea", {"sticky": "nsew"})]})]})])
    style.layout("TSpinbox", [("RoundField", {"sticky": "nsew", "children": [
        ("null", {"side": "right", "sticky": "", "children": [
            ("Spinbox.uparrow", {"side": "top", "sticky": "e"}),
            ("Spinbox.downarrow", {"side": "bottom", "sticky": "e"})]}),
        ("Spinbox.padding", {"sticky": "nsew", "children": [
            ("Spinbox.textarea", {"sticky": "nsew"})]})]})])
    style.layout("TCombobox", [("RoundField", {"sticky": "nsew", "children": [
        ("Combobox.downarrow", {"side": "right", "sticky": "ns"}),
        ("Combobox.padding", {"expand": "1", "sticky": "nsew", "children": [
            ("Combobox.textarea", {"sticky": "nsew"})]})]})])
    # Arrow buttons blend into the rounded field instead of drawing grey boxes.
    for widget in ("TSpinbox", "TCombobox"):
        style.configure(widget, background=SURFACE, lightcolor=SURFACE, darkcolor=SURFACE,
                        bordercolor=SURFACE)
