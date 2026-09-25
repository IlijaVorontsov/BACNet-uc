# HIL handover: Bluetooth mesh bench (start here)

For two readers: **the helper**, who builds and wires the bench, and **the Claude Code session on the host PC**, which drives the Raspberry Pi over SSH and writes the code. State as of 2026-09-25.

Shared log: the *Mesh Bench Notebook* page (link from the user). If you cannot write there, use [lab-notebook.md](lab-notebook.md) in this folder.

## 1. What exists today

- **The project.** BACNet-uc is Zephyr firmware for building automation (BACnet/IP, MQTT over TLS) plus tools. Several Claude sessions work on separate branches and coordinate through `docs/SESSION_NOTES.md` on each branch. `main` holds the BACnet product.
- **The HIL work so far** (this branch, `claude/hardware-in-loop-testing-x74tww`):
  - [docs/HIL.md](../../HIL.md): the design for the Ethernet rig (FRDM-MCXN947 DUT, FRDM-MCXN236 stimulus), reviewed twice.
  - [hil/](../../../hil/README.md): the Python test package `hilrig`. 337 tests pass in simulation. It has a logic-analyzer backend for the FX2 (sigrok), edge extraction, a clock fit, bench files and pytest fixtures. All of that is reusable here.
  - `apps/hil_stimulus`: firmware for a stimulus board. It builds but has not run on hardware.
- **Verified vs not.** Everything was checked in simulation (`native_sim`), where the real BACnet and MQTT firmware passed the first-iteration tests. **Nothing has run on real hardware yet.** This bench is the first contact with hardware.
- **Why a new bench.** The main transport for the next work is **Bluetooth mesh**. So this bench tests two mesh nodes on the air, not Ethernet. The method carries over unchanged: every result is a number measured on the wire, with a pass criterion written down first.

## 2. The bench

| Item | Role |
|---|---|
| nRF54L15 DK **A** | mesh node under test |
| nRF54L15 DK **B** | second mesh node (peer) |
| FX2 8-channel logic analyzer (24 MS/s, sigrok) | watches button and LED pins of **both** DKs on one clock, so node-to-node latency needs no clock sync |
| Raspberry Pi 4 (new SD image) | test host: flashes the DKs, talks to their serial consoles, runs the analyzer and the tests |
| Powered USB hub | DK A, DK B and the FX2 hang off it; the Pi's own USB ports all switch together, so only a hub that `uhubctl` supports can power-cycle one board |
| Host PC (on the same network) | runs Claude Code, reaches the Pi with SSH |

Board facts (Zephyr v4.4.2, `boards/nordic/nrf54l15dk`): target `nrf54l15dk/nrf54l15/cpuapp`. LEDs 0-3 are on P2.09, P1.10, P2.07 and P1.14 (active high). Buttons 0-3 are on P1.13, P1.09, P1.08 and P0.04 (active low, pull-up). Flash runners: `nrfutil` (default) and `jlink`.

## 3. Who does what

- **Helper:** physical work (imaging, cabling, wiring, multimeter checks, photos), watching the boards, recording results and lessons.
- **Claude on the host PC:** everything over SSH (installing, building, flashing, capturing, analysing), writing code and updating the docs. Before each wiring step it says exactly what to connect, and it waits for the helper to confirm.
- **The user:** the Wi-Fi name and password (ask them, never write them into any file or the notebook), purchases, and decisions.

The loop: Claude proposes a step, the helper does the physical part and confirms (with a photo when wiring), Claude runs it and records the numbers, and both add lessons.

## 4. Safety rules (read before touching a pin)

1. **The DKs ship with VDD = 1.8 V.** The Pi GPIO and the FX2 are 3.3 V logic. Set both DKs to **3.3 V** with nRF Connect Board Configurator (a desktop app; run it once on the host PC with each DK attached), then **measure VDD with a multimeter** before any wire goes to the FX2 or the Pi. Record the reading.
2. **Never connect 5 V to any GPIO** of a DK or the Pi. The Pi's GPIOs are not 5 V tolerant.
3. **One ground.** Connect FX2 GND to both DKs' GND before the signal wires, and keep every board on the same powered hub.
4. **Power off before rewiring.** Unplug the hub, change the wires, check them against the sheet, then power up.
5. **The FX2 only listens.** Put a 1 kΩ resistor in series with any wire that the Pi drives. Never drive an LED pin.
6. **Stop and ask** if a reading is off by more than the step says, a board gets warm, or a DK stops enumerating.

## 5. Raspberry Pi setup (helper, then Claude)

