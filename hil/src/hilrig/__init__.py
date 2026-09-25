"""Host side of the BACnet-uc hardware-in-the-loop rig.

Modules: bench (bench.yml), stim (stimulus board), capture (per-test pcapng), netns (rig
topology), services (dnsmasq, mosquitto, chrony, s_server), bacnet (bacnet-stack tools),
mqtt (DUT topics), pki (test certificates), mstp / la / sync (MS/TP and logic analyzer).
"""

__version__ = "0.1.0"
