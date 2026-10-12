# Spatial data formats (schema_version 1)

All files live in one **site directory** (sample: `samples/spatial/demo/`). Paths inside configs must be relative and stay inside that directory. Configs are versioned: a file whose `schema_version` is not `1` is rejected rather than guessed at. Treat calibration files as records: add a new `revision` with method/date instead of silently overwriting numbers, and keep backups.

## `site.json`

```json
{ "schema_version": 1, "site_id": "demo-room", "name": "...", "data_origin": "simulated|recorded|live",
  "map": { "map_id": "...", "name": "...", "revision": 1, "file": "map.ply", "source_units": "m|cm|mm|ft", "up_axis": "z|y", "floor_z": 0.0,
           "display_max_points": 12000, "note": "" },
  "units": ["units/unit-01.json"],
  "retention": { "track_history_seconds": 10, "persist_tracks": false, "video": "none" } }
```

- Map: ASCII or binary-little-endian PLY (x,y,z properties) or whitespace/CSV `.xyz`. Max 3,000,000 points; decimate larger scans offline. LAS/LAZ/E57 are not supported. The map's own axes define the `map` frame (z up expected).
- Retention: `track_history_seconds` is an in-memory trail (0 disables). `persist_tracks: true` and any `video` other than `"none"` are **rejected**: nothing is written to disk and no video is recorded.

## `units/<id>.json`

Each transform block has `translation_m` `[x,y,z]`, a `rotation` and a `calibration` record. `rotation` is exactly one of `{"rpy_deg":{"yaw","pitch","roll"}}` (yaw about +z, CCW from above; pitch about +x, positive raises the forward axis; roll about +y; `R = Rz·Rx·Ry`), `{"quaternion_wxyz":[..]}`, or `{"matrix":[[..],[..],[..]]}` (must be a proper rotation).

```json
{ "schema_version": 1, "unit_id": "unit-01", "label": "...",
  "enclosure_in_map": { "translation_m": [..], "rotation": {..}, "calibration": {..} },
  "radar":  { "sensor_id": "radar-01", "adapter": "jsonl_replay", "source": {"path": "radar_sample.jsonl", "loop": true},
              "in_enclosure": { ..transform.. }, "max_range_m": 15,
              "azimuth_fov_deg": [-60,60], "elevation_fov_deg": [-30,30],
              "nominal_sigma": {"range_m": .06, "azimuth_deg": 1.0, "elevation_deg": 3.0},
              "unknown_elevation_sigma_deg": 20, "stale_after_s": 1.5, "missing_after_s": 5 },
  "camera": { "sensor_id": "cam-01", "stream": "none|browser_primary_feed",
              "in_enclosure": { ..transform.. },   // rotation = adjustment to a nominal forward, level camera
              "intrinsics": { "model": "pinhole_brown_conrady", "width": 1280, "height": 720, "fx": 900, "fy": 900,
                              "cx": 640, "cy": 360, "distortion": [k1,k2,p1,p2,(k3)], "calibration": {..} } } }
```

`calibration`: `status` (`uncalibrated|simulated|nominal|measured|verified`), `revision`, `calibrated_at`, `method`, `position_sigma_m`, `rotation_sigma_deg` (declared 1σ; if absent the UI says *not declared* and overlay error is flagged unquantified), `reprojection_error_px`, `quality` (`good|fair|poor|insufficient|unassessed`), `evidence_points` (reference spots behind it), `notes`. `uncalibrated` suppresses everything that depends on the transform.

## Radar adapters

`jsonl_replay` (documented below) and `simulator` (demo/test only: needs a `simulation.truth` block in the unit file describing a hidden true installation; used by the setup wizard's demo sensors). The setup wizard writes these files for you; its working state (`setup.json`, `preview.json`, `history/`) is internal and not meant to be edited by hand.

Only the two above exist. An adapter implements `RadarAdapter.poll(now) -> [RadarFrame]`, returning timestamps in unix seconds and measurements already converted to the radar frame and units below. **Do not add a vendor adapter until its output format is documented.**

### `omni-radar-jsonl/1` (project-defined, vendor-neutral)

Line 1 (header): `{"format":"omni-radar-jsonl/1","sensor_id":"radar-01","frame":"radar","units":{"range":"m","angle":"deg","velocity":"m/s",...},"rate_hz":10,"duration_s":60}`. Units other than these are rejected.

Following lines, one radar scan each (an empty `targets` list still counts as the sensor being alive):
`{"t": 12.3, "targets":[{"range_m":6.2,"azimuth_deg":-4.1,"elevation_deg":-7.5,"radial_velocity_mps":0.4,"snr_db":18.2,"confidence":0.9,"src_id":3}]}`

- `t`: seconds from file start, non-decreasing. Replay rebases it onto the wall clock, so emitted measurements carry real timestamps; the file loops.
- Required per target: `range_m` (0.05–500), `azimuth_deg` (−180..180, right-positive from boresight). Optional: `elevation_deg` (omit when the sensor has none), `radial_velocity_mps`, `snr_db`, `confidence` (0..1), `src_id` (sensor-local index; never an identity). Invalid targets/lines are counted and dropped.

## API (all require a signed-in session; read-only)

`GET /api/spatial/status`, `/state` (units, health, calibrations, warnings, tracks with per-unit image markers), `/map` (bounded point list + unit poses/FOV), `/camera/<unit_id>` (map points rendered through the camera model; **not video**).
