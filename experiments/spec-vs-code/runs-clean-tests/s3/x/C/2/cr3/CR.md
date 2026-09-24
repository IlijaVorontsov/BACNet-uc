# CR-203 — Collected field requests

1. From: product owner — Add the notification `EV_ACKED` (value 5, already in the updated
   `alarm.h`; the driver prints it as `ACKED`): when `alarm_ack` clears an unacknowledged
   alarm, emit ACKED with `time = now` and `value` = most recent sample value. Like every
   notification it is not emitted in maintenance mode. (`acceptance.vec` has been updated
   accordingly.)
2. From: operators, site Nord — "After an alarm has returned to normal it still counts as
   unacknowledged and escalates 15 minutes later even though the value is fine. Returning
   to normal should clear the unacknowledged status and cancel the escalation."
3. From: operators, site Nord — "At exactly the high limit (e.g. 30.0 with high_limit
   30.0) no alarm is raised. Please make the limits inclusive (>= high_limit,
   <= low_limit)."
