##
## This file is part of the BACNet-uc HIL rig (sigrok protocol decoder).
## SPDX-License-Identifier: GPL-3.0-or-later
##

"""
Frame assembly for the bacnet_mstp decoder, free of sigrokdecode so it can be unit-tested.

It is the Clause 9 Receive Frame state machine of hilrig.mstp.FrameParser (bacnet-stack
MSTP_Receive_Frame_FSM), run on sample numbers: the uart decoder hands over each character
with ss = start of the start bit, es = end of the stop bit and a validity flag. The CRC and
COBS code repeats hilrig.mstp because a sigrok decoder directory must be self-contained; the
unit tests compare the two on the same octet streams.
"""

from collections import namedtuple

HEADER_LEN = 8
COBS_TYPES = range(32, 128)

Octet = namedtuple("Octet", "ss es value valid")
# error: None or one of 'timeout', 'receive_error', 'bad_header_crc', 'bad_data_crc'
# (the values of hilrig.mstp.RxError); data: the NPDU of a valid frame.
Record = namedtuple("Record", "octets error data")

FRAME_TYPES = {
    0: "TOKEN",
    1: "POLL_FOR_MASTER",
    2: "REPLY_TO_POLL_FOR_MASTER",
    3: "TEST_REQUEST",
    4: "TEST_RESPONSE",
    5: "BACNET_DATA_EXPECTING_REPLY",
    6: "BACNET_DATA_NOT_EXPECTING_REPLY",
    7: "REPLY_POSTPONED",
    32: "BACNET_EXTENDED_DATA_EXPECTING_REPLY",
    33: "BACNET_EXTENDED_DATA_NOT_EXPECTING_REPLY",
    34: "IPV6_ENCAPSULATION",
}


def frame_type_name(ftype):
    return FRAME_TYPES.get(ftype, "TYPE_%d" % ftype)


def crc8(data, crc=0xFF):
    """CRC_Calc_Header() of bacnet-stack crc.c, over several octets."""
    for octet in data:
        c = crc ^ octet
        c = c ^ (c << 1) ^ (c << 2) ^ (c << 3) ^ (c << 4) ^ (c << 5) ^ (c << 6) ^ (c << 7)
        crc = ((c & 0xFE) ^ ((c >> 8) & 1)) & 0xFF
    return crc


def crc16(data, crc=0xFFFF):
    """CRC_Calc_Data() of bacnet-stack crc.c, over several octets."""
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
    """cobs_crc32k() of bacnet-stack cobs.c, over several octets."""
    for octet in data:
        for _ in range(8):
            mix = (octet ^ crc) & 1
            crc >>= 1
            if mix:
                crc ^= 0xEB31D82E
            octet >>= 1
    return crc


def cobs_decode(data, mask=0x55):
    """cobs_decode() of bacnet-stack cobs.c; None for a malformed encoding."""
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


def cobs_frame_decode(field):
    """Payload of an extended frame's data field, None on bad encoding or CRC-32K."""
    if len(field) < 5:
        return None
    body, crc_field = field[:-5], field[-5:]
    data, crc_octets = cobs_decode(body), cobs_decode(crc_field)
    if not data or crc_octets is None or len(crc_octets) != 4:
        return None
    return data if crc32k(crc_octets, crc32k(body)) == 0x0843323B else None


class Assembler:
    """Turns uart characters into Records. ``abort`` is Tframe_abort in samples (None: never)."""

    def __init__(self, abort=None):
        self.abort = abort
        self.state = "IDLE"
        self.octets = []
        self.need = 0
        self.last_es = None

    def feed(self, octet):
        """Process one character; return the Record it completed or aborted, else None."""
        silence = None if self.last_es is None else octet.es - self.last_es
        self.last_es = octet.es
        if self.state != "IDLE" and self.abort is not None and silence > self.abort:
            timed_out = self.state in ("HEADER", "DATA")
            record = self._finish("timeout") if timed_out else None
            self._reset()
            self._idle(octet)
            return record
        return getattr(self, "_" + self.state.lower())(octet)

    def _reset(self):
        self.state = "IDLE"
        self.octets = []

    def _finish(self, error, data=b""):
        record = Record(tuple(self.octets), error, data)
        self._reset()
        return record

    def _idle(self, octet):
        if octet.valid and octet.value == 0x55:
            self.octets = [octet]
            self.state = "PREAMBLE"

    def _preamble(self, octet):
        if not octet.valid:
            self._reset()
        elif octet.value == 0xFF:
            self.octets.append(octet)
            self.state = "HEADER"
        elif octet.value == 0x55:
            self.octets = [octet]
        else:
            self._reset()

    def _header(self, octet):
        self.octets.append(octet)
        if not octet.valid:
            return self._finish("receive_error")
        if len(self.octets) < HEADER_LEN:
            return None
        if crc8(o.value for o in self.octets[2:HEADER_LEN]) != 0x55:
            return self._finish("bad_header_crc")
        length = self.octets[5].value << 8 | self.octets[6].value
        if length == 0:
            return self._finish(None)
        self.need = HEADER_LEN + length + 2
        self.state = "DATA"
        return None

    def _data(self, octet):
        self.octets.append(octet)
        if not octet.valid:
            return self._finish("receive_error")
        if len(self.octets) < self.need:
            return None
        field = bytes(o.value for o in self.octets[HEADER_LEN:])
        if self.octets[2].value in COBS_TYPES:
            data = cobs_frame_decode(field)
        else:
            data = field[:-2] if crc16(field) == 0xF0B8 else None
        return self._finish("bad_data_crc") if data is None else self._finish(None, data)
