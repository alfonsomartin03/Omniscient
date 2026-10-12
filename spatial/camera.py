"""Pinhole camera with Brown-Conrady distortion (OpenCV parameter order)."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .geometry import ConfigError, Vec

MIN_DEPTH_M = 0.05
MAX_NORMALIZED_RADIUS = 3.0  # beyond ~72 deg off-axis the distortion polynomial is not trustworthy


@dataclass(frozen=True)
class Projection:
    u_px: float
    v_px: float
    u: float  # normalised 0..1 across the calibrated image width
    v: float  # normalised 0..1 down the calibrated image height
    depth_m: float
    in_image: bool


@dataclass(frozen=True)
class CameraModel:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    distortion: tuple[float, float, float, float, float] = (0.0, 0.0, 0.0, 0.0, 0.0)  # k1, k2, p1, p2, k3

    def project(self, p_cam: Vec) -> Projection | None:
        """Project a camera-frame point. Returns None when behind the camera or outside the model's validity."""
        x, y, z = p_cam
        if not z > MIN_DEPTH_M:
            return None
        xn, yn = x / z, y / z
        r2 = xn * xn + yn * yn
        if r2 > MAX_NORMALIZED_RADIUS ** 2:
            return None
        k1, k2, p1, p2, k3 = self.distortion
        radial = 1 + k1 * r2 + k2 * r2 * r2 + k3 * r2 ** 3
        xd = xn * radial + 2 * p1 * xn * yn + p2 * (r2 + 2 * xn * xn)
        yd = yn * radial + p1 * (r2 + 2 * yn * yn) + 2 * p2 * xn * yn
        u_px, v_px = self.fx * xd + self.cx, self.fy * yd + self.cy
        return Projection(u_px, v_px, u_px / self.width, v_px / self.height, z,
                          0 <= u_px < self.width and 0 <= v_px < self.height)

    def horizontal_fov_deg(self) -> float:
        return math.degrees(2 * math.atan(self.width / (2 * self.fx)))

    def vertical_fov_deg(self) -> float:
        return math.degrees(2 * math.atan(self.height / (2 * self.fy)))


def parse_intrinsics(spec: dict, where: str) -> CameraModel:
    if not isinstance(spec, dict):
        raise ConfigError(f"{where} must be an object")
    if spec.get("model") != "pinhole_brown_conrady":
        raise ConfigError(f"{where}.model must be 'pinhole_brown_conrady' (fisheye/omnidirectional lenses are not supported)")

    def number(key: str, low: float, high: float) -> float:
        value = spec.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low < value <= high:
            raise ConfigError(f"{where}.{key} must be a number in ({low}, {high}]")
        return float(value)

    width, height = int(number("width", 0, 16384)), int(number("height", 0, 16384))
    fx, fy = number("fx", 0, 1e6), number("fy", 0, 1e6)
    cx, cy = number("cx", -1e6, 1e6), number("cy", -1e6, 1e6)
    dist = spec.get("distortion", [0, 0, 0, 0, 0])
    if not isinstance(dist, list) or len(dist) not in (4, 5) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in dist):
        raise ConfigError(f"{where}.distortion must be [k1,k2,p1,p2] or [k1,k2,p1,p2,k3]")
    dist = [float(v) for v in dist] + [0.0] * (5 - len(dist))
    return CameraModel(width, height, fx, fy, cx, cy, tuple(dist))  # type: ignore[arg-type]
