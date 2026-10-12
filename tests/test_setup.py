"""Operator-flow tests for the setup wizard, driven against the simulated hidden-truth installation."""
import io
import json
import math
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spatial import setup_api  # noqa: E402
from spatial.geometry import mat_mul, transpose, yaw_pitch_roll_deg, CAMERA_NOMINAL_FORWARD  # noqa: E402
from spatial.runtime import SpatialRuntime  # noqa: E402
from spatial.setup import SetupError, SetupStore, effective_camera, effective_pose, _sim_truth  # noqa: E402
from spatial.site import load_site  # noqa: E402

SAMPLE_MAP = ROOT / "samples" / "spatial" / "demo" / "map.ply"
# spots inside both the radar cone and the (narrower) camera picture, near and far, left and right
FIT_SPOTS = [(4.0, 3.5), (7.8, 3.5), (5.9, 6.0), (3.0, 7.5), (8.8, 7.5), (6.0, 4.5), (4.2, 5.5), (7.6, 5.5)]
CHECK_SPOTS = [(5.0, 7.0), (7.0, 3.0), (6.8, 6.8)]


def pick_spots(store, site_id, n, avoid=()):
    """Choose n well-spread spots that the radar sees and that sit comfortably inside the video."""
    grid = [(x / 2, y / 2) for x in range(6, 22, 2) for y in range(5, 18, 2)]
    ok = []
    for x, y in grid:
        shown = store.sim_reflector(site_id, x, y)
        if shown["radar_visible"] and shown["in_image"] and 80 < shown["px"][0] < 1200 and 80 < shown["px"][1] < 640:
            ok.append((x, y))
    chosen = [ok[0]]
    while len(chosen) < n:
        chosen.append(max((p for p in ok if p not in chosen and p not in avoid),
                          key=lambda p: min(math.dist(p, q) for q in chosen + list(avoid))))
    return chosen


def operator(store, site_id, spots, role="fit", rng=None):
    """What a careful operator does: stand the reflector, mark it on the map, click it in the video, capture."""
    rng = rng or random.Random(5)
    for x, y in spots:
        shown = store.sim_reflector(site_id, x, y)
        assert shown["radar_visible"] and shown["in_image"], (x, y, shown)
        click = [shown["px"][0] + rng.gauss(0, 2), shown["px"][1] + rng.gauss(0, 2)]
        store.add_capture(site_id, {"x": x + rng.gauss(0, 0.03), "y": y + rng.gauss(0, 0.03), "px": click, "role": role})


class WizardFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        counter = iter(range(1000, 9999))
        self.store = SetupStore(Path(self.tmp.name), id_suffix=lambda: str(next(counter)))  # deterministic ids => deterministic hidden truth

    def new_unit(self):
        site = self.store.create_site("Test room", SAMPLE_MAP)["site_id"]
        self.store.set_unit(site, "Wall unit", "sim-radar", "sim-camera")
        self.store.set_placement(site, {"x": 6.0, "y": 0.3, "height_m": 2.4, "yaw_deg": 0, "tilt_deg": 10})
        return site

    def truth(self, site):
        return _sim_truth(self.store._load(site)["unit"])

    def test_full_guided_flow_recovers_hidden_installation(self):
        site = self.new_unit()
        before = self.store.view(site)["report"]
        self.assertEqual(before["pose"]["status"], "nominal")
        self.assertEqual(before["pose"]["quality"], "unassessed")  # never "calibrated" from placement alone
        fit_spots = pick_spots(self.store, site, 8)
        operator(self.store, site, fit_spots)
        report = self.store.view(site)["report"]
        self.assertEqual(report["pose"]["status"], "measured")
        self.assertIn(report["pose"]["quality"], ("good", "fair"))
        self.assertIn(report["camera"]["quality"], ("good", "fair"))
        self.assertIsNone(report["verification"]["pose"])  # fitted but not yet checked
        draft = self.store._load(site)["unit"]
        truth = self.truth(site)
        pose, camera = effective_pose(draft), effective_camera(draft)
        self.assertLess(math.dist(pose.translation, truth.enclosure_in_map.translation), 0.25)
        yaw_err = abs(yaw_pitch_roll_deg(pose.rotation)[0] - yaw_pitch_roll_deg(truth.enclosure_in_map.rotation)[0])
        self.assertLess(yaw_err, 3.0)
        adj = mat_mul(camera.rotation, transpose(truth.camera_in_enclosure.rotation))
        angle = math.degrees(math.acos(max(-1, min(1, (adj[0][0] + adj[1][1] + adj[2][2] - 1) / 2))))
        self.assertLess(angle, 2.5, f"camera rotation off by {angle:.2f} deg")
        # declared uncertainty must be honest: actual errors sit within a few declared sigmas
        self.assertLess(math.dist(pose.translation, truth.enclosure_in_map.translation), 4 * report["pose"]["sigma_point_m"] + 0.05)

        operator(self.store, site, pick_spots(self.store, site, 3, avoid=fit_spots), role="verify", rng=random.Random(11))
        report = self.store.view(site)["report"]
        self.assertEqual(report["verification"]["n"], 3)
        self.assertTrue(report["verification"]["pose"]["passed"])
        self.assertEqual(report["pose"]["status"], "verified")

    def test_save_load_live_accuracy_beats_uncalibrated(self):
        site = self.new_unit()
        saved = self.store.save(site)
        self.assertEqual(saved["report"]["pose"]["status"], "nominal")
        uncal = self.live_error(site)
        fit_spots = pick_spots(self.store, site, 8)
        operator(self.store, site, fit_spots)
        operator(self.store, site, pick_spots(self.store, site, 3, avoid=fit_spots), "verify", random.Random(11))
        self.store.save(site)
        cal = self.live_error(site)
        self.assertLess(cal, uncal)
        self.assertLess(cal, 0.4, f"calibrated live error {cal:.2f} m")
        unit_doc = json.loads((self.store._dir(site) / "units" / "unit-01.json").read_text())
        self.assertEqual(unit_doc["enclosure_in_map"]["calibration"]["status"], "verified")
        self.assertGreaterEqual(unit_doc["enclosure_in_map"]["calibration"]["revision"], 2)  # new evidence -> new revision
        self.assertTrue(list((self.store._dir(site) / "history").iterdir()))  # earlier config archived

    def test_declared_uncertainty_is_honest_across_many_hidden_installations(self):
        """The reported sigma must be the right order of magnitude vs the real error, not just small."""
        inside_cam = inside_pose = 0
        runs = 15
        for k in range(runs):
            store = SetupStore(Path(self.tmp.name) / f"mc{k}", id_suffix=lambda k=k: str(7000 + k))
            site = store.create_site("MC", SAMPLE_MAP)["site_id"]
            store.set_unit(site, "u", "sim-radar", "sim-camera")
            store.set_placement(site, {"x": 6.0, "y": 0.3, "height_m": 2.4, "yaw_deg": 0, "tilt_deg": 10})
            operator(store, site, pick_spots(store, site, 8), rng=random.Random(k))
            draft, report = store._load(site)["unit"], store.view(site)["report"]
            truth = _sim_truth(draft)
            adj = mat_mul(effective_camera(draft).rotation, transpose(truth.camera_in_enclosure.rotation))
            angle = math.degrees(math.acos(max(-1, min(1, (adj[0][0] + adj[1][1] + adj[2][2] - 1) / 2))))
            inside_cam += angle <= 2 * report["camera"]["sigma_rot_deg"]
            perr = math.dist(effective_pose(draft).translation, truth.enclosure_in_map.translation)
            inside_pose += perr <= 3 * report["pose"]["sigma_point_m"] + 0.02
        self.assertGreaterEqual(inside_cam / runs, 0.85)
        self.assertGreaterEqual(inside_pose / runs, 0.85)

    def live_error(self, site):
        runtime = SpatialRuntime.from_directory(self.store.saved_directory(site), clock=lambda: 0.0)
        t0, errors = 5_000.0, []
        adapter = runtime.adapters["unit-01"]
        for step in range(0, 400):
            now = t0 + step / 10
            snap = runtime.snapshot(now)
            if step < 80:
                continue
            truth = dict(adapter.truth_positions(now))
            if 0 in truth:
                best = min((math.dist(t["position"][:2], truth[0][:2]) for t in snap["tracks"] if t["dimension"] == "3d"), default=None)
                if best is not None:
                    errors.append(best)
        self.assertGreater(len(errors), 100)
        return math.sqrt(sum(e * e for e in errors) / len(errors))

    def test_draft_can_be_resumed_after_restart(self):
        site = self.new_unit()
        operator(self.store, site, pick_spots(self.store, site, 3))
        again = SetupStore(Path(self.tmp.name))
        listed = again.list_sites()
        self.assertEqual(listed[0]["stage"], "calibrate")
        view = again.view(site)
        self.assertEqual(len(view["unit"]["captures"]), 3)
        self.assertNotIn("simulation", view["unit"])  # hidden truth never leaves the server

    def test_too_few_or_collinear_spots_are_insufficient_with_help(self):
        site = self.new_unit()
        operator(self.store, site, pick_spots(self.store, site, 2))
        r = self.store.view(site)["report"]
        self.assertEqual(r["pose"]["quality"], "insufficient")
        self.assertTrue(r["pose"]["hints"])
        self.assertEqual(r["pose"]["status"], "nominal")
        site2 = self.new_unit()
        shown = [(x, 5.0) for x in (3.5, 4.5, 6.0, 7.5, 8.5)]
        shown = [p for p in shown if self.store.sim_reflector(site2, *p)["in_image"] and self.store.sim_reflector(site2, *p)["radar_visible"]]
        self.assertGreaterEqual(len(shown), 4)
        operator(self.store, site2, shown)
        r2 = self.store.view(site2)["report"]
        self.assertEqual(r2["pose"]["quality"], "insufficient")
        self.assertTrue(any("line" in h for h in r2["pose"]["hints"]))

    def test_wrong_video_clicks_are_graded_poor_not_good(self):
        site = self.new_unit()
        rng = random.Random(2)
        for x, y in pick_spots(self.store, site, 8):
            shown = self.store.sim_reflector(site, x, y)
            click = [shown["px"][0] + rng.uniform(-90, 90), shown["px"][1] + rng.uniform(-90, 90)]  # clicked the wrong thing
            self.store.add_capture(site, {"x": x, "y": y, "px": click})
        r = self.store.view(site)["report"]
        self.assertEqual(r["camera"]["quality"], "poor")
        self.assertTrue(r["camera"]["hints"])
        self.assertNotEqual(r["camera"]["status"], "verified")

    def test_manual_nudge_changes_residuals_and_resets_with_new_evidence(self):
        site = self.new_unit()
        operator(self.store, site, pick_spots(self.store, site, 8))
        base = self.store.view(site)["report"]["pose"]["rms_m"]
        self.store.nudge(site, {"pose": {"dx": 1.0, "dyaw_deg": 8}})
        r = self.store.view(site)["report"]
        self.assertTrue(r["pose"]["nudged"])
        self.assertGreater(r["pose"]["rms_m"], base + 0.3)  # a bad hand adjustment shows up as worse quality
        self.assertIn(r["pose"]["quality"], ("fair", "poor"))
        self.store.nudge(site, {"reset": True})
        self.assertAlmostEqual(self.store.view(site)["report"]["pose"]["rms_m"], base, places=3)

    def test_capture_outside_radar_view_is_explained(self):
        site = self.new_unit()
        with self.assertRaises(SetupError) as ctx:
            self.store.add_capture(site, {"x": 6.0, "y": -0.9, "px": [640, 360]})  # behind the unit
        self.assertIn("radar", ctx.exception.message.lower())
        self.assertTrue(ctx.exception.hint)

    def test_unsupported_map_formats_and_limits(self):
        site = self.store.create_site("M")["site_id"]
        for name, status in (("scan.laz", 415), ("scan.e57", 415), ("scan.exe", 415)):
            with self.assertRaises(SetupError) as ctx:
                self.store.receive_map(site, io.BytesIO(b"x").read, 1, name)
            self.assertEqual(ctx.exception.status, status)
            self.assertTrue(ctx.exception.hint)
        with self.assertRaises(SetupError) as ctx:
            self.store.receive_map(site, io.BytesIO(b"x").read, 10 ** 9, "big.ply")
        self.assertEqual(ctx.exception.status, 413)
        with self.assertRaises(SetupError) as ctx:
            self.store.receive_map(site, io.BytesIO(b"not a ply file at all").read, 21, "bad.ply")
        self.assertEqual(ctx.exception.status, 422)
        with self.assertRaises(SetupError):  # truncated upload
            self.store.receive_map(site, io.BytesIO(b"ply").read, 500, "cut.ply")
        self.assertIsNone(self.store.view(site)["map"])  # failed uploads leave no half-saved map

    def test_units_and_up_axis_options_and_warnings(self):
        site = self.store.create_site("U", SAMPLE_MAP)["site_id"]
        before = self.store.map_preview(site)
        self.assertAlmostEqual(before["size_m"][0], 12.0, delta=0.2)
        self.store.set_map_options(site, "cm", None)
        after = self.store.map_preview(site)
        self.assertAlmostEqual(after["size_m"][0], 0.12, places=1)
        self.assertTrue(any("small" in w for w in after["warnings"]))
        self.store.set_map_options(site, "m", "y")
        self.assertEqual(self.store.map_preview(site)["up_axis"], "y")

    def test_input_testing_and_error_help(self):
        bad = self.store.test_input("sim-radar-offline")
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["hint"])
        self.assertTrue(self.store.test_input("sim-radar")["ok"])
        self.assertFalse(self.store.inputs()["discovery"]["supported"])
        site = self.store.create_site("I", SAMPLE_MAP)["site_id"]
        with self.assertRaises(SetupError):
            self.store.set_unit(site, "x", "sim-radar-offline", "sim-camera")

    def test_browser_camera_is_preview_only(self):
        site = self.store.create_site("B", SAMPLE_MAP)["site_id"]
        self.store.set_unit(site, "x", "sim-radar", "browser-camera")
        report = self.store.view(site)["report"]
        self.assertFalse(report["capture_supported"])
        with self.assertRaises(SetupError) as ctx:
            self.store.add_capture(site, {"x": 5, "y": 5, "px": [10, 10]})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.store.save(site)["report"]["camera"]["status"], "nominal")  # still saves, honestly labelled

    def test_api_router_and_validation(self):
        status, body = setup_api.handle(self.store, "POST", "/api/setup/sites", {"name": "  "})
        self.assertEqual(status, 400)
        status, body = setup_api.handle(self.store, "POST", "/api/setup/sites", {"name": "Demo", "use_sample": True}, SAMPLE_MAP)
        self.assertEqual(status, 201)
        site = body["site_id"]
        self.assertEqual(setup_api.handle(self.store, "GET", "/api/setup/sites/../../etc", {})[0], 404)
        self.assertEqual(setup_api.handle(self.store, "POST", f"/api/setup/sites/{site}/placement", {"x": "evil"})[0], 409 if False else 400)
        self.assertEqual(setup_api.handle(self.store, "POST", f"/api/setup/sites/{site}/placement", {"x": 5})[0], 400)  # no unit yet
        self.assertEqual(setup_api.handle(self.store, "GET", "/api/setup/sites/nope-0000", {})[0], 404)
        self.assertEqual(setup_api.handle(self.store, "GET", "/api/setup/inputs", {})[0], 200)

    def test_private_permissions(self):
        site = self.store.create_site("P", SAMPLE_MAP)["site_id"]
        for path in (self.store._dir(site) / "setup.json", self.store._dir(site) / "preview.json", self.store._dir(site) / "map.ply"):
            self.assertEqual(path.stat().st_mode & 0o077, 0, path.name)


if __name__ == "__main__":
    unittest.main()
