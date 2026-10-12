"""Setup wizard back end: resumable drafts, map ingest, guided calibration evidence, and compile-to-config.

A *draft* (``setup.json``) holds everything the operator has done so far and is rewritten after every action,
so setup can be left and resumed. Saving *compiles* the draft into the versioned site configuration read by
``spatial.site`` (and archives the previous version under ``history/``).
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

from . import calibration as cal
from .camera import CameraModel
from .geometry import (CAMERA_NOMINAL_FORWARD, ConfigError, Transform, mat_mul, quaternion_from_rotation, rot_z,
                       rotation_from_rpy_deg, transpose, yaw_pitch_roll_deg)
from .inputs import DISCOVERY_NOTE, INPUTS, public_inputs
from .pointcloud import SCALE_TO_METRES, load_point_cloud
from .runtime import SpatialRuntime
from .simulate import scenario_for_bounds, simulate_reflector, target_position, truth_pixel, _as_tuple
from .site import ID_RE, load_site
from .models import SimTruth

MAX_MAP_BYTES = 120_000_000
MAP_EXTS = (".ply", ".xyz", ".txt", ".csv")
UNSUPPORTED = {".las": "LAS", ".laz": "LAZ", ".e57": "E57", ".pcd": "PCD", ".obj": "OBJ", ".fbx": "FBX", ".rcp": "ReCap", ".pts": "PTS"}
STAGES = ("map", "sensors", "place", "calibrate", "review")
MAX_SITES, MAX_CAPTURES = 20, 40
UNIT_ID = "unit-01"
PREVIEW_POINTS = 12_000


class SetupError(Exception):
    def __init__(self, message: str, hint: str = "", status: int = 400):
        super().__init__(message)
        self.message, self.hint, self.status = message, hint, status


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(value, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise SetupError(f"{name} must be a number between {low} and {high}.")
    return float(value)


def _write_private(path: Path, value) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    temp.write_text(json.dumps(value, separators=(",", ":")))
    os.chmod(temp, 0o600)
    temp.replace(path)


# ---- effective transforms -------------------------------------------------------------------------
def lens_model(unit: dict) -> CameraModel:
    lens = unit["lens"]
    w, h = int(lens["width"]), int(lens["height"])
    fx = (w / 2) / math.tan(math.radians(lens["hfov_deg"]) / 2)
    return CameraModel(w, h, fx, fx, w / 2, h / 2)


def _pose_base(unit: dict) -> Transform | None:
    fit = unit.get("pose_fit")
    if fit:
        return Transform(tuple(tuple(r) for r in fit["rotation"]), tuple(fit["translation"]))  # type: ignore[arg-type]
    p = unit.get("placement")
    if not p:
        return None
    return Transform(rotation_from_rpy_deg(p["yaw_deg"], -p["tilt_deg"], p["roll_deg"]), (p["x"], p["y"], p["z"]))


def effective_pose(unit: dict) -> Transform | None:
    base = _pose_base(unit)
    if base is None:
        return None
    n = unit.get("pose_nudge", {})
    rot = mat_mul(rot_z(math.radians(n.get("dyaw_deg", 0.0))), base.rotation)
    return Transform(rot, (base.translation[0] + n.get("dx", 0.0), base.translation[1] + n.get("dy", 0.0), base.translation[2] + n.get("dz", 0.0)))


def effective_camera(unit: dict) -> Transform:
    fit = unit.get("camera_fit")
    base = (Transform(tuple(tuple(r) for r in fit["rotation"]), tuple(fit["translation"]))  # type: ignore[arg-type]
            if fit else Transform(CAMERA_NOMINAL_FORWARD, tuple(unit.get("camera_offset_m", [0, 0, 0]))))  # type: ignore[arg-type]
    n = unit.get("camera_nudge", {})
    rot = mat_mul(rotation_from_rpy_deg(n.get("yaw", 0.0), n.get("pitch", 0.0), n.get("roll", 0.0)), base.rotation)
    return Transform(rot, base.translation)


def _nudged(unit: dict) -> tuple[bool, bool]:
    pn, cn = unit.get("pose_nudge", {}), unit.get("camera_nudge", {})
    return any(abs(v) > 1e-9 for v in pn.values()), any(abs(v) > 1e-9 for v in cn.values())


def _sim_truth(unit: dict) -> SimTruth | None:
    from .site import parse_simulation
    return parse_simulation(unit.get("simulation"), "draft") if unit.get("simulation") else None


class SetupStore:
    def __init__(self, root: Path, clock=time.time, id_suffix=None):
        self.id_suffix = id_suffix or (lambda: secrets.token_hex(2))
        self.root = Path(root)
        self.sites = self.root / "sites"
        self.clock = clock

    # ---- storage ----------------------------------------------------------------------------------
    def _dir(self, site_id: str) -> Path:
        if not isinstance(site_id, str) or not ID_RE.match(site_id):
            raise SetupError("Unknown space.", status=404)
        return self.sites / site_id

    def _load(self, site_id: str) -> dict:
        path = self._dir(site_id) / "setup.json"
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SetupError("That space could not be found.", "It may have been deleted. Go back to the list of spaces.", 404) from exc

    def _save(self, draft: dict) -> None:
        draft["updated"] = _now()
        _write_private(self._dir(draft["site_id"]) / "setup.json", draft)

    def list_sites(self) -> list[dict]:
        out = []
        if self.sites.is_dir():
            for path in sorted(self.sites.iterdir()):
                try:
                    d = json.loads((path / "setup.json").read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                out.append({"site_id": d["site_id"], "name": d["name"], "stage": d.get("stage", "map"), "saved_revision": d.get("saved_revision", 0),
                            "updated": d.get("updated"), "has_map": bool(d.get("map")), "has_unit": bool(d.get("unit"))})
        return out

    def create_site(self, name: str, sample_map: Path | None = None) -> dict:
        name = (name or "").strip()
        if not 1 <= len(name) <= 60:
            raise SetupError("Give the space a name (up to 60 characters).")
        if len(self.list_sites()) >= MAX_SITES:
            raise SetupError("You have reached the limit of 20 spaces.", "Delete one you no longer need.")
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "space"
        site_id = f"{slug}-{self.id_suffix()}"
        draft = {"schema_version": 1, "site_id": site_id, "name": name, "created": _now(), "stage": "map", "saved_revision": 0,
                 "map": None, "unit": None}
        self._dir(site_id).mkdir(mode=0o700, parents=True, exist_ok=True)
        self._save(draft)
        if sample_map is not None:
            size = sample_map.stat().st_size
            with sample_map.open("rb") as handle:
                self.receive_map(site_id, handle.read, size, sample_map.name, "m", "z")
        return self.view(site_id)

    def delete_site(self, site_id: str) -> None:
        self._load(site_id)
        shutil.rmtree(self._dir(site_id))

    def set_stage(self, site_id: str, stage: str) -> dict:
        draft = self._load(site_id)
        if stage not in STAGES:
            raise SetupError("Unknown step.")
        draft["stage"] = stage
        self._save(draft)
        return self.view(site_id)

    # ---- step 1: map ------------------------------------------------------------------------------
    def receive_map(self, site_id: str, read, length: int, filename: str, units: str = "m", up_axis: str = "z") -> dict:
        draft = self._load(site_id)
        ext = Path(filename or "").suffix.lower()
        if ext in UNSUPPORTED:
            raise SetupError(f"{UNSUPPORTED[ext]} files aren't supported yet.",
                             "Open the scan in your scanner software (or a free tool such as CloudCompare) and export it as PLY, "
                             "then upload that file.", 415)
        if ext not in MAP_EXTS:
            raise SetupError("That file type isn't supported.", "Use a PLY or XYZ point-cloud file (.ply, .xyz, .txt or .csv).", 415)
        if units not in SCALE_TO_METRES or up_axis not in ("z", "y"):
            raise SetupError("Unknown units or up-axis.")
        if length <= 0:
            raise SetupError("The file is empty.", status=411)
        if length > MAX_MAP_BYTES:
            raise SetupError(f"That file is {length / 1e6:.0f} MB; the limit is {MAX_MAP_BYTES / 1e6:.0f} MB.",
                             "Reduce the number of points (a decimated or sub-sampled export is plenty for a floor plan) and try again.", 413)
        folder = self._dir(site_id)
        temp = folder / f"upload{ext}"
        try:
            remaining = length
            with temp.open("wb") as out:
                while remaining:
                    chunk = read(min(1 << 20, remaining))
                    if not chunk:
                        raise SetupError("The upload was interrupted.", "Check your connection to this server and try again.", 400)
                    out.write(chunk)
                    remaining -= len(chunk)
            os.chmod(temp, 0o600)
            return self._ingest(draft, temp, ext, filename, units, up_axis)
        finally:
            temp.unlink(missing_ok=True)

    def _ingest(self, draft: dict, temp: Path, ext: str, original: str, units: str, up_axis: str) -> dict:
        folder = self._dir(draft["site_id"])
        try:
            cloud = load_point_cloud(temp, units, up_axis)
        except ConfigError as exc:
            raise SetupError(f"This file could not be read: {exc}", "Make sure it is a PLY (ASCII or binary) or plain XYZ text file "
                             "with x, y, z columns. Re-export it from your scanner software if unsure.", 422) from exc
        except (ValueError, IndexError, OverflowError) as exc:
            raise SetupError("This file is damaged or not a point cloud.", "Re-export it as PLY and try again.", 422) from exc
        for old in folder.glob("map.*"):
            old.unlink()
        final = folder / f"map{ext}"
        shutil.copyfile(temp, final)
        os.chmod(final, 0o600)
        lo, hi = cloud.bounds()
        shown = cloud.downsample(PREVIEW_POINTS)
        floor = cloud.floor_z()
        preview = {"points": [round(v, 2) for v in shown.xyz], "points_total": cloud.count, "points_shown": shown.count}
        _write_private(folder / "preview.json", preview)
        revision = (draft["map"]["revision"] + 1) if draft.get("map") else 1
        draft["map"] = {"file": final.name, "original_name": os.path.basename(original or "")[:100], "source_units": units, "up_axis": up_axis,
                        "bytes": final.stat().st_size, "points": cloud.count, "bounds": {"min": [round(v, 3) for v in lo], "max": [round(v, 3) for v in hi]},
                        "floor_z": round(floor, 3), "revision": revision}
        if draft.get("unit"):
            self._reset_unit_for_new_map(draft)
        self._save(draft)
        return self.view(draft["site_id"])

    def set_map_options(self, site_id: str, units: str | None, up_axis: str | None, floor_z=None) -> dict:
        draft = self._load(site_id)
        meta = draft.get("map")
        if not meta:
            raise SetupError("Upload a map first.")
        units, up_axis = units or meta["source_units"], up_axis or meta["up_axis"]
        if (units, up_axis) != (meta["source_units"], meta["up_axis"]):
            path = self._dir(site_id) / meta["file"]
            self._ingest_existing(draft, path, units, up_axis)
            draft = self._load(site_id)
            meta = draft["map"]
        if floor_z is not None:
            meta["floor_z"] = round(_num(floor_z, "Floor height", -1e4, 1e4), 3)
            self._save(draft)
        return self.view(site_id)

    def _ingest_existing(self, draft, path: Path, units: str, up_axis: str) -> None:
        temp = path.with_name("reparse" + path.suffix)
        shutil.copyfile(path, temp)
        try:
            self._ingest(draft, temp, path.suffix, draft["map"]["original_name"], units, up_axis)
        finally:
            temp.unlink(missing_ok=True)

    def map_preview(self, site_id: str) -> dict:
        draft = self._load(site_id)
        if not draft.get("map"):
            raise SetupError("No map uploaded yet.", status=404)
        preview = json.loads((self._dir(site_id) / "preview.json").read_text())
        meta = draft["map"]
        lo, hi = meta["bounds"]["min"], meta["bounds"]["max"]
        warnings = []
        size = [hi[i] - lo[i] for i in range(3)]
        if max(size[:2]) > 400:
            warnings.append(f"This map is {max(size[:2]):.0f} m across. If that seems too big, the file may be in centimetres or millimetres: change the units.")
        if max(size[:2]) < 2:
            warnings.append(f"This map is only {max(size[:2]):.1f} m across. If that seems too small, the file may be in metres but read as another unit: change the units.")
        if size[2] > 0.9 * max(size[:2]) and size[2] > 8:
            warnings.append("The map is very tall compared with its floor area. If it looks sideways, set 'Which way is up?' to Y.")
        if meta["points"] < 500:
            warnings.append("This map has very few points, so features will be hard to recognise when marking spots.")
        return {"bounds": meta["bounds"], "floor_z": meta["floor_z"], "units": meta["source_units"], "up_axis": meta["up_axis"], "points": preview["points"],
                "points_total": preview["points_total"], "points_shown": preview["points_shown"],
                "size_m": [round(v, 2) for v in size], "warnings": warnings, "file": meta["original_name"], "bytes": meta["bytes"]}

    # ---- step 2: sensors --------------------------------------------------------------------------
    def inputs(self) -> dict:
        return public_inputs()

    def test_input(self, input_id: str) -> dict:
        item = INPUTS.get(input_id)
        if item is None:
            raise SetupError("That input isn't available.", "Press 'Look for devices' and choose from the list.", 404)
        if not item["connectable"]:
            return {"input_id": input_id, "ok": False, "status": "no_data", **item["failure"]}
        if item["kind"] == "radar":
            return {"input_id": input_id, "ok": True, "status": "connected", "frame_rate_hz": 10.0, "age_s": 0.1,
                    "message": "Receiving data (simulated, 10 frames per second).", "simulated": True}
        if item["preview"] == "simulated":
            return {"input_id": input_id, "ok": True, "status": "connected", "message": "Simulated video is available.", "simulated": True,
                    "image_size": item["image_size"]}
        return {"input_id": input_id, "ok": True, "status": "browser", "simulated": False,
                "message": "This runs in your browser. Press 'Show preview' and allow access if asked.", "image_size": item["image_size"]}

    def set_unit(self, site_id: str, label: str, radar_input: str, camera_input: str, hfov_deg=70, offset_cm=None) -> dict:
        draft = self._load(site_id)
        if not draft.get("map"):
            raise SetupError("Upload a map before adding a unit.")
        radar, camera = INPUTS.get(radar_input), INPUTS.get(camera_input)
        if not radar or radar["kind"] != "radar" or not camera or camera["kind"] != "camera":
            raise SetupError("Choose a radar and a camera from the lists.")
        if not radar["connectable"]:
            raise SetupError("That radar isn't responding.", radar["failure"]["hint"], 409)
        label = (label or "Sensor unit").strip()[:60] or "Sensor unit"
        hfov = _num(hfov_deg, "Field of view", 20, 140)
        offset = [0.0, 0.0, 0.0] if offset_cm is None else [_num(v, "Camera offset", -100, 100) / 100 for v in offset_cm]
        if len(offset) != 3:
            raise SetupError("Camera offset needs three numbers (right, forward, up) in centimetres.")
        previous = draft.get("unit") or {}
        inputs_changed = (previous.get("radar_input"), previous.get("camera_input")) != (radar_input, camera_input)
        unit = {"unit_id": UNIT_ID, "label": label, "radar_input": radar_input, "camera_input": camera_input,
                "lens": {"hfov_deg": hfov, "width": camera["image_size"][0], "height": camera["image_size"][1]},
                "camera_offset_m": offset, "reflector_height_m": previous.get("reflector_height_m", 1.0),
                "placement": previous.get("placement"), "captures": [], "next_capture": 1, "pose_nudge": {}, "camera_nudge": {}}
        if not inputs_changed:
            for key in ("captures", "next_capture", "pose_fit", "camera_fit", "pose_info", "camera_info", "pose_nudge", "camera_nudge", "simulation", "compiled"):
                if key in previous:
                    unit[key] = previous[key]
        draft["unit"] = unit
        self._ensure_defaults(draft)
        if previous and (previous.get("lens", {}).get("hfov_deg") != hfov or previous.get("camera_offset_m") != offset) and unit.get("captures"):
            self._resolve(draft)
        draft["stage"] = "sensors"
        self._save(draft)
        return self.view(site_id)

    def _ensure_defaults(self, draft: dict) -> None:
        unit, meta = draft["unit"], draft["map"]
        lo, hi = meta["bounds"]["min"], meta["bounds"]["max"]
        floor = meta["floor_z"]
        if unit.get("placement") is None:
            unit["placement"] = {"x": round((lo[0] + hi[0]) / 2, 2), "y": round(lo[1] + 0.3, 2), "z": round(floor + 2.4, 2),
                                 "yaw_deg": 0.0, "tilt_deg": 10.0, "roll_deg": 0.0, "source": "default"}
        if INPUTS[unit["radar_input"]]["simulated"] and not unit.get("simulation"):
            unit["simulation"] = scenario_for_bounds(lo, hi, floor, zlib.crc32(draft["site_id"].encode()) & 0xFFFF)

    def _reset_unit_for_new_map(self, draft: dict) -> None:
        unit = draft["unit"]
        for key in ("captures", "pose_fit", "camera_fit", "pose_info", "camera_info", "simulation", "compiled"):
            unit.pop(key, None)
        unit.update(captures=[], next_capture=1, pose_nudge={}, camera_nudge={}, placement=None)
        self._ensure_defaults(draft)

    # ---- step 3: placement ------------------------------------------------------------------------
    def set_placement(self, site_id: str, data: dict) -> dict:
        draft = self._load(site_id)
        unit = self._unit(draft)
        lo, hi, floor = draft["map"]["bounds"]["min"], draft["map"]["bounds"]["max"], draft["map"]["floor_z"]
        place = dict(unit["placement"])
        if "x" in data:
            place["x"] = _num(data["x"], "x", lo[0] - 2, hi[0] + 2)
        if "y" in data:
            place["y"] = _num(data["y"], "y", lo[1] - 2, hi[1] + 2)
        if "height_m" in data:
            place["z"] = floor + _num(data["height_m"], "Mounting height", 0.2, 30)
        if "yaw_deg" in data:
            place["yaw_deg"] = ((_num(data["yaw_deg"], "Direction", -720, 720) + 180) % 360) - 180
        if "tilt_deg" in data:
            place["tilt_deg"] = _num(data["tilt_deg"], "Downward tilt", -45, 80)
        place["source"] = "manual"
        unit["placement"] = place
        if unit.get("pose_fit"):
            pass  # the measured pose supersedes placement; placement stays as the starting guess only
        draft["stage"] = "place"
        self._save(draft)
        return self.view(site_id)

    def _unit(self, draft: dict) -> dict:
        if not draft.get("unit"):
            raise SetupError("Add a sensor unit first.")
        return draft["unit"]

    # ---- step 4: guided evidence ------------------------------------------------------------------
    def capture_supported(self, unit: dict) -> tuple[bool, str]:
        radar, camera = INPUTS[unit["radar_input"]], INPUTS[unit["camera_input"]]
        if not radar.get("capabilities", {}).get("guided_capture"):
            return False, "This radar can't be read by the guided calibration."
        if not camera.get("guided_capture"):
            return False, ("Guided calibration needs a camera that the server can read together with the radar. A browser camera or video file "
                           "can show a preview but cannot yet be calibrated against a radar here. Choose the demo camera to try the procedure.")
        return True, ""

    def set_reflector_height(self, site_id: str, height_m) -> dict:
        draft = self._load(site_id)
        self._unit(draft)["reflector_height_m"] = _num(height_m, "Reflector height", 0.0, 3.0)
        self._save(draft)
        return self.view(site_id)

    def _map_point(self, draft: dict, x, y) -> list[float]:
        lo, hi = draft["map"]["bounds"]["min"], draft["map"]["bounds"]["max"]
        px, py = _num(x, "x", lo[0] - 1, hi[0] + 1), _num(y, "y", lo[1] - 1, hi[1] + 1)
        return [px, py, draft["map"]["floor_z"] + draft["unit"]["reflector_height_m"]]

    def sim_reflector(self, site_id: str, x, y) -> dict:
        """What the simulated radar and camera would show for a reflector standing at map (x, y)."""
        draft = self._load(site_id)
        unit = self._unit(draft)
        sim = _sim_truth(unit)
        ok, why = self.capture_supported(unit)
        if not ok or sim is None:
            raise SetupError(why or "No simulated sensors.", status=409)
        point = self._map_point(draft, x, y)
        radar = INPUTS[unit["radar_input"]]["spec"]
        spec = self._radar_spec(radar)
        seen = simulate_reflector(spec, sim, point, seed=1, frames=1)
        proj = truth_pixel(sim, tuple(point))
        return {"map": point, "radar_visible": seen is not None,
                "radar_range_m": None if seen is None else round(seen["range_m"], 2),
                "px": None if proj is None else [round(proj.u_px, 1), round(proj.v_px, 1)], "in_image": bool(proj and proj.in_image)}

    @staticmethod
    def _radar_spec(spec: dict):
        from types import SimpleNamespace
        return SimpleNamespace(max_range_m=spec["max_range_m"], azimuth_fov_deg=spec["azimuth_fov_deg"], elevation_fov_deg=spec["elevation_fov_deg"],
                               sigma_range_m=spec["sigma"]["range_m"], sigma_azimuth_deg=spec["sigma"]["azimuth_deg"],
                               sigma_elevation_deg=spec["sigma"]["elevation_deg"])

    def add_capture(self, site_id: str, data: dict) -> dict:
        draft = self._load(site_id)
        unit = self._unit(draft)
        ok, why = self.capture_supported(unit)
        if not ok:
            raise SetupError("Capturing isn't available with these sensors.", why, 409)
        if len(unit["captures"]) >= MAX_CAPTURES:
            raise SetupError("That is the maximum number of reference spots.", "Remove spots you no longer need.")
        role = data.get("role", "fit")
        if role not in ("fit", "verify"):
            raise SetupError("Unknown spot type.")
        point = self._map_point(draft, data.get("x"), data.get("y"))
        px = data.get("px")
        if px is not None:
            if not isinstance(px, list) or len(px) != 2:
                raise SetupError("Click the reflector in the video.")
            width, height = unit["lens"]["width"], unit["lens"]["height"]
            px = [_num(px[0], "Video x", 0, width), _num(px[1], "Video y", 0, height)]
        elif role == "verify":
            raise SetupError("Click the reflector in the video to record a check spot.")
        sim = _sim_truth(unit)
        seed = (zlib.crc32(site_id.encode()) + unit["next_capture"] * 7919) & 0xFFFFFFFF
        seen = simulate_reflector(self._radar_spec(INPUTS[unit["radar_input"]]["spec"]), sim, point, seed)
        if seen is None:
            raise SetupError("The radar didn't see the reflector there.",
                             "Move the reflector into the radar's cone (the shaded wedge on the map), between 1 m and the radar's range, "
                             "and hold it still. Metal reflectors work best.")
        capture = {"id": unit["next_capture"], "role": role, "map": [round(v, 3) for v in point], "radar": [round(v, 4) for v in seen["radar"]],
                   "radar_sigma_m": round(seen["sigma_m"], 4), "frames": seen["frames"], "range_m": round(seen["range_m"], 2),
                   "px": None if px is None else [round(v, 1) for v in px], "at": _now()}
        unit["next_capture"] += 1
        unit["captures"].append(capture)
        self._resolve(draft)
        draft["stage"] = "calibrate"
        self._save(draft)
        return self.view(site_id)

    def delete_capture(self, site_id: str, capture_id: int) -> dict:
        draft = self._load(site_id)
        unit = self._unit(draft)
        before = len(unit["captures"])
        unit["captures"] = [c for c in unit["captures"] if c["id"] != capture_id]
        if len(unit["captures"]) == before:
            raise SetupError("That spot no longer exists.", status=404)
        self._resolve(draft)
        self._save(draft)
        return self.view(site_id)

    def set_capture_role(self, site_id: str, capture_id: int, role: str) -> dict:
        draft = self._load(site_id)
        unit = self._unit(draft)
        for c in unit["captures"]:
            if c["id"] == capture_id:
                if role not in ("fit", "verify") or (role == "verify" and c.get("px") is None):
                    raise SetupError("A check spot needs a click in the video.")
                c["role"] = role
                break
        else:
            raise SetupError("That spot no longer exists.", status=404)
        self._resolve(draft)
        self._save(draft)
        return self.view(site_id)

    def _resolve(self, draft: dict) -> None:
        """Re-estimate from all evidence. New evidence supersedes manual fine-tuning."""
        unit = draft["unit"]
        pose, pinfo = cal.fit_pose(unit["captures"])
        camera, cinfo = cal.fit_camera(unit["captures"], lens_model(unit), tuple(unit["camera_offset_m"]))
        unit["pose_fit"] = None if pose is None else {"rotation": [list(r) for r in pose.rotation], "translation": list(pose.translation)}
        unit["camera_fit"] = None if camera is None else {"rotation": [list(r) for r in camera.rotation], "translation": list(camera.translation)}
        unit["pose_info"], unit["camera_info"] = pinfo, cinfo
        unit["pose_nudge"], unit["camera_nudge"] = {}, {}

    # ---- step 4b: fine-tuning ---------------------------------------------------------------------
    def nudge(self, site_id: str, data: dict) -> dict:
        draft = self._load(site_id)
        unit = self._unit(draft)
        if data.get("reset"):
            unit["pose_nudge"], unit["camera_nudge"] = {}, {}
        else:
            limits = {"pose_nudge": {"dx": 2.0, "dy": 2.0, "dz": 1.0, "dyaw_deg": 20.0}, "camera_nudge": {"yaw": 10.0, "pitch": 10.0, "roll": 10.0}}
            for group, keys in limits.items():
                incoming = data.get(group.split("_")[0], {})
                if not isinstance(incoming, dict):
                    raise SetupError("Invalid adjustment.")
                current = unit.setdefault(group, {})
                for key, bound in keys.items():
                    if key in incoming:
                        current[key] = max(-bound, min(bound, current.get(key, 0.0) + _num(incoming[key], key, -bound, bound)))
        self._save(draft)
        return self.view(site_id)

    # ---- quality report ---------------------------------------------------------------------------
    def report(self, draft: dict) -> dict | None:
        unit = draft.get("unit")
        if not unit:
            return None
        model = lens_model(unit)
        caps = unit["captures"]
        pose_t, cam_t = effective_pose(unit), effective_camera(unit)
        pose_nudged, cam_nudged = _nudged(unit)
        supported, why = self.capture_supported(unit)
        fit_caps = [c for c in caps if c["role"] == "fit"]
        pinfo, cinfo = dict(unit.get("pose_info") or {}), dict(unit.get("camera_info") or {})

        def evidence_grade(fit, info, residual_rms, kind):
            if not fit:
                return "unassessed" if not info.get("n") else "insufficient"
            if kind == "pose":
                return cal.grade_pose(residual_rms, info["sigma_rot_deg"], info["n"])
            return cal.grade_camera(residual_rms, info["sigma_rot_deg"], info["n"])

        pose_res = [round(v, 3) for v in cal.pose_residuals(pose_t, caps)] if pose_t else []
        px_all = cal.camera_residuals_px(cam_t, [c for c in caps if c.get("px")], model) if caps else []
        px_by_id = {c["id"]: (None if v is None else round(v, 1)) for c, v in zip([c for c in caps if c.get("px")], px_all)}
        pose_rms = cal._rms(cal.pose_residuals(pose_t, fit_caps)) if (pose_t and unit.get("pose_fit") and fit_caps) else None
        cam_rms = cal._rms(cal.camera_residuals_px(cam_t, [c for c in fit_caps if c.get("px")], model)) if unit.get("camera_fit") else None
        pgrade = evidence_grade(unit.get("pose_fit"), pinfo, pose_rms, "pose")
        cgrade = evidence_grade(unit.get("camera_fit"), cinfo, cam_rms, "camera")
        ver = cal.verification(pose_t if unit.get("pose_fit") else None, cam_t if unit.get("camera_fit") else None, caps, model)
        pose_status = "nominal" if not unit.get("pose_fit") else "verified" if (ver["pose"] and ver["pose"]["passed"]) else "measured"
        cam_status = "nominal" if not unit.get("camera_fit") else "verified" if (ver["camera"] and ver["camera"]["passed"]) else "measured"
        pos_sigma = pinfo.get("sigma_point_m")
        if pos_sigma is not None and pose_nudged and pose_rms is not None:
            pos_sigma = round(max(pos_sigma, pose_rms), 3)
        warnings = []
        if pose_status == "nominal":
            warnings.append("The unit's position and direction are only your estimate. Add reference spots to measure them.")
        if cam_status == "nominal":
            warnings.append("The camera is assumed to point exactly where the radar points. Radar markers on the video will be off by however much the real mounting differs.")
        if pgrade == "poor":
            warnings.append("Position/direction fit is poor. The reference spots disagree with each other; re-check the largest-error spot.")
        if cgrade == "poor":
            warnings.append("Camera alignment fit is poor. Re-check the clicks and the lens field of view (Advanced).")
        if pose_nudged or cam_nudged:
            warnings.append("You adjusted the result by hand. Quality below is recomputed from your reference spots, so it shows whether the adjustment helped.")
        warnings.append("The lens is described only by its field of view (not measured), so markers near the edges of the picture are less accurate.")
        if pose_status != "nominal" and not ver["pose"]:
            warnings.append("Not yet verified: add 3 check spots (spots not used for the fit) to confirm the result.")
        if supported is False:
            warnings.append(why)
        if cinfo.get("range_span_m") is None and unit.get("camera_fit"):
            pass
        nxt = self._next_step(draft, unit, supported, pinfo, cinfo, ver, pose_status, cam_status)
        return {"capture_supported": supported, "capture_unsupported_reason": why,
                "pose": {"status": pose_status, "quality": pgrade, "source": "fit" if unit.get("pose_fit") else "placement", "nudged": pose_nudged,
                         "n": pinfo.get("n", 0), "needed": cal.POSE_MIN, "recommended": cal.POSE_RECOMMENDED, "rms_m": None if pose_rms is None else round(pose_rms, 3),
                         "sigma_point_m": pos_sigma, "sigma_rot_deg": pinfo.get("sigma_rot_deg"), "sigma_yaw_deg": pinfo.get("sigma_yaw_deg"),
                         "hints": pinfo.get("hints", []), "spread_m": pinfo.get("spread_m"),
                         "effective": dict(zip(("x", "y", "z"), [round(v, 3) for v in pose_t.translation])) | dict(zip(("yaw_deg", "pitch_deg", "roll_deg"), [round(v, 2) for v in yaw_pitch_roll_deg(pose_t.rotation)])) if pose_t else None},
                "camera": {"status": cam_status, "quality": cgrade, "source": "fit" if unit.get("camera_fit") else "assumed", "nudged": cam_nudged,
                           "n": cinfo.get("n", 0), "needed": cal.CAMERA_MIN, "recommended": cal.CAMERA_RECOMMENDED,
                           "rms_px": None if cam_rms is None or cam_rms != cam_rms else round(cam_rms, 1), "sigma_rot_deg": cinfo.get("sigma_rot_deg"),
                           "sigma_offset_m": cinfo.get("sigma_offset_m"), "hints": cinfo.get("hints", []), "range_span_m": cinfo.get("range_span_m")},
                "lens": {"status": "nominal", "quality": "unassessed", "hfov_deg": unit["lens"]["hfov_deg"]},
                "verification": ver,
                "capture_residuals": {c["id"]: self._where(c, pose_t, cam_t, model, pose_res[i] if i < len(pose_res) else None, px_by_id.get(c["id"]))
                                      for i, c in enumerate(caps)},
                "warnings": warnings, "next": nxt}

    @staticmethod
    def _where(capture, pose_t, cam_t, model, map_err, px_err) -> dict:
        """Where the *current* calibration places this spot, so the UI can draw estimate vs. what the operator marked."""
        est = pose_t.apply(tuple(capture["radar"])) if pose_t else None
        proj = model.project(cam_t.inverse().apply(tuple(capture["radar"])))
        return {"map_err_m": map_err, "px_err": px_err, "map_est": None if est is None else [round(v, 3) for v in est],
                "px_est": None if proj is None else [round(proj.u_px, 1), round(proj.v_px, 1)]}

    @staticmethod
    def _next_step(draft, unit, supported, pinfo, cinfo, ver, pose_status, cam_status) -> str:
        if not supported:
            return "You can save this setup now. Calibration needs a radar and camera pair that this server can read together."
        n = len(unit["captures"])
        fit_n = len([c for c in unit["captures"] if c["role"] == "fit"])
        if fit_n < cal.CAMERA_RECOMMENDED:
            return f"Add {cal.CAMERA_RECOMMENDED - fit_n} more reference spot(s): stand the reflector somewhere new, mark it on the map and click it in the video."
        if pinfo.get("grade") == "poor" or cinfo.get("grade") == "poor":
            return "The result is poor. Check the spot with the largest error and re-do it, or add spots in new places."
        if ver["n"] < cal.VERIFY_MIN:
            return f"Add {cal.VERIFY_MIN - ver['n']} check spot(s) in new places (tick 'Use as a check') to confirm the result is right."
        if pose_status == "verified" and cam_status == "verified":
            return "Both alignments are verified. You can review and save."
        return "Check spots disagree with the fit. Try fine-tuning, or add more spots and re-check."

    # ---- previews ---------------------------------------------------------------------------------
    def sim_camera_frame(self, site_id: str) -> dict:
        draft = self._load(site_id)
        if not draft.get("map"):
            raise SetupError("Upload a map first.", status=404)
        unit = draft.get("unit")
        sim = _sim_truth(unit) if unit else None
        if sim is None:  # before a unit exists, show what the demo camera would see on this map
            from .site import parse_simulation
            meta = draft["map"]
            sim = parse_simulation(scenario_for_bounds(meta["bounds"]["min"], meta["bounds"]["max"], meta["floor_z"],
                                                       zlib.crc32(site_id.encode()) & 0xFFFF), "preview")
        preview = json.loads((self._dir(site_id) / "preview.json").read_text())["points"]
        pts = []
        for i in range(0, len(preview), 3):
            p = truth_pixel(sim, (preview[i], preview[i + 1], preview[i + 2]))
            if p is not None and p.in_image and p.depth_m < 60:
                pts.extend((round(p.u, 4), round(p.v, 4), round(p.depth_m, 1)))
        t = (self.clock() % 60)
        people = []
        for i, spec in enumerate(sim.targets):
            pos = target_position(spec, t)
            if pos:
                proj = truth_pixel(sim, pos)
                if proj and proj.in_image:
                    people.append({"u": round(proj.u, 4), "v": round(proj.v, 4)})
        return {"simulated": True, "image_size": [sim.intrinsics.width, sim.intrinsics.height], "points": pts, "people": people,
                "note": "Simulated picture. A real camera would show live video here."}

    # ---- views ------------------------------------------------------------------------------------
    def view(self, site_id: str) -> dict:
        draft = self._load(site_id)
        out = json.loads(json.dumps(draft))
        unit = out.get("unit")
        if unit:
            unit.pop("simulation", None)  # hidden simulated truth is never sent to the browser
            for key in ("pose_fit", "camera_fit"):
                if unit.get(key):
                    unit[key] = True
            unit["simulated"] = INPUTS[unit["radar_input"]]["simulated"]
            unit["radar_spec"] = {k: v for k, v in INPUTS[unit["radar_input"]]["spec"].items()}
        out["report"] = self.report(draft)
        out["discovery_note"] = DISCOVERY_NOTE
        return out

    # ---- step 5: save (compile) -------------------------------------------------------------------
    def save(self, site_id: str) -> dict:
        draft = self._load(site_id)
        unit = self._unit(draft)
        report = self.report(draft)
        folder = self._dir(site_id)
        pose_t, cam_t = effective_pose(unit), effective_camera(unit)
        sig = lambda t: [round(v, 4) for r in t.rotation for v in r] + [round(v, 4) for v in t.translation]  # noqa: E731
        compiled = unit.setdefault("compiled", {})
        stamp = _now()

        def cal_doc(key, signature, status, method, pos_sigma, rot_sigma, quality, points):
            previous = compiled.get(key)
            if previous and previous["signature"] == signature and previous["status"] == status:
                rev, at = previous["revision"], previous["at"]
            else:
                rev, at = (previous["revision"] + 1 if previous else 1), stamp
            compiled[key] = {"signature": signature, "status": status, "revision": rev, "at": at}
            doc = {"status": status, "revision": rev, "calibrated_at": at, "method": method, "quality": quality, "evidence_points": points}
            if pos_sigma is not None and rot_sigma is not None:
                doc.update(position_sigma_m=pos_sigma, rotation_sigma_deg=rot_sigma)
            return doc

        p, c = report["pose"], report["camera"]
        pose_cal = cal_doc("pose", sig(pose_t), p["status"],
                           "operator placement (estimate)" if p["source"] == "placement" else f"reference-spot rigid fit ({p['n']} spots){', hand-adjusted' if p['nudged'] else ''}",
                           p["sigma_point_m"], max(p["sigma_rot_deg"] or 0, 0) if p["sigma_rot_deg"] is not None else None,
                           p["quality"], p["n"])
        cam_rot = mat_mul(cam_t.rotation, transpose(CAMERA_NOMINAL_FORWARD))
        cam_cal = cal_doc("camera", sig(cam_t), c["status"],
                          "assumed aligned with the radar (not measured)" if c["source"] == "assumed" else f"radar-to-video fit ({c['n']} spots){', hand-adjusted' if c['nudged'] else ''}",
                          c["sigma_offset_m"], c["sigma_rot_deg"], c["quality"], c["n"])
        radar_cal = {"status": "measured", "revision": 1, "calibrated_at": compiled.get("radar_at", stamp), "method": "defined: the unit's reference frame is the radar's own",
                     "position_sigma_m": 0.0, "rotation_sigma_deg": 0.0, "quality": "good"}
        compiled["radar_at"] = radar_cal["calibrated_at"]
        lens_cal = {"status": "nominal", "revision": 1, "calibrated_at": compiled.get("lens_at", stamp), "method": f"field of view {unit['lens']['hfov_deg']}° (not measured)",
                    "quality": "unassessed"}
        compiled["lens_at"] = lens_cal["calibrated_at"]
        model = lens_model(unit)
        radar_in = INPUTS[unit["radar_input"]]
        spec = radar_in["spec"]
        cam_in = INPUTS[unit["camera_input"]]

        def quat(r):
            return [round(v, 9) for v in quaternion_from_rotation(r)]
        site_doc = {"schema_version": 1, "site_id": site_id, "name": draft["name"],
                    "data_origin": "simulated" if radar_in["simulated"] else "live",
                    "map": {"map_id": site_id, "name": draft["name"], "revision": draft["map"]["revision"], "file": draft["map"]["file"],
                            "source_units": draft["map"]["source_units"], "up_axis": draft["map"]["up_axis"], "floor_z": draft["map"]["floor_z"],
                            "display_max_points": PREVIEW_POINTS, "note": f"Uploaded as {draft['map']['original_name']}"},
                    "units": [f"units/{UNIT_ID}.json"], "retention": {"track_history_seconds": 10, "persist_tracks": False, "video": "none"}}
        unit_doc = {"schema_version": 1, "unit_id": UNIT_ID, "label": unit["label"],
                    "enclosure_in_map": {"translation_m": [round(v, 4) for v in pose_t.translation], "rotation": {"quaternion_wxyz": quat(pose_t.rotation)},
                                         "calibration": pose_cal},
                    "radar": {"sensor_id": "radar-01", "adapter": radar_in["adapter"], "source": {}, "in_enclosure": {"translation_m": [0, 0, 0], "calibration": radar_cal},
                              "max_range_m": spec["max_range_m"], "azimuth_fov_deg": spec["azimuth_fov_deg"], "elevation_fov_deg": spec["elevation_fov_deg"],
                              "nominal_sigma": spec["sigma"], "unknown_elevation_sigma_deg": 20, "stale_after_s": 1.5, "missing_after_s": 5},
                    "camera": {"sensor_id": "cam-01", "stream": cam_in["stream"],
                               "in_enclosure": {"translation_m": [round(v, 4) for v in cam_t.translation], "rotation": {"quaternion_wxyz": quat(cam_rot)},
                                                "calibration": cam_cal},
                               "intrinsics": {"model": "pinhole_brown_conrady", "width": model.width, "height": model.height, "fx": round(model.fx, 3),
                                              "fy": round(model.fy, 3), "cx": model.cx, "cy": model.cy, "distortion": [0, 0, 0, 0], "calibration": lens_cal}}}
        if unit.get("simulation"):
            unit_doc["simulation"] = unit["simulation"]
        self._archive(folder)
        (folder / "units").mkdir(mode=0o700, exist_ok=True)
        _write_private(folder / "site.json", site_doc)
        _write_private(folder / "units" / f"{UNIT_ID}.json", unit_doc)
        try:
            runtime = SpatialRuntime(load_site(folder))
            del runtime
        except ConfigError as exc:
            raise SetupError(f"The setup could not be saved: {exc}", "Check the highlighted steps and try again.", 422) from exc
        draft["saved_revision"] = draft.get("saved_revision", 0) + 1
        draft["stage"] = "review"
        self._save(draft)
        view = self.view(site_id)
        view["saved"] = True
        return view

    def _archive(self, folder: Path) -> None:
        if not (folder / "site.json").exists():
            return
        target = folder / "history" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(folder / "site.json", target / "site.json")
        if (folder / "units").is_dir():
            shutil.copytree(folder / "units", target / "units", dirs_exist_ok=True)
        old = sorted((folder / "history").iterdir())
        for path in old[:-20]:
            shutil.rmtree(path, ignore_errors=True)

    def saved_directory(self, site_id: str) -> Path:
        draft = self._load(site_id)
        if not draft.get("saved_revision"):
            raise SetupError("Save the setup first.", "Press 'Save setup' on the last step.", 409)
        return self._dir(site_id)
