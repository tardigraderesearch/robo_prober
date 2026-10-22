#!/bin/sh
# robo-prober launcher for Linux and macOS.
#
#   ./run.sh                          open the range-of-motion window
#   ./run.sh run-demo --out demo.json any robo-prober command works too
#
# First run: finds Python 3.11+, makes a private .venv next to this file and
# installs robo-prober into it. Later runs skip straight to launching unless
# pyproject.toml, the Python version or the extras changed. Nothing is
# installed system-wide and sudo is never used.
#   ROBO_PROBER_PYTHON=/path/to/python   pick a Python
#   ROBO_PROBER_EXTRAS=all               also install camera / scan-to-URDF extras
#                                        (default: identify; "none" = core only)

set -u

HERE=$(cd "$(dirname "$0")" && pwd) || exit 1
VENV="$HERE/.venv"
VPY="$VENV/bin/python"
STAMP="$VENV/.robo-prober-installed"
EXTRAS="${ROBO_PROBER_EXTRAS:-identify}"

say() { printf '%s\n' "[robo-prober] $*" >&2; }
fail() {
    printf '\n%s\n' "[robo-prober] PROBLEM: $1" >&2
    shift
    for line in "$@"; do printf '%s\n' "$line" | sed 's/^/    /' >&2; done
    printf '\n' >&2
    exit 1
}

os_name=$(uname -s 2>/dev/null || echo unknown)

python_fix() {
    if [ "$os_name" = "Darwin" ]; then
        echo "Install Python 3.12 from https://www.python.org/downloads/macos/"
        echo "(or with Homebrew: brew install python@3.12 python-tk@3.12), then run ./run.sh again."
    else
        echo "Install Python 3.11 or newer with your package manager, for example:"
        echo "  Debian/Ubuntu: sudo apt install python3 python3-venv python3-tk"
        echo "  Fedora:        sudo dnf install python3 python3-tkinter"
        echo "  Arch:          sudo pacman -S python tk"
        echo "  openSUSE:      sudo zypper install python3 python3-tk"
        echo "  Alpine:        sudo apk add python3 py3-tkinter"
        echo "Then run ./run.sh again."
    fi
}

tk_fix() {
    if [ "$os_name" = "Darwin" ]; then
        echo "Install Python from https://www.python.org/downloads/macos/ (it includes Tk),"
        echo "or with Homebrew: brew install python-tk"
    else
        echo "Debian/Ubuntu: sudo apt install python3-tk"
        echo "Fedora:        sudo dnf install python3-tkinter"
        echo "Arch:          sudo pacman -S tk"
        echo "openSUSE:      sudo zypper install python3-tk"
        echo "Alpine:        sudo apk add py3-tkinter"
    fi
}

VERSION_OK='import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'

# 1. Find a Python 3.11+ ----------------------------------------------------
PY=""
for candidate in "${ROBO_PROBER_PYTHON:-}" python3 python3.13 python3.12 python3.11 python3.14 python; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "$VERSION_OK" >/dev/null 2>&1; then
        PY=$(command -v "$candidate")
        break
    fi
done
if [ -z "$PY" ]; then
    fail "Python 3.11 or newer was not found." "$(python_fix)"
fi
say "Using $("$PY" -c 'import sys; print("Python %d.%d.%d" % sys.version_info[:3])') at $PY"

# 2. Tk is needed for the window only ------------------------------------------
needs_window=0
case "${1:-}" in
    ""|rom-gui) needs_window=1 ;;
esac
if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
    if [ "$needs_window" -eq 1 ]; then
        fail "This Python has no tkinter, which the range-of-motion window needs." \
            "Fix it with:" "$(tk_fix)" \
            "(Without Tk you can still script it: ./run.sh rom --help)"
    fi
fi

# 3. Private virtual environment ---------------------------------------------
if [ ! -x "$VPY" ] || ! "$VPY" -c "import sys" >/dev/null 2>&1; then
    say "First run: creating a private Python environment in .venv (one time only)..."
    rm -rf "$VENV"
    if ! "$PY" -m venv "$VENV"; then
        rm -rf "$VENV"
        fail "Could not create the virtual environment." \
            "Debian/Ubuntu: sudo apt install python3-venv" \
            "Other systems: reinstall Python from python.org or your package manager."
    fi
fi

# 4. Install / update only when pyproject.toml, Python or extras changed ----
WANT=$("$VPY" -c 'import hashlib, sys
data = open(sys.argv[1], "rb").read() + sys.version.encode() + sys.argv[2].encode()
print(hashlib.sha256(data).hexdigest())' "$HERE/pyproject.toml" "$EXTRAS") || fail "Could not read pyproject.toml."
HAVE=$(cat "$STAMP" 2>/dev/null || true)
if [ "$WANT" != "$HAVE" ] || ! "$VPY" -c "import robo_prober, serial, PIL" >/dev/null 2>&1; then
    say "Installing robo-prober (extras: $EXTRAS). This needs internet once."
    "$VPY" -m pip install --disable-pip-version-check --quiet --upgrade pip \
        || say "Could not upgrade pip; continuing with the bundled one."
    if [ "$EXTRAS" = "none" ]; then SPEC="$HERE"; else SPEC="$HERE[$EXTRAS]"; fi
    if ! "$VPY" -m pip install --disable-pip-version-check --quiet --upgrade --editable "$SPEC"; then
        fail "Installing the Python libraries failed (see the pip messages above)." \
            "Check your internet connection and run ./run.sh again." \
            "To start over completely, delete the .venv folder."
    fi
    if ! "$VPY" -c "import robo_prober, serial, PIL"; then
        fail "robo-prober installed but does not import (see the error above)." \
            "Delete the .venv folder and run ./run.sh again."
    fi
    printf '%s\n' "$WANT" > "$STAMP"
    say "Install complete."
else
    say "Everything is already installed."
fi

# 5. Launch -------------------------------------------------------------------
if [ $# -eq 0 ]; then
    say "Opening the range-of-motion window..."
    exec "$VPY" -m robo_prober rom-gui
fi
exec "$VPY" -m robo_prober "$@"
