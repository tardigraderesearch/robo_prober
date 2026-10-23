"""Bounded line framing and nonblocking stdio/TCP service.

The loop ticks even with silent peers, partial commands, or blocked output.
Only one TCP session controls the agent at a time; disconnect always disarms.
"""
from __future__ import annotations

import os
import select
import socket
import threading
import time

from ..reference_device import MAX_LINE


class LineFramer:
    def __init__(self):
        self.pending = bytearray()
        self.overflow = False

    def feed(self, data):
        for byte in data:
            if byte == 10:
                if self.overflow:
                    yield None
                else:
                    yield bytes(self.pending).removesuffix(b"\r").decode("ascii", errors="replace")
                self.pending.clear()
                self.overflow = False
            elif not self.overflow:
                self.pending.append(byte)
                # One extra byte allows a CRLF terminator after MAX_LINE bytes.
                if len(self.pending) > MAX_LINE + 1:
                    self.pending.clear()
                    self.overflow = True


def now_ms():
    return time.monotonic_ns() // 1_000_000


def serve_stream(agent, read_fd, write_fd, *, stop=None):
    stop = stop or threading.Event()
    flags = {fd: os.get_blocking(fd) for fd in {read_fd, write_fd}}
    framer, replies = LineFramer(), bytearray()
    eof = False
    try:
        for fd in flags:
            os.set_blocking(fd, False)
        while not stop.is_set():
            agent.tick(now_ms())
            if eof and not replies:
                break
            readable, writable, _ = select.select(
                [] if eof else [read_fd], [write_fd] if replies else [], [], 0.01)
            if readable:
                try:
                    data = os.read(read_fd, 4096)
                except BlockingIOError:
                    data = None
                if data == b"":
                    eof = True
                    agent.handle("DISARM", now_ms())
                elif data:
                    for line in framer.feed(data):
                        reply = ("RP1 ERR reason=line_too_long" if line is None
                                 else agent.handle(line, now_ms()))
                        replies.extend((reply + "\n").encode("ascii"))
                    if len(replies) > 65536:
                        break  # unresponsive client; bounded memory and release
            if writable:
                try:
                    count = os.write(write_fd, replies)
                except BlockingIOError:
                    pass
                else:
                    del replies[:count]
    finally:
        agent.handle("DISARM", now_ms())
        for fd, blocking in flags.items():
            os.set_blocking(fd, blocking)


def serve_tcp(agent, port, *, host="127.0.0.1", stop=None, ready=None):
    stop = stop or threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(1)
        if ready:
            ready(listener.getsockname()[1])
        while not stop.is_set():
            if not select.select([listener], [], [], 0.05)[0]:
                continue
            connection, _ = listener.accept()
            with connection:
                try:
                    serve_stream(agent, connection.fileno(), connection.fileno(), stop=stop)
                except (BrokenPipeError, ConnectionResetError):
                    pass
