"""Stdlib-only tests: python3 -m unittest discover -s tests -v"""
import copy
import json
import math
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spatial import simulate  # noqa: E402
from spatial.camera import CameraModel  # noqa: E402
from spatial.geometry import (ConfigError, Transform, rotation_from_quaternion, rotation_from_rpy_deg,  # noqa: E402
                              is_rotation)
from spatial.models import RadarMeasurement  # noqa: E402
from spatial.overlay import project_map_point, project_track  # noqa: E402
from spatial.pointcloud import load_point_cloud  # noqa: E402
from spatial.radar import locate, radar_point_from_map  # noqa: E402
from spatial.runtime import SpatialRuntime  # noqa: E402
from spatial.site import load_site, parse_unit  # noqa: E402

DEMO = ROOT / "samples" / "spatial" / "demo"


def near(test, a, b, tol=1e-6):
    for x, y in zip(a, b):
        test.assertAlmostEqual(x, y, delta=tol)


class GeometryTests(unittest.TestCase):
    def test_rpy_conventions(self):
        forward = (0, 1, 0)
        near(self, Transform(rotation_from_rpy_deg(90, 0, 0)).rotate(forward), (-1, 0, 0))  # +yaw turns left (CCW from above)
        up = Transform(rotation_from_rpy_deg(0, 10, 0)).rotate(forward)
        self.assertGreater(up[2], 0.17)  # +pitch raises the forward axis
        self.assertTrue(is_rotation(rotation_from_rpy_deg(12, -34, 56)))

    def test_quaternion_matches_rpy(self):
        half = math.radians(90) / 2
        near(self, Transform(rotation_from_quaternion(math.cos(half), 0, 0, math.sin(half))).rotate((0, 1, 0)), (-1, 0, 0))

    def test_inverse_and_compose(self):
        a = Transform(rotation_from_rpy_deg(30, 5, -4), (1, 2, 3))
        b = Transform(rotation_from_rpy_deg(-10, 20, 7), (-4, 0.5, 2))
        p = (0.3, -2.0, 1.1)
        near(self, a.inverse().apply(a.apply(p)), p)
        near(self, a.compose(b).apply(p), a.apply(b.apply(p)))


class CameraTests(unittest.TestCase):
    cam = CameraModel(1280, 720, 900, 900, 640, 360)

    def test_principal_point_and_behind(self):
        p = self.cam.project((0, 0, 5))
        self.assertAlmostEqual(p.u_px, 640)
        self.assertAlmostEqual(p.v_px, 360)
        self.assertIsNone(self.cam.project((0, 0, -1)))

    def test_known_pixel(self):
        p = self.cam.project((1.0, -0.5, 5.0))  # x right, y up in image
        self.assertAlmostEqual(p.u_px, 640 + 900 * 0.2)
        self.assertAlmostEqual(p.v_px, 360 - 900 * 0.1)

    def test_distortion_moves_points_inward_for_negative_k1(self):
        barrel = CameraModel(1280, 720, 900, 900, 640, 360, (-0.2, 0, 0, 0, 0))
        self.assertLess(barrel.project((2, 0, 5)).u_px, self.cam.project((2, 0, 5)).u_px)


class SiteChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = load_site(DEMO)
        cls.unit = cls.site.units["unit-01"]

    def test_nominal_camera_axes(self):
        """With zero extrinsic adjustment the camera looks along enclosure +y and 'up' is enclosure +z."""
        doc = json.loads((DEMO / "units" / "unit-01.json").read_text())
        doc["enclosure_in_map"]["rotation"] = {"rpy_deg": {"yaw": 0, "pitch": 0, "roll": 0}}
        doc["enclosure_in_map"]["translation_m"] = [0, 0, 0]
        doc["camera"]["in_enclosure"] = {"translation_m": [0, 0, 0], "calibration": {"status": "simulated"}}
        unit = parse_unit(doc, "t")
        centre = project_map_point(unit, (0, 5, 0))
        self.assertAlmostEqual(centre.u_px, 640, places=3)
        self.assertAlmostEqual(centre.v_px, 360, places=3)
        self.assertLess(project_map_point(unit, (0, 5, 1)).v_px, 360)      # higher in the world -> higher in image
        self.assertGreater(project_map_point(unit, (1, 5, 0)).u_px, 640)   # to the right -> right in image

    def test_radar_roundtrip_through_full_chain(self):
        for truth in [(6.0, 5.0, 1.0), (3.0, 7.5, 0.4), (9.5, 3.0, 1.8)]:
            r, az, el = radar_point_from_map(truth, self.unit)
            m = RadarMeasurement(0.0, "radar-01", "unit-01", r, az, el)
            near(self, locate(m, self.unit).position, truth, 1e-9)

    def test_projection_matches_hand_computation(self):
        unit = self.unit
        point = (6.0, 5.0, 1.0)
        cam_in_map = unit.enclosure_in_map.compose(unit.camera.in_enclosure)
        p_cam = cam_in_map.inverse().apply(point)
        expected_u = 900 * (p_cam[0] / p_cam[2]) + 640
        projected = project_map_point(unit, point)
        self.assertAlmostEqual(projected.depth_m, p_cam[2], places=6)
        # k1/k2 are small; undistorted pixel must be within a few pixels, and different from the undistorted one.
        self.assertLess(abs(projected.u_px - expected_u), 15)

    def test_cross_range_sigma_and_missing_elevation(self):
        r = 8.0
        full = locate(RadarMeasurement(0, "radar-01", "unit-01", r, 0.0, 0.0), self.unit)
        flat = locate(RadarMeasurement(0, "radar-01", "unit-01", r, 0.0, None), self.unit)
        self.assertFalse(full.elevation_assumed)
        self.assertTrue(flat.elevation_assumed)
        self.assertAlmostEqual(full.sigma[0], r * math.radians(1.0), delta=0.02)  # x is cross-range for a yaw-0 unit
        self.assertGreater(flat.sigma[2] + flat.sigma[1], 3 * (full.sigma[2] + full.sigma[1]) / 2)
        self.assertGreater(flat.sigma[2], 2 * full.sigma[2])

    def test_overlay_radius_grows_with_unquantified_flag(self):
        marker = project_track(self.unit, (6.0, 5.0, 1.0), (0.1, 0.1, 0.1))
        self.assertTrue(marker["visible"])
        self.assertGreater(marker["radius_px"], 5)
        self.assertTrue(marker["calibration_error_quantified"])  # simulated records declare sigma 0
        doc = json.loads((DEMO / "units" / "unit-01.json").read_text())
        doc["camera"]["in_enclosure"]["calibration"] = {"status": "measured", "revision": 1}
        marker2 = project_track(parse_unit(doc, "t"), (6.0, 5.0, 1.0), (0.1, 0.1, 0.1))
        self.assertFalse(marker2["calibration_error_quantified"])


