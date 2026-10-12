"""Local point-cloud map loading: ASCII/binary-little-endian PLY and plain XYZ text.

LAS/LAZ/E57 are not parsed; export or decimate those to PLY/XYZ with a trusted offline tool first.
"""
from __future__ import annotations

import math
import struct
from array import array
from dataclasses import dataclass
from pathlib import Path

from .geometry import ConfigError

MAX_LOAD_POINTS = 3_000_000
_PLY_TYPES = {"char": "b", "int8": "b", "uchar": "B", "uint8": "B", "short": "h", "int16": "h", "ushort": "H",
              "uint16": "H", "int": "i", "int32": "i", "uint": "I", "uint32": "I", "float": "f", "float32": "f",
              "double": "d", "float64": "d"}
SCALE_TO_METRES = {"m": 1.0, "cm": 0.01, "mm": 0.001, "ft": 0.3048}


@dataclass
class PointCloud:
    xyz: array  # flat float32 x,y,z,...
    count: int

    def bounds(self) -> tuple[list[float], list[float]]:
        if not self.count:
            return [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
        lo, hi = [math.inf] * 3, [-math.inf] * 3
        for i in range(0, len(self.xyz), 3):
            for axis in range(3):
                value = self.xyz[i + axis]
                lo[axis] = min(lo[axis], value)
                hi[axis] = max(hi[axis], value)
        return lo, hi

    def floor_z(self) -> float:
        """Robust floor estimate: 2nd percentile of z over up to ~200k sampled points."""
        step = max(1, self.count // 200_000)
        zs = sorted(self.xyz[i + 2] for i in range(0, len(self.xyz), 3 * step))
        return float(zs[int(len(zs) * 0.02)]) if zs else 0.0

    def downsample(self, max_points: int) -> "PointCloud":
        """Voxel-grid thinning so the browser never receives an unbounded cloud."""
        if self.count <= max_points:
            return self
        lo, hi = self.bounds()
        volume = max(1e-6, (hi[0] - lo[0] + 1e-3) * (hi[1] - lo[1] + 1e-3) * (hi[2] - lo[2] + 1e-3))
        voxel = max(0.005, (volume / max_points) ** (1 / 3))
        best = None
        for _ in range(5):
            seen, kept = set(), array("f")
            for i in range(0, len(self.xyz), 3):
                x, y, z = self.xyz[i], self.xyz[i + 1], self.xyz[i + 2]
                key = (int((x - lo[0]) / voxel), int((y - lo[1]) / voxel), int((z - lo[2]) / voxel))
                if key not in seen:
                    seen.add(key)
                    kept.extend((x, y, z))
            count = len(kept) // 3
            if count <= max_points and (best is None or count > best.count):
                best = PointCloud(kept, count)
            if max_points * 0.7 <= count <= max_points:
                return best
            # Scans are surfaces, so the kept count scales roughly with voxel^-2 (not ^-3).
            voxel *= math.sqrt(count / (max_points * 0.85))
        if best is not None:
            return best
        step = max(1, self.count // max_points + 1)
        thinned = array("f")
        for i in range(0, len(self.xyz), 3 * step):
            thinned.extend(self.xyz[i:i + 3])
        return PointCloud(thinned, len(thinned) // 3)


def load_point_cloud(path: Path, source_units: str = "m", up_axis: str = "z") -> PointCloud:
    if source_units not in SCALE_TO_METRES:
        raise ConfigError(f"Unsupported map source_units '{source_units}'")
    scale = SCALE_TO_METRES[source_units]
    suffix = path.suffix.lower()
    try:
        with path.open("rb") as handle:
            if suffix == ".ply":
                cloud = _read_ply(handle)
            elif suffix in (".xyz", ".txt", ".csv"):
                cloud = _read_xyz(handle)
            else:
                raise ConfigError(f"Unsupported point-cloud format '{suffix}' (use .ply or .xyz)")
    except OSError as exc:
        raise ConfigError(f"Cannot read point cloud {path.name}: {exc.strerror}") from exc
    if scale != 1.0:
        cloud.xyz = array("f", (v * scale for v in cloud.xyz))
    if up_axis == "y":  # y-up scans (e.g. many game/photogrammetry exports) -> z-up: (x, y, z) -> (x, -z, y)
        flipped = array("f")
        for i in range(0, len(cloud.xyz), 3):
            flipped.extend((cloud.xyz[i], -cloud.xyz[i + 2], cloud.xyz[i + 1]))
        cloud.xyz = flipped
    if not cloud.count:
        raise ConfigError("Point cloud contains no points")
    if not all(math.isfinite(v) for v in cloud.xyz):
        raise ConfigError("Point cloud contains non-finite coordinates")
    return cloud


def _append(xyz: array, x: float, y: float, z: float) -> None:
    if len(xyz) // 3 >= MAX_LOAD_POINTS:
        raise ConfigError(f"Point cloud exceeds {MAX_LOAD_POINTS:,} points; decimate it offline first")
    xyz.extend((x, y, z))


def _read_xyz(handle) -> PointCloud:
    xyz = array("f")
    for raw in handle:
        line = raw.decode("utf-8", "replace").strip()
        if not line or line[0] in "#/":
            continue
        parts = line.replace(",", " ").split()
        try:
            _append(xyz, float(parts[0]), float(parts[1]), float(parts[2]))
        except (ValueError, IndexError):
            continue  # header or malformed row
    return PointCloud(xyz, len(xyz) // 3)


def _read_ply(handle) -> PointCloud:
    if handle.readline().strip() != b"ply":
        raise ConfigError("Not a PLY file")
    fmt, vertex_count, props, current = None, 0, [], None
    for _ in range(200):
        line = handle.readline().decode("ascii", "replace").strip()
        if not line:
            raise ConfigError("Truncated PLY header")
        parts = line.split()
        if parts[0] == "format":
            fmt = parts[1]
        elif parts[0] == "element":
            current = parts[1]
            if current == "vertex":
                vertex_count = int(parts[2])
        elif parts[0] == "property" and current == "vertex":
            if parts[1] == "list":
                raise ConfigError("PLY vertex list properties are not supported")
            props.append((parts[2], parts[1]))
        elif parts[0] == "end_header":
            break
    else:
        raise ConfigError("PLY header too long")
    names = [name for name, _ in props]
    if not {"x", "y", "z"} <= set(names):
        raise ConfigError("PLY vertices need x, y and z properties")
    if vertex_count <= 0 or vertex_count > MAX_LOAD_POINTS:
        raise ConfigError(f"PLY vertex count must be 1..{MAX_LOAD_POINTS:,}; decimate larger scans offline")
    xi, yi, zi = names.index("x"), names.index("y"), names.index("z")
    xyz = array("f")
    if fmt == "ascii":
        for _ in range(vertex_count):
            parts = handle.readline().split()
            _append(xyz, float(parts[xi]), float(parts[yi]), float(parts[zi]))
    elif fmt == "binary_little_endian":
        try:
            codes = "".join(_PLY_TYPES[t] for _, t in props)
        except KeyError as exc:
            raise ConfigError(f"Unsupported PLY property type {exc}") from exc
        record = struct.Struct("<" + codes)
        data = handle.read(record.size * vertex_count)
        if len(data) < record.size * vertex_count:
            raise ConfigError("PLY body is shorter than its header declares")
        for values in record.iter_unpack(data[:record.size * vertex_count]):
            _append(xyz, values[xi], values[yi], values[zi])
    else:
        raise ConfigError(f"Unsupported PLY format '{fmt}'")
    return PointCloud(xyz, len(xyz) // 3)
