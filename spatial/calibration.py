"""Evidence-based calibration solvers with uncertainty and plain-language quality grades.

Two things are estimated, and only from operator-supplied reference evidence (a static map alone cannot
determine either):

* ``fit_pose``   - the unit's pose in the map, from reflector spots whose map coordinates the operator
                   marked and whose positions the radar measured (rigid fit, Horn + Levenberg-Marquardt).
* ``fit_camera`` - camera orientation (and offset, with a measured prior) relative to the radar, from the
                   same spots seen by the radar (3D) and clicked in the video (pixels).

Each returns the transform, residuals and a covariance-derived uncertainty. The grade is a function of the
residuals AND the predicted uncertainty AND geometric coverage, so a handful of badly placed spots cannot
produce a "good" result.
"""
from __future__ import annotations

import math

from .camera import CameraModel
from .geometry import CAMERA_NOMINAL_FORWARD, Transform, Vec, exp_so3, mat_mul, mat_vec, transpose
from .linalg import covariance, jacobi_eigen, levenberg_marquardt

SIGMA_CLICK_M = 0.08      # how precisely an operator can mark a spot on the map
SIGMA_CLICK_PX = 3.0      # how precisely an operator can click a target in the video
POSE_MIN, POSE_RECOMMENDED = 3, 5
CAMERA_MIN, CAMERA_RECOMMENDED = 4, 6
VERIFY_MIN = 3
# Declared uncertainties are inflated by this factor: with only a handful of spots the covariance estimate itself is
# uncertain, and measured against simulated truth the raw values were slightly over-confident.
UNCERTAINTY_INFLATION = 1.3
VERIFY_POSE_RMS_M, VERIFY_CAMERA_RMS_PX = 0.25, 20.0


def grade_pose(rms_m: float, sigma_rot_deg: float, n: int) -> str:
    if rms_m <= 0.15 and sigma_rot_deg <= 2.0 and n >= 4:
        return "good"
    return "fair" if rms_m <= 0.35 and sigma_rot_deg <= 5.0 else "poor"


def grade_camera(rms_px: float | None, sigma_rot_deg: float, n: int) -> str:
    if rms_px is None or rms_px != rms_px:
        return "poor"
    if rms_px <= 10 and sigma_rot_deg <= 1.0 and n >= CAMERA_MIN:
        return "good"
    return "fair" if rms_px <= 25 and sigma_rot_deg <= 3.0 else "poor"


def _skew(v: Vec):
    return ((0.0, -v[2], v[1]), (v[2], 0.0, -v[0]), (-v[1], v[0], 0.0))


def _horn(pairs) -> Transform:
    """Closed-form rigid fit b = R a + t (Horn 1987, unit quaternion)."""
    n = len(pairs)
    ca = [sum(p[0][i] for p in pairs) / n for i in range(3)]
    cb = [sum(p[1][i] for p in pairs) / n for i in range(3)]
    s = [[sum((p[0][i] - ca[i]) * (p[1][j] - cb[j]) for p in pairs) for j in range(3)] for i in range(3)]
    n4 = [[s[0][0] + s[1][1] + s[2][2], s[1][2] - s[2][1], s[2][0] - s[0][2], s[0][1] - s[1][0]],
          [s[1][2] - s[2][1], s[0][0] - s[1][1] - s[2][2], s[0][1] + s[1][0], s[2][0] + s[0][2]],
          [s[2][0] - s[0][2], s[0][1] + s[1][0], -s[0][0] + s[1][1] - s[2][2], s[1][2] + s[2][1]],
          [s[0][1] - s[1][0], s[2][0] + s[0][2], s[1][2] + s[2][1], -s[0][0] - s[1][1] + s[2][2]]]
    values, vectors = jacobi_eigen(n4)
    best = max(range(4), key=lambda i: values[i])
    w, x, y, z = (vectors[k][best] for k in range(4))
    from .geometry import rotation_from_quaternion
    rot = rotation_from_quaternion(w, x, y, z)
    ra = mat_vec(rot, tuple(ca))  # type: ignore[arg-type]
    return Transform(rot, (cb[0] - ra[0], cb[1] - ra[1], cb[2] - ra[2]))


def _spread_m(points) -> float:
    """sqrt of the second-largest eigenvalue of the point covariance: ~0 when points are collinear."""
    n = len(points)
    c = [sum(p[i] for p in points) / n for i in range(3)]
    cov = [[sum((p[i] - c[i]) * (p[j] - c[j]) for p in points) / n for j in range(3)] for i in range(3)]
    values = sorted(jacobi_eigen(cov)[0], reverse=True)
    return math.sqrt(max(0.0, values[1]))


