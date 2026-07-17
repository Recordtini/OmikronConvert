#!/usr/bin/env python3
"""Convert Omikron: The Nomad Soul OD3X scenes to standards-compliant GLB.

The converter intentionally has no third-party runtime dependencies.  It reads
the original paired .3DO/.3DT files, embeds lossless PNG textures, preserves
vertex colors, cameras, mesh flags, and raw light records, and writes GLB 2.0.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import time
from typing import Any, Iterable, Sequence
import zlib


GL_ARRAY_BUFFER = 34962
GL_ELEMENT_ARRAY_BUFFER = 34963
GL_FLOAT = 5126
GL_UNSIGNED_BYTE = 5121
GL_UNSIGNED_SHORT = 5123
GL_UNSIGNED_INT = 5125


class FormatError(ValueError):
    """Raised when an input is not a supported or internally valid OD3X file."""


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _i32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<i", data, offset)[0]


def _f32(data: bytes, offset: int) -> float:
    return struct.unpack_from("<f", data, offset)[0]


def _vec3(data: bytes, offset: int) -> tuple[float, float, float]:
    return struct.unpack_from("<3f", data, offset)


def _name20(data: bytes, offset: int) -> str:
    raw = data[offset : offset + 20].split(b"\0", 1)[0]
    # OD3X was authored with the DOS/OEM Western European code page.  cp1252
    # turns byte 0x83 in names such as ``ToitBât`` into the spurious ``ƒ`` and
    # can break the game's prefix/name-based attachment rules.
    return raw.decode("cp850", errors="replace").strip()


def _finite(value: float, label: str) -> float:
    if not math.isfinite(value):
        raise FormatError(f"{label} contains a non-finite float")
    return value


def _transform_vec3(
    value: Sequence[float], scale: float = 1.0
) -> tuple[float, float, float]:
    """Game (X,Y,Z) -> glTF right-handed Y-up (X,-Y,-Z)."""
    x, y, z = value
    # Blender's glTF importer then maps this to (X,Z,-Y), exactly matching the
    # well-tested legacy Max/Blender coordinate transform without leaving a
    # formally Z-up scene inside a Y-up glTF container.
    return (float(x) * scale, -float(y) * scale, -float(z) * scale)


def _normalize(value: Sequence[float]) -> tuple[float, float, float] | None:
    length = math.sqrt(sum(float(v) * float(v) for v in value))
    if length < 1.0e-12 or not math.isfinite(length):
        return None
    return tuple(float(v) / length for v in value)  # type: ignore[return-value]


def _sub(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _look_at_matrix(
    position: Sequence[float], target: Sequence[float]
) -> list[float]:
    """Return a glTF column-major matrix whose local -Z looks at target."""
    forward = _normalize(_sub(target, position)) or (0.0, 0.0, -1.0)
    world_up = (0.0, 1.0, 0.0)
    if abs(sum(a * b for a, b in zip(forward, world_up))) > 0.999:
        world_up = (0.0, 0.0, 1.0)
    right = _normalize(_cross(forward, world_up)) or (1.0, 0.0, 0.0)
    up = _normalize(_cross(right, forward)) or world_up
    return [
        right[0], right[1], right[2], 0.0,
        up[0], up[1], up[2], 0.0,
        -forward[0], -forward[1], -forward[2], 0.0,
        position[0], position[1], position[2], 1.0,
    ]


@dataclasses.dataclass(frozen=True)
class Header:
    magic: str
    version_major: int
    version_minor: int
    materials_offset: int
    vertices_offset: int
    triangles_offset: int
    rectangles_offset: int
    meshes_offset: int
    doors_offset: int
    cameras_offset: int
    lights_offset: int
    num_triangles: int
    num_rectangles: int
    num_vertices: int
    num_materials: int
    num_cameras: int
    num_meshes: int
    num_doors: int
    total_lights: int
    num_mesh_lights: int
    num_lights: int


@dataclasses.dataclass(frozen=True)
class SourceMaterial:
    name: str
    bitmap_name: str
    source_image_name: str
    data_size: int
    bits_per_pixel: int
    width: int
    height: int


@dataclasses.dataclass(frozen=True)
class Vertex:
    position: tuple[float, float, float]
    normal: tuple[float, float, float]
    alpha: float
    bgra: tuple[int, int, int, int]

    @property
    def rgba(self) -> tuple[int, int, int, int]:
        # The stored BGRA byte alpha is zero throughout the v4 game assets.
        # The separate float is preserved in _OD3_ALPHA and is promoted to
        # COLOR_0 alpha only for meshes explicitly flagged transparent.
        return (
            self.bgra[2],
            self.bgra[1],
            self.bgra[0],
            255,
        )


@dataclasses.dataclass(frozen=True)
class Polygon:
    indices: tuple[int, ...]
    uv_bytes: tuple[int, ...]
    material_index: int
    normal: tuple[float, float, float]


@dataclasses.dataclass(frozen=True)
class SourceMesh:
    flags: tuple[int, int, int, int]
    mover_flags: int
    mesh_id: int
    editor_mesh_id: int
    name: str
    position: tuple[float, float, float]
    parent_id: int
    child_id: int
    next_child_id: int
    unknown_long: int
    vertex_start: int
    triangle_start: int
    rectangle_start: int
    num_vertices: int
    num_triangles: int
    num_rectangles: int
    float_set_1: tuple[float, float, float, float]
    bounds_raw: tuple[float, float, float, float, float, float]
    float_set_2: tuple[float, float, float]
    relative_position: tuple[float, float, float]

    @property
    def hidden_in_game(self) -> bool:
        return bool((self.flags[0] & 0x01) or (self.flags[2] & 0x80))


@dataclasses.dataclass(frozen=True)
class Door:
    name: str
    value_1: int
    value_2: int


@dataclasses.dataclass(frozen=True)
class SourceCamera:
    name: str
    position: tuple[float, float, float]
    target: tuple[float, float, float]
    unknown_float: float
    field_of_view_degrees: float


@dataclasses.dataclass(frozen=True)
class SourceLight:
    flags: tuple[int, int]
    name: str
    values: tuple[float, float, float, float, float]
    bgra: tuple[int, int, int, int]
    points: tuple[tuple[float, float, float], ...]

    @property
    def rgb(self) -> tuple[float, float, float]:
        return (
            self.bgra[2] / 255.0,
            self.bgra[1] / 255.0,
            self.bgra[0] / 255.0,
        )


@dataclasses.dataclass
class Scene3DO:
    path: Path
    source_bytes: bytes
    header: Header
    materials: list[SourceMaterial]
    vertices: list[Vertex]
    triangles: list[Polygon]
    rectangles: list[Polygon]
    meshes: list[SourceMesh]
    doors: list[Door]
    cameras: list[SourceCamera]
    lights: list[SourceLight]
    warnings: list[str]


@dataclasses.dataclass(frozen=True)
class IndexedTexture:
    name: str
    width: int
    height: int
    palette_rgb: tuple[tuple[int, int, int], ...]
    pixels_bottom_up: bytes
    bits_per_pixel: int


def _validate_section(
    data: bytes, offset: int, count: int, stride: int, label: str
) -> None:
    if count < 0 or offset < 0:
        raise FormatError(f"invalid {label} offset/count")
    end = offset + count * stride
    if end > len(data):
        raise FormatError(
            f"{label} section ends at 0x{end:X}, beyond file size 0x{len(data):X}"
        )


def parse_3do(path: os.PathLike[str] | str) -> Scene3DO:
    source_path = Path(path)
    data = source_path.read_bytes()
    if len(data) < 0x174:
        raise FormatError(f"{source_path.name}: file is too small to contain an OD3X header")
    if data[:4] != b"OD3X":
        raise FormatError(f"{source_path.name}: expected OD3X magic, got {data[:4]!r}")

    header = Header(
        magic="OD3X",
        version_major=_u32(data, 0x04),
        version_minor=_u32(data, 0x08),
        materials_offset=_u32(data, 0x0C),
        vertices_offset=_u32(data, 0x10),
        triangles_offset=_u32(data, 0x14),
        rectangles_offset=_u32(data, 0x18),
        meshes_offset=_u32(data, 0x1C),
        doors_offset=_u32(data, 0x20),
        cameras_offset=_u32(data, 0x24),
        lights_offset=_u32(data, 0x28),
        num_triangles=_u32(data, 0xE8),
        num_rectangles=_u32(data, 0xEC),
        num_vertices=_u32(data, 0xF0),
        num_materials=_u32(data, 0xFC),
        num_cameras=_u32(data, 0x108),
        num_meshes=_u32(data, 0x10C),
        num_doors=_u32(data, 0x110),
        total_lights=_u32(data, 0x114),
        num_mesh_lights=_u32(data, 0x118),
        num_lights=_u32(data, 0x11C),
    )
    if (header.version_major, header.version_minor) not in {(3, 44), (4, 44)}:
        raise FormatError(
            f"{source_path.name}: unsupported OD3X version "
            f"{header.version_major}.{header.version_minor}"
        )

    for offset, count, stride, label in (
        (header.materials_offset, header.num_materials, 80, "materials"),
        (header.vertices_offset, header.num_vertices, 32, "vertices"),
        (header.triangles_offset, header.num_triangles, 28, "triangles"),
        (header.rectangles_offset, header.num_rectangles, 32, "rectangles"),
        (header.meshes_offset, header.num_meshes, 140, "meshes"),
        (header.doors_offset, header.num_doors, 28, "doors"),
        (header.cameras_offset, header.num_cameras, 52, "cameras"),
        (header.lights_offset, header.num_lights, 304, "lights"),
    ):
        _validate_section(data, offset, count, stride, label)

    materials: list[SourceMaterial] = []
    for index in range(header.num_materials):
        offset = header.materials_offset + index * 80
        material = SourceMaterial(
            name=_name20(data, offset),
            bitmap_name=_name20(data, offset + 20),
            source_image_name=_name20(data, offset + 40),
            data_size=_u32(data, offset + 60),
            bits_per_pixel=_u32(data, offset + 72),
            width=_u16(data, offset + 76),
            height=_u16(data, offset + 78),
        )
        if material.bits_per_pixel not in (4, 8):
            raise FormatError(
                f"material {index} ({material.name}) uses unsupported "
                f"{material.bits_per_pixel}-bit indexed pixels"
            )
        if material.width <= 0 or material.height <= 0:
            raise FormatError(f"material {index} has invalid dimensions")
        materials.append(material)

    vertices: list[Vertex] = []
    for index in range(header.num_vertices):
        offset = header.vertices_offset + index * 32
        position = tuple(_finite(v, f"vertex {index} position") for v in _vec3(data, offset))
        normal = tuple(_finite(v, f"vertex {index} normal") for v in _vec3(data, offset + 12))
        alpha = _finite(_f32(data, offset + 24), f"vertex {index} alpha")
        vertices.append(
            Vertex(
                position=position,  # type: ignore[arg-type]
                normal=normal,  # type: ignore[arg-type]
                alpha=alpha,
                bgra=tuple(data[offset + 28 : offset + 32]),  # type: ignore[arg-type]
            )
        )

    triangles: list[Polygon] = []
    for index in range(header.num_triangles):
        offset = header.triangles_offset + index * 28
        triangles.append(
            Polygon(
                indices=struct.unpack_from("<3H", data, offset),
                uv_bytes=tuple(data[offset + 6 : offset + 12]),
                material_index=_u32(data, offset + 12),
                normal=_vec3(data, offset + 16),
            )
        )

    rectangles: list[Polygon] = []
    for index in range(header.num_rectangles):
        offset = header.rectangles_offset + index * 32
        rectangles.append(
            Polygon(
                indices=struct.unpack_from("<4H", data, offset),
                uv_bytes=tuple(data[offset + 8 : offset + 16]),
                material_index=_u32(data, offset + 16),
                normal=_vec3(data, offset + 20),
            )
        )

    warnings: list[str] = []
    meshes: list[SourceMesh] = []
    vertex_start = triangle_start = rectangle_start = 0
    for index in range(header.num_meshes):
        offset = header.meshes_offset + index * 140
        mesh = SourceMesh(
            flags=tuple(data[offset : offset + 4]),  # type: ignore[arg-type]
            mover_flags=_u32(data, offset + 4),
            mesh_id=_i32(data, offset + 8),
            editor_mesh_id=_i32(data, offset + 12),
            name=_name20(data, offset + 16) or f"mesh_{index:04d}",
            position=_vec3(data, offset + 36),
            parent_id=_i32(data, offset + 48),
            child_id=_i32(data, offset + 52),
            next_child_id=_i32(data, offset + 56),
            unknown_long=_u32(data, offset + 60),
            vertex_start=vertex_start,
            triangle_start=triangle_start,
            rectangle_start=rectangle_start,
            num_vertices=_u32(data, offset + 64),
            num_triangles=_u32(data, offset + 68),
            num_rectangles=_u32(data, offset + 72),
            float_set_1=struct.unpack_from("<4f", data, offset + 76),
            bounds_raw=struct.unpack_from("<6f", data, offset + 92),
            float_set_2=struct.unpack_from("<3f", data, offset + 116),
            relative_position=_vec3(data, offset + 128),
        )
        meshes.append(mesh)
        vertex_start += mesh.num_vertices
        triangle_start += mesh.num_triangles
        rectangle_start += mesh.num_rectangles

    totals = (vertex_start, triangle_start, rectangle_start)
    expected = (header.num_vertices, header.num_triangles, header.num_rectangles)
    if totals != expected:
        raise FormatError(f"mesh section totals {totals} do not match header totals {expected}")

    doors = [
        Door(
            name=_name20(data, header.doors_offset + index * 28),
            value_1=_u32(data, header.doors_offset + index * 28 + 20),
            value_2=_u32(data, header.doors_offset + index * 28 + 24),
        )
        for index in range(header.num_doors)
    ]

    cameras: list[SourceCamera] = []
    for index in range(header.num_cameras):
        offset = header.cameras_offset + index * 52
        cameras.append(
            SourceCamera(
                name=_name20(data, offset) or f"camera_{index:03d}",
                position=_vec3(data, offset + 20),
                target=_vec3(data, offset + 32),
                unknown_float=_f32(data, offset + 44),
                field_of_view_degrees=_f32(data, offset + 48),
            )
        )

    lights: list[SourceLight] = []
    for index in range(header.num_lights):
        offset = header.lights_offset + index * 304
        points = tuple(_vec3(data, offset + 48 + point * 32) for point in range(6))
        lights.append(
            SourceLight(
                flags=struct.unpack_from("<2H", data, offset),
                name=_name20(data, offset + 4) or f"light_{index:03d}",
                values=struct.unpack_from("<5f", data, offset + 24),
                bgra=tuple(data[offset + 44 : offset + 48]),  # type: ignore[arg-type]
                points=points,
            )
        )

    if header.total_lights != header.num_mesh_lights + header.num_lights:
        warnings.append(
            "header total_lights does not equal mesh-light plus explicit-light counts"
        )

    return Scene3DO(
        path=source_path,
        source_bytes=data,
        header=header,
        materials=materials,
        vertices=vertices,
        triangles=triangles,
        rectangles=rectangles,
        meshes=meshes,
        doors=doors,
        cameras=cameras,
        lights=lights,
        warnings=warnings,
    )


def _copy_back_reference(output: bytearray, start: int, length: int) -> None:
    if start < 0 or start >= len(output):
        raise FormatError(
            f"invalid .3DT back-reference start {start} at output byte {len(output)}"
        )
    for index in range(length):
        source_index = start + index
        if source_index < 0 or source_index >= len(output):
            raise FormatError("invalid overlapping .3DT back-reference")
        output.append(output[source_index])


def decompress_3dt_pixels(payload: bytes, expected_size: int) -> bytes:
    """Decode the game's flag/LZ pixel stream.

    This mirrors the original importer's 1-based MaxScript indexing exactly;
    the seemingly unusual minus-one offsets are therefore intentional.
    """
    if expected_size < 0:
        raise FormatError("negative expected texture size")
    if len(payload) == expected_size:
        return payload
    if not payload:
        raise FormatError("empty compressed texture payload")

    output = bytearray((payload[0],))
    cursor = 1
    while cursor < len(payload):
        flags = payload[cursor]
        cursor += 1
        bit = 0x80
        for _ in range(8):
            if cursor >= len(payload):
                break
            if flags & bit:
                packed = payload[cursor]
                cursor += 1
                length = ((packed & 0xFC) // 4) + 3
                mode = packed & 0x03
                if mode == 0:
                    if not output:
                        raise FormatError("repeat token before first output byte")
                    output.extend((output[-1],) * (length - 1))
                elif mode == 1:
                    if cursor >= len(payload):
                        raise FormatError("truncated 8-bit texture back-reference")
                    distance = payload[cursor]
                    cursor += 1
                    _copy_back_reference(output, len(output) - distance - 1, length)
                elif mode == 2:
                    if cursor + 1 >= len(payload):
                        raise FormatError("truncated 16-bit texture back-reference")
                    distance = (payload[cursor] << 8) | payload[cursor + 1]
                    cursor += 2
                    _copy_back_reference(output, len(output) - distance - 1, length)
                else:
                    if cursor >= len(payload):
                        raise FormatError("truncated page texture back-reference")
                    page = payload[cursor]
                    cursor += 1
                    _copy_back_reference(output, len(output) - 256 * page, length)
            else:
                output.append(payload[cursor])
                cursor += 1
            if len(output) > expected_size:
                raise FormatError(
                    f"decompressed texture exceeded expected {expected_size} bytes"
                )
            bit >>= 1

    if len(output) != expected_size:
        raise FormatError(
            f"decompressed texture has {len(output)} bytes, expected {expected_size}"
        )
    return bytes(output)


def decode_3dt(scene: Scene3DO, path: os.PathLike[str] | str) -> list[IndexedTexture]:
    texture_path = Path(path)
    data = texture_path.read_bytes()
    cursor = 0
    textures: list[IndexedTexture] = []
    for index, material in enumerate(scene.materials):
        color_count = 16 if material.bits_per_pixel == 4 else 256
        palette_size = color_count * 3
        end_palette = cursor + palette_size
        end_payload = end_palette + material.data_size
        if end_payload > len(data):
            raise FormatError(
                f"{texture_path.name}: material {index} payload extends beyond file"
            )
        palette_bytes = data[cursor:end_palette]
        palette = tuple(
            tuple(palette_bytes[pos : pos + 3])  # type: ignore[misc]
            for pos in range(0, palette_size, 3)
        )
        payload = data[end_palette:end_payload]
        pixels = decompress_3dt_pixels(payload, material.width * material.height)
        if pixels and max(pixels) >= color_count:
            raise FormatError(
                f"material {index} contains an index outside its {color_count}-color palette"
            )
        textures.append(
            IndexedTexture(
                name=material.bitmap_name or f"texture_{index:03d}.bmp",
                width=material.width,
                height=material.height,
                palette_rgb=palette,  # type: ignore[arg-type]
                pixels_bottom_up=pixels,
                bits_per_pixel=material.bits_per_pixel,
            )
        )
        cursor = end_payload
    if cursor != len(data):
        raise FormatError(
            f"{texture_path.name}: {len(data) - cursor} unexplained trailing bytes"
        )
    return textures


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def texture_to_png(texture: IndexedTexture, transparent_black: bool = False) -> bytes:
    rows = bytearray()
    width = texture.width
    for row in range(texture.height - 1, -1, -1):
        rows.append(0)  # PNG filter: None
        start = row * width
        for palette_index in texture.pixels_bottom_up[start : start + width]:
            red, green, blue = texture.palette_rgb[palette_index]
            # The alpha-test key is the palette color pure black, which is not
            # guaranteed to occupy palette slot zero.
            alpha = 0 if transparent_black and (red, green, blue) == (0, 0, 0) else 255
            rows.extend((red, green, blue, alpha))
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, texture.height, 8, 6, 0, 0, 0)
    return (
        signature
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _png_chunk(b"IEND", b"")
    )


class GltfBuilder:
    def __init__(self) -> None:
        self.document: dict[str, Any] = {
            "asset": {
                "version": "2.0",
                "generator": "omikron_glb",
            },
            "scene": 0,
            "scenes": [{"nodes": []}],
            "nodes": [],
            "meshes": [],
            "materials": [],
            "accessors": [],
            "bufferViews": [],
        }
        self.binary = bytearray()
        self.extensions_used: set[str] = set()

    def add_buffer_view(self, payload: bytes, target: int | None = None) -> int:
        while len(self.binary) % 4:
            self.binary.append(0)
        offset = len(self.binary)
        self.binary.extend(payload)
        view: dict[str, Any] = {
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(payload),
        }
        if target is not None:
            view["target"] = target
        self.document["bufferViews"].append(view)
        return len(self.document["bufferViews"]) - 1

    def add_accessor(
        self,
        payload: bytes,
        *,
        component_type: int,
        value_type: str,
        count: int,
        target: int,
        normalized: bool = False,
        minimum: Sequence[float | int] | None = None,
        maximum: Sequence[float | int] | None = None,
    ) -> int:
        view = self.add_buffer_view(payload, target)
        accessor: dict[str, Any] = {
            "bufferView": view,
            "byteOffset": 0,
            "componentType": component_type,
            "count": count,
            "type": value_type,
        }
        if normalized:
            accessor["normalized"] = True
        if minimum is not None:
            accessor["min"] = list(minimum)
        if maximum is not None:
            accessor["max"] = list(maximum)
        self.document["accessors"].append(accessor)
        return len(self.document["accessors"]) - 1

    def root_node(self, node: dict[str, Any]) -> int:
        self.document["nodes"].append(node)
        index = len(self.document["nodes"]) - 1
        self.document["scenes"][0]["nodes"].append(index)
        return index

    def finish_document(self) -> dict[str, Any]:
        self.document["buffers"] = [{"byteLength": len(self.binary)}]
        if self.extensions_used:
            self.document["extensionsUsed"] = sorted(self.extensions_used)
        for key in (
            "materials",
            "accessors",
            "bufferViews",
            "nodes",
            "meshes",
        ):
            if not self.document.get(key):
                self.document.pop(key, None)
        return self.document


def _pack_floats(values: Iterable[float]) -> bytes:
    sequence = list(values)
    return struct.pack(f"<{len(sequence)}f", *sequence)


def _pack_indices(values: Sequence[int]) -> tuple[bytes, int]:
    if not values:
        return b"", GL_UNSIGNED_SHORT
    if max(values) <= 0xFFFF:
        return struct.pack(f"<{len(values)}H", *values), GL_UNSIGNED_SHORT
    return struct.pack(f"<{len(values)}I", *values), GL_UNSIGNED_INT


def _minmax_vec3(values: Sequence[Sequence[float]]) -> tuple[list[float], list[float]]:
    return (
        [min(value[axis] for value in values) for axis in range(3)],
        [max(value[axis] for value in values) for axis in range(3)],
    )


def _alpha_mode(flags: Sequence[int]) -> tuple[str, bool]:
    # Eric Morin's format notes identify byte 2 bit 3 as opacity mask and
    # byte 2 bit 4 as transparency.  Water is byte 4 bit 5.
    has_mask = bool(flags[1] & 0x08)
    has_blend = bool((flags[1] & 0x10) or (flags[3] & 0x20))
    if has_blend:
        return "BLEND", has_mask
    if has_mask:
        return "MASK", True
    return "OPAQUE", False


def _safe_name(name: str, fallback: str) -> str:
    return name.replace("\0", "").strip() or fallback


@dataclasses.dataclass
class ConversionOptions:
    # Omikron characters are about 70 game units tall; 1/40 produces a
    # human-scale ~1.75 m result and matches the independent Blender importer.
    scale: float = 0.025
    lighting: str = "baked"
    include_hidden: bool = False
    include_cameras: bool = True
    include_lights: bool = True
    texture_filter: str = "linear"
    light_intensity_scale: float = 100.0
    # The original PC presentation and its authored cameras are 4:3.  OD3X's
    # camera value is horizontal FOV, while glTF stores vertical FOV.
    camera_aspect_ratio: float = 4.0 / 3.0


def build_glb(
    scene: Scene3DO,
    textures: Sequence[IndexedTexture],
    texture_source: Path,
    options: ConversionOptions,
) -> tuple[bytes, dict[str, Any]]:
    if len(textures) != len(scene.materials):
        raise ValueError("material/texture count mismatch")
    if options.scale <= 0 or not math.isfinite(options.scale):
        raise ValueError("scale must be a finite positive number")
    if options.lighting not in {"baked", "dynamic"}:
        raise ValueError("lighting must be 'baked' or 'dynamic'")
    if (
        options.light_intensity_scale < 0
        or not math.isfinite(options.light_intensity_scale)
    ):
        raise ValueError("light_intensity_scale must be finite and non-negative")
    if options.camera_aspect_ratio <= 0 or not math.isfinite(
        options.camera_aspect_ratio
    ):
        raise ValueError("camera_aspect_ratio must be a finite positive number")

    builder = GltfBuilder()
    doc = builder.document
    doc["asset"]["copyright"] = "Source game assets remain property of their owners"
    doc["scenes"][0]["name"] = scene.path.stem
    doc["extras"] = {
        "omikron": {
            "source3do": scene.path.name,
            "source3dt": texture_source.name,
            "source3doSha256": hashlib.sha256(scene.source_bytes).hexdigest(),
            "source3dtSha256": hashlib.sha256(texture_source.read_bytes()).hexdigest(),
            "od3xVersion": f"{scene.header.version_major}.{scene.header.version_minor}",
            "gameToGltfCoordinates": "(X,Y,Z) -> (X,-Y,-Z), glTF Y-up",
            "gameUnitsPerMeter": 1.0 / options.scale,
            "sourceCameraFov": "horizontal degrees",
            "cameraAspectRatio": options.camera_aspect_ratio,
            "lightingMode": options.lighting,
            "lightMapping": "best-effort spotlights; raw source records are retained in extras",
            "meshLightRecordsNotDecoded": scene.header.num_mesh_lights,
            "doors": [dataclasses.asdict(door) for door in scene.doors],
            "warnings": list(scene.warnings),
        }
    }

    # One shared sampler.  Omikron's fixed-function renderer used repeating UVs.
    if options.texture_filter == "nearest":
        sampler = {"magFilter": 9728, "minFilter": 9984, "wrapS": 10497, "wrapT": 10497}
    else:
        sampler = {"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}
    doc["samplers"] = [sampler]
    doc["images"] = []
    doc["textures"] = []
    image_cache: dict[tuple[int, bool], int] = {}
    gltf_material_cache: dict[tuple[int, tuple[int, int, int, int]], int] = {}

    def texture_index(source_index: int, keyed: bool) -> int:
        cache_key = (source_index, keyed)
        if cache_key in image_cache:
            return image_cache[cache_key]
        source = textures[source_index]
        png = texture_to_png(source, transparent_black=keyed)
        view = builder.add_buffer_view(png)
        image_name = Path(source.name).stem + ("_mask" if keyed else "")
        doc["images"].append(
            {"name": image_name, "mimeType": "image/png", "bufferView": view}
        )
        image_index = len(doc["images"]) - 1
        doc["textures"].append(
            {"name": image_name, "sampler": 0, "source": image_index}
        )
        result = len(doc["textures"]) - 1
        image_cache[cache_key] = result
        return result

    def material_index(
        source_index: int, mesh_flags: tuple[int, int, int, int]
    ) -> int:
        alpha_mode, keyed = _alpha_mode(mesh_flags)
        cache_key = (source_index, mesh_flags)
        if cache_key in gltf_material_cache:
            return gltf_material_cache[cache_key]
        source = scene.materials[source_index]
        packed_flags = sum(value << (8 * index) for index, value in enumerate(mesh_flags))
        # Byte 2 bit 0x08 is the color-key/mask flag.  The original importer
        # established that 0x10|0x20 denotes transparency, but a deeper audit
        # of Runtime.exe did not support assigning those individual bits the
        # previously guessed "alpha/additive" meanings.  Keep the combined,
        # observed behavior and avoid inventing a blend equation in metadata.
        decoded_effects: list[str] = []
        if mesh_flags[1] & 0x08:
            decoded_effects.append("alpha_test")
        if mesh_flags[1] & 0x30:
            decoded_effects.append("alpha_blend")
        if mesh_flags[1] & 0x40:
            decoded_effects.append("special_blend_flag_0x40")
        decoded_effects.extend(
            name
            for bit, name in (
                (20, "mirror"),
                (24, "skybox"),
                (26, "environment_mapped"),
                (27, "underwater"),
                (28, "special_fx_unknown"),
                (29, "water_surface"),
                (30, "scripted_fx"),
            )
            if packed_flags & (1 << bit)
        )
        gltf_material: dict[str, Any] = {
            "name": (
                f"{_safe_name(source.name, f'material_{source_index:03d}')}"
                f"__flags_{packed_flags:08X}"
            ),
            "pbrMetallicRoughness": {
                "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                "baseColorTexture": {"index": texture_index(source_index, keyed)},
                "metallicFactor": 0.0,
                "roughnessFactor": 1.0,
            },
            "alphaMode": alpha_mode,
            "doubleSided": alpha_mode != "OPAQUE",
            "extras": {
                "omikronMaterialIndex": source_index,
                "bitmapName": source.bitmap_name,
                "sourceImageName": source.source_image_name,
                "bitsPerPixel": source.bits_per_pixel,
                "colorKeyRGB": [0, 0, 0] if keyed else None,
                "meshFlagsBytes": list(mesh_flags),
                "meshFlagsU32": packed_flags,
                "decodedEffects": decoded_effects,
                "vertexAlphaAppliedForBlend": alpha_mode == "BLEND",
                "vertexAlphaSource": (
                    "OD3X vertex float at byte offset 24"
                    if alpha_mode == "BLEND"
                    else None
                ),
                "unsupportedPortableEffects": [
                    effect
                    for effect in decoded_effects
                    if effect
                    in {
                        "special_blend_flag_0x40",
                        "mirror",
                        "environment_mapped",
                        "underwater",
                        "water_surface",
                        "special_fx_unknown",
                        "scripted_fx",
                    }
                ],
            },
        }
        if alpha_mode == "MASK":
            gltf_material["alphaCutoff"] = 0.5
        if options.lighting == "baked":
            gltf_material["extensions"] = {"KHR_materials_unlit": {}}
            builder.extensions_used.add("KHR_materials_unlit")
        doc["materials"].append(gltf_material)
        result = len(doc["materials"]) - 1
        gltf_material_cache[cache_key] = result
        return result

    omitted_hidden = 0
    omitted_empty = 0
    emitted_meshes = 0
    emitted_primitives = 0
    emitted_triangles = 0
    duplicate_faces_split = 0
    degenerate_source_triangles = 0
    names_seen: collections.Counter[str] = collections.Counter()

    for mesh_number, source_mesh in enumerate(scene.meshes):
        if source_mesh.hidden_in_game and not options.include_hidden:
            omitted_hidden += 1
            continue
        if source_mesh.num_triangles + source_mesh.num_rectangles == 0:
            omitted_empty += 1
            continue

        alpha_mode, keyed = _alpha_mode(source_mesh.flags)
        triangles_by_material: dict[
            int,
            list[tuple[tuple[int, tuple[int, int]], tuple[int, tuple[int, int]], tuple[int, tuple[int, int]], tuple[float, float, float], bool]],
        ] = collections.defaultdict(list)
        source_face_keys: set[tuple[int, int, int]] = set()

        def add_triangle(
            polygon: Polygon,
            corners: tuple[int, int, int],
        ) -> None:
            material = polygon.material_index
            if material >= len(scene.materials):
                raise FormatError(
                    f"mesh {source_mesh.name} refers to material {material}, "
                    f"but only {len(scene.materials)} exist"
                )
            output_corners: list[tuple[int, tuple[int, int]]] = []
            for corner in corners:
                raw_index = polygon.indices[corner]
                local_index = raw_index & 0x7FFF
                if raw_index & 0x8000:
                    raise FormatError(
                        f"mesh {source_mesh.name} uses parent-mesh vertex references; "
                        "this conversion path currently targets static DECORS scenes"
                    )
                if local_index >= source_mesh.num_vertices:
                    raise FormatError(
                        f"mesh {source_mesh.name} vertex index {local_index} is out of range"
                    )
                output_corners.append(
                    (
                        local_index,
                        (polygon.uv_bytes[corner * 2], polygon.uv_bytes[corner * 2 + 1]),
                    )
                )
            nonlocal duplicate_faces_split, degenerate_source_triangles
            face_key = tuple(sorted(corner[0] for corner in output_corners))
            is_duplicate = face_key in source_face_keys
            if is_duplicate:
                duplicate_faces_split += 1
            else:
                source_face_keys.add(face_key)
            if len(set(face_key)) < 3:
                degenerate_source_triangles += 1
            triangles_by_material[material].append(
                (
                    output_corners[0],
                    output_corners[1],
                    output_corners[2],
                    polygon.normal,
                    is_duplicate,
                )
            )

        for polygon in scene.triangles[
            source_mesh.triangle_start : source_mesh.triangle_start + source_mesh.num_triangles
        ]:
            add_triangle(polygon, (0, 1, 2))
        for polygon in scene.rectangles[
            source_mesh.rectangle_start : source_mesh.rectangle_start + source_mesh.num_rectangles
        ]:
            add_triangle(polygon, (0, 1, 2))
            add_triangle(polygon, (0, 2, 3))

        primitives: list[dict[str, Any]] = []
        for source_material_index, source_triangles in sorted(triangles_by_material.items()):
            source_material = scene.materials[source_material_index]
            vertex_map: dict[tuple[Any, ...], int] = {}
            positions: list[tuple[float, float, float]] = []
            normals: list[tuple[float, float, float]] = []
            texcoords: list[tuple[float, float]] = []
            source_alphas: list[float] = []
            colors = bytearray()
            dynamic_colors = bytearray()
            indices: list[int] = []

            def vertex_index(
                corner: tuple[int, tuple[int, int]],
                face_normal: tuple[float, float, float],
                force_unique: bool,
            ) -> int:
                local_index, uv_byte = corner
                source_vertex = scene.vertices[source_mesh.vertex_start + local_index]
                normal = _normalize(_transform_vec3(source_vertex.normal)) or face_normal
                # PNG rows are stored top-down, whereas the original BMP/Max UV origin
                # is bottom-left.  glTF's texture V is therefore flipped here.
                uv = (
                    uv_byte[0] / float(source_material.width),
                    1.0 - uv_byte[1] / float(source_material.height),
                )
                key = (local_index, uv_byte[0], uv_byte[1], *normal)
                output_index = None if force_unique else vertex_map.get(key)
                if output_index is None:
                    output_index = len(positions)
                    if not force_unique:
                        vertex_map[key] = output_index
                    positions.append(_transform_vec3(source_vertex.position, options.scale))
                    normals.append(normal)
                    texcoords.append(uv)
                    source_alphas.append(source_vertex.alpha)
                    output_alpha = (
                        max(0, min(255, round(source_vertex.alpha * 255.0)))
                        if alpha_mode == "BLEND"
                        else 255
                    )
                    colors.extend((*source_vertex.rgba[:3], output_alpha))
                    dynamic_colors.extend((255, 255, 255, output_alpha))
                return output_index

            for (
                corner_a,
                corner_b,
                corner_c,
                source_face_normal,
                force_unique,
            ) in source_triangles:
                face_normal = _normalize(_transform_vec3(source_face_normal)) or (0.0, 1.0, 0.0)
                indices.extend(
                    vertex_index(corner, face_normal, force_unique)
                    for corner in (corner_a, corner_b, corner_c)
                )

            if not positions:
                continue
            minimum, maximum = _minmax_vec3(positions)
            position_accessor = builder.add_accessor(
                _pack_floats(component for value in positions for component in value),
                component_type=GL_FLOAT,
                value_type="VEC3",
                count=len(positions),
                target=GL_ARRAY_BUFFER,
                minimum=minimum,
                maximum=maximum,
            )
            normal_accessor = builder.add_accessor(
                _pack_floats(component for value in normals for component in value),
                component_type=GL_FLOAT,
                value_type="VEC3",
                count=len(normals),
                target=GL_ARRAY_BUFFER,
            )
            texcoord_accessor = builder.add_accessor(
                _pack_floats(component for value in texcoords for component in value),
                component_type=GL_FLOAT,
                value_type="VEC2",
                count=len(texcoords),
                target=GL_ARRAY_BUFFER,
            )
            color_accessor = builder.add_accessor(
                bytes(colors),
                component_type=GL_UNSIGNED_BYTE,
                value_type="VEC4",
                count=len(positions),
                target=GL_ARRAY_BUFFER,
                normalized=True,
            )
            dynamic_color_accessor = None
            if options.lighting == "dynamic":
                dynamic_color_accessor = builder.add_accessor(
                    bytes(dynamic_colors),
                    component_type=GL_UNSIGNED_BYTE,
                    value_type="VEC4",
                    count=len(positions),
                    target=GL_ARRAY_BUFFER,
                    normalized=True,
                )
            source_alpha_accessor = builder.add_accessor(
                _pack_floats(source_alphas),
                component_type=GL_FLOAT,
                value_type="SCALAR",
                count=len(source_alphas),
                target=GL_ARRAY_BUFFER,
            )
            index_payload, index_component = _pack_indices(indices)
            index_accessor = builder.add_accessor(
                index_payload,
                component_type=index_component,
                value_type="SCALAR",
                count=len(indices),
                target=GL_ELEMENT_ARRAY_BUFFER,
                minimum=[min(indices)],
                maximum=[max(indices)],
            )
            attributes = {
                "POSITION": position_accessor,
                "NORMAL": normal_accessor,
                "TEXCOORD_0": texcoord_accessor,
                "_OD3_ALPHA": source_alpha_accessor,
            }
            if options.lighting == "baked":
                attributes["COLOR_0"] = color_accessor
            else:
                # Preserve the original baked-light RGB without applying it a
                # second time before glTF's punctual/PBR lighting.
                attributes["COLOR_0"] = dynamic_color_accessor
                attributes["_OD3_BAKED_COLOR"] = color_accessor
            primitives.append(
                {
                    "attributes": attributes,
                    "indices": index_accessor,
                    "material": material_index(source_material_index, source_mesh.flags),
                    "mode": 4,
                    "extras": {
                        "omikronSourceMaterialIndex": source_material_index,
                        "bakedLightAttribute": (
                            "COLOR_0"
                            if options.lighting == "baked"
                            else "_OD3_BAKED_COLOR"
                        ),
                    },
                }
            )
            emitted_primitives += 1
            emitted_triangles += len(indices) // 3

        if not primitives:
            omitted_empty += 1
            continue
        original_name = source_mesh.name
        names_seen[original_name] += 1
        unique_name = original_name
        if names_seen[original_name] > 1:
            unique_name = f"{original_name}__{mesh_number:04d}"
        gltf_mesh = {
            "name": unique_name,
            "primitives": primitives,
            "extras": {
                "omikron": {
                    "originalName": original_name,
                    "sourceMeshIndex": mesh_number,
                    "meshId": source_mesh.mesh_id,
                    "editorMeshId": source_mesh.editor_mesh_id,
                    "flags": list(source_mesh.flags),
                    "moverFlags": source_mesh.mover_flags,
                    "parentId": source_mesh.parent_id,
                    "childId": source_mesh.child_id,
                    "nextChildId": source_mesh.next_child_id,
                    "relativePosition": list(source_mesh.relative_position),
                    "hiddenInGame": source_mesh.hidden_in_game,
                }
            },
        }
        doc["meshes"].append(gltf_mesh)
        gltf_mesh_index = len(doc["meshes"]) - 1
        node = {
            "name": unique_name,
            "mesh": gltf_mesh_index,
            "translation": list(_transform_vec3(source_mesh.position, options.scale)),
        }
        builder.root_node(node)
        emitted_meshes += 1

    if options.include_cameras and scene.cameras:
        doc["cameras"] = []
        for camera_number, source_camera in enumerate(scene.cameras):
            horizontal_fov = source_camera.field_of_view_degrees
            if not (1.0 <= horizontal_fov < 179.0):
                horizontal_fov = 80.0
            vertical_fov = 2.0 * math.atan(
                math.tan(math.radians(horizontal_fov) * 0.5)
                / options.camera_aspect_ratio
            )
            doc["cameras"].append(
                {
                    "name": source_camera.name,
                    "type": "perspective",
                    "perspective": {
                        "aspectRatio": options.camera_aspect_ratio,
                        "yfov": vertical_fov,
                        "znear": max(0.001, 1.0 * options.scale),
                    },
                    "extras": {
                        "omikronUnknownFloat": source_camera.unknown_float,
                        "omikronSourceFovDegrees": source_camera.field_of_view_degrees,
                        "omikronSourceFovAxis": "horizontal",
                    },
                }
            )
            position = _transform_vec3(source_camera.position, options.scale)
            target = _transform_vec3(source_camera.target, options.scale)
            builder.root_node(
                {
                    "name": f"{source_camera.name}__camera_{camera_number:03d}",
                    "camera": len(doc["cameras"]) - 1,
                    "matrix": _look_at_matrix(position, target),
                }
            )

    if options.include_lights and scene.lights:
        builder.extensions_used.add("KHR_lights_punctual")
        doc.setdefault("extensions", {})["KHR_lights_punctual"] = {"lights": []}
        gltf_lights = doc["extensions"]["KHR_lights_punctual"]["lights"]
        for light_number, source_light in enumerate(scene.lights):
            far_range, near_range, source_intensity, value_4, value_5 = source_light.values
            source_points = source_light.points
            position = _transform_vec3(source_points[0], options.scale)
            target = _transform_vec3(source_points[1], options.scale)
            mapped_intensity = max(
                0.0, source_intensity * options.light_intensity_scale
            )
            source_record = {
                "recordSizeBytes": 304,
                "flags": list(source_light.flags),
                "values": list(source_light.values),
                "bgra": list(source_light.bgra),
                "pointsGameCoordinates": [list(point) for point in source_points],
                "nearAttenuationGameUnits": near_range,
                "farAttenuationGameUnits": far_range,
                "sourceIntensity": source_intensity,
                "sourceToCandelaMultiplier": options.light_intensity_scale,
                "mappedIntensityCandela": mapped_intensity,
                "secondaryAttenuationValues": [value_4, value_5],
                "legacyImporterMapping": {
                    "type": "targeted spot",
                    "positionPoint": 1,
                    "targetPoint": 2,
                    "hotspotFullAngleDegrees": 40.0,
                    "falloffFullAngleDegrees": 120.0,
                    "nearAttenuationValue": 2,
                    "farAttenuationValue": 1,
                },
                "mappingCaveat": (
                    "flag semantics remain unresolved; points 3 through 6 usually "
                    "form a rectangular target plane around point 2, but portable "
                    "circular-spot semantics remain unproven; KHR_lights_punctual "
                    "also has no near-attenuation field"
                ),
            }
            light: dict[str, Any] = {
                "name": source_light.name,
                "type": "spot",
                "color": list(source_light.rgb),
                "intensity": mapped_intensity,
                "spot": {
                    "innerConeAngle": math.radians(20.0),
                    "outerConeAngle": math.radians(60.0),
                },
                "extras": {"omikron": source_record},
            }
            if math.isfinite(far_range) and far_range > 0:
                light["range"] = far_range * options.scale
            gltf_lights.append(light)
            builder.root_node(
                {
                    "name": f"{source_light.name}__light_{light_number:03d}",
                    "matrix": _look_at_matrix(position, target),
                    "extensions": {
                        "KHR_lights_punctual": {"light": len(gltf_lights) - 1}
                    },
                    "extras": {
                        "omikron": {
                            "sourceKind": "decoded explicit light",
                            "sourceIndex": light_number,
                            "sourceName": source_light.name,
                        }
                    },
                }
            )

    stats = {
        "source": scene.path.name,
        "version": f"{scene.header.version_major}.{scene.header.version_minor}",
        "sourceMeshes": scene.header.num_meshes,
        "emittedMeshes": emitted_meshes,
        "omittedHiddenMeshes": omitted_hidden,
        "omittedEmptyMeshes": omitted_empty,
        "emittedPrimitives": emitted_primitives,
        "emittedTriangles": emitted_triangles,
        "duplicateFacesPreservedByVertexSplit": duplicate_faces_split,
        "degenerateSourceTriangles": degenerate_source_triangles,
        "sourceTriangles": scene.header.num_triangles,
        "sourceRectangles": scene.header.num_rectangles,
        "sourceMaterials": len(scene.materials),
        "outputMaterials": len(doc.get("materials", [])),
        "embeddedImages": len(doc.get("images", [])),
        "cameras": len(scene.cameras) if options.include_cameras else 0,
        "explicitLights": len(scene.lights) if options.include_lights else 0,
        "undecodedMeshLights": scene.header.num_mesh_lights,
        "doors": len(scene.doors),
        "lighting": options.lighting,
        "scale": options.scale,
        "cameraAspectRatio": options.camera_aspect_ratio,
        "warnings": list(scene.warnings),
    }
    doc["extras"]["omikron"]["conversionStats"] = stats
    document = builder.finish_document()
    json_bytes = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    json_bytes += b" " * ((-len(json_bytes)) % 4)
    binary = bytes(builder.binary)
    binary += b"\0" * ((-len(binary)) % 4)
    total_length = 12 + 8 + len(json_bytes) + 8 + len(binary)
    glb = (
        struct.pack("<4sII", b"glTF", 2, total_length)
        + struct.pack("<I4s", len(json_bytes), b"JSON")
        + json_bytes
        + struct.pack("<I4s", len(binary), b"BIN\0")
        + binary
    )
    stats["outputBytes"] = len(glb)
    return glb, stats


def validate_glb_bytes(data: bytes) -> dict[str, Any]:
    if len(data) < 20:
        raise FormatError("GLB is too small")
    magic, version, total_length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2 or total_length != len(data):
        raise FormatError("invalid GLB header")
    json_length, json_type = struct.unpack_from("<I4s", data, 12)
    if json_type != b"JSON":
        raise FormatError("GLB first chunk is not JSON")
    json_start = 20
    json_end = json_start + json_length
    document = json.loads(data[json_start:json_end].decode("utf-8"))
    bin_length, bin_type = struct.unpack_from("<I4s", data, json_end)
    if bin_type != b"BIN\0" or json_end + 8 + bin_length != len(data):
        raise FormatError("invalid GLB BIN chunk")
    declared = document.get("buffers", [{}])[0].get("byteLength", -1)
    if declared < 0 or declared > bin_length or bin_length - declared > 3:
        raise FormatError("GLB buffer byteLength does not match BIN chunk")
    for index, view in enumerate(document.get("bufferViews", [])):
        start = view.get("byteOffset", 0)
        length = view.get("byteLength", 0)
        if start < 0 or length < 0 or start + length > declared:
            raise FormatError(f"bufferView {index} is outside the declared buffer")
    return document


def convert_file(
    source: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    options: ConversionOptions,
) -> dict[str, Any]:
    source_path = Path(source)
    texture_path = source_path.with_suffix(".3DT")
    if not texture_path.exists():
        # Windows is case-insensitive, but this also supports case-sensitive test hosts.
        alternate = source_path.with_suffix(".3dt")
        if alternate.exists():
            texture_path = alternate
        else:
            raise FileNotFoundError(f"paired texture file not found: {texture_path}")
    scene = parse_3do(source_path)
    textures = decode_3dt(scene, texture_path)
    glb, stats = build_glb(scene, textures, texture_path, options)
    validate_glb_bytes(glb)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    temporary.write_bytes(glb)
    temporary.replace(output_path)
    stats["output"] = str(output_path.resolve())
    return stats


def inspect_scene(path: os.PathLike[str] | str) -> dict[str, Any]:
    scene = parse_3do(path)
    flag_counts = collections.Counter(mesh.flags for mesh in scene.meshes)
    return {
        "file": str(scene.path.resolve()),
        "sha256": hashlib.sha256(scene.source_bytes).hexdigest(),
        "magic": scene.header.magic,
        "version": f"{scene.header.version_major}.{scene.header.version_minor}",
        "vertices": scene.header.num_vertices,
        "triangles": scene.header.num_triangles,
        "rectangles": scene.header.num_rectangles,
        "renderedTriangles": scene.header.num_triangles + 2 * scene.header.num_rectangles,
        "materials": scene.header.num_materials,
        "meshes": scene.header.num_meshes,
        "hiddenMeshes": sum(mesh.hidden_in_game for mesh in scene.meshes),
        "doors": scene.header.num_doors,
        "cameras": scene.header.num_cameras,
        "totalLights": scene.header.total_lights,
        "meshLights": scene.header.num_mesh_lights,
        "explicitLights": scene.header.num_lights,
        "meshFlagCounts": [
            {"flags": list(flags), "count": count}
            for flags, count in flag_counts.most_common()
        ],
        "materialsInfo": [dataclasses.asdict(material) for material in scene.materials],
        "warnings": scene.warnings,
    }


def _options_from_args(args: argparse.Namespace) -> ConversionOptions:
    return ConversionOptions(
        scale=args.scale,
        lighting=args.lighting,
        include_hidden=args.include_hidden,
        include_cameras=not args.no_cameras,
        include_lights=not args.no_lights,
        texture_filter=args.texture_filter,
        light_intensity_scale=args.light_intensity_scale,
        camera_aspect_ratio=args.camera_aspect_ratio,
    )


def _add_conversion_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scale",
        type=float,
        default=0.025,
        help="glTF meters per game unit (default: 0.025, 40 game units per meter)",
    )
    parser.add_argument(
        "--lighting",
        choices=("baked", "dynamic"),
        default="baked",
        help="baked uses unlit texture*COLOR_0; dynamic enables PBR response to mapped lights",
    )
    parser.add_argument("--include-hidden", action="store_true")
    parser.add_argument("--no-cameras", action="store_true")
    parser.add_argument("--no-lights", action="store_true")
    parser.add_argument(
        "--texture-filter", choices=("linear", "nearest"), default="linear"
    )
    parser.add_argument(
        "--light-intensity-scale",
        type=float,
        default=100.0,
        help="best-effort source-to-candela multiplier used by explicit lights",
    )
    parser.add_argument(
        "--camera-aspect-ratio",
        type=float,
        default=4.0 / 3.0,
        help="authored camera aspect ratio used to convert horizontal FOV to glTF yfov (default: 4:3)",
    )


def _command_convert(args: argparse.Namespace) -> int:
    source = Path(args.source)
    output = Path(args.output) if args.output else source.with_suffix(".glb")
    stats = convert_file(source, output, _options_from_args(args))
    print(json.dumps(stats, indent=2))
    return 0


def _command_batch(args: argparse.Namespace) -> int:
    source_directory = Path(args.source_directory)
    output_directory = Path(args.output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    sources = sorted(
        source_directory.glob(args.pattern), key=lambda path: path.name.casefold()
    )
    if not sources:
        raise FileNotFoundError(
            f"no files matching {args.pattern!r} in {source_directory}"
        )
    options = _options_from_args(args)
    started = time.time()
    manifest: dict[str, Any] = {
        "sourceDirectory": str(source_directory.resolve()),
        "outputDirectory": str(output_directory.resolve()),
        "pattern": args.pattern,
        "options": dataclasses.asdict(options),
        "results": [],
        "errors": [],
    }
    for number, source in enumerate(sources, 1):
        output = output_directory / f"{source.stem}.glb"
        if output.exists() and not args.overwrite:
            print(f"[{number}/{len(sources)}] skip {source.name} (output exists)")
            manifest["results"].append(
                {"source": source.name, "output": str(output.resolve()), "skipped": True}
            )
            continue
        print(f"[{number}/{len(sources)}] convert {source.name}", flush=True)
        try:
            manifest["results"].append(convert_file(source, output, options))
        except Exception as error:  # keep a long batch useful while reporting exact failures
            failure = {"source": source.name, "error": f"{type(error).__name__}: {error}"}
            manifest["errors"].append(failure)
            print(f"  ERROR: {failure['error']}", file=sys.stderr, flush=True)
            if args.fail_fast:
                break
    manifest["elapsedSeconds"] = round(time.time() - started, 3)
    manifest["converted"] = sum(
        1 for result in manifest["results"] if not result.get("skipped")
    )
    manifest["skipped"] = sum(
        1 for result in manifest["results"] if result.get("skipped")
    )
    manifest["failed"] = len(manifest["errors"])
    manifest_path = output_directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({key: manifest[key] for key in ("converted", "skipped", "failed", "elapsedSeconds")}, indent=2))
    print(f"Manifest: {manifest_path.resolve()}")
    return 1 if manifest["errors"] else 0


def _command_inspect(args: argparse.Namespace) -> int:
    print(json.dumps(inspect_scene(args.source), indent=2))
    return 0


def _command_verify(args: argparse.Namespace) -> int:
    path = Path(args.source)
    document = validate_glb_bytes(path.read_bytes())
    result = {
        "file": str(path.resolve()),
        "bytes": path.stat().st_size,
        "meshes": len(document.get("meshes", [])),
        "nodes": len(document.get("nodes", [])),
        "materials": len(document.get("materials", [])),
        "images": len(document.get("images", [])),
        "accessors": len(document.get("accessors", [])),
        "extensionsUsed": document.get("extensionsUsed", []),
        "valid": True,
    }
    print(json.dumps(result, indent=2))
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert original Omikron OD3X scenes and textures to GLB 2.0"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    convert_parser = subparsers.add_parser("convert", help="convert one .3DO/.3DT pair")
    convert_parser.add_argument("source", help="source .3DO file")
    convert_parser.add_argument("-o", "--output", help="destination .glb file")
    _add_conversion_options(convert_parser)
    convert_parser.set_defaults(func=_command_convert)

    batch_parser = subparsers.add_parser("batch", help="convert a directory of scenes")
    batch_parser.add_argument("source_directory", help="directory containing .3DO/.3DT pairs")
    batch_parser.add_argument("output_directory", help="destination directory")
    batch_parser.add_argument("--pattern", default="*.3DO")
    batch_parser.add_argument("--overwrite", action="store_true")
    batch_parser.add_argument("--fail-fast", action="store_true")
    _add_conversion_options(batch_parser)
    batch_parser.set_defaults(func=_command_batch)

    inspect_parser = subparsers.add_parser("inspect", help="show OD3X scene metadata")
    inspect_parser.add_argument("source")
    inspect_parser.set_defaults(func=_command_inspect)

    verify_parser = subparsers.add_parser("verify", help="perform structural GLB validation")
    verify_parser.add_argument("source")
    verify_parser.set_defaults(func=_command_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, FormatError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
