"""Registry of sensor inputs the setup wizard can offer.

Only inputs with a documented, implemented adapter are listed. There is deliberately no network discovery:
no vendor protocol is implemented, and guessing at devices would be misleading. A new device type becomes a
registry entry only together with a documented adapter.
"""
from __future__ import annotations

RADAR_SIM = {"max_range_m": 15.0, "azimuth_fov_deg": [-60, 60], "elevation_fov_deg": [-30, 30],
             "sigma": {"range_m": 0.06, "azimuth_deg": 1.0, "elevation_deg": 3.0}}

INPUTS = {
    "sim-radar": {"id": "sim-radar", "kind": "radar", "name": "Demo radar (simulated)", "simulated": True, "adapter": "simulator",
                  "connectable": True, "spec": RADAR_SIM,
                  "capabilities": {"reports_elevation": True, "guided_capture": True, "auto_pose": False, "auto_camera_radar": False}},
    "sim-radar-offline": {"id": "sim-radar-offline", "kind": "radar", "name": "Demo radar - unplugged (shows the error help)",
                          "simulated": True, "adapter": "simulator", "connectable": False, "spec": RADAR_SIM,
                          "failure": {"message": "No data received from this radar.",
                                      "hint": "Check that it is powered and its cable is seated, then press Test again. If it still fails, "
                                              "restart the radar and make sure no other program is using it."},
                          "capabilities": {"reports_elevation": True, "guided_capture": False, "auto_pose": False, "auto_camera_radar": False}},
    "sim-camera": {"id": "sim-camera", "kind": "camera", "name": "Demo camera (simulated)", "simulated": True, "connectable": True,
                   "image_size": [1280, 720], "preview": "simulated", "stream": "none", "guided_capture": True},
    "browser-camera": {"id": "browser-camera", "kind": "camera", "name": "A camera on this computer (shown in your browser)",
                       "simulated": False, "connectable": True, "image_size": [1280, 720], "preview": "browser_camera",
                       "stream": "browser_primary_feed", "guided_capture": False},
    "browser-file": {"id": "browser-file", "kind": "camera", "name": "A video file (shown in your browser)", "simulated": False,
                     "connectable": True, "image_size": [1280, 720], "preview": "browser_file",
                     "stream": "browser_primary_feed", "guided_capture": False},
}

DISCOVERY_NOTE = ("Automatic discovery of network radars and cameras is not available: no device protocol is installed yet. "
                  "Pick from the supported inputs below. Real devices need a documented adapter first.")


def public_inputs() -> dict:
    out = {"radar": [], "camera": [], "discovery": {"supported": False, "note": DISCOVERY_NOTE}}
    for item in INPUTS.values():
        out[item["kind"]].append({k: v for k, v in item.items() if k not in ("spec",)})
    return out
