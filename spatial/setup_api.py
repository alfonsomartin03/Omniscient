"""HTTP-agnostic routing for the setup wizard: (method, path, query, payload) -> (status, JSON-able body)."""
from __future__ import annotations

import re

from .setup import SetupError, SetupStore

SITE = r"(?P<site>[A-Za-z0-9][A-Za-z0-9_-]{0,31})"
ROUTES = [
    ("GET", r"/api/setup/sites", "list"),
    ("POST", r"/api/setup/sites", "create"),
    ("GET", rf"/api/setup/sites/{SITE}", "get"),
    ("DELETE", rf"/api/setup/sites/{SITE}", "delete"),
    ("POST", rf"/api/setup/sites/{SITE}/stage", "stage"),
    ("GET", rf"/api/setup/sites/{SITE}/map/preview", "map_preview"),
    ("POST", rf"/api/setup/sites/{SITE}/map/options", "map_options"),
    ("POST", rf"/api/setup/sites/{SITE}/unit", "unit"),
    ("POST", rf"/api/setup/sites/{SITE}/placement", "placement"),
    ("POST", rf"/api/setup/sites/{SITE}/reflector-height", "reflector_height"),
    ("POST", rf"/api/setup/sites/{SITE}/sim/reflector", "sim_reflector"),
    ("GET", rf"/api/setup/sites/{SITE}/preview/camera", "sim_camera"),
    ("POST", rf"/api/setup/sites/{SITE}/captures", "capture_add"),
    ("DELETE", rf"/api/setup/sites/{SITE}/captures/(?P<cid>\d{{1,6}})", "capture_delete"),
    ("POST", rf"/api/setup/sites/{SITE}/captures/(?P<cid>\d{{1,6}})/role", "capture_role"),
    ("POST", rf"/api/setup/sites/{SITE}/nudge", "nudge"),
    ("POST", rf"/api/setup/sites/{SITE}/save", "save"),
    ("GET", r"/api/setup/inputs", "inputs"),
    ("POST", rf"/api/setup/inputs/(?P<input>[a-z0-9-]{{1,40}})/test", "input_test"),
]
COMPILED = [(m, re.compile(f"^{p}$"), name) for m, p, name in ROUTES]


def match(method: str, path: str):
    for m, pattern, name in COMPILED:
        if m == method:
            hit = pattern.match(path)
            if hit:
                return name, hit.groupdict()
    return None, None


def handle(store: SetupStore, method: str, path: str, payload: dict, sample_map=None) -> tuple[int, dict]:
    name, args = match(method, path)
    if name is None:
        return 404, {"error": "Not found"}
    try:
        site = args.get("site")
        if name == "list":
            return 200, {"sites": store.list_sites()}
        if name == "create":
            return 201, store.create_site(payload.get("name", ""), sample_map if payload.get("use_sample") else None)
        if name == "get":
            return 200, store.view(site)
        if name == "delete":
            store.delete_site(site)
            return 200, {"ok": True}
        if name == "stage":
            return 200, store.set_stage(site, payload.get("stage", ""))
        if name == "map_preview":
            return 200, store.map_preview(site)
        if name == "map_options":
            return 200, store.set_map_options(site, payload.get("units"), payload.get("up_axis"), payload.get("floor_z"))
        if name == "unit":
            return 200, store.set_unit(site, payload.get("label", ""), payload.get("radar_input", ""), payload.get("camera_input", ""),
                                       payload.get("hfov_deg", 70), payload.get("offset_cm"))
        if name == "placement":
            return 200, store.set_placement(site, payload)
        if name == "reflector_height":
            return 200, store.set_reflector_height(site, payload.get("height_m"))
        if name == "sim_reflector":
            return 200, store.sim_reflector(site, payload.get("x"), payload.get("y"))
        if name == "sim_camera":
            return 200, store.sim_camera_frame(site)
        if name == "capture_add":
            return 201, store.add_capture(site, payload)
        if name == "capture_delete":
            return 200, store.delete_capture(site, int(args["cid"]))
        if name == "capture_role":
            return 200, store.set_capture_role(site, int(args["cid"]), payload.get("role", ""))
        if name == "nudge":
            return 200, store.nudge(site, payload)
        if name == "save":
            return 200, store.save(site)
        if name == "inputs":
            return 200, store.inputs()
        if name == "input_test":
            return 200, store.test_input(args["input"])
    except SetupError as exc:
        return exc.status, {"error": exc.message, "hint": exc.hint}
    return 404, {"error": "Not found"}
