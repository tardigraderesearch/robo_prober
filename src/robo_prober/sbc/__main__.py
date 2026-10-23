"""Run with python -m robo_prober.sbc (stdio), or --tcp PORT."""
import argparse
import importlib.util
import signal
import sys
import threading

from .backends import EmulatedBackend, GpiodBackend, LgpioBackend, MultiChipBackend, SoftwarePWMBackend
from .boards import Board, detect_board, get_profile
from .protocol import SBCAgent
from .server import serve_stream, serve_tcp


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tcp", type=int, metavar="PORT")
    parser.add_argument("--host", default="127.0.0.1", help="TCP bind address (default: localhost)")
    parser.add_argument("--backend", choices=("auto", "gpiod", "lgpio", "software", "emulated"), default="auto")
    parser.add_argument("--board", help="override detected catalog profile ID")
    parser.add_argument("--chip", help="explicit /dev/gpiochipN (otherwise match board chip label)")
    args = parser.parse_args(argv)
    if args.tcp is not None and not 0 <= args.tcp <= 65535:
        parser.error("TCP port must be in 0..65535")
    board = Board("emulated", "EMULATED") if args.backend == "emulated" else detect_board()
    if args.board:
        board = Board(args.board, board.model, board.chip_labels)
    try:
        profile = get_profile(board.id)
    except KeyError:
        profile = None
    kind = args.backend
    if kind == "auto":
        kind = ("lgpio" if board.id.startswith("raspberry_pi_")
                and importlib.util.find_spec("lgpio") is not None else "software")
    backend = None
    agent = None
    stop = threading.Event()
    previous = {}
    try:
        if kind == "emulated":
            backend = EmulatedBackend()
        else:
            factory = {"gpiod": GpiodBackend, "lgpio": LgpioBackend, "software": SoftwarePWMBackend}[kind]
            labels = sorted({p["gpiochip"] for p in profile["pins"].values()
                             if p.get("usable") and p.get("gpiochip")}) if profile else []
            if labels and not args.chip:
                chips = {}
                try:
                    for label in labels:
                        chips[label] = factory((label,))
                except Exception:
                    for chip in chips.values():
                        chip.close()
                    raise
                backend = next(iter(chips.values())) if len(chips) == 1 else MultiChipBackend(chips)
            else:
                backend = factory(board.chip_labels, chip_path=args.chip)
        agent = SBCAgent(backend, board=board, profile=profile)
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
        if args.tcp is None:
            serve_stream(agent, sys.stdin.fileno(), sys.stdout.fileno(), stop=stop)
        else:
            serve_tcp(agent, args.tcp, host=args.host, stop=stop)
        return 0
    except (ImportError, OSError, RuntimeError) as error:
        print(f"robo-prober SBC: {error}", file=sys.stderr)
        return 1
    finally:
        try:
            if agent:
                agent.close()
            elif backend:
                backend.close()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
