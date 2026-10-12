"""Map -> camera projection with an explicit, conservative uncertainty radius."""
from __future__ import annotations

import math

from .geometry import Vec
from .models import SensorUnit


def map_to_camera(unit: SensorUnit, p_map: Vec) -> Vec:
    return unit.camera_in_map.inverse().apply(p_map)


def project_map_point(unit: SensorUnit, p_map: Vec):
    return unit.camera.intrinsics.project(map_to_camera(unit, p_map))


def calibration_error_px(unit: SensorUnit, depth_m: float) -> tuple[float | None, bool]:
    """Pixel error contributed by the declared calibration accuracy. (None, False) when unquantified."""
    # Map pose errors move a track on the map but cancel in the camera frame (track and camera share the
    # same pose), so only the radar and camera mounting calibrations contribute to marker misplacement.
    cals = [unit.radar.calibration, unit.camera.calibration]
    if not all(c.quantified for c in cals):
        return None, False
    pos = math.sqrt(sum(c.position_sigma_m ** 2 for c in cals))
    rot = math.sqrt(sum(math.radians(c.rotation_sigma_deg) ** 2 for c in cals))
    fx = unit.camera.intrinsics.fx
    return fx * math.hypot(rot, pos / max(depth_m, 0.5)), True


def project_track(unit: SensorUnit, position: Vec, sigma: Vec) -> dict:
    """Image marker for one track. The radius is the largest pixel displacement of the 1-sigma track
    uncertainty along each map axis plus declared calibration error. It is a plausibility region for the
    radar's reference point, not a body outline and not a statistical guarantee."""
    out = {"unit_id": unit.unit_id, "sensor_id": unit.camera.sensor_id, "visible": False, "reason": None}
    if not unit.image_ready:
        out["reason"] = "calibration_missing"
        return out
    centre = project_map_point(unit, position)
    if centre is None:
        out["reason"] = "behind_camera"
        return out
    radius = 0.0
    for axis in range(3):
        for sign in (-1, 1):
            moved = list(position)
            moved[axis] += sign * sigma[axis]
            other = project_map_point(unit, tuple(moved))  # type: ignore[arg-type]
            if other is not None:
                radius = max(radius, math.hypot(other.u_px - centre.u_px, other.v_px - centre.v_px))
    cal_px, quantified = calibration_error_px(unit, centre.depth_m)
    radius_total = math.hypot(radius, cal_px or 0.0)
    width, height = unit.camera.intrinsics.width, unit.camera.intrinsics.height
    out.update(visible=centre.in_image, reason=None if centre.in_image else "outside_image",
               u=round(centre.u, 5), v=round(centre.v, 5), depth_m=round(centre.depth_m, 2),
               radius_px=round(radius_total, 1), radius_norm=round(radius_total / width, 5),
               image_size=[width, height], calibration_error_quantified=quantified,
               basis="radar reference point (not a body outline)")
    return out
