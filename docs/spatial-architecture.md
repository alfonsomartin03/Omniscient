# Spatial tracking: assessment, architecture, calibration and staging

Status: **Milestone 1 and the guided setup/calibration experience (§8) are implemented**, validated on simulated data only. No real hardware is integrated.

## 1. Repository assessment (before this work)

| Area | Finding |
|---|---|
| Shape | Single-process Python 3 server (`server.py`, stdlib `http.server`) + static UI (`index.html`, `app.js`, `styles.css`) + `vision.py` (RT-DETRv2 in a spawned, idle-unloading worker process) + `setup_model.py` (one-time pinned model download). No tests, no build step. |
| Camera path | The **browser** decodes a camera/video file, resizes to ≤640×360 JPEG and POSTs it to `/api/frame` every ~350 ms. The server makes a 40×22 grayscale sample (baseline learning for 30 frames, scene-change gating), runs RT-DETRv2 when needed, associates detections by class + IoU, and returns normalised boxes, danger levels and restricted-space events. Server-side RTSP/ONVIF ingest does not exist. |
| Interfaces | `GET /api/session`, `GET/POST/DELETE /api/zones`, `POST /api/frame` (JPEG), `POST /api/calibrate` (resets the *scene baseline*, unrelated to sensor calibration), `POST /api/logout`, `/login`. |
| State | In memory per session (`ANALYSIS`), plus `data/restricted_spaces.json` (polygons in **normalised image coordinates**, so zones are camera-view-specific, not world-space). Nothing about video or tracks is persisted. |
| Security assumptions | Loopback bind by default; TLS 1.2+ only with a private CA created by OpenSSL; PBKDF2 admin password; `__Host-` Secure/HttpOnly/SameSite=Strict cookies; CSRF header on mutations; Host allowlist; bounded bodies/threads; strict CSP; no telemetry; offline model flags. Trust boundary = the host and the LAN clients holding the CA. |
| Limitations | One feed at a time and only while a browser tab is open; no multi-camera model; tracks exist only in image space; "calibration" means a grayscale baseline; COCO labels only; danger score is a heuristic; no automated tests. |

## 2. Concept and frames

A LiDAR scan defines the **map** frame. Each **sensor unit** is a rigid enclosure holding one radar and one camera. Sharing an enclosure fixes the radar↔camera relationship but does not align their axes; both extrinsics must be measured and stored, together with lens intrinsics and the enclosure's pose in the map.

Frames (right-handed, metres; full conventions in `spatial/geometry.py`):

- `map`: x right/east, y forward/north, z up.
- `enclosure`: x right, y forward, z up.
- `radar`: same axes as enclosure by definition; azimuth from +y toward +x (right-positive), elevation positive up. **A device adapter must convert its native convention into this.**
- `camera`: OpenCV (x right, y down, z optical axis).

`T_a_b` maps points from frame *b* into frame *a*. Stored per unit: `enclosure_in_map` = `T_map_enclosure`, `radar.in_enclosure` = `T_enclosure_radar`, `camera.in_enclosure` = `T_enclosure_camera` (camera rotation is entered as an *adjustment to a nominal forward-looking level camera*, which is far easier to measure than a raw OpenCV-axes rotation), plus camera intrinsics.

## 3. Architecture (staged)

```
 radar adapter ──► RadarFrame(timestamp, sensor_id, measurements[radar frame, units, snr/conf])
                         │ locate(): spherical → radar xyz → enclosure → map  (+ covariance)
                         ▼
                    Tracker (map frame, anonymous IDs, Kalman per axis, gating, coasting)
                         │
        SpatialRuntime ──┼─► health (fresh / stale / missing), counters, calibration warnings
                         └─► overlay.project_track(): map → camera frame → pixels (+ radius)
 site config (versioned JSON + point cloud) ─► /api/spatial/{status,state,map,camera/<unit>} ─► spatial.js
 existing camera path (/api/frame, RT-DETR, zones) ─► unchanged
```

