# BACnet MS/TP High Level Analyzer for Saleae Logic 2.
#
# Input: one Async Serial analyzer (8 data bits, no parity, 1 stop bit) on an RS-485 logic
# signal (DUT TX, DUT RX or a listen-only sniffer RO). Output: one 'frame' per MS/TP frame,
# valid or discarded, with the same receive rules as hilrig.mstp.FrameParser (bacnet-stack
# MSTP_Receive_Frame_FSM): Tframe_abort (60 bit times) between characters -> 'timeout', an
# Async Serial error inside a frame -> 'receive_error', 'bad_header_crc', 'bad_data_crc'
# (CRC-16, or COBS / CRC-32K for extended frame types 32..127).
#
# Logic 2 extensions must be self-contained, so the CRC and COBS code repeats hilrig.mstp;
# hil/tests/unit/test_mstp_decoders.py checks both against the same octet streams.
# Install: Logic 2 > Extensions > Load Existing Extension > this directory.

from saleae.analyzers import AnalyzerFrame, ChoicesSetting, HighLevelAnalyzer

BAUDS = ("38400", "9600", "19200", "57600", "76800", "115200")
T_FRAME_GAP_BITS = 20
T_FRAME_ABORT_BITS = 60
HEADER_LEN = 8
COBS_TYPES = range(32, 128)
FRAME_TYPES = {
    0: "TOKEN",
    1: "PFM",
    2: "REPLY_PFM",
    3: "TEST_REQ",
    4: "TEST_RESP",
    5: "DER",
    6: "DNER",
    7: "REPLY_POSTPONED",
    32: "EXT_DER",
    33: "EXT_DNER",
    34: "IPV6",
}


def crc8(data, crc=0xFF):
    for octet in data:
        c = crc ^ octet
        c = c ^ (c << 1) ^ (c << 2) ^ (c << 3) ^ (c << 4) ^ (c << 5) ^ (c << 6) ^ (c << 7)
        crc = ((c & 0xFE) ^ ((c >> 8) & 1)) & 0xFF
    return crc


def crc16(data, crc=0xFFFF):
    for octet in data:
        low = (crc & 0xFF) ^ octet
        crc = (
            (crc >> 8)
            ^ (low << 8)
            ^ (low << 3)
            ^ (low << 12)
            ^ (low >> 4)
            ^ (low & 0x0F)
            ^ ((low & 0x0F) << 7)
        ) & 0xFFFF
    return crc


def crc32k(data, crc=0xFFFFFFFF):
    for octet in data:
        for _ in range(8):
            mix = (octet ^ crc) & 1
            crc >>= 1
            if mix:
                crc ^= 0xEB31D82E
            octet >>= 1
    return crc


def cobs_decode(data, mask=0x55):
    out = []
    i, n = 0, len(data)
    while i < n:
        code = data[i] ^ mask
        if code == 0 or i + code > n:
            return None
        i += 1
        out.extend(b ^ mask for b in data[i : i + code - 1])
        i += code - 1
        if code != 255 and i < n:
            out.append(0)
    return bytes(out)


def data_valid(ftype, field):
    """True when the data field (data plus CRC octets) passes its CRC check."""
    if ftype not in COBS_TYPES:
        return crc16(field) == 0xF0B8
    if len(field) < 5:
        return False
    body, crc_field = field[:-5], field[-5:]
    data, crc_octets = cobs_decode(body), cobs_decode(crc_field)
    if not data or crc_octets is None or len(crc_octets) != 4:
        return False
    return crc32k(crc_octets, crc32k(body)) == 0x0843323B


class MstpHla(HighLevelAnalyzer):
    baud = ChoicesSetting(choices=BAUDS)

    result_types = {
        "frame": {
            "format": "{{data.type}} {{data.src}}->{{data.dst}} len={{data.length}} "
            "{{data.status}} gap={{data.max_gap_bits}}b"
        },
    }

    def __init__(self):
        self.bit_s = 1.0 / int(self.baud)
        self.state = "IDLE"
        self.octets = []  # (start_time, end_time, value)
        self.need = 0
        self.last_end = None

    def decode(self, frame):
        if frame.type != "data":
            return None
        value, valid = frame.data["data"][0], not frame.data.get("error")
        silence = None if self.last_end is None else float(frame.end_time - self.last_end)
        self.last_end = frame.end_time
        octet = (frame.start_time, frame.end_time, value)
        if self.state != "IDLE" and silence > T_FRAME_ABORT_BITS * self.bit_s:
            out = self.emit("timeout") if self.state in ("HEADER", "DATA") else None
            self.reset()
            self.idle(octet, valid)
            return out
        if self.state == "IDLE":
            return self.idle(octet, valid)
        if self.state == "PREAMBLE":
            return self.preamble(octet, valid)
        self.octets.append(octet)
        if not valid:
            return self.emit("receive_error")
        if self.state == "HEADER" and len(self.octets) == HEADER_LEN:
            if crc8(o[2] for o in self.octets[2:]) != 0x55:
                return self.emit("bad_header_crc")
            length = self.octets[5][2] << 8 | self.octets[6][2]
            if length == 0:
                return self.emit(None)
            self.need, self.state = HEADER_LEN + length + 2, "DATA"
        elif self.state == "DATA" and len(self.octets) == self.need:
            field = bytes(o[2] for o in self.octets[HEADER_LEN:])
            return self.emit(None if data_valid(self.octets[2][2], field) else "bad_data_crc")
        return None

    def reset(self):
        self.state = "IDLE"
        self.octets = []

    def idle(self, octet, valid):
        if valid and octet[2] == 0x55:
            self.octets, self.state = [octet], "PREAMBLE"

    def preamble(self, octet, valid):
        if valid and octet[2] == 0xFF:
            self.octets.append(octet)
            self.state = "HEADER"
        elif valid and octet[2] == 0x55:
            self.octets = [octet]
        else:
            self.reset()

    def emit(self, error):
        o = self.octets
        values = [x[2] for x in o]
        gaps = [float(b[0] - a[1]) / self.bit_s for a, b in zip(o, o[1:])]
        max_gap = max(gaps) if gaps else 0.0
        status = error or ("ok" if max_gap <= T_FRAME_GAP_BITS else "ok, Tframe_gap exceeded")
        out = AnalyzerFrame(
            "frame",
            o[0][0],
            o[-1][1],
            {
                "type": FRAME_TYPES.get(values[2], str(values[2])) if len(o) > 2 else "?",
                "dst": values[3] if len(o) > 3 else -1,
                "src": values[4] if len(o) > 4 else -1,
                "length": values[5] << 8 | values[6] if len(o) > 6 else -1,
                "status": status,
                "max_gap_bits": "%.1f" % max_gap,
            },
        )
        self.reset()
        return out