class RuntimeTests(unittest.TestCase):
    T0 = 1_000_000.0

    @classmethod
    def setUpClass(cls):
        cls.runtime = SpatialRuntime.from_directory(DEMO, clock=lambda: cls.T0)
        cls.unit = cls.runtime.site.units["unit-01"]
        _h, _f, cls.truth = simulate.simulate_radar(cls.unit)
        cls.snaps = []
        for step in range(0, 600):
            cls.snaps.append((step / 10, cls.runtime.snapshot(cls.T0 + step / 10)))

    def test_tracks_follow_truth(self):
        errors = []
        for t, snap in self.snaps:
            truth = dict(self.truth.get(round(t, 3), []))
            if 0 not in truth or not 4 < t < 28:
                continue
            xy = truth[0][:2]
            best = min((math.dist(track["position"][:2], xy) for track in snap["tracks"] if track["dimension"] == "3d"), default=None)
            errors.append(best)
        covered = [e for e in errors if e is not None]
        self.assertGreater(len(covered) / len(errors), 0.9)
        rms = math.sqrt(sum(e * e for e in covered) / len(covered))
        self.assertLess(rms, 0.35, f"RMS horizontal error {rms:.3f} m")

    def test_no_duplicate_tracks_on_one_target(self):
        """Regression: wide, clutter-born tentative tracks used to steal measurements and become duplicates."""
        crowded = total = 0
        for t, snap in self.snaps:
            truth = dict(self.truth.get(round(t, 3), []))
            if 0 not in truth or not 6 < t < 28:
                continue
            total += 1
            near = [k for k in snap["tracks"] if k["dimension"] == "3d" and math.dist(k["position"][:2], truth[0][:2]) < 1.0]
            crowded += len(near) > 1
        self.assertLessEqual(crowded / total, 0.02, f"{crowded}/{total} steps had duplicate tracks")

    def test_2d_target_is_flagged_and_uncertain_in_height(self):
        seen = [track for _t, snap in self.snaps for track in snap["tracks"] if track["dimension"] == "2d_elevation_assumed"]
        self.assertTrue(seen)
        self.assertGreater(min(track["sigma_m"][2] for track in seen[-20:]), 0.3)

    def test_clutter_does_not_become_confirmed_tracks(self):
        ids = {track["id"] for _t, snap in self.snaps for track in snap["tracks"]}
        self.assertLessEqual(len(ids), 8)  # two real targets (+ re-acquisition after the dropout), no clutter explosion

    def test_ids_are_anonymous_and_fields_complete(self):
        track = next(track for _t, snap in self.snaps for track in snap["tracks"])
        self.assertRegex(track["id"], r"^T-\d{4}$")
        for key in ("timestamp", "frame", "units", "sensor_ids", "sigma_m", "quality", "state", "dimension", "position"):
            self.assertIn(key, track)
        self.assertEqual(track["frame"], "map")
        self.assertNotIn("name", track)

    def test_dropout_is_visible_as_stale_then_missing_and_coasting(self):
        status = {round(t, 1): snap["units"][0]["radar"]["status"] for t, snap in self.snaps}
        self.assertEqual(status[20.0], "ok")
        self.assertEqual(status[32.5], "stale")
        self.assertEqual(status[35.5], "missing")
        self.assertEqual(status[40.0], "ok")
        states = {track["state"] for t, snap in self.snaps if 30.6 < t < 31.6 for track in snap["tracks"]}
        self.assertIn("coasting", states)

    def test_map_and_camera_payloads_are_bounded(self):
        payload = self.runtime.map_payload()
        self.assertLessEqual(len(payload["points"]) // 3, 12000)
        view = self.runtime.camera_view("unit-01")
        self.assertTrue(view["synthetic"] and view["points"])
        self.assertIsNone(self.runtime.camera_view("../etc/passwd"))

    def test_history_retention_zero_keeps_no_trail(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil
            shutil.copytree(DEMO, tmp, dirs_exist_ok=True)
            doc = json.loads((Path(tmp) / "site.json").read_text())
            doc["retention"]["track_history_seconds"] = 0
            (Path(tmp) / "site.json").write_text(json.dumps(doc))
            runtime = SpatialRuntime.from_directory(tmp, clock=lambda: 0.0)
            for step in range(300):
                snap = runtime.snapshot(self.T0 + step / 10)
            self.assertTrue(snap["tracks"])
            self.assertTrue(all(track["trail"] == [] for track in snap["tracks"]))

    def test_default_retention_keeps_short_in_memory_trail(self):
        trails = [len(track["trail"]) for _t, snap in self.snaps for track in snap["tracks"]]
        self.assertGreater(max(trails), 0)
        self.assertLessEqual(max(trails), 11 * 10)  # <= ~10 s at 10 Hz

    def test_uncalibrated_unit_places_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            import shutil
            shutil.copytree(DEMO, tmp, dirs_exist_ok=True)
            path = Path(tmp) / "units" / "unit-01.json"
            doc = json.loads(path.read_text())
            doc["enclosure_in_map"]["calibration"] = {"status": "uncalibrated"}
            path.write_text(json.dumps(doc))
            runtime = SpatialRuntime.from_directory(tmp, clock=lambda: 0.0)
            snap = None
            for step in range(200):
                snap = runtime.snapshot(self.T0 + step / 10)
            self.assertEqual(snap["tracks"], [])
            self.assertFalse(snap["units"][0]["placed"])
            self.assertTrue(any("uncalibrated" in w for w in snap["units"][0]["warnings"]))


class ConfigTests(unittest.TestCase):
    def copy_demo(self):
        import shutil
        tmp = tempfile.TemporaryDirectory()
        shutil.copytree(DEMO, tmp.name, dirs_exist_ok=True)
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def edit(self, root, name, fn):
        path = root / name
        doc = json.loads(path.read_text())
        fn(doc)
        path.write_text(json.dumps(doc))

    def test_rejects_unknown_schema_version(self):
        root = self.copy_demo()
        self.edit(root, "site.json", lambda d: d.update(schema_version=99))
        with self.assertRaises(ConfigError):
            load_site(root)

    def test_rejects_path_traversal(self):
        root = self.copy_demo()
        self.edit(root, "site.json", lambda d: d["map"].update(file="../../etc/passwd"))
        with self.assertRaises(ConfigError):
            load_site(root)

    def test_rejects_unimplemented_adapter(self):
        root = self.copy_demo()
        self.edit(root, "units/unit-01.json", lambda d: d["radar"].update(adapter="acme_vendor_x"))
        with self.assertRaises(ConfigError):
            SpatialRuntime.from_directory(root)

    def test_rejects_video_retention_and_persisted_tracks(self):
        root = self.copy_demo()
        self.edit(root, "site.json", lambda d: d["retention"].update(video="7d"))
        with self.assertRaises(ConfigError):
            load_site(root)
        root = self.copy_demo()
        self.edit(root, "site.json", lambda d: d["retention"].update(persist_tracks=True))
        with self.assertRaises(ConfigError):
            load_site(root)

    def test_rejects_non_rotation_matrix_and_fisheye(self):
        root = self.copy_demo()
        self.edit(root, "units/unit-01.json", lambda d: d["radar"]["in_enclosure"].update(rotation={"matrix": [[2, 0, 0], [0, 1, 0], [0, 0, 1]]}))
        with self.assertRaises(ConfigError):
            load_site(root)
        root = self.copy_demo()
        self.edit(root, "units/unit-01.json", lambda d: d["camera"]["intrinsics"].update(model="fisheye"))
        with self.assertRaises(ConfigError):
            load_site(root)

    def test_replay_rejects_wrong_units_and_frame(self):
        root = self.copy_demo()
        lines = (root / "radar_sample.jsonl").read_text().splitlines()
        header = json.loads(lines[0])
        header["units"]["range"] = "ft"
        (root / "radar_sample.jsonl").write_text("\n".join([json.dumps(header)] + lines[1:]))
        with self.assertRaises(ConfigError):
            SpatialRuntime.from_directory(root)

    def test_replay_skips_malformed_lines_and_invalid_targets(self):
        root = self.copy_demo()
        lines = (root / "radar_sample.jsonl").read_text().splitlines()
        lines.insert(5, "not json")
        lines.insert(7, json.dumps({"t": 0.45, "targets": [{"range_m": -3, "azimuth_deg": 0}, {"range_m": 4, "azimuth_deg": 999}]}))
        (root / "radar_sample.jsonl").write_text("\n".join(lines))
        runtime = SpatialRuntime.from_directory(root, clock=lambda: 0.0)
        runtime.update(0.0)
        runtime.update(10.0)
        counters = runtime.counters["unit-01"]
        self.assertGreaterEqual(counters["rejected"], 2)
        self.assertEqual(runtime.adapters["unit-01"].rejected_on_load, 1)


class PointCloudTests(unittest.TestCase):
    def test_binary_ply(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.ply"
            header = b"ply\nformat binary_little_endian 1.0\nelement vertex 2\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nend_header\n"
            path.write_bytes(header + struct.pack("<fffB", 1, 2, 3, 9) + struct.pack("<fffB", 4, 5, 6, 9))
            cloud = load_point_cloud(path)
            self.assertEqual(cloud.count, 2)
            self.assertEqual(list(cloud.xyz), [1, 2, 3, 4, 5, 6])

    def test_unit_scaling_and_truncation_and_downsample(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.xyz"
            path.write_text("\n".join(f"{i * 1000} {i} 0" for i in range(500)))
            cloud = load_point_cloud(path, "mm")
            self.assertAlmostEqual(cloud.xyz[3], 1.0, places=4)
            self.assertLessEqual(cloud.downsample(100).count, 100)
            bad = Path(tmp) / "t.ply"
            bad.write_bytes(b"ply\nformat binary_little_endian 1.0\nelement vertex 5\nproperty float x\nproperty float y\nproperty float z\nend_header\n\x00")
            with self.assertRaises(ConfigError):
                load_point_cloud(bad)


if __name__ == "__main__":
    unittest.main()