Data model (all in `spatial/models.py`): `MapSpec`, `SensorUnit{RadarSpec, CameraSpec}`, `Calibration{status, revision, calibrated_at, method, position/rotation sigma, reprojection error}`, `RadarMeasurement{timestamp, sensor_id, unit_id, range/azimuth/elevation?, radial velocity?, snr?, confidence?, frame, units}`, `Track` snapshot (`id, state, timestamp, frame, units, position, velocity, sigma_m, dimension, quality, sensor_ids, trail`), `Retention`.

Calibration statuses: `uncalibrated` (outputs depending on it are suppressed), `simulated` (demo only), `nominal` (CAD/guess, flagged), `measured`, `verified` (hold-out validated).

Design decisions worth knowing:

- **Opt-in.** `OMNI_SPATIAL_SITE` enables it; otherwise nothing changes. A broken spatial config disables only spatial tracking and is shown in the UI.
- **Lazy, thread-free.** State advances when the API is polled. Fine for replay; a live adapter will need a reader thread (Stage 2).
- **Honest uncertainty.** Per-axis σ from range/azimuth/elevation noise (or a large assumed vertical σ when no elevation is reported), propagated through the rotation, filtered by the tracker; the overlay radius adds declared calibration error, or flags it as *unquantified*.
- **Shared state.** Unlike the per-session camera analysis, spatial state is global to the server.
- **No identity.** IDs are sequential per run (`T-0001`); no classification, no face recognition, sensor-supplied IDs are never used as identities.

## 4. Calibration procedure and how each transform is applied

All steps are offline/local. Use a radar corner reflector carrying a high-contrast fiducial so one object is visible to both sensors. Record every result as a new `revision` in the unit file with method, date, residual and declared σ.

1. **Lens intrinsics (`camera.intrinsics`).** Checkerboard/ChArUco images at the operating resolution, focus and zoom (≥15 poses covering the frame and corners). Solve with OpenCV `calibrateCamera`; store fx, fy, cx, cy, k1,k2,p1,p2(,k3) and RMS reprojection error. Redo if focus, zoom or resolution/crop changes. Pinhole + Brown–Conrady only; fisheye is rejected.
2. **Radar → enclosure (`radar.in_enclosure`).** Translation from the enclosure drawing/measurement. Rotation (boresight alignment) by placing the reflector at ≥6 non-collinear points in front of the unit, measuring each point in the enclosure frame with tape/laser, reading the radar's range/azimuth/elevation, and solving the rigid fit (Kabsch/Procrustes). Report residual per point. Applied as `p_enc = T_enc_radar · p_radar`.
3. **Camera → enclosure (`camera.in_enclosure`).** With intrinsics known, the same reflector/fiducial positions (enclosure frame) and their pixel coordinates feed `solvePnP`; convert to a rotation relative to the nominal forward camera. Applied as `p_enc = T_enc_cam · p_cam`; the inverse is used for projection.
4. **Enclosure → map (`enclosure_in_map`).** Place the reflector on ≥6 floor/wall marks whose coordinates are read from the point cloud, measure them from the unit (radar through steps 2–3), solve the rigid fit. Applied as `p_map = T_map_enc · p_enc`. Check the unit's height/tilt against a plumb/inclinometer reading.
5. **Map → image.** `p_cam = (T_map_enc · T_enc_cam)⁻¹ · p_map`; `x'=x/z, y'=y/z`; Brown–Conrady distortion; `u = fx·x_d + cx`, `v = fy·y_d + cy`. Points with z ≤ 0.05 m or beyond the model's validity are not drawn.
6. **Verify with hold-out points** not used in the fits; record residuals and set `status: verified` and the declared σ. Re-calibrate after any knock/remount, firmware change, lens change or map re-scan.

Not yet modelled (documented gaps): radar/camera **time offset** and latency; map drift; per-unit temperature effects; radar-specific bias such as range offset.

## 5. Staged plan

