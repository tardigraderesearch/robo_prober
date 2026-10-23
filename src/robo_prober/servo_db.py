"""Hobby / robotics servo database: type-ahead lookup and a 20-questions identifier.

The data lives in ``data/servos.json`` (schema ``robo-prober.servo-db/1``).
Every number is copied from a cited source or left null; ``verified`` is true
only when the key numbers came from a manufacturer page or datasheet. Cheap
clones sold under the same model name (Amazon "MG996R", "SG90", "DS3218")
often differ, so treat a spec as a starting point, not as a limit.

Reference data only: nothing here drives a pin. ``safe_pulse_range`` gives a
conservative *start* band for the range-of-motion step; the operator still
finds the real limits by hand.

Stdlib only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

from .rom import ABS_MAX_US, ABS_MIN_US

SCHEMA = "robo-prober.servo-db/1"
DEFAULT_BAND_US = (1000, 2000)
UNKNOWN = "unknown"


# ---------------------------------------------------------------- loading

def load(path: str | Path | None = None) -> dict[str, Any]:
    """Return the whole database document ({"schema", "servos", ...})."""
    if path is not None:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return _packaged()


@lru_cache(maxsize=1)
def _packaged() -> dict[str, Any]:
    text = (resources.files("robo_prober") / "data" / "servos.json").read_text(encoding="utf-8")
    return json.loads(text)


def servos(db: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return (db or load())["servos"]


def get(servo_id: str, db: dict[str, Any] | None = None) -> dict[str, Any]:
    for entry in servos(db):
        if entry["id"] == servo_id:
            return entry
    raise KeyError(f"no servo with id {servo_id!r}")


# ---------------------------------------------------------------- search

def _norm(text: str) -> str:
    """Case- and punctuation-insensitive key: "HS-645 MG" -> "hs645mg"."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _tokens(text: str) -> list[str]:
    """Alphanumeric runs, also split at letter/digit boundaries: "HS-645MG" -> hs, 645, mg."""
    return re.findall(r"[a-z]+|[0-9]+", text.lower())


def _keys(entry: dict[str, Any]) -> list[str]:
    names = [entry["model"], entry["brand"] + entry["model"], *entry.get("aliases", [])]
    return [k for k in dict.fromkeys(_norm(n) for n in names) if k]


def _score(query: str, entry: dict[str, Any]) -> float:
    q = _norm(query)
    if not q:
        return 0.0
    best = 0.0
    keys = _keys(entry)
    for key in keys:
        if key == q:
            return 100.0
        if key.startswith(q):
            best = max(best, 90.0 - min(len(key) - len(q), 10) * 0.5)
        elif q in key:
            best = max(best, 75.0 - min(len(key) - len(q), 10) * 0.5)
    # every query token present somewhere ("hitec 645", "sg 90"); word-start hits rank higher
    q_tokens = _tokens(query)
    words = {t for name in (entry["brand"], entry["model"], *entry.get("aliases", [])) for t in _tokens(name)}
    hay = "".join(keys) + _norm(entry["brand"])
    if q_tokens and all(t in hay for t in q_tokens):
        starts = sum(any(w.startswith(t) for w in words) for t in q_tokens)
        best = max(best, 60.0 + 20.0 * starts / len(q_tokens))
    if _norm(entry["brand"]).startswith(q):
        best = max(best, 55.0)
    if best == 0.0 and len(q) >= 3:  # typo tolerance
        ratio = max(SequenceMatcher(None, q, key).ratio() for key in keys)
        if ratio >= 0.75:
            best = 50.0 * ratio
    return best


