##
## This file is part of the BACNet-uc HIL rig (sigrok protocol decoder).
## SPDX-License-Identifier: GPL-3.0-or-later
##

import sigrokdecode as srd

from .core import COBS_TYPES, HEADER_LEN, Assembler, Octet, frame_type_name

T_FRAME_GAP_BITS = 20
T_FRAME_ABORT_BITS = 60


class Ann:
    PREAMBLE, TYPE, DST, SRC, LEN, HCRC, DATA, DCRC, FRAME, ERROR, WARN = range(11)


class Decoder(srd.Decoder):
    api_version = 3
    id = "bacnet_mstp"
    name = "BACnet MS/TP"
    longname = "BACnet Master-Slave/Token-Passing"
    desc = "BACnet MS/TP data link frames (RS-485)."
    license = "gplv3+"
    inputs = ["uart"]
    outputs = []
    tags = ["Embedded/industrial"]
    annotations = (
        ("preamble", "Preamble"),
        ("type", "Frame type"),
        ("dst", "Destination"),
        ("src", "Source"),
        ("len", "Length"),
        ("hcrc", "Header CRC"),
        ("data", "Data"),
        ("dcrc", "Data CRC"),
        ("frame", "Frame"),
        ("error", "Receive error"),
        ("warning", "Timing warning"),
    )
    annotation_rows = (
        (
            "fields",
            "Fields",
            (Ann.PREAMBLE, Ann.TYPE, Ann.DST, Ann.SRC, Ann.LEN, Ann.HCRC, Ann.DATA, Ann.DCRC),
        ),
        ("frames", "Frames", (Ann.FRAME,)),
        ("errors", "Errors", (Ann.ERROR, Ann.WARN)),
    )
    options = (
        {
            "id": "channel",
            "desc": "UART direction to decode",
            "default": "RX",
            "values": ("RX", "TX", "both"),
        },
        {"id": "baudrate", "desc": "Baud rate (for Tframe_abort and Tframe_gap)", "default": 38400},
    )

    def __init__(self):
        self.reset()

    def reset(self):
        self.samplerate = None
        self.assemblers = {}

    def start(self):
        self.out_ann = self.register(srd.OUTPUT_ANN)

    def metadata(self, key, value):
        if key == srd.SRD_CONF_SAMPLERATE:
            self.samplerate = value

    def bit_samples(self):
        if not self.samplerate:
            return None
        return self.samplerate / float(self.options["baudrate"])

    def ann(self, ss, es, cls, texts):
        self.put(ss, es, self.out_ann, [cls, texts])

    def span(self, octets, first, last, cls, texts):
        if len(octets) > last:
            self.ann(octets[first].ss, octets[last].es, cls, texts)

    def annotate(self, rxtx, record):
        o, error = record.octets, record.error
        ftype = o[2].value if len(o) > 2 else None
        tname = "?" if ftype is None else frame_type_name(ftype)
        self.span(o, 0, 1, Ann.PREAMBLE, ["Preamble", "Pre", "P"])
        self.span(o, 2, 2, Ann.TYPE, [tname, str(ftype)])
        if len(o) > 3:
            self.span(o, 3, 3, Ann.DST, ["Dst %d" % o[3].value, "%d" % o[3].value])
        if len(o) > 4:
            self.span(o, 4, 4, Ann.SRC, ["Src %d" % o[4].value, "%d" % o[4].value])
        length = o[5].value << 8 | o[6].value if len(o) > 6 else None
        if length is not None:
            self.span(o, 5, 6, Ann.LEN, ["Len %d" % length, "%d" % length])
        if error == "bad_header_crc":
            hcrc = "bad"
        elif len(o) > 8 or (len(o) == 8 and error is None):
            hcrc = "ok"
        else:
            hcrc = "-"
        self.span(o, 7, 7, Ann.HCRC, ["Header CRC " + hcrc, hcrc])
        dcrc = "-"
        if length and len(o) > 8:
            dcrc = {None: "ok", "bad_data_crc": "bad"}.get(error, "-")
            crc_len = 5 if ftype in COBS_TYPES else 2  # extended frames: encoded CRC-32K
            last = HEADER_LEN + length + 1
            data_last = min(len(o) - 1, last - crc_len)
            if data_last >= HEADER_LEN:
                shown = (
                    record.data if error is None else bytes(x.value for x in o[HEADER_LEN : data_last + 1])
                )
                self.span(o, HEADER_LEN, data_last, Ann.DATA, [" ".join("%02X" % b for b in shown), "Data"])
            self.span(o, max(HEADER_LEN, last - crc_len + 1), last, Ann.DCRC, ["Data CRC " + dcrc, dcrc])
        text = "FRAME ch=%s type=%s ft=%s dst=%s src=%s len=%s hcrc=%s dcrc=%s err=%s" % (
            ("RX", "TX")[rxtx],
            tname,
            ftype,
            o[3].value if len(o) > 3 else "-",
            o[4].value if len(o) > 4 else "-",
            "-" if length is None else length,
            hcrc,
            dcrc,
            error or "-",
        )
        self.ann(o[0].ss, o[-1].es, Ann.FRAME, [text, "%s %s" % (tname, error or "ok"), tname])
        if error:
            self.ann(o[0].ss, o[-1].es, Ann.ERROR, [error])
        bit = self.bit_samples()
        if bit:
            gaps = [(b.ss - a.es) / bit for a, b in zip(o, o[1:])]
            if gaps and max(gaps) > T_FRAME_GAP_BITS:
                self.ann(
                    o[0].ss, o[-1].es, Ann.WARN, ["Tframe_gap exceeded: %.1f bit" % max(gaps), "Tframe_gap"]
                )

    def decode(self, ss, es, data):
        ptype, rxtx, pdata = data
        if ptype != "FRAME":
            return
        want = self.options["channel"]
        if want != "both" and ("RX", "TX")[rxtx] != want:
            return
        if rxtx not in self.assemblers:
            bit = self.bit_samples()
            self.assemblers[rxtx] = Assembler(None if bit is None else T_FRAME_ABORT_BITS * bit)
        value, valid = pdata
        record = self.assemblers[rxtx].feed(Octet(ss, es, value, valid))
        if record is not None:
            self.annotate(rxtx, record)
