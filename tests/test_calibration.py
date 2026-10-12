"""Solver-level tests with synthetic ground truth (no wizard involved)."""
import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spatial import calibration as cal  # noqa: E402
from spatial.camera import CameraModel  # noqa: E402
from spatial.geometry import CAMERA_NOMINAL_FORWARD, Transform, mat_mul, rotation_from_rpy_deg, transpose  # noqa: E402

MODEL = CameraModel(1280, 720, 900, 900, 640, 360)
TRUTH = Transform(rotation_from_rpy_deg(8, -12, 0), (6.2, 0.4, 2.4))
CAM = Transform(mat_mul(rotation_from_rpy_deg(1.8, -1.2, 0.6), CAMERA_NOMINAL_FORWARD), (0.04, 0.03, 0.05))
SPOTS = [(3, 4), (9, 4.5), (6, 7), (4, 8), (8.5, 7.5), (6, 3), (2.5, 6), (10, 6), (5, 5.5), (7, 4)]


def captures(map_noise=0.04, px_noise=2.0, seed=3, spots=SPOTS):
    rng = random.Random(seed)
    out = []
    for i, (x, y) in enumerate(spots):
        m = (x, y, 1.0)
        a = tuple(v + rng.gauss(0, 0.04) for v in TRUTH.inverse().apply(m))
        p = MODEL.project(CAM.inverse().apply(TRUTH.inverse().apply(m)))
        out.append({"id": i, "role": "fit", "map": [m[0] + rng.gauss(0, map_noise), m[1] + rng.gauss(0, map_noise), 1.0], "radar": list(a),
                    "radar_sigma_m": 0.05, "px": [p.u_px + rng.gauss(0, px_noise), p.v_px + rng.gauss(0, px_noise)]})
    return out


class SolverTests(unittest.TestCase):
    def test_pose_recovered_with_honest_uncertainty(self):
        errors, sigmas = [], []
        for seed in range(10):
            t, info = cal.fit_pose(captures(seed=seed))
            errors.append(math.dist(t.translation, TRUTH.translation))
            sigmas.append(info["sigma_point_m"])
        self.assertLess(sum(errors) / len(errors), 0.12)
        # declared sigma should be of the same order as the real error (not wildly over- or under-confident)
        self.assertLess(sum(errors) / len(errors), 4 * sum(sigmas) / len(sigmas) + 0.05)

    def test_camera_rotation_recovered(self):
        t, info = cal.fit_camera(captures(), MODEL)
        adj = mat_mul(t.rotation, transpose(CAM.rotation))
        angle = math.degrees(math.acos(max(-1, min(1, (adj[0][0] + adj[1][1] + adj[2][2] - 1) / 2))))
        self.assertLess(angle, 1.0)
        self.assertIn(info["grade"], ("good", "fair"))

    def test_bad_map_clicks_are_not_graded_good(self):
        rng = random.Random(9)
        bad = captures()
        for c in bad:
            c["map"][0] += rng.uniform(-0.8, 0.8)
            c["map"][1] += rng.uniform(-0.8, 0.8)
        _t, info = cal.fit_pose(bad)
        self.assertIn(info["grade"], ("fair", "poor"))
        self.assertGreater(info["rms_m"], 0.3)

    def test_degenerate_inputs_refused(self):
        self.assertEqual(cal.fit_pose(captures(spots=SPOTS[:2]))[1]["grade"], "insufficient")
        line = [(x, 5.0) for x in (3, 4, 5.5, 7, 8.5)]
        t, info = cal.fit_pose(captures(spots=line))
        self.assertIsNone(t)
        self.assertEqual(info["grade"], "insufficient")
        self.assertEqual(cal.fit_camera(captures(spots=SPOTS[:3]), MODEL)[1]["grade"], "insufficient")

    def test_verification_uses_only_check_spots(self):
        caps = captures()
        for c in caps[7:]:
            c["role"] = "verify"
        pose, _ = cal.fit_pose(caps)
        camera, _ = cal.fit_camera(caps, MODEL)
        v = cal.verification(pose, camera, caps, MODEL)
        self.assertEqual(v["pose"]["n"], 3)
        self.assertTrue(v["pose"]["passed"] and v["camera"]["passed"])
        self.assertFalse(cal.verification(pose, camera, caps[:7], MODEL)["pose"])  # no checks yet -> nothing claimed


if __name__ == "__main__":
    unittest.main()
