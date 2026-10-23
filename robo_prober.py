"""robo-prober. To use:  python robo_prober.py  [command]

One file, any OS. It checks Python and Tk, and if the libraries are missing it asks
before installing them into a private .venv next to this file (nothing system-wide).
When everything is there it says so and starts. With no command it opens the
range-of-motion window; any command (doctor, rom, run-demo, ...) passes straight through.

-y / --yes, or a non-interactive run, installs without asking.
ROBO_PROBER_EXTRAS picks the pip extras (default: identify; none = core only).
Standard library only above the install step.
"""

import hashlib
import os
import platform
import shutil
import subprocess
import sys

if __name__ == "robo_prober":
    # Imported (not run) from this folder: this file has the package's name, so hand
    # the import over to the real package in src/ instead of shadowing it.
    import importlib.util

    _init = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "robo_prober", "__init__.py")
    _spec = importlib.util.spec_from_file_location(
        "robo_prober", _init, submodule_search_locations=[os.path.dirname(_init)])
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["robo_prober"] = _pkg
    _spec.loader.exec_module(_pkg)

APP = "robo-prober"
MODULE = "robo_prober"
IMPORTS = "import robo_prober, serial, PIL"
WINDOW_COMMANDS = {"rom-gui"}
EXTRAS = os.environ.get("ROBO_PROBER_EXTRAS") or "identify"
MIN_PYTHON = (3, 11)

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
WINDOWS = os.name == "nt"
VPY = os.path.join(VENV, "Scripts", "python.exe") if WINDOWS else os.path.join(VENV, "bin", "python")
STAMP = os.path.join(VENV, ".%s-installed" % APP)
SYSTEM = platform.system()
RUN = "python robo_prober.py"


class Problem(Exception):
    def __init__(self, message, *lines):
        super().__init__(message)
        self.message = message
        self.lines = lines


def say(message):
    print("[%s] %s" % (APP, message), file=sys.stderr, flush=True)


def python_fix():
    if WINDOWS:
        return [
            "Install it from https://www.python.org/downloads/windows/",
            'In the installer, tick "Add python.exe to PATH" and keep "tcl/tk and IDLE" ticked.',
            "Then run %s again." % RUN,
        ]
    if SYSTEM == "Darwin":
        return [
            "Install Python 3.12 from https://www.python.org/downloads/macos/",
            "(or with Homebrew: brew install python@3.12 python-tk@3.12), then run python3 robo_prober.py again.",
        ]
    return [
        "Install Python 3.11 or newer with your package manager, for example:",
        "  Debian/Ubuntu: sudo apt install python3 python3-venv python3-tk",
        "  Fedora:        sudo dnf install python3 python3-tkinter",
        "  Arch:          sudo pacman -S python tk",
        "  openSUSE:      sudo zypper install python3 python3-tk",
        "  Alpine:        sudo apk add python3 py3-tkinter",
        "Then run python3 robo_prober.py again.",
    ]


def tk_fix():
    if WINDOWS:
        return [
            "Re-run the Python installer from https://www.python.org/downloads/windows/",
            'choose "Modify" and tick "tcl/tk and IDLE". Then run python robo_prober.py again.',
        ]
    if SYSTEM == "Darwin":
        return [
            "Install Python from https://www.python.org/downloads/macos/ (it includes Tk),",
            "or with Homebrew: brew install python-tk",
        ]
    return [
        "Debian/Ubuntu: sudo apt install python3-tk",
        "Fedora:        sudo dnf install python3-tkinter",
        "Arch:          sudo pacman -S tk",
        "openSUSE:      sudo zypper install python3-tk",
        "Alpine:        sudo apk add py3-tkinter",
    ]


def venv_fix():
    if WINDOWS:
        return ["Reinstall Python from https://www.python.org/downloads/windows/ and try again."]
    return [
        "Debian/Ubuntu: sudo apt install python3-venv",
        "Other systems: reinstall Python from python.org or your package manager.",
    ]


def quiet_ok(cmd):
    """Run cmd silently; True when it exits 0."""
    try:
        return subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def check_python():
    if sys.version_info < MIN_PYTHON:
        raise Problem(
            "Python %d.%d or newer is needed (this is %s)." % (MIN_PYTHON + (platform.python_version(),)),
            *python_fix()
        )
    say("Using Python %s at %s" % (platform.python_version(), sys.executable))


