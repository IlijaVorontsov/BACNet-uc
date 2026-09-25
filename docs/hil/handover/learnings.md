# Learnings for future HIL projects

Seeded with what this project has taught so far. Add new lessons at the end of each section: one line saying what happened, then one saying what to do next time.

## Planning and coordination
- **The platform moved under the design twice in one day** (Zephyr 3.7 on H563, then 4.4 on F767, then MCXN947). *Next time:* ask which boards are owned before designing. Re-read the other branches before each revision (`hil/tools/survey.sh` automates that).
- **Parallel sessions can't message each other.** A per-branch `docs/SESSION_NOTES.md` with a "requests / status" table worked, and other sessions picked up the HIL contract from it. *Next time:* keep the requests small (six items) and numbered.
- **Adversarial reviews paid off.** They caught a load switch that could never turn on, wrong connector pins, a snippet key that was silently ignored, and a "passing" test that could not fail. *Next time:* review every design and every test suite before hardware arrives.

## Hardware and wiring
- **I/O voltages differ between boards:** nRF54L DKs ship at 1.8 V, while the MCX, Pi and FX2 are 3.3 V. *Next time:* measure VDD before wiring.
- **Series resistors (1-2.2 kΩ) on every rig wire** limit the damage from two outputs fighting and from powering a board through its pins while it is off.
- **Reset and power control must fail safe:** a stimulus that is off or in reset must leave the device under test running (open-drain driver, pull-up on the device side).
- **The Pi 4's USB ports switch power all together.** *Next time:* use a per-port hub that `uhubctl` supports, or a load switch.

## Tools
- **Select the debug probe by serial, always.** `west flash --serial` did not select the probe on nucleo_f767zi, and the linkserver runner ignores `-i`.
- **Zephyr 4.x snippet board keys need the full target** (`nucleo_f767zi/stm32f767xx`). A bare board name is ignored without any error.
- **tshark prints "Capturing on" before the capture is live.** Send sentinel packets until one shows up. The same idea applies to any recorder: prove it is recording before the test starts.
- **The encryption key log is the best debugging aid for encrypted protocols** (mosquitto 2.1 `--tls-keylog`). For Bluetooth mesh the equivalent is giving Wireshark the NetKey/AppKey.
- **Anything that uses network namespaces needs root**, and test code must not depend on the user's git or shell configuration.

## Measurement
- **Take timing only from one clock**: the logic analyzer or the stimulus counter, never host timestamps. Put both edges on the analyzer and no clock sync is needed.
- **Write the pass criterion with a number before running the test.**

## Bluetooth mesh bench (the helper adds these)
- …

## Working with Claude (the helper adds these)
- …
