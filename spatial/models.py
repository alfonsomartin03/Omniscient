"""Core data model. Every measurement and track carries time, sensor, frame, units and quality."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .camera import CameraModel
from .geometry import ConfigError, Transform

SCHEMA_VERSION = 1
CAL_STATUSES = ("uncalibrated", "simulated", "nominal", "measured", "verified")
# Statuses whose numbers describe the real installation. "simulated" is synthetic demo truth.
MEASURED_STATUSES = ("nominal", "measured", "verified")
MEASUREMENT_UNITS = {"range": "m", "angle": "deg", "velocity": "m/s", "snr": "dB", "position": "m", "time": "s (unix epoch)"}


@dataclass(frozen=True)
class Calibration:
    """Provenance and declared accuracy of one transform or lens model."""

    name: str
    status: str = "uncalibrated"
    revision: int = 0
    calibrated_at: str | None = None
    method: str | None = None
    position_sigma_m: float | None = None      # declared 1-sigma translation error
    rotation_sigma_deg: float | None = None    # declared 1-sigma rotation error
    reprojection_error_px: float | None = None
    notes: str = ""
    quality: str | None = None                 # good | fair | poor | insufficient | unassessed
    evidence_points: int | None = None         # reference spots behind this calibration

    @property
    def quantified(self) -> bool:
        return self.position_sigma_m is not None and self.rotation_sigma_deg is not None

    @property
    def usable(self) -> bool:
        return self.status != "uncalibrated"

    def public(self) -> dict:
        return {"name": self.name, "status": self.status, "revision": self.revision, "calibrated_at": self.calibrated_at,
                "method": self.method, "position_sigma_m": self.position_sigma_m,
                "rotation_sigma_deg": self.rotation_sigma_deg, "reprojection_error_px": self.reprojection_error_px,
                "quantified": self.quantified, "notes": self.notes, "quality": self.quality,
                "evidence_points": self.evidence_points}


@dataclass(frozen=True)
class RadarMeasurement:
    """One radar target detection in the radar frame (spherical, units in ``MEASUREMENT_UNITS``)."""

    timestamp: float            # unix epoch seconds, UTC (replay rebases to current time)
    sensor_id: str
    unit_id: str
    range_m: float
    azimuth_deg: float
    elevation_deg: float | None  # None => the sensor gave no usable elevation
    radial_velocity_mps: float | None = None
    snr_db: float | None = None
    confidence: float | None = None  # 0..1 as reported by the sensor; None when not provided
    source_track_id: int | None = None  # the sensor's own index; never used as an identity
    frame: str = "radar"
    units: dict = field(default_factory=lambda: dict(MEASUREMENT_UNITS))

    def valid(self) -> bool:
        return (math.isfinite(self.timestamp) and math.isfinite(self.range_m) and 0.05 <= self.range_m <= 500
                and math.isfinite(self.azimuth_deg) and -180 <= self.azimuth_deg <= 180
                and (self.elevation_deg is None or (math.isfinite(self.elevation_deg) and -90 <= self.elevation_deg <= 90))
                and (self.confidence is None or 0 <= self.confidence <= 1))


@dataclass(frozen=True)
class RadarSpec:
    sensor_id: str
    adapter: str
    source: dict
    in_enclosure: Transform
    calibration: Calibration
    max_range_m: float
    azimuth_fov_deg: tuple[float, float]
    elevation_fov_deg: tuple[float, float]
    sigma_range_m: float
    sigma_azimuth_deg: float
    sigma_elevation_deg: float
    unknown_elevation_sigma_deg: float
    stale_after_s: float
    missing_after_s: float


@dataclass(frozen=True)
class CameraSpec:
    sensor_id: str
    in_enclosure: Transform
    calibration: Calibration
    intrinsics: CameraModel
    intrinsics_calibration: Calibration
    stream: str  # "none" | "browser_primary_feed" -- how video reaches the UI; no direct protocol yet


@dataclass(frozen=True)
class SimTruth:
    """Hidden ground truth for a SIMULATED unit. Never present for real hardware."""

    enclosure_in_map: Transform
    camera_in_enclosure: Transform
    intrinsics: CameraModel
    seed: int
    targets: tuple


@dataclass(frozen=True)
class SensorUnit:
    unit_id: str
    label: str
    enclosure_in_map: Transform
    pose_calibration: Calibration
    radar: RadarSpec
    camera: CameraSpec
    simulation: SimTruth | None = None

    def calibrations(self) -> list[Calibration]:
        return [self.pose_calibration, self.radar.calibration, self.camera.calibration, self.camera.intrinsics_calibration]

    @property
    def radar_in_map(self) -> Transform:
        return self.enclosure_in_map.compose(self.radar.in_enclosure)

    @property
    def camera_in_map(self) -> Transform:
        return self.enclosure_in_map.compose(self.camera.in_enclosure)

    @property
    def placed(self) -> bool:
        """Radar targets can be located in the map only if both required transforms exist."""
        return self.pose_calibration.usable and self.radar.calibration.usable

    @property
    def image_ready(self) -> bool:
        return self.placed and self.camera.calibration.usable and self.camera.intrinsics_calibration.usable

    def simulated(self) -> bool:
        return any(c.status == "simulated" for c in self.calibrations())


@dataclass(frozen=True)
class Retention:
    track_history_seconds: float = 10.0   # in-memory trail only; nothing is written to disk
    persist_tracks: bool = False
    video: str = "none"                   # this system records no video; only "none" is accepted


@dataclass(frozen=True)
class MapSpec:
    map_id: str
    name: str
    revision: int
    cloud_path: str
    source_units: str
    display_max_points: int
    note: str = ""
    up_axis: str = "z"
    floor_z: float | None = None


@dataclass
class Site:
    site_id: str
    name: str
    schema_version: int
    map: MapSpec
    cloud: object  # PointCloud (kept loosely typed to avoid an import cycle)
    units: dict[str, SensorUnit]
    retention: Retention
    root: object  # Path
    data_origin: str  # "simulated" | "recorded" | "live"


class ConfigVersionError(ConfigError):
    pass