1. **Image.** On the host PC, use Raspberry Pi Imager to write **Raspberry Pi OS Lite (64-bit)**. In its settings:
   - hostname `hil-pi`, user `hil`;
   - SSH on, with **public-key login only** (paste the host PC's public key);
   - Wi-Fi: **ask the user** for the network name and password and type them into Imager only;
   - your time zone.
2. **First boot.** Put in the SD card, power the Pi and wait 2 minutes. From the host PC: `ssh hil@hil-pi.local`. If that name does not resolve, find the Pi's address in the router list.
3. **Base tools** (Claude over SSH):
   ```sh
   sudo apt update && sudo apt full-upgrade -y && sudo rpi-eeprom-update -a
   sudo apt install -y git python3-venv python3-dev build-essential sigrok-cli \
       sigrok-firmware-fx2lafw uhubctl gpiod python3-libgpiod minicom chrony
   git clone https://github.com/IlijaVorontsov/BACNet-uc.git ~/BACNet-uc   # public repository
   cd ~/BACNet-uc && git checkout claude/hardware-in-loop-testing-x74tww
   python3 -m venv ~/.venvs/hil && ~/.venvs/hil/bin/pip install -e 'hil[dev]'
   ~/.venvs/hil/bin/pytest -q hil/tests/unit            # expect pass; namespace tests skip without root
   ```
4. **Flashing tools.** Install `nrfutil` for Linux arm64 and its `device` command, plus the SEGGER J-Link software for Linux ARM64 (a licence click-through on SEGGER's site; the helper downloads it on the host PC and copies it over). Check with `nrfutil device list`: it must show both DKs with their serial numbers. **Unverified:** that the arm64 builds exist for the versions current today. If they do not, flash from the host PC instead and record that as a lesson.
5. **Analyzer check.** `sigrok-cli --scan` must list `fx2lafw`. Then `sigrok-cli -d fx2lafw --config samplerate=24m --time 60s -o /tmp/t.sr` must finish without "samples lost". Try the FX2 on the hub and on a Pi port, and keep whichever passes.
6. **Hub check.** `uhubctl` lists the hubs that can switch power per port. Record whether the helper's hub is on that list.

## 6. Wiring (after safety rule 1 is done)

| FX2 channel | Signal | Pin | Why |
|---|---|---|---|
| CH0 | DK A button 0 | P1.13 | stimulus edge (press) |
| CH1 | DK A LED 0 | P2.09 | local reaction |
| CH2 | DK B LED 0 | P2.09 | remote reaction (mesh delivery) |
| CH3 | DK B button 0 | P1.13 | reverse direction |
| CH4-CH7 | spare markers (firmware GPIOs, later) | Claude names them | timing inside the firmware |
| GND | both DKs' GND | any GND pin | common ground, connect first |

Find each pin on the DK headers by its silkscreen name (P1.13 and so on) and photograph the wiring. Optional, once the above works: a Pi GPIO pulls DK A button 0 low through 1 kΩ to press the button automatically. The Pi's pin is only ever an input (released) or driven low (pressed), never high.

## 7. Experiments

Each has a goal, an expected result and what to record. Run them in order and record everything in the notebook.

| # | Experiment | Expected / record |
|---|---|---|
| E0 | Inventory: photos of every board and its labels, DK serial numbers, FX2 model and USB ID, hub model, Pi model and RAM | complete list |
| E1 | Image the Pi and log in with SSH (§5.1-5.2) | SSH works with the key only; hostname hil-pi |
| E2 | Base tools and hilrig unit tests on the Pi (§5.3) | test counts; anything that fails on ARM64 |
| E3 | Set both DKs to 3.3 V and measure (safety rule 1) | VDD reading of each DK (3.25-3.35 V) |
| E4 | Flash a blinky sample to each DK **by serial** from the Pi (`west flash --dev-id <serial>`, or nrfutil) | both blink; commands and times |
| E5 | FX2 bring-up: capture DK A LED 0 blinking | measured period matches the sample's (±1 %); no lost samples in 60 s |
| E6 | Mesh bring-up: build a Bluetooth mesh sample for both DKs (Zephyr `samples/bluetooth/mesh`, or a node with `CONFIG_BT_MESH_SHELL` for serial control), provision both, send Generic OnOff | button on A switches the LED on B; provisioning time |
| E7 | Latency: 100 presses on A, with CH0 → CH2 edge-to-edge on the FX2 | min / median / p95 / max in ms; any missed deliveries |
| E8 | Reliability: 1000 OnOff messages at a fixed rate (Claude drives the serial shell) | delivered count, loss %, and the same test at 1 m and 5 m |
| E9 | Persistence: power-cycle each DK (hub port or unplug), then check the nodes are still provisioned and working | pass or fail per cycle over 10 cycles |
| E10 | Write-up: lessons in the notebook and in [learnings.md](learnings.md); Claude publishes a summary page for future projects | done |

Decide before E6 (Claude checks and reports): upstream Zephyr v4.4.2 or the nRF Connect SDK for mesh on the nRF54L15. The NCS has the qualified mesh stack and the full model set; upstream keeps the same Zephyr as the rest of the repo. Build first with upstream, and switch if the controller or crypto support is missing.

## 8. Next steps for the Claude session on the host PC

1. Read this file, [learnings.md](learnings.md), `hil/README.md` and `hil/src/hilrig/la.py` (the sigrok backend: `open_analyzer("sigrok", channels)`, then `acquire()`, then edges per channel).
2. Build for the DKs from a workspace that includes `hal_nordic`. The repo's `west.yml` allowlist does not include it, so use a separate west workspace for Nordic builds and record the exact manifest.
3. Add, small and tested: `hil/src/hilrig/meshshell.py` (a serial driver for the Zephyr mesh shell: provision, send OnOff, read status), `hil/tools/latency.py` (edges from two FX2 channels to a latency distribution), `hil/host/bench-nrf54-mesh.yml` (serials, channels), and pytest tests `hil/tests/mesh/test_e7_latency.py` and `test_e8_reliability.py` with the pass criteria written down before running.
4. Keep this file and the notebook current. Note anything other sessions need to know in `docs/SESSION_NOTES.md` on this branch.
