##
## This file is part of the BACNet-uc HIL rig (sigrok protocol decoder).
## SPDX-License-Identifier: GPL-3.0-or-later
##

"""
BACnet MS/TP (ANSI/ASHRAE 135 Clause 9) frames on top of the 'uart' decoder.

Frame: 0x55 0xFF, frame type, destination, source, length (MSB first), header CRC-8, then
'length' data octets and a CRC-16 (LSB first) when length > 0. Extended frame types 32..127
carry COBS-encoded data and a CRC-32K instead (length field = encoded size - 2).

The receive state machine is the one of bacnet-stack's MSTP_Receive_Frame_FSM(): more than
Tframe_abort (60 bit times) between two characters aborts a frame ('timeout'), a UART framing
error inside a frame gives 'receive_error', and the CRCs give 'bad_header_crc' /
'bad_data_crc'. Inter-octet gaps above Tframe_gap (20 bit times) are flagged as warnings.
Set the 'baudrate' option to the uart decoder's rate; it is only used for these two limits.

The 'frame' annotation is machine readable, for sigrok-cli -A bacnet_mstp=frame:
  FRAME ch=RX type=TOKEN ft=0 dst=5 src=7 len=0 hcrc=ok dcrc=- err=-

Use from the HIL repository (hil/tests/unit/test_la.py runs this on generated captures):
  SIGROKDECODE_DIR=hil/decoders sigrok-cli -i capture.sr \
      -P uart:rx=D2:baudrate=38400,bacnet_mstp:baudrate=38400 -A bacnet_mstp=frame
In PulseView, start it with SIGROKDECODE_DIR=<repo>/hil/decoders and stack 'BACnet MS/TP'
on a UART decoder.
"""

from .pd import Decoder