def search(text: str, limit: int = 10, db: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Fuzzy type-ahead over brand, model and aliases ("mg996", "hitec 645", "sg 90")."""
    scored = [(_score(text, e), e) for e in servos(db)]
    scored = [(s, e) for s, e in scored if s > 0]
    # genuine parts before same-name clones, then manufacturer-verified first
    scored.sort(key=lambda se: (-se[0], se[1].get("clone_of") is not None, not se[1].get("verified"), se[1]["id"]))
    return [e for _, e in scored[:limit]]


# ---------------------------------------------------------------- pulse band

def safe_pulse_range(servo: dict[str, Any] | str | None, shrink: float = 0.2) -> tuple[int, int]:
    """Conservative start band (low_us, high_us) for the range-of-motion step.

    The datasheet range is pulled toward neutral by ``shrink`` of each half-span
    and never wider than 1000-2000 us. Unknown range (or a bus servo) gives
    1000-2000 us.
    """
    if isinstance(servo, str):
        servo = get(servo)
    lo = (servo or {}).get("pulse_min_us")
    hi = (servo or {}).get("pulse_max_us")
    if not (isinstance(lo, int) and isinstance(hi, int) and lo < hi):
        return DEFAULT_BAND_US
    mid = servo.get("pulse_neutral_us") or (lo + hi) // 2
    if not lo < mid < hi:
        mid = (lo + hi) // 2
    low = round(mid - (mid - lo) * (1 - shrink))
    high = round(mid + (hi - mid) * (1 - shrink))
    # A safe *start* is never wider than the default: datasheet maxima are travel limits, not a safe opening band.
    return max(DEFAULT_BAND_US[0], low), min(DEFAULT_BAND_US[1], high)


# ---------------------------------------------------------------- DTG bridge

def to_dtg(entry: dict[str, Any]) -> dict[str, Any]:
    """Map an entry to digital_twin_generator's ``dtg.servo-library/1`` servo shape."""
    dims = entry.get("dims_mm") or {}
    body = [dims.get("l"), dims.get("w"), dims.get("h")]
    pulse = [entry.get("pulse_min_us"), entry.get("pulse_max_us")]
    vmin, vmax = entry.get("voltage_min"), entry.get("voltage_max")
    return {
        "model": entry["model"],
        "manufacturer": entry["brand"],
        "size_class": entry["category"],
        "body_mm": body if None not in body else None,
        "height_with_spline_mm": dims.get("height_with_spline"),
        "length_with_tabs_mm": dims.get("length_with_tabs"),
        "tab_height_mm": dims.get("tab_height"),
        "shaft_offset_from_end_mm": None,
        "mass_g": entry.get("weight_g"),
        "stall_torque_kgcm": {f"{p['volts']:.1f}V": p["kgcm"] for p in entry.get("torque_kgcm") or []},
        "speed_s_per_60deg": {f"{p['volts']:.1f}V": p["s"] for p in entry.get("speed_s_per_60deg") or []},
        "voltage_range_v": [vmin, vmax] if vmin is not None and vmax is not None else None,
        "rotation_deg": entry.get("travel_deg"),
        "pulse_range_us": pulse if None not in pulse else None,
        "sources": list(entry["sources"]),
        "dims_source": "robo-prober servo db" + (" (manufacturer)" if entry.get("verified") else ""),
        "dims_verified": bool(entry.get("verified")) and None not in body,
        "mass_verified": bool(entry.get("verified")) and entry.get("weight_g") is not None,
        "notes": entry.get("notes") or "",
    }


# ---------------------------------------------------------------- 20 questions

def _weight_bucket(e: dict[str, Any]) -> str | None:
    g = e.get("weight_g")
    if g is None:
        return None
    for limit, label in ((15, "under 15 g"), (30, "15-30 g"), (50, "30-50 g"), (80, "50-80 g")):
        if g < limit:
            return label
    return "80 g or more"


def _wire_colours(e: dict[str, Any]) -> str | None:
    w = e.get("wires") or {}
    parts = [w.get("signal"), w.get("positive"), w.get("negative")]
    return "/".join(p.lower() for p in parts) if None not in parts else None


def _torque_bucket(e: dict[str, Any]) -> str | None:
    kg = max((p["kgcm"] for p in e.get("torque_kgcm") or []), default=None)
    if kg is None:
        return None
    for limit, label in ((2, "under 2 kg-cm"), (5, "2-5 kg-cm"), (10, "5-10 kg-cm"), (20, "10-20 kg-cm"), (35, "20-35 kg-cm")):
        if kg < limit:
            return label
    return "35 kg-cm or more"


def _high_voltage(e: dict[str, Any]) -> str | None:
    v = e.get("voltage_max")
    return None if v is None else ("yes" if v >= 7.4 else "no")


def _yes_no(key: str):
    return lambda e: None if e.get(key) is None else ("yes" if e[key] else "no")


def _metal(e: dict[str, Any]) -> str | None:
    return {"metal": "yes", "titanium": "yes", "plastic": "no", "mixed": "partly"}.get(e.get("gears"))


def _get(key: str):
    return lambda e: e.get(key)


# key, question, value extractor. Only things you can see or check on the bench.
QUESTIONS: list[tuple[str, str, Any]] = [
    ("category", "Roughly what size is it (micro, mini, standard, large, giant)?", _get("category")),
    ("interface", "Is it plain PWM or a serial-bus servo (daisy-chained, ID-addressed)?", _get("interface")),
    ("wire_count", "How many wires does it have?", lambda e: (e.get("wires") or {}).get("count")),
    ("wire_colours", "What are the wire colours (signal/+/-)?", _wire_colours),
    ("bus_protocol", "Which bus protocol does it speak?", _get("bus_protocol")),
    ("metal_gears", "Does it have metal gears ('MG' in the name, heavier)?", _metal),
    ("rotation", "Does it stop at an angle, or spin continuously?", _get("rotation")),
    ("brand", "What brand is printed on the label?", _get("brand")),
    ("case_colour", "What colour is the case / label?", _get("case_colour")),
    ("weight", "About how much does it weigh?", _weight_bucket),
    ("spline_teeth", "How many teeth on the output spline (horn)?", _get("spline_teeth")),
    ("torque", "What torque does the label or listing claim (max)?", _torque_bucket),
    ("high_voltage", "Is it rated for 7.4 V or more ('HV', 2S LiPo)?", _high_voltage),
    ("digital", "Does the label or listing say 'digital'?", _yes_no("digital")),
]


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    options: tuple  # known answers among the remaining candidates, most common first


class Identifier:
    """Narrow the database down by asking the most informative question next.

    A servo whose value for a question is unknown (null) stays in play whatever
    the answer, but ranks below servos that matched. Answer ``"unknown"`` to
    skip a question without narrowing.
    """

    def __init__(self, entries: list[dict[str, Any]] | None = None):
        self.candidates = list(entries if entries is not None else servos())
        self.matches = {e["id"]: 0 for e in self.candidates}
        self.asked: set[str] = set()

    def next_question(self) -> Question | None:
        if len(self.candidates) <= 1:
            return None
        best = None
        for key, text, value_of in QUESTIONS:
            if key in self.asked:
                continue
            counts: dict[Any, int] = {}
            unknown = 0
            for e in self.candidates:
                v = value_of(e)
                if v is None:
                    unknown += 1
                else:
                    counts[v] = counts.get(v, 0) + 1
            if len(counts) < 2:
                continue
            known = sum(counts.values())
            # expected candidates left after the answer; unknowns survive every answer
            expected = sum(n / known * (n + unknown) for n in counts.values())
            if best is None or expected < best[0]:
                options = tuple(sorted(counts, key=lambda v: (-counts[v], str(v))))
                best = (expected, Question(key, text, options))
        return best[1] if best else None

    def answer(self, key: str, value: Any) -> list[dict[str, Any]]:
        self.asked.add(key)
        if value is None or value == UNKNOWN:
            return self.ranked()
        value_of = next(f for k, _, f in QUESTIONS if k == key)
        target = str(value).lower()
        kept = []
        for e in self.candidates:
            v = value_of(e)
            if v is None:
                kept.append(e)
            elif str(v).lower() == target:
                self.matches[e["id"]] += 1
                kept.append(e)
        self.candidates = kept
        return self.ranked()

    def ranked(self) -> list[dict[str, Any]]:
        return sorted(self.candidates, key=lambda e: (-self.matches[e["id"]], e["id"]))


def questions(entries: list[dict[str, Any]] | None = None) -> Iterator[Question]:
    """Generator form of :class:`Identifier`: ``q = next(it)``, then ``it.send(answer)``.

    Each ``send`` returns the next question; when nothing useful is left to ask
    the generator returns the ranked candidates (``StopIteration.value``).
    """
    ident = Identifier(entries)
    while (q := ident.next_question()) is not None:
        reply = yield q
        ident.answer(q.key, reply)
    return ident.ranked()
