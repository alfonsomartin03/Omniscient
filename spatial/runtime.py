"""Runtime: pull frames from adapters, track in the map frame, and publish an honest snapshot.

State advances lazily when the API is polled (no background thread), matching the project's small-footprint design.
A live adapter that blocks or needs its own cadence would add a reader thread here; none exists yet.
"""
from __future__ import annotations

import math
import threading
import time
from pathlib import Path

from .models import MEASUREMENT_UNITS, SensorUnit, Site
from .overlay import project_map_point, project_track
from .radar import RadarAdapter, build_adapter, locate
from .simulate import truth_pixel
from .site import load_site
from .tracker import Tracker


class SpatialRuntime:
    def __init__(self, site: Site, clock=time.time):
        self.site = site
        self.clock = clock
        self.lock = threading.RLock()
        self.tracker = Tracker(site.retention.track_history_seconds)
        self.adapters: dict[str, RadarAdapter] = {uid: build_adapter(unit, Path(site.root)) for uid, unit in site.units.items()}
        self.last_frame_time: dict[str, float | None] = {uid: None for uid in site.units}
        self.counters = {uid: {"frames": 0, "measurements": 0, "rejected": 0} for uid in site.units}
        self._display_cloud = site.cloud.downsample(site.map.display_max_points)
        self._camera_view_cache: dict[str, dict] = {}

    @classmethod
    def from_directory(cls, directory, clock=time.time) -> "SpatialRuntime":
        return cls(load_site(directory), clock)

    # -- ingestion -----------------------------------------------------------------------------
    def update(self, now: float | None = None) -> float:
        now = self.clock() if now is None else now
        with self.lock:
            batches = []
            for uid, adapter in self.adapters.items():
                unit = self.site.units[uid]
                for frame in adapter.poll(now):
                    batches.append((frame.timestamp, uid, unit, frame))
            for timestamp, uid, unit, frame in sorted(batches, key=lambda item: item[0]):
                self.last_frame_time[uid] = timestamp
                stats = self.counters[uid]
                stats["frames"] += 1
                stats["rejected"] += frame.rejected
                stats["measurements"] += len(frame.measurements)
                # Radar targets are only placed in the map when pose + radar extrinsics exist.
                located = [locate(m, unit) for m in frame.measurements] if unit.placed else []
                self.tracker.update(timestamp, located)
        return now

    # -- snapshots -----------------------------------------------------------------------------
    def _radar_health(self, unit: SensorUnit, now: float) -> dict:
        last = self.last_frame_time[unit.unit_id]
        if last is None:
            status, age = "waiting", None
        else:
            age = max(0.0, now - last)
            status = "missing" if age >= unit.radar.missing_after_s else "stale" if age >= unit.radar.stale_after_s else "ok"
        return {"sensor_id": unit.radar.sensor_id, "status": status, "age_s": None if age is None else round(age, 2),
                "stale_after_s": unit.radar.stale_after_s, "missing_after_s": unit.radar.missing_after_s,
                **self.counters[unit.unit_id]}

    def snapshot(self, now: float | None = None) -> dict:
        with self.lock:
            now = self.update(now)
            tracks = self.tracker.snapshot(now)
            for track in tracks:
                track["image"] = [project_track(self.site.units[uid], tuple(track["position"]), tuple(track["sigma_m"]))
                                  for uid in sorted(self.site.units)]
            units = []
            for uid, unit in self.site.units.items():
                radar = self._radar_health(unit, now)
                units.append({
                    "unit_id": uid, "label": unit.label, "placed": unit.placed, "image_ready": unit.image_ready,
                    "simulated": unit.simulated(), "radar": radar,
                    "camera": {"sensor_id": unit.camera.sensor_id, "stream": unit.camera.stream,
                               "status": "not_monitored",
                               "detail": "Video is supplied by the browser; the server does not track camera liveness yet."},
                    "calibrations": [c.public() for c in unit.calibrations()],
                    "warnings": self._warnings(unit, radar)})
            people = []
            for uid, adapter in self.adapters.items():
                unit = self.site.units[uid]
                if unit.simulation is not None and hasattr(adapter, "truth_positions"):
                    for index, position in adapter.truth_positions(now):
                        proj = truth_pixel(unit.simulation, position)
                        people.append({"index": index, "map": [round(v, 2) for v in position], "unit_id": uid,
                                       "u": None if proj is None or not proj.in_image else round(proj.u, 4),
                                       "v": None if proj is None or not proj.in_image else round(proj.v, 4)})
            return {"enabled": True, "server_time": now, "simulated_people": people,
                    "simulation_note": "Simulated walkers' true positions, shown for demonstration only." if people else None, "frame": "map", "measurement_units": dict(MEASUREMENT_UNITS),
                    "data_origin": self.site.data_origin, "site": {"id": self.site.site_id, "name": self.site.name},
                    "map": {"id": self.site.map.map_id, "revision": self.site.map.revision},
                    "retention": {"track_history_seconds": self.site.retention.track_history_seconds,
                                  "persist_tracks": False, "video": "none"},
                    "units": units, "tracks": tracks, "tracker": {"frames": self.tracker.frames,
                                                                  "out_of_order_dropped": self.tracker.out_of_order},
                    "disclaimer": "Approximate sensor-derived positions. Not a precise measurement and not safety-rated."}

    @staticmethod
    def _warnings(unit: SensorUnit, radar: dict) -> list[str]:
        out = []
        if unit.simulated():
            out.append("Calibration values are SIMULATED demo data, not a real installation.")
        for c in unit.calibrations():
            if c.status == "uncalibrated":
                out.append(f"{c.name}: uncalibrated; dependent outputs are suppressed.")
            elif c.status == "nominal":
                out.append(f"{c.name}: nominal (not measured); expect visible misalignment.")
            elif c.status != "simulated" and not c.quantified and c.name.endswith(("map", "enclosure")):
                out.append(f"{c.name}: accuracy not declared; overlay error is under-reported.")
        if radar["status"] in ("stale", "missing"):
            out.append(f"Radar {radar['status']}: no frame for {radar['age_s']} s; tracks are predictions only.")
        if radar["status"] == "waiting":
            out.append("Radar: no frames received yet.")
        return out

    # -- static payloads -----------------------------------------------------------------------
    def map_payload(self) -> dict:
        cloud = self._display_cloud
        lo, hi = self.site.cloud.bounds()
        flat = [round(v, 2) for v in cloud.xyz]
        units = []
        for uid, unit in self.site.units.items():
            radar_pose, camera_pose = unit.radar_in_map, unit.camera_in_map
            r = unit.radar
            wedge = [list(radar_pose.apply((0, 0, 0))[:2])]
            steps = 16
            for i in range(steps + 1):
                az = math.radians(r.azimuth_fov_deg[0] + (r.azimuth_fov_deg[1] - r.azimuth_fov_deg[0]) * i / steps)
                wedge.append(list(radar_pose.apply((r.max_range_m * math.sin(az), r.max_range_m * math.cos(az), 0.0))[:2]))
            intr = unit.camera.intrinsics
            cam_wedge = [list(camera_pose.apply((0, 0, 0))[:2])]
            for i in range(9):
                xn = ((intr.width * i / 8) - intr.cx) / intr.fx
                n = math.sqrt(xn * xn + 1)
                cam_wedge.append(list(camera_pose.apply((xn / n * 6.0, 0.0, 1 / n * 6.0))[:2]))
            units.append({"unit_id": uid, "label": unit.label, "position": [round(v, 3) for v in unit.enclosure_in_map.translation],
                          "radar_fov": [[round(a, 2), round(b, 2)] for a, b in wedge],
                          "camera_fov": [[round(a, 2), round(b, 2)] for a, b in cam_wedge],
                          "placed": unit.placed, "simulated": unit.simulated()})
        return {"frame": "map", "units_of_measure": "m", "map": {"id": self.site.map.map_id, "name": self.site.map.name,
                                                                  "revision": self.site.map.revision, "note": self.site.map.note,
                                                                  "points_total": self.site.cloud.count,
                                                                  "points_shown": cloud.count},
                "bounds": {"min": [round(v, 2) for v in lo], "max": [round(v, 2) for v in hi]},
                "points": flat, "sensor_units": units}

    def camera_view(self, unit_id: str) -> dict | None:
        unit = self.site.units.get(unit_id)
        if unit is None:
            return None
        with self.lock:
            cached = self._camera_view_cache.get(unit_id)
            if cached is not None:
                return cached
            points = []
            simulated = unit.simulation is not None
            if unit.image_ready or simulated:
                cloud = self._display_cloud.xyz
                for i in range(0, len(cloud), 3):
                    q = (cloud[i], cloud[i + 1], cloud[i + 2])
                    # A simulated unit's "video" is what the hidden true camera sees, not the estimated model.
                    p = truth_pixel(unit.simulation, q) if simulated else project_map_point(unit, q)
                    if p is not None and p.in_image and p.depth_m < 60:
                        points.extend((round(p.u, 4), round(p.v, 4), round(p.depth_m, 1)))
            intr = unit.camera.intrinsics
            view = {"unit_id": unit_id, "image_ready": unit.image_ready, "image_size": [intr.width, intr.height],
                    "synthetic": True, "simulated_video": simulated,
                    "note": ("SIMULATED video (what the demo camera sees). Markers use the saved calibration, so any calibration error shows up as an offset."
                             if simulated else "Map points rendered through the calibrated camera model. This is NOT camera video."),
                    "points": points}
            self._camera_view_cache[unit_id] = view
            return view
