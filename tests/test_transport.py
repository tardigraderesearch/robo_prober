import threading

import pytest

from robo_prober.transport import ProtocolError, PtyReferenceEmulator, SerialProtocolTransport, TransportTimeout, parse_response


def serve(emulator):
    while emulator.master_fd is not None:
        try:
            emulator.serve_once(timeout_s=0.1)
        except OSError:
            return


def test_parse_response_rejects_malformed_ack():
    with pytest.raises(ProtocolError):
        parse_response("OK sure")


def test_transport_uses_real_pty_loopback():
    emulator = PtyReferenceEmulator()
    port = emulator.open()
    thread = threading.Thread(target=serve, args=(emulator,), daemon=True)
    thread.start()
    try:
        with SerialProtocolTransport(port, timeout_s=1.0) as transport:
            assert transport.hello()["id"] == "reference-emulator"
            transport.arm("test", cutoff_confirmed=True)
            assert transport.move_us(2, 1510)["target_us"] == "1510"
            transport.disarm()
    finally:
        emulator.close()
        thread.join(timeout=1.0)


def test_transport_stops_on_malformed_ack():
    emulator = PtyReferenceEmulator(malformed_after=2)
    port = emulator.open()
    thread = threading.Thread(target=serve, args=(emulator,), daemon=True)
    thread.start()
    try:
        with SerialProtocolTransport(port, timeout_s=1.0) as transport:
            transport.hello()
            transport.arm("test", cutoff_confirmed=True)
            with pytest.raises(ProtocolError, match="malformed"):
                transport.move_us(0, 1500)
    finally:
        emulator.close()
        thread.join(timeout=1.0)


def test_transport_timeout_without_emulator_response():
    emulator = PtyReferenceEmulator()
    port = emulator.open()
    try:
        with SerialProtocolTransport(port, timeout_s=0.1) as transport:
            with pytest.raises(TransportTimeout):
                transport.hello()
    finally:
        emulator.close()
