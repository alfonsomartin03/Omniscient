"""Load and validate versioned local site configuration (map + sensor units + calibrations)."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .camera import parse_intrinsics
from .geometry import CAMERA_NOMINAL_FORWARD, ConfigError, Transform, mat_mul, parse_rotation, parse_translation
from .models import (CAL_STATUSES, SCHEMA_VERSION, CameraSpec, Calibration, ConfigVersionError, MapSpec, RadarSpec,
                     Retention, SensorUnit, SimTruth, Site)
from .pointcloud import SCALE_TO_METRES, load_point_cloud

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
MAX_JSON_BYTES = 1_000_000
ORIGINS = ("simulated", "recorded", "live")
STREAMS = ("none", "browser_primary_feed")


def read_config_json(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ConfigError(f"{path.name} is larger than {MAX_JSON_BYTES} bytes")
        value = json.loads(path.read_text("utf-8"))
    except OSError as exc:
        raise ConfigError(f"Cannot read {path.name}: {exc.strerror}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{path.name} must contain a JSON object")
    return value


def safe_path(root: Path, relative: str, where: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ConfigError(f"{where} must be a relative path inside the site directory")
    resolved = (root / relative).resolve()
    if root.resolve() not in resolved.parents:
        raise ConfigError(f"{where} escapes the site directory")
    return resolved


def _check_version(doc: dict, name: str) -> None:
    version = doc.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ConfigVersionError(f"{name}: schema_version {version!r} is not supported (this build reads {SCHEMA_VERSION}). "
                                 "Migrate the file; never edit in place without a backup.")


def _ident(value, where: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ConfigError(f"{where} must be 1-32 characters of letters, digits, '_' or '-'")
    return value


def _opt_number(spec: dict, key: str, where: str, low: float = 0.0, high: float = 1e6) -> float | None:
    value = spec.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ConfigError(f"{where}.{key} must be a number in [{low}, {high}]")
    return float(value)


def _req_number(spec: dict, key: str, where: str, low: float, high: float) -> float:
    value = _opt_number(spec, key, where, low, high)
    if value is None:
        raise ConfigError(f"{where}.{key} is required")
    return value


def parse_calibration(spec, name: str, where: str) -> Calibration:
    if spec is None:
        return Calibration(name=name)
    if not isinstance(spec, dict):
        raise ConfigError(f"{where} must be an object")
    status = spec.get("status", "uncalibrated")
    if status not in CAL_STATUSES:
        raise ConfigError(f"{where}.status must be one of {', '.join(CAL_STATUSES)}")
    revision = spec.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ConfigError(f"{where}.revision must be a non-negative integer")
    for key in ("calibrated_at", "method", "notes"):
        if spec.get(key) is not None and (not isinstance(spec[key], str) or len(spec[key]) > 500):
            raise ConfigError(f"{where}.{key} must be a short string")
    quality = spec.get("quality")
    if quality is not None and quality not in ("good", "fair", "poor", "insufficient", "unassessed"):
        raise ConfigError(f"{where}.quality must be good, fair, poor, insufficient or unassessed")
    points = spec.get("evidence_points")
    if points is not None and (isinstance(points, bool) or not isinstance(points, int) or points < 0):
        raise ConfigError(f"{where}.evidence_points must be a non-negative integer")
    return Calibration(name=name, status=status, revision=revision, calibrated_at=spec.get("calibrated_at"),
                       quality=quality, evidence_points=points,
                       method=spec.get("method"), position_sigma_m=_opt_number(spec, "position_sigma_m", where, 0, 1000),
                       rotation_sigma_deg=_opt_number(spec, "rotation_sigma_deg", where, 0, 180),
                       reprojection_error_px=_opt_number(spec, "reprojection_error_px", where, 0, 1e4),
                       notes=spec.get("notes", ""))


def parse_pose(spec, name: str, where: str, camera: bool = False) -> tuple[Transform, Calibration]:
    if not isinstance(spec, dict):
        raise ConfigError(f"{where} is required and must be an object")
    rotation = parse_rotation(spec.get("rotation"), f"{where}.rotation")
    if camera:
        # Camera rotations are adjustments applied to a nominal forward, level OpenCV camera.
        rotation = mat_mul(rotation, CAMERA_NOMINAL_FORWARD)
    translation = parse_translation(spec.get("translation_m"), f"{where}.translation_m")
    return Transform(rotation, translation), parse_calibration(spec.get("calibration"), name, f"{where}.calibration")


def _fov(spec: dict, key: str, default: tuple[float, float], where: str, limit: float) -> tuple[float, float]:
    value = spec.get(key, list(default))
    if (not isinstance(value, list) or len(value) != 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                                   and math.isfinite(v) for v in value)
            or not -limit <= value[0] < value[1] <= limit):
        raise ConfigError(f"{where}.{key} must be [min,max] degrees within +/-{limit}")
    return float(value[0]), float(value[1])


def parse_simulation(spec, name: str) -> SimTruth | None:
    if spec is None:
        return None
    if not isinstance(spec, dict) or not isinstance(spec.get("truth"), dict):
        raise ConfigError(f"{name}.simulation.truth is required for simulated units")
    truth = spec["truth"]
    pose, _ = parse_pose(truth.get("enclosure_in_map"), "truth", f"{name}.simulation.truth.enclosure_in_map")
    cam, _ = parse_pose(truth.get("camera_in_enclosure"), "truth", f"{name}.simulation.truth.camera_in_enclosure", camera=True)
    intrinsics = parse_intrinsics(truth.get("intrinsics"), f"{name}.simulation.truth.intrinsics")
    seed = spec.get("seed", 7)
    targets = spec.get("targets", [])
    if isinstance(seed, bool) or not isinstance(seed, int) or not isinstance(targets, list) or len(targets) > 8:
        raise ConfigError(f"{name}.simulation needs an integer seed and at most 8 targets")
    for target in targets:
        ok = (isinstance(target, dict) and isinstance(target.get("waypoints"), list) and 2 <= len(target["waypoints"]) <= 12
              and all(isinstance(v, (int, float)) for k in ("start", "end", "height") for v in [target.get(k)])
              and target["end"] > target["start"]
              and all(isinstance(w, list) and len(w) == 2 and all(isinstance(c, (int, float)) for c in w) for w in target["waypoints"]))
        if not ok:
            raise ConfigError(f"{name}.simulation.targets entries need start, end, height and 2-12 [x,y] waypoints")
    return SimTruth(pose, cam, intrinsics, seed, tuple(targets))


def parse_unit(doc: dict, name: str) -> SensorUnit:
    _check_version(doc, name)
    unit_id = _ident(doc.get("unit_id"), f"{name}: unit_id")
    pose, pose_cal = parse_pose(doc.get("enclosure_in_map"), f"{unit_id}: enclosure -> map", f"{name}.enclosure_in_map")

    radar_doc = doc.get("radar")
    if not isinstance(radar_doc, dict):
        raise ConfigError(f"{name}.radar is required")
    radar_pose, radar_cal = parse_pose(radar_doc.get("in_enclosure"), f"{unit_id}: radar -> enclosure", f"{name}.radar.in_enclosure")
    sigma = radar_doc.get("nominal_sigma", {})
    if not isinstance(sigma, dict):
        raise ConfigError(f"{name}.radar.nominal_sigma must be an object")
    source = radar_doc.get("source", {})
    if not isinstance(source, dict):
        raise ConfigError(f"{name}.radar.source must be an object")
    radar = RadarSpec(
        sensor_id=_ident(radar_doc.get("sensor_id"), f"{name}.radar.sensor_id"),
        adapter=_ident(radar_doc.get("adapter"), f"{name}.radar.adapter"), source=source,
        in_enclosure=radar_pose, calibration=radar_cal,
        max_range_m=_req_number(radar_doc, "max_range_m", f"{name}.radar", 0.1, 500),
        azimuth_fov_deg=_fov(radar_doc, "azimuth_fov_deg", (-60.0, 60.0), f"{name}.radar", 180),
        elevation_fov_deg=_fov(radar_doc, "elevation_fov_deg", (-20.0, 20.0), f"{name}.radar", 90),
        sigma_range_m=_req_number(sigma, "range_m", f"{name}.radar.nominal_sigma", 0.001, 100),
        sigma_azimuth_deg=_req_number(sigma, "azimuth_deg", f"{name}.radar.nominal_sigma", 0.01, 90),
        sigma_elevation_deg=_req_number(sigma, "elevation_deg", f"{name}.radar.nominal_sigma", 0.01, 90),
        unknown_elevation_sigma_deg=_opt_number(radar_doc, "unknown_elevation_sigma_deg", f"{name}.radar", 1, 90) or 20.0,
        stale_after_s=_opt_number(radar_doc, "stale_after_s", f"{name}.radar", 0.1, 3600) or 1.5,
        missing_after_s=_opt_number(radar_doc, "missing_after_s", f"{name}.radar", 0.2, 3600) or 5.0)
    if radar.missing_after_s <= radar.stale_after_s:
        raise ConfigError(f"{name}.radar.missing_after_s must exceed stale_after_s")

    cam_doc = doc.get("camera")
    if not isinstance(cam_doc, dict):
        raise ConfigError(f"{name}.camera is required")
    cam_pose, cam_cal = parse_pose(cam_doc.get("in_enclosure"), f"{unit_id}: camera -> enclosure", f"{name}.camera.in_enclosure", camera=True)
    intr_doc = cam_doc.get("intrinsics")
    intrinsics = parse_intrinsics(intr_doc, f"{name}.camera.intrinsics")
    intr_cal = parse_calibration(intr_doc.get("calibration"), f"{unit_id}: lens intrinsics", f"{name}.camera.intrinsics.calibration")
    stream = cam_doc.get("stream", "none")
    if stream not in STREAMS:
        raise ConfigError(f"{name}.camera.stream must be one of {', '.join(STREAMS)} (no direct camera protocol is implemented)")
    camera = CameraSpec(_ident(cam_doc.get("sensor_id"), f"{name}.camera.sensor_id"), cam_pose, cam_cal, intrinsics, intr_cal, stream)
    label = doc.get("label", unit_id)
    if not isinstance(label, str) or len(label) > 80:
        raise ConfigError(f"{name}.label must be a short string")
    return SensorUnit(unit_id, label, pose, pose_cal, radar, camera, parse_simulation(doc.get("simulation"), name))


def parse_retention(spec) -> Retention:
    if spec is None:
        return Retention()
    if not isinstance(spec, dict):
        raise ConfigError("retention must be an object")
    seconds = _opt_number(spec, "track_history_seconds", "retention", 0, 3600)
    if spec.get("persist_tracks", False) is not False:
        raise ConfigError("retention.persist_tracks is not implemented; track history is memory-only")
    if spec.get("video", "none") != "none":
        raise ConfigError("retention.video must be 'none'; this system does not record video")
    return Retention(track_history_seconds=10.0 if seconds is None else seconds)


def load_site(directory: str | Path) -> Site:
    root = Path(directory).resolve()
    doc = read_config_json(root / "site.json")
    _check_version(doc, "site.json")
    origin = doc.get("data_origin")
    if origin not in ORIGINS:
        raise ConfigError(f"site.json.data_origin must be one of {', '.join(ORIGINS)}")
    map_doc = doc.get("map")
    if not isinstance(map_doc, dict):
        raise ConfigError("site.json.map is required")
    units_doc = map_doc.get("source_units", "m")
    if units_doc not in SCALE_TO_METRES:
        raise ConfigError(f"site.json.map.source_units must be one of {', '.join(SCALE_TO_METRES)}")
    up_axis = map_doc.get("up_axis", "z")
    if up_axis not in ("z", "y"):
        raise ConfigError("site.json.map.up_axis must be 'z' or 'y'")
    revision = map_doc.get("revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ConfigError("site.json.map.revision must be a positive integer")
    max_points = int(_opt_number(map_doc, "display_max_points", "site.json.map", 100, 200_000) or 15_000)
    map_spec = MapSpec(_ident(map_doc.get("map_id"), "site.json.map.map_id"), str(map_doc.get("name", ""))[:80], revision,
                       map_doc.get("file", ""), units_doc, max_points, str(map_doc.get("note", ""))[:300],
                       up_axis, _opt_number(map_doc, "floor_z", "site.json.map", -1e5, 1e5))
    cloud = load_point_cloud(safe_path(root, map_spec.cloud_path, "site.json.map.file"), map_spec.source_units, map_spec.up_axis)
    refs = doc.get("units")
    if not isinstance(refs, list) or not 1 <= len(refs) <= 16:
        raise ConfigError("site.json.units must list 1 to 16 unit files")
    units: dict[str, SensorUnit] = {}
    for ref in refs:
        path = safe_path(root, ref, "site.json.units[]")
        unit = parse_unit(read_config_json(path), path.name)
        if unit.unit_id in units:
            raise ConfigError(f"Duplicate unit_id {unit.unit_id}")
        units[unit.unit_id] = unit
    return Site(_ident(doc.get("site_id"), "site.json.site_id"), str(doc.get("name", ""))[:80], SCHEMA_VERSION, map_spec,
                cloud, units, parse_retention(doc.get("retention")), root, origin)
