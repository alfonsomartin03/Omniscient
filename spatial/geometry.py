"""Rigid transforms and frame conventions (pure Python, no dependencies).

Conventions (all right-handed, metres, degrees at the config boundary):

* ``map``       x right/east, y forward/north, z up. Origin and axes come from the LiDAR map.
* ``enclosure`` x right, y forward (the enclosure's front face), z up.
* ``radar``     x right, y forward (boresight), z up. Azimuth is measured from +y toward +x
                (right-positive); elevation is positive upward.
* ``camera``    OpenCV: x right in the image, y down in the image, z along the optical axis.

``Transform`` named ``T_a_b`` maps points expressed in frame ``b`` into frame ``a``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

Vec = tuple[float, float, float]
Mat = tuple[Vec, Vec, Vec]


class ConfigError(ValueError):
    """Raised for invalid or unsafe local configuration."""


def mat_mul(a: Mat, b: Mat) -> Mat:
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))  # type: ignore[return-value]


def mat_vec(m: Mat, v: Vec) -> Vec:
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])


def transpose(m: Mat) -> Mat:
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))  # type: ignore[return-value]


def vec_add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vec_sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def norm(v: Vec) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


IDENTITY: Mat = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

# A nominal, forward-looking, level camera: OpenCV camera axes expressed in enclosure axes.
# enclosure.x = cam.x, enclosure.y = cam.z (forward), enclosure.z = -cam.y (up).
CAMERA_NOMINAL_FORWARD: Mat = ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0))


def rot_x(a: float) -> Mat:
    c, s = math.cos(a), math.sin(a)
    return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))


def rot_y(a: float) -> Mat:
    c, s = math.cos(a), math.sin(a)
    return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))


def rot_z(a: float) -> Mat:
    c, s = math.cos(a), math.sin(a)
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def rotation_from_rpy_deg(yaw: float, pitch: float, roll: float) -> Mat:
    """yaw about +z (counter-clockwise from above), pitch about +x (positive tilts the
    forward axis upward), roll about +y. R = Rz(yaw) @ Rx(pitch) @ Ry(roll)."""
    return mat_mul(mat_mul(rot_z(math.radians(yaw)), rot_x(math.radians(pitch))), rot_y(math.radians(roll)))


def rotation_from_quaternion(w: float, x: float, y: float, z: float) -> Mat:
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if not math.isfinite(n) or n < 1e-6:
        raise ConfigError("Quaternion must be finite and non-zero")
    w, x, y, z = w / n, x / n, y / n, z / n
    return ((1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
            (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
            (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)))


def exp_so3(w: Vec) -> Mat:
    """Rotation matrix for rotation vector ``w`` (axis * angle, radians)."""
    theta = math.sqrt(w[0] * w[0] + w[1] * w[1] + w[2] * w[2])
    if theta < 1e-12:
        return IDENTITY
    x, y, z = w[0] / theta, w[1] / theta, w[2] / theta
    c, s, o = math.cos(theta), math.sin(theta), 1 - math.cos(theta)
    return ((c + x * x * o, x * y * o - z * s, x * z * o + y * s),
            (y * x * o + z * s, c + y * y * o, y * z * o - x * s),
            (z * x * o - y * s, z * y * o + x * s, c + z * z * o))


def quaternion_from_rotation(m: Mat) -> tuple[float, float, float, float]:
    t = m[0][0] + m[1][1] + m[2][2]
    if t > 0:
        s = math.sqrt(t + 1) * 2
        q = (s / 4, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s)
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1 + m[0][0] - m[1][1] - m[2][2]) * 2
        q = ((m[2][1] - m[1][2]) / s, s / 4, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s)
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1 + m[1][1] - m[0][0] - m[2][2]) * 2
        q = ((m[0][2] - m[2][0]) / s, (m[0][1] + m[1][0]) / s, s / 4, (m[1][2] + m[2][1]) / s)
    else:
        s = math.sqrt(1 + m[2][2] - m[0][0] - m[1][1]) * 2
        q = ((m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, s / 4)
    return q if q[0] >= 0 else (-q[0], -q[1], -q[2], -q[3])


def yaw_pitch_roll_deg(m: Mat) -> tuple[float, float, float]:
    """Inverse of ``rotation_from_rpy_deg`` (R = Rz(yaw) Rx(pitch) Ry(roll))."""
    pitch = math.asin(max(-1.0, min(1.0, m[2][1])))
    if abs(m[2][1]) < 0.999999:
        yaw = math.atan2(-m[0][1], m[1][1])
        roll = math.atan2(-m[2][0], m[2][2])
    else:
        yaw, roll = math.atan2(m[1][0], m[0][0]), 0.0
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


def is_rotation(m: Mat, tol: float = 1e-6) -> bool:
    product = mat_mul(m, transpose(m))
    det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
           - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    return abs(det - 1.0) <= tol and all(abs(product[i][j] - IDENTITY[i][j]) <= tol for i in range(3) for j in range(3))


@dataclass(frozen=True)
class Transform:
    """``T_parent_child``: maps child-frame points into the parent frame."""

    rotation: Mat = IDENTITY
    translation: Vec = (0.0, 0.0, 0.0)

    def apply(self, p: Vec) -> Vec:
        return vec_add(mat_vec(self.rotation, p), self.translation)

    def rotate(self, v: Vec) -> Vec:
        return mat_vec(self.rotation, v)

    def inverse(self) -> "Transform":
        rt = transpose(self.rotation)
        t = mat_vec(rt, self.translation)
        return Transform(rt, (-t[0], -t[1], -t[2]))

    def compose(self, other: "Transform") -> "Transform":
        """``self.compose(other)`` applies ``other`` first, then ``self``."""
        return Transform(mat_mul(self.rotation, other.rotation), self.apply(other.translation))


def _finite_list(value, count: int, where: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != count:
        raise ConfigError(f"{where} must be a list of {count} numbers")
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ConfigError(f"{where} must contain only finite numbers")
        out.append(float(item))
    return out


def parse_rotation(spec, where: str) -> Mat:
    """Accepts {"rpy_deg": {yaw,pitch,roll}}, {"quaternion_wxyz": [w,x,y,z]} or {"matrix": [[..]*3]*3}."""
    if spec is None:
        return IDENTITY
    if not isinstance(spec, dict) or len(spec) != 1:
        raise ConfigError(f"{where} must contain exactly one of rpy_deg, quaternion_wxyz, matrix")
    (kind, value), = spec.items()
    if kind == "rpy_deg":
        if not isinstance(value, dict) or set(value) - {"yaw", "pitch", "roll"}:
            raise ConfigError(f"{where}.rpy_deg must have yaw, pitch, roll")
        vals = _finite_list([value.get("yaw", 0), value.get("pitch", 0), value.get("roll", 0)], 3, f"{where}.rpy_deg")
        return rotation_from_rpy_deg(*vals)
    if kind == "quaternion_wxyz":
        return rotation_from_quaternion(*_finite_list(value, 4, f"{where}.quaternion_wxyz"))
    if kind == "matrix":
        if not isinstance(value, list) or len(value) != 3:
            raise ConfigError(f"{where}.matrix must be 3x3")
        matrix = tuple(tuple(_finite_list(row, 3, f"{where}.matrix")) for row in value)
        if not is_rotation(matrix, 1e-4):  # type: ignore[arg-type]
            raise ConfigError(f"{where}.matrix is not a proper rotation (orthonormal, det=+1)")
        return matrix  # type: ignore[return-value]
    raise ConfigError(f"{where} has unknown rotation form '{kind}'")


def parse_translation(spec, where: str) -> Vec:
    values = _finite_list(spec if spec is not None else [0, 0, 0], 3, where)
    if any(abs(v) > 100_000 for v in values):
        raise ConfigError(f"{where} is outside the plausible +/-100 km range; check units (metres)")
    return (values[0], values[1], values[2])
