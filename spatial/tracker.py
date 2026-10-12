"""Anonymous multi-target tracker in the map frame.

Per-axis constant-velocity Kalman filters with greedy gated nearest-neighbour association. Track IDs are
sequential, per-run and carry no identity; nothing here classifies or recognises people.
Measurement covariance is reduced to its per-axis diagonal (documented approximation).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from .models import MEASUREMENT_UNITS
from .radar import LocatedMeasurement

GATE_CHI2 = 16.0           # ~99.9% for 3 degrees of freedom
CONFIRM_HITS = 3
TENTATIVE_DROP_S = 1.0
COAST_AFTER_S = 0.5        # shown as "coasting" (prediction only) after this long without an update
MAX_COAST_S = 3.0
MAX_TRACKS = 64
ACCEL_SIGMA = 1.5          # m/s^2 process noise for walking-speed targets
INITIAL_VELOCITY_SIGMA = 1.5


@dataclass
class _Axis:
    p: float
    v: float
    ppp: float
    ppv: float = 0.0
    pvv: float = INITIAL_VELOCITY_SIGMA ** 2

    def predict(self, dt: float, q: float) -> None:
        if dt <= 0:
            return
        self.p += self.v * dt
        ppp = self.ppp + 2 * dt * self.ppv + dt * dt * self.pvv + q * dt ** 3 / 3
        ppv = self.ppv + dt * self.pvv + q * dt ** 2 / 2
        self.ppp, self.ppv, self.pvv = ppp, ppv, self.pvv + q * dt

    def update(self, z: float, r: float) -> None:
        s = self.ppp + r
        kp, kv = self.ppp / s, self.ppv / s
        innovation = z - self.p
        self.p += kp * innovation
        self.v += kv * innovation
        ppp, ppv, pvv = self.ppp, self.ppv, self.pvv
        self.ppp, self.ppv, self.pvv = (1 - kp) * ppp, (1 - kp) * ppv, pvv - kv * ppv


@dataclass
class Track:
    id: str
    axes: list[_Axis]
    created: float
    last_update: float
    last_time: float
    hits: int = 1
    confirmed: bool = False
    sensor_ids: set = field(default_factory=set)
    unit_ids: set = field(default_factory=set)
    snr_sum: float = 0.0
    snr_n: int = 0
    conf_sum: float = 0.0
    conf_n: int = 0
    elevation_known: bool = False
    history: deque = field(default_factory=deque)  # (t, x, y, z) -- in-memory trail only

    @property
    def position(self):
        return (self.axes[0].p, self.axes[1].p, self.axes[2].p)

    @property
    def velocity(self):
        return (self.axes[0].v, self.axes[1].v, self.axes[2].v)

    @property
    def sigma(self):
        return tuple(math.sqrt(max(0.0, a.ppp)) for a in self.axes)

    def state(self, now: float) -> str:
        since = now - self.last_update
        if not self.confirmed:
            return "tentative"
        return "coasting" if since > COAST_AFTER_S else "confirmed"


class Tracker:
    def __init__(self, history_seconds: float = 10.0):
        self.tracks: dict[str, Track] = {}
        self.history_seconds = history_seconds
        self._next = 1
        self.last_time = -math.inf
        self.out_of_order = 0
        self.frames = 0

    def _predict(self, t: float) -> None:
        for track in self.tracks.values():
            dt = t - track.last_time
            for axis in track.axes:
                axis.predict(dt, ACCEL_SIGMA ** 2)
            track.last_time = t

    def update(self, t: float, located: list[LocatedMeasurement]) -> None:
        if t < self.last_time:
            self.out_of_order += 1
            return
        self.last_time = t
        self.frames += 1
        self._predict(t)

        pairs = []
        for ti, track in enumerate(self.tracks.values()):
            for mi, item in enumerate(located):
                d2 = log_det = 0.0
                for axis_index, axis in enumerate(track.axes):
                    innovation_var = axis.ppp + item.sigma[axis_index] ** 2
                    d2 += (item.position[axis_index] - axis.p) ** 2 / innovation_var
                    log_det += math.log(innovation_var)
                if d2 <= GATE_CHI2:
                    # Gate on Mahalanobis distance, but rank by Gaussian negative log-likelihood. Ranking by d2 alone
                    # lets a poorly known (wide) track outbid a precise one for the same measurement.
                    pairs.append((d2 + log_det, ti, mi))
        pairs.sort()
        tracks = list(self.tracks.values())
        used_t, used_m = set(), set()
        for _d2, ti, mi in pairs:
            if ti in used_t or mi in used_m:
                continue
            used_t.add(ti)
            used_m.add(mi)
            self._apply(tracks[ti], located[mi], t)

        for mi, item in enumerate(located):
            if mi in used_m or len(self.tracks) >= MAX_TRACKS:
                continue
            axes = [_Axis(item.position[i], 0.0, item.sigma[i] ** 2) for i in range(3)]
            track = Track(f"T-{self._next:04d}", axes, t, t, t)
            self._next += 1
            self._record(track, item)
            self.tracks[track.id] = track

        for track_id, track in list(self.tracks.items()):
            since = t - track.last_update
            if (not track.confirmed and since > TENTATIVE_DROP_S) or since > MAX_COAST_S:
                del self.tracks[track_id]
            else:
                self._trail(track, t)

    def _record(self, track: Track, item: LocatedMeasurement) -> None:
        m = item.measurement
        track.sensor_ids.add(m.sensor_id)
        track.unit_ids.add(m.unit_id)
        if m.snr_db is not None:
            track.snr_sum += m.snr_db
            track.snr_n += 1
        if m.confidence is not None:
            track.conf_sum += m.confidence
            track.conf_n += 1
        track.elevation_known = not item.elevation_assumed

    def _apply(self, track: Track, item: LocatedMeasurement, t: float) -> None:
        for i, axis in enumerate(track.axes):
            axis.update(item.position[i], item.sigma[i] ** 2)
        track.last_update = t
        track.hits += 1
        if track.hits >= CONFIRM_HITS:
            track.confirmed = True
        self._record(track, item)

    def _trail(self, track: Track, t: float) -> None:
        if self.history_seconds <= 0:
            track.history.clear()
            return
        track.history.append((t, *track.position))
        while track.history and t - track.history[0][0] > self.history_seconds:
            track.history.popleft()

    def snapshot(self, now: float, include_tentative: bool = False) -> list[dict]:
        out = []
        for track in self.tracks.values():
            if not track.confirmed and not include_tentative:
                continue
            vx, vy, vz = track.velocity
            out.append({
                "id": track.id, "state": track.state(now), "timestamp": track.last_update,
                "since_update_s": round(max(0.0, now - track.last_update), 3), "age_s": round(track.last_update - track.created, 3),
                "frame": "map", "units": {"position": "m", "velocity": "m/s", "time": MEASUREMENT_UNITS["time"]},
                "position": [round(v, 3) for v in track.position],
                "velocity": [round(vx, 3), round(vy, 3), round(vz, 3)],
                "speed_mps": round(math.hypot(vx, vy), 3),
                "sigma_m": [round(v, 3) for v in track.sigma],
                "dimension": "3d" if track.elevation_known else "2d_elevation_assumed",
                "sensor_ids": sorted(track.sensor_ids), "unit_ids": sorted(track.unit_ids), "hits": track.hits,
                "quality": {"mean_snr_db": round(track.snr_sum / track.snr_n, 1) if track.snr_n else None,
                            "mean_sensor_confidence": round(track.conf_sum / track.conf_n, 2) if track.conf_n else None},
                "trail": [[round(p, 2) for p in point[1:]] for point in track.history],
            })
        return out
