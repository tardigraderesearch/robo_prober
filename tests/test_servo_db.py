import re

import pytest

from robo_prober import servo_db

DB = servo_db.load()
ENTRIES = DB["servos"]

CATEGORIES = {"micro", "mini", "standard", "large", "giant"}
INTERFACES = {"pwm", "serial_bus", "pwm+serial"}
ROTATIONS = {"positional", "continuous", "multi_turn"}
GEARS = {"plastic", "metal", "titanium", "mixed", None}
MANUFACTURER_DOMAINS = (
    "towerpro.com.tw", "hitecrcd.com", "hitecrcd.co.kr", "futabausa.com", "futaba.co.jp", "savoxusa.com",
    "savox.com", "dsservo.com", "jx-servo.com", "jxservo.com", "feetechrc.com", "feetech.cn", "coronarc.com",
    "emaxmodel.com", "emax-usa.com", "power-hd.com", "gobilda.com", "annimos.com", "hiwonder.com",
    "lewansoul.com", "robotis.com", "kstservos.com", "kstservo.com", "blue-bird-model.com", "miuzei.com",
    "parallax.com", "springrc.com", "savox-servo.com", "diymore.cc",
)


def _num(value, lo, hi):
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and lo <= value <= hi)


def test_document_header():
    assert DB["schema"] == servo_db.SCHEMA
    assert len(ENTRIES) >= 80
    ids = [e["id"] for e in ENTRIES]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("e", ENTRIES, ids=lambda e: e["id"])
def test_entry_schema(e):
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", e["id"])
    assert isinstance(e["brand"], str) and e["brand"]
    assert isinstance(e["model"], str) and e["model"]
    assert isinstance(e["aliases"], list) and all(isinstance(a, str) for a in e["aliases"])
    assert e["category"] in CATEGORIES
    assert e["interface"] in INTERFACES
    assert (e["bus_protocol"] is None) == (e["interface"] == "pwm")
    assert e["rotation"] in ROTATIONS
    assert e["digital"] in (True, False, None)
    assert e["gears"] in GEARS
    assert e["bearings"] is None or isinstance(e["bearings"], str)

    assert _num(e["voltage_min"], 2.0, 30.0) and _num(e["voltage_max"], 2.0, 30.0)
    if e["voltage_min"] is not None and e["voltage_max"] is not None:
        assert e["voltage_min"] <= e["voltage_max"]
    for point in e["torque_kgcm"]:
        assert set(point) == {"volts", "kgcm"}
        assert _num(point["volts"], 2.0, 30.0) and _num(point["kgcm"], 0.05, 500)
    for point in e["speed_s_per_60deg"]:
        assert set(point) == {"volts", "s"}
        assert _num(point["volts"], 2.0, 30.0) and _num(point["s"], 0.01, 5.0)

    lo, mid, hi = e["pulse_min_us"], e["pulse_neutral_us"], e["pulse_max_us"]
    for p in (lo, mid, hi):
        assert p is None or (isinstance(p, int) and 400 <= p <= 3100)
    if lo is not None and hi is not None:
        assert lo < hi
        if mid is not None:
            assert lo < mid < hi
    assert _num(e["travel_deg"], 10, 3600 * 10)
    assert _num(e["dead_band_us"], 0, 100)  # continuous servos quote a wide stop band (FS90R: 90 us)
    if e["frequency_hz"] is not None:
        assert set(e["frequency_hz"]) <= {"typ", "max"}
        assert all(_num(v, 20, 2000) for v in e["frequency_hz"].values())
    assert e["spline"] is None or isinstance(e["spline"], str)
    assert e["spline_teeth"] is None or (isinstance(e["spline_teeth"], int) and 10 <= e["spline_teeth"] <= 40)

    dims = e["dims_mm"]
    if dims is not None:
        assert set(dims) <= {"l", "w", "h", "length_with_tabs", "height_with_spline", "tab_height",
                             "hole_spacing_l", "hole_spacing_w"}
        assert all(_num(v, 1, 300) for v in dims.values())
    assert _num(e["weight_g"], 1, 2000)
    if e["wires"] is not None:
        assert e["wires"].get("count") in (None, 2, 3, 4, 5)
    assert e["case_colour"] is None or isinstance(e["case_colour"], str)
    assert _num(e["stall_current_a"], 0.01, 50)
    assert isinstance(e["notes"], str)
    assert e["clone_of"] is None or e["clone_of"] in {x["id"] for x in ENTRIES}

    assert e["sources"] and all(isinstance(s, str) and s.startswith("http") for s in e["sources"])
    assert isinstance(e["verified"], bool)
    if e["verified"]:
        assert any(d in e["sources"][0] for d in MANUFACTURER_DOMAINS), e["sources"][0]


