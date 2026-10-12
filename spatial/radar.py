"""Radar adapter interface, the documented replay adapter, and radar -> map conversion.

Only formats with a published description in this repository are implemented. A vendor sensor needs its
own adapter written against that vendor's documentation; see docs/spatial-data-formats.md.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .geometry import ConfigError, Vec, mat_mul, transpose
from .models import MEASUREMENT_UNITS, RadarMeasurement, SensorUnit
from .site import safe_path

FORMAT_ID = "omni-radar-jsonl/1"
MAX_REPLAY_BYTES = 64_000_000
MAX_REPLAY_FRAMES = 500_000
MAX_TARGETS_PER_FRAME = 64


@dataclass(frozen=True)
class RadarFrame:
    """One radar scan. ``measurements`` may be empty: an empty frame still proves the sensor is alive."""

    timestamp: float
    sensor_id: str
    measurements: tuple[RadarMeasurement, ...]
    rejected: int = 0


class RadarAdapter:
    """Contract for sensor adapters. Implementations convert a documented device output into the
    radar frame/units defined in geometry.py and return frames whose timestamps are unix seconds."""

    def poll(self, now: float) -> list[RadarFrame]:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


class JsonlReplayAdapter(RadarAdapter):
    """Replays a recorded or simulated ``omni-radar-jsonl/1`` file in a loop, rebasing time to the wall clock."""

    def __init__(self, unit: SensorUnit, root: Path):
        source = unit.radar.source
        self.unit = unit
        self.path = safe_path(root, source.get("path", ""), f"{unit.unit_id}.radar.source.path")
        self.loop = bool(source.get("loop", True))
        self.speed = float(source.get("speed", 1.0))
        if not 0.1 <= self.speed <= 10:
            raise ConfigError("radar.source.speed must be between 0.1 and 10")
        self.frames: list[tuple[float, list[dict]]] = []
        self.duration = 0.0
        self.rejected_on_load = 0
        self._load()
        self._origin: float | None = None
        self._cursor = (0, 0)  # (loop index, frame index)

    def _load(self) -> None:
        try:
            if self.path.stat().st_size > MAX_REPLAY_BYTES:
                raise ConfigError("Replay file is larger than 64 MB")
            lines = self.path.read_text("utf-8").splitlines()
        except OSError as exc:
            raise ConfigError(f"Cannot read replay file {self.path.name}: {exc.strerror}") from exc
        if not lines:
            raise ConfigError("Replay file is empty")
        try:
            header = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise ConfigError("Replay header is not valid JSON") from exc
        if not isinstance(header, dict) or header.get("format") != FORMAT_ID:
            raise ConfigError(f"Replay header must declare format '{FORMAT_ID}'")
        if header.get("sensor_id") != self.unit.radar.sensor_id:
            raise ConfigError("Replay sensor_id does not match the unit's radar.sensor_id")
        if header.get("frame") != "radar":
            raise ConfigError("Replay data must already be expressed in the 'radar' frame")
        units = header.get("units", {})
        for key in ("range", "angle", "velocity"):
            if units.get(key) != MEASUREMENT_UNITS[key]:
                raise ConfigError(f"Replay units.{key} must be '{MEASUREMENT_UNITS[key]}'; convert in the adapter")
        last = -math.inf
        for number, line in enumerate(lines[1:MAX_REPLAY_FRAMES + 1], start=2):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                t = float(row["t"])
                targets = row.get("targets", [])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                self.rejected_on_load += 1
                continue
            if not math.isfinite(t) or t < last or not isinstance(targets, list):
                self.rejected_on_load += 1
                continue
            last = t
            self.frames.append((t, targets[:MAX_TARGETS_PER_FRAME]))
        if not self.frames:
            raise ConfigError("Replay file has no usable frames")
        declared = header.get("duration_s")
        self.duration = float(declared) if isinstance(declared, (int, float)) and declared > last else last + 1.0 / max(1.0, float(header.get("rate_hz", 10)))

    def _measurement(self, target, timestamp: float) -> RadarMeasurement | None:
        try:
            def opt(key):
                value = target.get(key)
                return None if value is None else float(value)
            ident = target.get("src_id")
            measurement = RadarMeasurement(
                timestamp=timestamp, sensor_id=self.unit.radar.sensor_id, unit_id=self.unit.unit_id,
                range_m=float(target["range_m"]), azimuth_deg=float(target["azimuth_deg"]), elevation_deg=opt("elevation_deg"),
                radial_velocity_mps=opt("radial_velocity_mps"), snr_db=opt("snr_db"), confidence=opt("confidence"),
                source_track_id=int(ident) if isinstance(ident, int) and not isinstance(ident, bool) else None)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        return measurement if measurement.valid() else None

    def poll(self, now: float) -> list[RadarFrame]:
        if self._origin is None:
            self._origin = now
        out: list[RadarFrame] = []
        loop_index, frame_index = self._cursor
        elapsed = (now - self._origin) * self.speed
        while True:
            if frame_index >= len(self.frames):
                if not self.loop:
                    break
                loop_index, frame_index = loop_index + 1, 0
                continue
            t, targets = self.frames[frame_index]
            offset = loop_index * self.duration + t
            if offset > elapsed:
                break
            timestamp = self._origin + offset / self.speed
            parsed = [self._measurement(target, timestamp) for target in targets]
            good = tuple(m for m in parsed if m is not None)
            out.append(RadarFrame(timestamp, self.unit.radar.sensor_id, good, len(parsed) - len(good)))
            frame_index += 1
            if len(out) >= 2000:  # bound work after a very long idle period
                break
        self._cursor = (loop_index, frame_index)
        return out

    def describe(self) -> dict:
        return {"adapter": "jsonl_replay", "format": FORMAT_ID, "file": self.path.name, "loop": self.loop,
                "speed": self.speed, "duration_s": round(self.duration, 3), "frames": len(self.frames),
                "rejected_on_load": self.rejected_on_load}


def build_adapter(unit: SensorUnit, root: Path) -> RadarAdapter:
    if unit.radar.adapter == "jsonl_replay":
        return JsonlReplayAdapter(unit, root)
    if unit.radar.adapter == "simulator":
        if unit.simulation is None:
            raise ConfigError("The 'simulator' radar adapter needs a simulation.truth block (demo/test units only).")
        from .simulate import SimulatorAdapter
        return SimulatorAdapter(unit)
    raise ConfigError(f"Radar adapter '{unit.radar.adapter}' is not implemented. Only documented adapters are available "
                      "(jsonl_replay); add a vendor adapter only after its output format is documented.")


@dataclass(frozen=True)
class LocatedMeasurement:
    measurement: RadarMeasurement
    position: Vec          # map frame, metres
    sigma: Vec             # map frame, 1-sigma per axis (metres)
    elevation_assumed: bool


def locate(measurement: RadarMeasurement, unit: SensorUnit) -> LocatedMeasurement:
    """radar spherical -> radar Cartesian -> enclosure -> map, with first-order covariance propagation.

    Without elevation, 0 deg is assumed and the vertical error is inflated using
    ``unknown_elevation_sigma_deg``: the target height is NOT known, only that it lies somewhere along that arc.
    """
    radar = unit.radar
    elevation_assumed = measurement.elevation_deg is None
    r = measurement.range_m
    theta = math.radians(measurement.azimuth_deg)
    phi = math.radians(0.0 if elevation_assumed else measurement.elevation_deg)
    s_r = radar.sigma_range_m
    s_theta = math.radians(radar.sigma_azimuth_deg)
    s_phi = math.radians(radar.unknown_elevation_sigma_deg if elevation_assumed else radar.sigma_elevation_deg)
    cp, sp, ct, st = math.cos(phi), math.sin(phi), math.cos(theta), math.sin(theta)
    p_radar = (r * cp * st, r * cp * ct, r * sp)
    columns = ((cp * st, cp * ct, sp),                 # d/dr
               (r * cp * ct, -r * cp * st, 0.0),        # d/dtheta
               (-r * sp * st, -r * sp * ct, r * cp))    # d/dphi
    variances = (s_r ** 2, s_theta ** 2, s_phi ** 2)
    cov = [[sum(columns[k][i] * columns[k][j] * variances[k] for k in range(3)) for j in range(3)] for i in range(3)]
    transform = unit.radar_in_map
    rot = transform.rotation
    cov_map = mat_mul(mat_mul(rot, tuple(map(tuple, cov))), transpose(rot))  # type: ignore[arg-type]
    sigma = tuple(math.sqrt(max(0.0, cov_map[i][i])) for i in range(3))
    return LocatedMeasurement(measurement, transform.apply(p_radar), sigma, elevation_assumed)  # type: ignore[arg-type]


def radar_point_from_map(position: Vec, unit: SensorUnit, radar_in_map=None) -> tuple[float, float, float]:
    """Inverse of ``locate`` for simulation/tests: map point -> (range_m, azimuth_deg, elevation_deg)."""
    x, y, z = (radar_in_map or unit.radar_in_map).inverse().apply(position)
    r = math.sqrt(x * x + y * y + z * z)
    return r, math.degrees(math.atan2(x, y)), math.degrees(math.asin(max(-1.0, min(1.0, z / r))))


__all__ = ["RadarAdapter", "RadarFrame", "JsonlReplayAdapter", "LocatedMeasurement", "build_adapter", "locate",
           "radar_point_from_map"]