| Stage | Scope | Status |
|---|---|---|
| **M1** | One map, one rigid unit, documented replay format + simulator, transforms, tracker, projection with uncertainty, health/calibration/staleness UI, retention config, tests | **Done** |
| M2 | Calibration tooling (solve from recorded control points, write a new revision), calibration editing with history, overlay onto the real primary video feed, multi-unit fusion, reader thread, camera/radar clock sync | Planned |
| M3 | First real radar adapter (blocked on exact model and its output documentation), association of RT-DETR boxes to radar tracks, 3D restricted zones in the map frame | Planned |
| M4 | Optional encrypted history with explicit retention UI, alerts on map events, soak/failure testing | Planned |

## 6. Hardware assumptions that cannot be resolved from the repository

Needed only when moving past M1 (nothing here blocks M1):

- Exact radar model, its documented output (UART/CAN/Ethernet/UDP framing, field definitions, units, angle sign conventions, coordinate origin) and whether it reports elevation, Doppler, SNR/confidence, or its own tracks.
- Exact camera model and how its video is reachable (UVC, RTSP profile, ONVIF), resolution/FOV, and whether the lens is rectilinear.
- Timestamping: do the devices have a shared or disciplined clock, and what is the radar's end-to-end latency?
- The LiDAR map: file format (PLY/XYZ are supported; LAS/E57 are not), units, origin/axes, and whether the scan has a gravity-aligned z.
- Mounting: enclosure drawing (radar/camera offsets) and mounting height/tilt method.

## 7. Milestone 1 report

Implemented: `spatial/` package (stdlib only), sample site `samples/spatial/demo/`, simulator (`python -m spatial.simulate`), authenticated read-only endpoints under `/api/spatial/`, UI panel (top-down map with FOV wedges, uncertainty ellipses, trails; synthetic camera view with markers; per-unit health and calibration table; track table), `tests/test_spatial.py`. See `README.md` for how to run, and `docs/spatial-data-formats.md` for file formats.

Verification: `python3 -m unittest discover -s tests` (transform algebra and frame conventions, hand-checked projection, full-chain round trip, covariance, missing-elevation handling, tracker accuracy against simulator truth, dropout → stale/missing/coasting, config rejection cases, retention, binary PLY). Against the simulator's ground truth the 3-D target is tracked with ≈0.12 m RMS horizontal error and the elevation-less target with ≈0.35 m (these describe the *simulator's noise model*, not real hardware). The authenticated HTTPS endpoints were exercised against a throwaway server instance (401 without a session, bounded payloads, unit-id validation), and the UI was exercised in the in-app browser through a no-auth harness serving the same files.

Not verified: any real radar or camera, real LiDAR maps at scale (very large clouds must be decimated offline; load is pure Python), projection accuracy against a physical installation, behaviour across browser versions, and the primary camera path's interaction with the new panel on a live feed.


## 8. Setup and calibration experience (implemented, simulated sensors only)

Open **Set up a space** in the console sidebar (or `/setup`). Nothing needs the command line. Five short steps, with every action saved to a private draft so setup can be left and resumed (`data/spatial/sites/<id>/setup.json`):

