I'm building a BACnet stack for a microcontroller (bare-metal C11, no heap, no stdio in the
library). Implement `bacapp.c` for the API in `bacapp.h`: encoding and decoding of BACnet
application-tagged primitive values (ASHRAE 135 clause 20.2), plus the context-tag helpers
declared there. `driver.c` is a small CLI wrapper used for testing; `make` builds it as
`drv`. My acceptance test is `acceptance.vec`: `python3 run_vectors.py acceptance.vec`
must pass.
