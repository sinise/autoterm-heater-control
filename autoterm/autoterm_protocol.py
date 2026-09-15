"""
CRC and frame-splitting primitives for the Autoterm UART protocol -- kept
in its own module because a Supervisor add-on build can only see files
inside its own add-on directory (can't import from elsewhere in the repo).
See docs/PROTOCOL.md in the main repo for the full protocol derivation
(frame format, CRC algorithm, device roles) this implements.

Frame layout:

    AA | dev(1) | len(2, little-endian) | type(1) | payload(len) | crc16(2, big-endian)

    total frame length = 7 + len
    dev  0x03 = comfort panel, 0x04 = heater (0x00/0x02 = heater ack identities)
    crc  CRC-16/MODBUS (poly 0xA001 reflected, init 0xFFFF) over frame[0:-2],
         transmitted most-significant byte first
"""

KNOWN_DEV = {0x00: "heater/ack", 0x02: "heater/ack2", 0x03: "panel", 0x04: "heater"}
MAX_PAYLOAD = 250
HEADER_LEN = 5
CRC_LEN = 2


def _make_table():
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC_TABLE = _make_table()


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return crc


def crc_bytes(data: bytes) -> bytes:
    """CRC over data, in the order it appears on the wire (big-endian)."""
    return crc16_modbus(data).to_bytes(2, "big")


class Framer:
    """Incremental 0xAA-synchronised frame splitter with CRC validation."""

    def __init__(self):
        self.buf = bytearray()
        self.resyncs = 0
        self.dropped = 0
        self.bad_crc = 0

    def feed(self, data: bytes):
        """Append bytes, return a list of ('frame', raw, crc_ok) / ('stray', bytes)."""
        self.buf.extend(data)
        out = []
        while True:
            start = self.buf.find(0xAA)
            if start < 0:
                if self.buf:
                    out.append(("stray", bytes(self.buf)))
                    self.dropped += len(self.buf)
                    self.buf.clear()
                break
            if start > 0:
                out.append(("stray", bytes(self.buf[:start])))
                self.dropped += start
                del self.buf[:start]

            if len(self.buf) < HEADER_LEN:
                break

            length = int.from_bytes(self.buf[2:4], "little")

            if length > MAX_PAYLOAD:
                self._slip(out)
                continue

            total = HEADER_LEN + length + CRC_LEN
            if len(self.buf) < total:
                break

            raw = bytes(self.buf[:total])
            if crc_bytes(raw[:-CRC_LEN]) == raw[-CRC_LEN:]:
                out.append(("frame", raw, True))
                del self.buf[:total]
            else:
                if raw[1] in KNOWN_DEV:
                    self.bad_crc += 1
                    out.append(("frame", raw, False))
                self._slip(out)
        return out

    def _slip(self, out):
        self.resyncs += 1
        out.append(("stray", bytes(self.buf[:1])))
        del self.buf[:1]