def check_tk(args):
    needs_window = not args or args[0] in WINDOW_COMMANDS
    try:
        import tkinter  # noqa: F401
    except ImportError:
        if needs_window:
            raise Problem(
                "This Python has no tkinter, which the range-of-motion window needs.",
                "Fix it with:",
                *(tk_fix() + ["(Without Tk you can still script it: %s rom --help)" % RUN])
            )


ASSUME_YES = False


def confirm(what):
    """Ask before installing. Yes by default; non-interactive runs and -y skip the question."""
    if ASSUME_YES or not sys.stdin.isatty():
        say("%s (auto-yes)." % what)
        return
    try:
        answer = input("[%s] %s Install now? [Y/n] " % (APP, what)).strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("", "y", "yes"):
        say("Nothing installed. Run %s again when you're ready." % RUN)
        sys.exit(0)


def ensure_venv():
    if os.path.isfile(VPY) and quiet_ok([VPY, "-c", "import sys"]):
        return
    confirm("robo-prober needs its Python libraries (pyserial, Pillow, esptool) in a private .venv.")
    say("First run: creating a private Python environment in .venv (one time only)...")
    shutil.rmtree(VENV, ignore_errors=True)
    if subprocess.call([sys.executable, "-m", "venv", VENV]) != 0 or not os.path.isfile(VPY):
        shutil.rmtree(VENV, ignore_errors=True)
        raise Problem("Could not create the virtual environment.", *venv_fix())


def wanted_stamp():
    try:
        with open(os.path.join(HERE, "pyproject.toml"), "rb") as handle:
            data = handle.read()
    except OSError:
        raise Problem("Could not read pyproject.toml next to %s." % RUN)
    # The venv's own Python version and the extras, so changing either reinstalls.
    version = subprocess.run(
        [VPY, "-c", "import sys; sys.stdout.write(sys.version)"],
        stdout=subprocess.PIPE, check=False,
    ).stdout
    return hashlib.sha256(data + version + EXTRAS.encode()).hexdigest()


def ensure_installed():
    want = wanted_stamp()
    try:
        with open(STAMP, encoding="utf-8") as handle:
            have = handle.read().strip()
    except OSError:
        have = ""
    if want == have and quiet_ok([VPY, "-P", "-c", IMPORTS]):
        say("All dependencies present. Good to go.")
        return
    if have:
        confirm("robo-prober's libraries need updating.")
    say("Installing %s (extras: %s). This needs internet once." % (APP, EXTRAS))
    pip = [VPY, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", "--upgrade"]
    if subprocess.call(pip + ["pip"]) != 0:
        say("Could not upgrade pip; continuing with the bundled one.")
    spec = HERE if EXTRAS.lower() == "none" else "%s[%s]" % (HERE, EXTRAS)
    if subprocess.call(pip + ["--editable", spec]) != 0:
        raise Problem(
            "Installing the Python libraries failed (see the pip messages above).",
            "Check your internet connection and run %s again." % RUN,
            "To start over completely, delete the .venv folder.",
        )
    if subprocess.call([VPY, "-P", "-c", IMPORTS]) != 0:
        raise Problem(
            "robo-prober installed but does not import (see the error above).",
            "Delete the .venv folder and run %s again." % RUN,
        )
    with open(STAMP, "w", encoding="utf-8") as handle:
        handle.write(want + "\n")
    say("Install complete. Good to go.")


def launch(args):
    if not args:
        say("Opening the range-of-motion window...")
        args = ["rom-gui"]
    # -P: never import from the current folder, so robo_prober.py here can't shadow the package.
    cmd = [VPY, "-P", "-m", MODULE] + args
    if not WINDOWS:
        os.execv(VPY, cmd)
    code = subprocess.call(cmd)
    if code != 0:
        print("", file=sys.stderr, flush=True)
        say("robo-prober exited with code %d." % code)
    return code


def main(args):
    global ASSUME_YES
    if args[:1] in (["-y"], ["--yes"]):
        ASSUME_YES, args = True, args[1:]
    try:
        check_python()
        check_tk(args)
        ensure_venv()
        ensure_installed()
    except Problem as problem:
        sys.stdout.flush()
        lines = ["", "[%s] PROBLEM: %s" % (APP, problem.message)]
        lines += ["    " + line for line in problem.lines]
        print("\n".join(lines) + "\n", file=sys.stderr, flush=True)
        return 1
    return launch(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
