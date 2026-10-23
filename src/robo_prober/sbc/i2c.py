"""Read-only SMBus receive-byte probing; never issue a write transaction.

SMBus write_quick sends a WRITE, and zero-length I2C reads are unsupported by
some Pi controllers. Receive-byte is the portable read-only quick probe: it
sends no register address and leaves claimed slave addresses alone.
"""
import errno


class SMBusScanner:
    def scan(self, bus: int) -> list[int]:
        # Lazy: importing the agent or using emulation needs no smbus2.
        from smbus2 import SMBus

        found = []
        with SMBus(f"/dev/i2c-{bus}") as device:
            for address in range(0x08, 0x78):
                try:
                    device.read_byte(address)
                except OSError as error:
                    if error.errno not in (errno.ENXIO, errno.EREMOTEIO, errno.EIO, errno.EBUSY):
                        # Unsupported quick reads must not look like an empty bus.
                        raise
                else:
                    found.append(address)
        return found