def pose_residuals(transform: Transform, captures: list[dict]) -> list[float]:
    out = []
    for c in captures:
        p = transform.apply(tuple(c["radar"]))  # type: ignore[arg-type]
        out.append(math.dist(p, c["map"]))
    return out


def camera_residuals_px(cam_in_radar: Transform, captures: list[dict], model: CameraModel) -> list[float | None]:
    out = []
    inverse = cam_in_radar.inverse()
    for c in captures:
        proj = model.project(inverse.apply(tuple(c["radar"])))  # type: ignore[arg-type]
        out.append(None if proj is None else math.hypot(proj.u_px - c["px"][0], proj.v_px - c["px"][1]))
    return out


def _rms(values) -> float:
    values = [v for v in values if v is not None]
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else float("nan")


def fit_pose(captures: list[dict]):
    """captures: dicts with 'radar' xyz (radar frame), 'map' xyz, 'radar_sigma_m'. Returns (Transform|None, info)."""
    use = [c for c in captures if c.get("role", "fit") == "fit" and c.get("radar") and c.get("map")]
    info = {"n": len(use), "needed": POSE_MIN, "recommended": POSE_RECOMMENDED, "hints": []}
    if len(use) < POSE_MIN:
        info.update(grade="insufficient", hints=[f"Add {POSE_MIN - len(use)} more reference spot(s) to estimate the unit's position and direction."])
        return None, info
    pairs = [(tuple(c["radar"]), tuple(c["map"])) for c in use]
    spread = _spread_m([p[1] for p in pairs])
    info["spread_m"] = round(spread, 2)
    if spread < 0.3:
        info.update(grade="insufficient", hints=["The spots are almost in a straight line. Spread them across the room, not along one wall."])
        return None, info
    base = _horn(pairs)
    sig = [math.hypot(float(c.get("radar_sigma_m", 0.05)), SIGMA_CLICK_M) for c in use]

    def residual(x):
        rot = mat_mul(exp_so3((x[0], x[1], x[2])), base.rotation)
        t = (base.translation[0] + x[3], base.translation[1] + x[4], base.translation[2] + x[5])
        r = []
        for (a, b), s in zip(pairs, sig):
            p = mat_vec(rot, a)
            r.extend(((p[i] + t[i] - b[i]) / s for i in range(3)))
        return r

    x, jac, res = levenberg_marquardt(residual, [0.0] * 6)
    transform = Transform(mat_mul(exp_so3((x[0], x[1], x[2])), base.rotation),
                          (base.translation[0] + x[3], base.translation[1] + x[4], base.translation[2] + x[5]))
    rms = _rms(pose_residuals(transform, use))
    try:
        cov, scale = covariance(jac, res)
    except ValueError:
        info.update(grade="insufficient", hints=["These spots do not pin down the unit's pose. Add spots at different distances and angles."])
        return None, info
    # uncertainty of a *mapped target* at the evidence locations (accounts for rotation lever arm)
    var = 0.0
    for a, _b in pairs:
        ra = mat_vec(transform.rotation, a)
        jr = _skew(ra)
        jmat = [[-jr[i][k] for k in range(3)] + [1.0 if i == k else 0.0 for k in range(3)] for i in range(3)]
        var += sum(jmat[i][p] * cov[p][q] * jmat[i][q] for i in range(3) for p in range(6) for q in range(6)) / 3
    sigma_point = math.sqrt(max(0.0, var / len(pairs))) * UNCERTAINTY_INFLATION
    sigma_rot = math.degrees(math.sqrt(max(cov[i][i] for i in range(3)))) * UNCERTAINTY_INFLATION
    sigma_yaw = math.degrees(math.sqrt(max(0.0, cov[2][2]))) * UNCERTAINTY_INFLATION
    grade = grade_pose(rms, sigma_rot, len(use))
    hints = []
    if len(use) < POSE_RECOMMENDED:
        hints.append(f"{POSE_RECOMMENDED - len(use)} more spot(s) would tighten this estimate.")
    if spread < 1.0:
        hints.append("Spots are clustered; spread them over a wider part of the room.")
    if rms > 0.35:
        hints.append("Some spots disagree with each other by more than 35 cm. Re-check the largest-error spot below; the map click or the reflector position may be off.")
    info.update(grade=grade, rms_m=round(rms, 3), sigma_point_m=round(sigma_point, 3), sigma_rot_deg=round(sigma_rot, 2),
                sigma_yaw_deg=round(sigma_yaw, 2), uncertainty_scale=round(scale, 2), hints=hints,
                residuals_m=[round(v, 3) for v in pose_residuals(transform, use)], ids=[c["id"] for c in use])
    return transform, info


