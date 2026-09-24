Implement `alarm.c` for the API in `alarm.h`: out-of-range alarming for analog input points
on our BACnet controller (bare-metal C11, no heap). Requirements: high/low limits with a
deadband and a time delay; sensor faults; a maintenance mode in which notifications are
suppressed; operator acknowledgement; and an escalation notification if an alarm stays
unacknowledged for `escalate_after_s` seconds. `driver.c` is a small CLI wrapper used for
testing; `make` builds it as `drv`. My acceptance test is `acceptance.vec`:
`python3 run_vectors.py acceptance.vec` must pass.
