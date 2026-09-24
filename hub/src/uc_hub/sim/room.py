"""Single-zone thermal room for simulated nodes.

The room loses heat to the outdoors with time constant ``tau_s`` and gains
it from a hot-water valve (0..100 %) and an on/off heater::

    dT/dt = (outdoor - T) / tau + valve/100 * valve_gain + heater * heater_gain

A ``SimNode`` with a room reads the valve and heater from its output
channels (``ao0``, ``do0``) and drives the temperature sensor channel
(``ai0``, millivolts, the inverse of the io.json scaling) on every step. Time
comes from the node's clock, so tests advance it by hand.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RoomModel:
    temp_c: float = 20.0
    outdoor_c: float = 5.0
    tau_s: float = 900.0
    #: Temperature rise per second at 100 % valve (steady state 100 % =
    #: outdoor + tau * valve_gain).
    valve_gain_c_per_s: float = 0.04
    heater_gain_c_per_s: float = 0.02
    #: Sensor transfer, the io.json ``scale``/``offset`` of the sensor point:
    #: T = mV * scale + offset.
    sensor_scale: float = 0.1
    sensor_offset: float = -50.0
    sensor_channel: str = "ai0"
    valve_channel: str = "ao0"
    heater_channel: str = "do0"
    #: Euler sub-step; well below tau keeps the integration stable.
    max_step_s: float = 5.0

    def __post_init__(self) -> None:
        if self.tau_s <= 0 or self.max_step_s <= 0:
            raise ValueError("tau_s and max_step_s must be positive")
        if self.sensor_scale == 0:
            raise ValueError("sensor_scale must not be 0")

    def advance(self, dt_s: float, valve_pct: float, heater_on: bool) -> float:
        """Integrate ``dt_s`` seconds with constant inputs; returns the new
        temperature."""
        if dt_s <= 0:
            return self.temp_c
        valve = min(max(valve_pct, 0.0), 100.0) / 100.0
        gain = valve * self.valve_gain_c_per_s + (self.heater_gain_c_per_s if heater_on else 0.0)
        remaining = dt_s
        while remaining > 0:
            h = min(remaining, self.max_step_s)
            self.temp_c += h * ((self.outdoor_c - self.temp_c) / self.tau_s + gain)
            remaining -= h
        return self.temp_c

    def sensor_mv(self) -> float:
        return (self.temp_c - self.sensor_offset) / self.sensor_scale

    def steady_state_c(self, valve_pct: float, heater_on: bool = False) -> float:
        valve = min(max(valve_pct, 0.0), 100.0) / 100.0
        gain = valve * self.valve_gain_c_per_s + (self.heater_gain_c_per_s if heater_on else 0.0)
        return self.outdoor_c + self.tau_s * gain