def fit_camera(captures: list[dict], model: CameraModel, offset_m: Vec = (0.0, 0.0, 0.0), offset_sigma_m: float = 0.05):
    """Returns (T_enclosure_camera|None, info). Radar frame == enclosure frame in this system."""
    use = [c for c in captures if c.get("role", "fit") == "fit" and c.get("radar") and c.get("px")]
    info = {"n": len(use), "needed": CAMERA_MIN, "recommended": CAMERA_RECOMMENDED, "hints": []}
    if len(use) < CAMERA_MIN:
        info.update(grade="insufficient", hints=[f"Add {CAMERA_MIN - len(use)} more spot(s) that you can click in the video."])
        return None, info
    base_rot = CAMERA_NOMINAL_FORWARD
    sig = []
    for c in use:
        depth = max(0.5, c["radar"][1])
        sig.append(math.hypot(SIGMA_CLICK_PX, model.fx * float(c.get("radar_sigma_m", 0.05)) / depth))

    def pose_of(x):
        return Transform(mat_mul(exp_so3((x[0], x[1], x[2])), base_rot),
                         (offset_m[0] + x[3], offset_m[1] + x[4], offset_m[2] + x[5]))

    def residual(x):
        inverse = pose_of(x).inverse()
        r = []
        for c, s in zip(use, sig):
            proj = model.project(inverse.apply(tuple(c["radar"])))  # type: ignore[arg-type]
            if proj is None:
                r.extend((1e3, 1e3))
            else:
                r.extend(((proj.u_px - c["px"][0]) / s, (proj.v_px - c["px"][1]) / s))
        r.extend(v / offset_sigma_m for v in x[3:])
        return r

    x, jac, res = levenberg_marquardt(residual, [0.0] * 6)
    pose = pose_of(x)
    px = camera_residuals_px(pose, use, model)
    rms = _rms(px)
    try:
        cov, scale = covariance(jac, res)
    except ValueError:
        info.update(grade="insufficient", hints=["These spots do not determine the camera direction. Use spots spread across the video."])
        return None, info
    sigma_rot = math.degrees(math.sqrt(max(cov[i][i] for i in range(3)))) * UNCERTAINTY_INFLATION
    sigma_t = math.sqrt(max(0.0, sum(cov[i][i] for i in range(3, 6)) / 3)) * UNCERTAINTY_INFLATION
    ranges = [math.sqrt(sum(v * v for v in c["radar"])) for c in use]
    u_span = (max(c["px"][0] for c in use) - min(c["px"][0] for c in use)) / model.width
    hints = []
    if rms != rms or any(v is None for v in px):
        grade = "poor"
        hints.append("Some spots fall outside what this camera model can show. Re-check the clicks or the lens field of view.")
    else:
        grade = grade_camera(rms, sigma_rot, len(use))
    if len(use) < CAMERA_RECOMMENDED:
        hints.append(f"{CAMERA_RECOMMENDED - len(use)} more spot(s) would tighten this estimate.")
    if max(ranges) / max(min(ranges), 0.1) < 1.5:
        hints.append("All spots are about the same distance away. Include some near and some far spots.")
    if u_span < 0.25:
        hints.append("Spots cover only a narrow part of the video. Include spots toward the left and right edges.")
    if rms > 25:
        hints.append("Clicks and radar readings disagree by more than 25 px. Check that you clicked the reflector itself, and that the lens field of view (Advanced) is right.")
    info.update(grade=grade, rms_px=None if rms != rms else round(rms, 1), sigma_rot_deg=round(sigma_rot, 2),
                sigma_offset_m=round(sigma_t, 3), uncertainty_scale=round(scale, 2), hints=hints,
                residuals_px=[None if v is None else round(v, 1) for v in px], ids=[c["id"] for c in use],
                range_span_m=[round(min(ranges), 1), round(max(ranges), 1)])
    return pose, info


def verification(pose: Transform | None, camera: Transform | None, captures: list[dict], model: CameraModel) -> dict:
    """Independent check on spots NOT used in the fits."""
    checks = [c for c in captures if c.get("role") == "verify"]
    out = {"n": len(checks), "needed": VERIFY_MIN, "pose": None, "camera": None}
    if pose is not None:
        have = [c for c in checks if c.get("radar") and c.get("map")]
        if have:
            rms = _rms(pose_residuals(pose, have))
            out["pose"] = {"n": len(have), "rms_m": round(rms, 3), "passed": len(have) >= VERIFY_MIN and rms <= VERIFY_POSE_RMS_M}
    if camera is not None:
        have = [c for c in checks if c.get("radar") and c.get("px")]
        if have:
            px = camera_residuals_px(camera, have, model)
            rms = _rms(px)
            out["camera"] = {"n": len(have), "rms_px": None if rms != rms else round(rms, 1),
                             "passed": len(have) >= VERIFY_MIN and rms == rms and rms <= VERIFY_CAMERA_RMS_PX and all(v is not None for v in px)}
    return out