@pytest.mark.parametrize("query, expected", [
    ("mg996", "towerpro-mg996r"),
    ("MG996R", "towerpro-mg996r"),
    ("hitec 645", "hitec-hs-645mg"),
    ("HS645MG", "hitec-hs-645mg"),
    ("sg 90", "towerpro-sg90"),
    ("SG-90", "towerpro-sg90"),
    ("mg90s", "towerpro-mg90s"),
    ("ds3218", "dsservo-ds3218mg"),
    ("xl430", "robotis-xl430-w250-t"),
    ("sts3215", None),
    ("futaba s3003", "futaba-s3003"),
    ("hs 311", "hitec-hs-311"),
])
def test_search_examples(query, expected):
    hits = servo_db.search(query, limit=5)
    assert hits, query
    if expected is None:
        assert any("sts3215" in h["id"] for h in hits)
    else:
        assert hits[0]["id"] == expected


def test_search_edge_cases():
    assert servo_db.search("") == []
    assert servo_db.search("zzzzqqq") == []
    assert len(servo_db.search("hitec", limit=3)) == 3
    assert all(h["brand"] == "Hitec" for h in servo_db.search("hitec", limit=5))
    assert servo_db.search("mg 996r")[0]["id"] == "towerpro-mg996r"


def test_get():
    assert servo_db.get("hitec-hs-645mg")["model"] == "HS-645MG"
    with pytest.raises(KeyError):
        servo_db.get("nope")


def test_safe_pulse_range():
    assert servo_db.safe_pulse_range(None) == (1000, 2000)
    assert servo_db.safe_pulse_range({"pulse_min_us": None, "pulse_max_us": None}) == (1000, 2000)
    assert servo_db.safe_pulse_range({"pulse_min_us": 500, "pulse_neutral_us": 1500, "pulse_max_us": 2500}) == (1000, 2000)  # never wider than the default
    assert servo_db.safe_pulse_range({"pulse_min_us": 900, "pulse_neutral_us": 1500, "pulse_max_us": 2100}) == (1020, 1980)
    assert servo_db.safe_pulse_range({"pulse_min_us": 400, "pulse_max_us": 3000}, shrink=0) == (1000, 2000)
    assert servo_db.safe_pulse_range({"pulse_min_us": 1100, "pulse_neutral_us": 1500, "pulse_max_us": 1900}) == (1180, 1820)
    for e in ENTRIES:
        lo, hi = servo_db.safe_pulse_range(e)
        assert 1000 <= lo < hi <= 2000
        if e["pulse_min_us"] is not None and e["pulse_max_us"] is not None:
            assert lo >= e["pulse_min_us"] and hi <= e["pulse_max_us"]


def test_to_dtg_shape():
    d = servo_db.to_dtg(servo_db.get("hitec-hs-645mg"))
    for key in ("model", "manufacturer", "size_class", "body_mm", "mass_g", "stall_torque_kgcm",
                "speed_s_per_60deg", "voltage_range_v", "pulse_range_us", "sources", "dims_verified", "notes"):
        assert key in d
    assert d["manufacturer"] == "Hitec" and d["model"] == "HS-645MG"


def _play(answers):
    """Answer every question from ``answers`` (missing keys -> "unknown"); return the ranked result."""
    it = servo_db.questions()
    asked = []
    try:
        q = next(it)
        while True:
            asked.append(q.key)
            assert len(q.options) >= 2
            q = it.send(answers.get(q.key, servo_db.UNKNOWN))
    except StopIteration as stop:
        return stop.value, asked


# Answers a person would give looking at the servo; unasked keys / missing answers are "unknown".
SCRIPTED = [
    ("towerpro-sg90", {"brand": "TowerPro", "category": "micro", "torque": "under 2 kg-cm",
                       "weight": "under 15 g", "metal_gears": "no"}),
    ("robotis-xl430-w250-t", {"brand": "unknown", "interface": "serial_bus", "bus_protocol": "Dynamixel 2.0",
                              "category": "standard", "metal_gears": "no", "torque": "10-20 kg-cm",
                              "weight": "50-80 g", "rotation": "positional"}),
    ("feetech-fs90r", {"brand": "unknown", "rotation": "continuous", "category": "micro", "weight": "under 15 g",
                       "metal_gears": "no", "interface": "pwm", "torque": "under 2 kg-cm"}),
]


@pytest.mark.parametrize("expected, answers", SCRIPTED, ids=[s[0] for s in SCRIPTED])
def test_twenty_questions(expected, answers):
    ranked, asked = _play(answers)
    assert [e["id"] for e in ranked] == [expected]
    assert len(asked) <= 20


def test_unknown_answers_do_not_narrow():
    ident = servo_db.Identifier()
    q = ident.next_question()
    before = len(ident.candidates)
    ident.answer(q.key, "unknown")
    assert len(ident.candidates) == before
    assert ident.next_question().key != q.key


def test_unknown_valued_servos_survive_an_answer():
    entries = [{"id": "a", "category": "micro"}, {"id": "b", "category": "standard"}, {"id": "c", "category": None}]
    ident = servo_db.Identifier(entries)
    ranked = ident.answer("category", "micro")
    assert [e["id"] for e in ranked] == ["a", "c"]