1. **Map.** Drag-and-drop or pick a file (PLY text/binary, XYZ/TXT/CSV, up to 120 MB; LAS/LAZ/E57 and other formats are refused with instructions to export PLY). Upload progress is shown, the scan is parsed, and a top-down preview with its size is displayed. Units (m/cm/mm/ft), up-axis (Z/Y) and floor height can be corrected and the preview updates; implausible sizes produce a plain-language warning.
2. **Sensors.** Choose radar and camera inputs; each is tested and shows connected / not responding with a "what to try" message. The camera shows a live preview where possible (simulated picture, the computer's own camera, or a video file). A "what can be worked out automatically?" box states plainly that a map cannot reveal where the unit is or how the camera lines up. Lens field of view and camera-to-radar offset live under *Advanced*. There is **no device discovery**: no device protocol is installed, so the UI says so instead of guessing.
3. **Place the unit.** Click the map, drag a handle to set direction, set height and downward tilt, with radar and camera cones drawn live. This is explicitly an *estimate*.
4. **Calibrate with reference spots.** The operator stands a radar reflector at a spot, clicks that spot on the map and the reflector in the video, and captures (the radar reading is averaged). About 6 spots near/far/left/right fit the result; about 3 further *check* spots verify it without being used in the fit. Evidence is drawn back onto the map and video (red offsets), spots can be re-typed or removed, and optional fine-tune nudges are available. Adding or removing a spot recomputes everything and clears manual nudges.
5. **Review and go live.** Per-transform status, quality, uncertainty and warnings; *Save setup* compiles the draft into the versioned site configuration (the previous version is archived under `history/`, calibration `revision` increments only when values change); *Go live* shows the live map and camera view for that saved setup. The active setup is remembered across server restarts.

### What is estimated, and from what evidence

| Quantity | How | Evidence needed | Automatic? |
|---|---|---|---|
| Unit pose in the map | Rigid fit (Horn closed form + Levenberg–Marquardt) of radar-measured reflector positions to operator-marked map positions | ≥3 non-collinear spots (≥5 recommended) | **No.** A static scan cannot determine it. Placement alone is labelled *Estimate only*. |
| Camera orientation (and small offset) relative to the radar | LM fit of reflector radar positions to clicked pixels, with the measured/zero offset as a prior | ≥4 spots (≥6 recommended), spread in range and across the picture | **No** for the demo hardware. |
| Radar frame vs enclosure | Defined equal (the unit's reference frame is the radar's) | none | by definition |
| Lens | Field of view only (nominal) | none | not estimated; shown as *Estimate only* |

Automatic estimation would need data this repository cannot assume: live LiDAR or visual features matched to the map (pose), or visual detections correlated with radar tracks of a walking person (camera↔radar). Those are future work and would require a device that provides them; the UI does not offer them.

### Quality and uncertainty

Each fit reports residuals, a covariance-derived 1σ (position of a mapped target at the evidence locations, direction in degrees), and geometric-coverage hints (collinear spots, all spots at one distance, narrow video coverage). Declared sigmas are inflated ×1.3 because, against simulated truth, the raw covariance was slightly over-confident. The grade (good / fair / poor / not enough spots / not measured) depends on residuals, predicted uncertainty and spot count, so a few badly placed spots cannot yield "good". *Verified* requires ≥3 independent check spots within thresholds (map ≤25 cm RMS, video ≤20 px RMS) and is shown separately for pose and camera. Hand adjustments are allowed, but quality is recomputed from the spots, so a bad nudge visibly worsens it. Notes: map-pose error moves a track on the map but cancels in the camera frame (track and camera share the pose), so overlay error depends only on the camera↔radar calibration.

### How it was validated

All against a **simulated hidden-truth installation** (`spatial/simulate.py`): a true unit pose and camera misalignment the operator never sees, a radar that reports what the *true* installation would measure, and a simulated camera picture from the true camera. `tests/test_setup.py` drives the wizard back end like an operator (visible spot selection, noisy map and video clicks) and checks that the hidden truth is recovered (unit position ≈ 0.1 m, camera rotation ≈ 1°), that declared uncertainty is honest across 15 random installations (≥85% of errors within 2σ camera / 3σ pose), that wrong video clicks are graded *poor*, that too few or collinear spots are refused with help, that calibrated live tracking beats the uncalibrated placement, and that drafts resume after a restart. The UI was exercised end to end in the in-app browser (upload errors, unit-warning flow, sensor test failures with recovery text, placement dragging, an 11-spot calibration through real clicks, save, go live). A tracker defect found during this work (wide clutter tracks stealing measurements and creating duplicate tracks) was fixed and has a regression test.

### Limits to be aware of

- Guided calibration only works with the **demo radar + demo camera pair**. A browser camera or video file can be previewed and saved, but cannot be calibrated against a radar. For real hardware the capture step (`SetupStore.add_capture`, which today asks the simulator what the true radar would see) must read averaged live radar frames from a documented adapter; the rest of the pipeline is unchanged.
- One unit per space in the wizard (the configuration format supports several).
- Operator click precision is assumed (8 cm on the map, 3 px in the video); reflector height is assumed correct; the lens is nominal.
- No overlay onto the real primary video feed yet; the live camera view is a synthetic one for simulated units.
- Accuracy numbers above describe the simulator's noise model, not any real sensor.
