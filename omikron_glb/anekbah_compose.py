#!/usr/bin/env python3
"""Compose Anekbah's exterior and IAM-selected interiors into one GLB.

This is a lossless container-level merger, not a Blender round trip.  It keeps
the source accessors, materials, images, custom attributes, and extras intact,
concatenates the embedded BIN payloads, and rebases every glTF index used by the
Omikron converter. Each source scene is placed below a composition parent node.
Exterior-connected sources keep an identity parent; disconnected teleport-local
interiors receive only a documented parent translation so their OD3X node
transforms stay intact.

The interior list always comes from ``anekbah_interiors_report.json``.  The
directory is never globbed, so stale or unrelated GLBs cannot enter a build.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import math
from pathlib import Path
import re
import struct
from typing import Any, Iterable, Sequence
import unicodedata

import omikron_glb as converter


INTERIOR_REPORT_NAME = "anekbah_interiors_report.json"
INTERIOR_REPORT_SCHEMA = "omikron-anekbah-interiors-build-v2"
COMPOSITION_REPORT_SCHEMA = "omikron-anekbah-glb-composition-v1"
ANEKBAH_ZONE_LAYOUT_FILE = "anekbah_zone_layout.json"
ANEKBAH_ZONE_LAYOUT_SCHEMA = "omikron-anekbah-zone-layout-v1"
SUPPORTED_EXTENSIONS = {"KHR_materials_unlit", "KHR_lights_punctual"}
DOOR_SEAM_ANCHOR_TOLERANCE_METERS = 0.05
DOOR_NAME_MARKERS = ("porte", "door", "dorb")
AUDITED_DOOR_SEAM_NAMES = {"acoultoit", "ba06eport4"}
AUDITED_NON_DOOR_SEAM_NAMES = {"ggrockx"}
_BLENDER_DUPLICATE_SUFFIX = re.compile(r"\.\d{3}$")
ARRAY_NAMES = (
    "accessors",
    "bufferViews",
    "samplers",
    "images",
    "textures",
    "materials",
    "meshes",
    "cameras",
    "skins",
    "nodes",
    "animations",
)


class CompositionError(ValueError):
    """Raised when a source or composition manifest is unsafe to merge."""


@dataclasses.dataclass(frozen=True)
class GlbSource:
    path: Path
    role: str
    decor_stem: str
    data: bytes
    document: dict[str, Any]
    binary: bytes
    sha256: str
    manifest_entry: dict[str, Any] | None = None


@dataclasses.dataclass(frozen=True)
class IndexOffsets:
    accessors: int
    buffer_views: int
    samplers: int
    images: int
    textures: int
    materials: int
    meshes: int
    cameras: int
    skins: int
    nodes: int
    lights: int


def _matrix_multiply(
    left: tuple[tuple[float, ...], ...],
    right: tuple[tuple[float, ...], ...],
) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(sum(left[row][item] * right[item][column] for item in range(4)) for column in range(4))
        for row in range(4)
    )


def _node_matrix(node: dict[str, Any]) -> tuple[tuple[float, ...], ...]:
    """Return a glTF node transform as a conventional row-major matrix."""
    matrix = node.get("matrix")
    if matrix is not None:
        if not isinstance(matrix, list) or len(matrix) != 16:
            raise CompositionError("node matrix must contain 16 values")
        # glTF serializes matrices column-major.
        return tuple(
            tuple(float(matrix[column * 4 + row]) for column in range(4))
            for row in range(4)
        )

    translation = node.get("translation", (0.0, 0.0, 0.0))
    rotation = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    scale = node.get("scale", (1.0, 1.0, 1.0))
    if len(translation) != 3 or len(rotation) != 4 or len(scale) != 3:
        raise CompositionError("node TRS transform has an invalid component count")
    tx, ty, tz = map(float, translation)
    x, y, z, w = map(float, rotation)
    sx, sy, sz = map(float, scale)
    quaternion_length = math.sqrt(x * x + y * y + z * z + w * w)
    if quaternion_length <= 1e-12:
        raise CompositionError("node rotation quaternion has zero length")
    x, y, z, w = (
        x / quaternion_length,
        y / quaternion_length,
        z / quaternion_length,
        w / quaternion_length,
    )
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return (
        ((1.0 - 2.0 * (yy + zz)) * sx, (2.0 * (xy - wz)) * sy, (2.0 * (xz + wy)) * sz, tx),
        ((2.0 * (xy + wz)) * sx, (1.0 - 2.0 * (xx + zz)) * sy, (2.0 * (yz - wx)) * sz, ty),
        ((2.0 * (xz - wy)) * sx, (2.0 * (yz + wx)) * sy, (1.0 - 2.0 * (xx + yy)) * sz, tz),
        (0.0, 0.0, 0.0, 1.0),
    )


def _scene_world_anchors(document: dict[str, Any]) -> dict[int, tuple[float, float, float]]:
    """Resolve the authored world-space origin of every default-scene node."""
    nodes = document.get("nodes", [])
    scenes = document.get("scenes", [])
    scene_index = document.get("scene", 0)
    if not isinstance(scene_index, int) or scene_index < 0 or scene_index >= len(scenes):
        raise CompositionError("document has no valid default scene")
    identity = (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    anchors: dict[int, tuple[float, float, float]] = {}
    active: set[int] = set()

    def visit(index: int, parent_matrix: tuple[tuple[float, ...], ...]) -> None:
        _require_index(index, len(nodes), "scene node")
        if index in active:
            raise CompositionError("node hierarchy contains a cycle")
        active.add(index)
        world = _matrix_multiply(parent_matrix, _node_matrix(nodes[index]))
        anchor = (world[0][3], world[1][3], world[2][3])
        previous = anchors.get(index)
        if previous is not None and math.dist(previous, anchor) > 1e-9:
            raise CompositionError("node has multiple parents with different world transforms")
        anchors[index] = anchor
        for child in nodes[index].get("children", []):
            visit(child, world)
        active.remove(index)

    for root in scenes[scene_index].get("nodes", []):
        visit(root, identity)
    return anchors


def _normalized_original_name(document: dict[str, Any], node: dict[str, Any]) -> str:
    if "mesh" not in node:
        return ""
    mesh_index = _require_index(node["mesh"], len(document.get("meshes", [])), "node mesh")
    mesh = document["meshes"][mesh_index]
    value = (
        mesh.get("extras", {}).get("omikron", {}).get("originalName")
        or mesh.get("name")
        or node.get("name")
        or ""
    )
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = " ".join(normalized.split())
    return _BLENDER_DUPLICATE_SUFFIX.sub("", normalized)


def _is_door_source_name(normalized_name: str) -> bool:
    if not normalized_name or normalized_name in AUDITED_NON_DOOR_SEAM_NAMES:
        return False
    return (
        any(marker in normalized_name for marker in DOOR_NAME_MARKERS)
        or normalized_name in AUDITED_DOOR_SEAM_NAMES
    )


def _door_candidates(source: GlbSource) -> list[dict[str, Any]]:
    anchors = _scene_world_anchors(source.document)
    candidates: list[dict[str, Any]] = []
    for node_index, anchor in anchors.items():
        node = source.document["nodes"][node_index]
        normalized_name = _normalized_original_name(source.document, node)
        if not _is_door_source_name(normalized_name):
            continue
        mesh = source.document["meshes"][node["mesh"]]
        original_name = (
            mesh.get("extras", {}).get("omikron", {}).get("originalName")
            or mesh.get("name")
            or node.get("name")
        )
        candidates.append(
            {
                "sourceNodeIndex": node_index,
                "nodeName": node.get("name"),
                "originalName": original_name,
                "normalizedOriginalName": normalized_name,
                "worldAnchorMeters": list(anchor),
            }
        )
    return candidates


def _find_exterior_door_seam_matches(
    sources: Sequence[GlbSource],
    ignored_nodes_by_source: dict[int, set[int]] | None = None,
) -> list[dict[str, Any]]:
    if not sources or sources[0].role != "base":
        raise CompositionError("door seam matching requires the base exterior first")
    ignored = ignored_nodes_by_source or {}
    base_by_name: dict[str, list[dict[str, Any]]] = {}
    for candidate in _door_candidates(sources[0]):
        base_by_name.setdefault(candidate["normalizedOriginalName"], []).append(candidate)

    pairs: list[dict[str, Any]] = []
    for source_index, source in enumerate(sources[1:], 1):
        if source.role != "interior":
            continue
        for interior in _door_candidates(source):
            if interior["sourceNodeIndex"] in ignored.get(source_index, set()):
                continue
            matches: list[tuple[float, dict[str, Any]]] = []
            for base in base_by_name.get(interior["normalizedOriginalName"], []):
                distance = math.dist(
                    interior["worldAnchorMeters"], base["worldAnchorMeters"]
                )
                if distance <= DOOR_SEAM_ANCHOR_TOLERANCE_METERS:
                    matches.append((distance, base))
            if not matches:
                continue
            matches.sort(key=lambda item: (item[0], item[1]["sourceNodeIndex"]))
            if len(matches) > 1 and abs(matches[0][0] - matches[1][0]) <= 1e-12:
                raise CompositionError(
                    f"ambiguous exterior seam match for {source.decor_stem} "
                    f"node {interior['sourceNodeIndex']}"
                )
            distance, base = matches[0]
            pairs.append(
                {
                    "normalizedOriginalName": interior["normalizedOriginalName"],
                    "distanceMeters": distance,
                    "baseExterior": {
                        "role": "base",
                        "decorStem": sources[0].decor_stem,
                        **copy.deepcopy(base),
                    },
                    "suppressedInterior": {
                        "role": "interior",
                        "decorStem": source.decor_stem,
                        **copy.deepcopy(interior),
                    },
                    "sourceIndex": source_index,
                }
            )
    pairs.sort(
        key=lambda pair: (
            pair["normalizedOriginalName"],
            pair["suppressedInterior"]["decorStem"].casefold(),
            pair["suppressedInterior"]["sourceNodeIndex"],
        )
    )
    return pairs


def _find_interior_door_seam_matches(
    sources: Sequence[GlbSource],
    ignored_nodes_by_source: dict[int, set[int]] | None = None,
) -> list[dict[str, Any]]:
    """Find later-manifest interior doors duplicated by an earlier interior."""
    ignored = ignored_nodes_by_source or {}
    retained_by_name: dict[str, list[dict[str, Any]]] = {}
    pairs: list[dict[str, Any]] = []
    for source_index, source in enumerate(sources[1:], 1):
        if source.role != "interior":
            continue
        source_candidates = [
            candidate
            for candidate in _door_candidates(source)
            if candidate["sourceNodeIndex"] not in ignored.get(source_index, set())
        ]
        retained_from_source: list[dict[str, Any]] = []
        for interior in source_candidates:
            matches: list[tuple[float, dict[str, Any]]] = []
            for earlier in retained_by_name.get(
                interior["normalizedOriginalName"], []
            ):
                distance = math.dist(
                    interior["worldAnchorMeters"], earlier["worldAnchorMeters"]
                )
                if distance <= DOOR_SEAM_ANCHOR_TOLERANCE_METERS:
                    matches.append((distance, earlier))
            if not matches:
                retained_from_source.append(
                    {
                        "role": "interior",
                        "decorStem": source.decor_stem,
                        "sourceIndex": source_index,
                        **copy.deepcopy(interior),
                    }
                )
                continue
            matches.sort(
                key=lambda item: (
                    item[0],
                    item[1]["sourceIndex"],
                    item[1]["sourceNodeIndex"],
                )
            )
            if len(matches) > 1 and abs(matches[0][0] - matches[1][0]) <= 1e-12:
                raise CompositionError(
                    f"ambiguous earlier-interior seam match for {source.decor_stem} "
                    f"node {interior['sourceNodeIndex']}"
                )
            distance, earlier = matches[0]
            pairs.append(
                {
                    "normalizedOriginalName": interior["normalizedOriginalName"],
                    "distanceMeters": distance,
                    "retainedInterior": copy.deepcopy(earlier),
                    "suppressedInterior": {
                        "role": "interior",
                        "decorStem": source.decor_stem,
                        **copy.deepcopy(interior),
                    },
                    "sourceIndex": source_index,
                }
            )
        for retained in retained_from_source:
            retained_by_name.setdefault(
                retained["normalizedOriginalName"], []
            ).append(retained)
    pairs.sort(
        key=lambda pair: (
            pair["sourceIndex"],
            pair["suppressedInterior"]["sourceNodeIndex"],
            pair["normalizedOriginalName"],
        )
    )
    return pairs


def _door_seam_suppression_plan(
    sources: Sequence[GlbSource],
) -> tuple[dict[int, dict[int, dict[str, Any]]], dict[str, Any]]:
    exterior_pairs = _find_exterior_door_seam_matches(sources)
    nodes: dict[int, dict[int, dict[str, Any]]] = {}
    for pair in exterior_pairs:
        source_index = pair.pop("sourceIndex")
        pair["phase"] = "exteriorPriority"
        node_index = pair["suppressedInterior"]["sourceNodeIndex"]
        nodes.setdefault(source_index, {})[node_index] = pair
    exterior_ignored = {
        source_index: set(source_nodes) for source_index, source_nodes in nodes.items()
    }
    interior_pairs = _find_interior_door_seam_matches(sources, exterior_ignored)
    for pair in interior_pairs:
        source_index = pair.pop("sourceIndex")
        pair["phase"] = "manifestInteriorPriority"
        node_index = pair["suppressedInterior"]["sourceNodeIndex"]
        nodes.setdefault(source_index, {})[node_index] = pair

    all_ignored = {
        source_index: set(source_nodes) for source_index, source_nodes in nodes.items()
    }
    remaining_exterior = _find_exterior_door_seam_matches(sources, all_ignored)
    remaining_interior = _find_interior_door_seam_matches(sources, all_ignored)
    if remaining_exterior or remaining_interior:
        raise CompositionError("door seam suppression left renderable door matches")
    pairs = exterior_pairs + interior_pairs
    for number, pair in enumerate(pairs, 1):
        pair["pairId"] = f"door-seam-{number:03d}"
    report = {
        "policy": (
            "two phase exterior/manifest priority: first retain matching base exterior "
            "doors; then retain the earliest report-ordered interior among remaining matches"
        ),
        "strategy": (
            "remove the matched interior node's mesh binding; retain the base exterior "
            "node and untouched source resources/BIN bytes"
        ),
        "normalizedNamePolicy": (
            "Unicode NFKC, trim, casefold, collapse whitespace, remove trailing Blender .### suffix"
        ),
        "doorNamePolicy": {
            "markers": list(DOOR_NAME_MARKERS),
            "auditedPortalNames": sorted(AUDITED_DOOR_SEAM_NAMES),
            "auditedNonDoorExclusions": sorted(AUDITED_NON_DOOR_SEAM_NAMES),
        },
        "anchorToleranceMeters": DOOR_SEAM_ANCHOR_TOLERANCE_METERS,
        "matchedPairCount": len(pairs),
        "suppressedInteriorNodeCount": len(pairs),
        "baseExteriorNodeCountRetained": len(exterior_pairs),
        "earlierManifestInteriorNodeCountRetained": len(interior_pairs),
        "phases": {
            "exteriorPriority": {
                "matchedPairCount": len(exterior_pairs),
                "remainingRenderableMatches": len(remaining_exterior),
            },
            "manifestInteriorPriority": {
                "matchedPairCount": len(interior_pairs),
                "winner": "lower sourceIndex from the authoritative manifest/report order",
                "remainingRenderableMatches": len(remaining_interior),
            },
        },
        "remainingRenderableSeamMatches": len(remaining_exterior)
        + len(remaining_interior),
        "assertionPassed": not remaining_exterior and not remaining_interior,
        "pairs": pairs,
    }
    return nodes, report


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _resolve_interior_report(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=True)
    if candidate.is_file():
        return candidate
    if not candidate.is_dir():
        raise CompositionError(f"interior report/directory does not exist: {candidate}")
    direct = candidate / INTERIOR_REPORT_NAME
    parent = candidate.parent / INTERIOR_REPORT_NAME
    if direct.is_file():
        return direct
    if candidate.name.casefold() == "glb" and parent.is_file():
        return parent
    raise CompositionError(
        f"directory does not contain {INTERIOR_REPORT_NAME}: {candidate}"
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CompositionError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise CompositionError(f"expected a JSON object in {path}")
    return value


def _aabb_overlaps(
    left_minimum: Sequence[float],
    left_maximum: Sequence[float],
    right_minimum: Sequence[float],
    right_maximum: Sequence[float],
    tolerance: float = 1.0e-5,
) -> bool:
    return all(
        min(float(left_maximum[axis]), float(right_maximum[axis]))
        - max(float(left_minimum[axis]), float(right_minimum[axis]))
        > tolerance
        for axis in range(3)
    )


def _load_anekbah_zone_layout(
    sources: Sequence[GlbSource],
) -> tuple[Path, dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    path = Path(__file__).resolve().with_name(ANEKBAH_ZONE_LAYOUT_FILE)
    layout = _read_json_object(path)
    if layout.get("$schema") != ANEKBAH_ZONE_LAYOUT_SCHEMA:
        raise CompositionError(
            f"unsupported Anekbah zone layout schema: {layout.get('$schema')!r}"
        )
    if layout.get("level") != "Anekbah" or layout.get("enabled") is not True:
        raise CompositionError("Anekbah zone layout is not enabled for Anekbah")
    zones = layout.get("zones")
    if not isinstance(zones, list) or not zones:
        raise CompositionError("Anekbah zone layout has no zones")

    source_by_stem = {
        source.decor_stem.casefold(): source
        for source in sources
        if source.role == "interior"
    }
    source_stems = {
        key: source.decor_stem for key, source in source_by_stem.items()
    }
    assignments: dict[str, dict[str, Any]] = {}
    zone_ids: set[str] = set()
    bounds: list[tuple[str, Sequence[float], Sequence[float]]] = []
    exterior_bounds = layout.get("placement", {}).get(
        "exteriorBoundsBlenderMeters", {}
    )
    exterior_minimum = exterior_bounds.get("minimum")
    exterior_maximum = exterior_bounds.get("maximum")
    if not (
        isinstance(exterior_minimum, list)
        and len(exterior_minimum) == 3
        and isinstance(exterior_maximum, list)
        and len(exterior_maximum) == 3
    ):
        raise CompositionError("Anekbah zone layout has invalid exterior bounds")

    zone_reports: list[dict[str, Any]] = []
    for zone_index, zone_value in enumerate(zones):
        if not isinstance(zone_value, dict):
            raise CompositionError(f"Anekbah zone {zone_index} is not an object")
        zone = copy.deepcopy(zone_value)
        zone_id = zone.get("id")
        stems = zone.get("stems")
        translation_glb = zone.get("translationGlbMeters")
        translation_blender = zone.get("translationBlenderMeters")
        declared_bounds = zone.get("finalBoundsBlenderMeters", {})
        minimum = declared_bounds.get("minimum")
        maximum = declared_bounds.get("maximum")
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise CompositionError(f"Anekbah zone {zone_index} has no id")
        zone_key = zone_id.casefold()
        if zone_key in zone_ids:
            raise CompositionError(f"duplicate Anekbah zone id: {zone_id}")
        zone_ids.add(zone_key)
        if not isinstance(stems, list) or not stems:
            raise CompositionError(f"Anekbah zone {zone_id} has no stems")
        if not (
            isinstance(translation_glb, list)
            and len(translation_glb) == 3
            and isinstance(translation_blender, list)
            and len(translation_blender) == 3
            and all(math.isfinite(float(value)) for value in translation_glb)
            and all(math.isfinite(float(value)) for value in translation_blender)
        ):
            raise CompositionError(f"Anekbah zone {zone_id} has invalid translations")
        expected_blender = (
            float(translation_glb[0]),
            -float(translation_glb[2]),
            float(translation_glb[1]),
        )
        if max(
            abs(float(translation_blender[index]) - expected_blender[index])
            for index in range(3)
        ) > 1.0e-8:
            raise CompositionError(
                f"Anekbah zone {zone_id} GLB/Blender translations disagree"
            )
        if not (
            isinstance(minimum, list)
            and len(minimum) == 3
            and isinstance(maximum, list)
            and len(maximum) == 3
            and all(math.isfinite(float(value)) for value in minimum + maximum)
        ):
            raise CompositionError(f"Anekbah zone {zone_id} has invalid final bounds")
        if _aabb_overlaps(minimum, maximum, exterior_minimum, exterior_maximum):
            raise CompositionError(f"Anekbah zone {zone_id} overlaps the exterior")
        for prior_id, prior_minimum, prior_maximum in bounds:
            if _aabb_overlaps(minimum, maximum, prior_minimum, prior_maximum):
                raise CompositionError(
                    f"Anekbah zones {prior_id} and {zone_id} overlap"
                )
        bounds.append((zone_id, minimum, maximum))

        for stem_value in stems:
            if not isinstance(stem_value, str) or not stem_value.strip():
                raise CompositionError(f"Anekbah zone {zone_id} has a blank stem")
            stem_key = stem_value.casefold()
            if stem_key in assignments:
                raise CompositionError(
                    f"Anekbah interior {stem_value} is assigned to multiple zones"
                )
            if stem_key not in source_stems:
                raise CompositionError(
                    f"Anekbah zone {zone_id} references absent interior {stem_value}"
                )
            assignments[stem_key] = zone
        zone_reports.append(
            {
                "index": zone_index,
                "id": zone_id,
                "label": zone.get("label"),
                "stems": copy.deepcopy(stems),
                "explicitLights": sum(
                    len(_lights(source_by_stem[str(stem).casefold()].document))
                    for stem in stems
                ),
                "translationGlbMeters": copy.deepcopy(translation_glb),
                "translationBlenderMeters": copy.deepcopy(translation_blender),
                "finalBoundsBlenderMeters": copy.deepcopy(declared_bounds),
                "overlapsExterior": False,
            }
        )

    selection = layout.get("selection", {})
    expected_relocated = selection.get("relocatedInteriors")
    expected_relocated_lights = selection.get("relocatedExplicitLights")
    expected_zones = selection.get("zones")
    relocated_lights = sum(
        len(_lights(source_by_stem[key].document)) for key in assignments
    )
    if expected_relocated != len(assignments):
        raise CompositionError(
            "Anekbah zone layout relocated interior count mismatch: "
            f"{len(assignments)} != {expected_relocated}"
        )
    if expected_zones != len(zone_reports):
        raise CompositionError(
            f"Anekbah zone layout zone count mismatch: {len(zone_reports)} != {expected_zones}"
        )
    if expected_relocated_lights != relocated_lights:
        raise CompositionError(
            "Anekbah zone layout relocated explicit-light count mismatch: "
            f"{relocated_lights} != {expected_relocated_lights}"
        )
    report = {
        "enabled": True,
        "layout": {
            "file": str(path),
            "bytes": path.stat().st_size,
            "sha256": _sha256_path(path),
            "schema": layout["$schema"],
        },
        "purpose": layout.get("purpose"),
        "selection": copy.deepcopy(selection),
        "placement": copy.deepcopy(layout.get("placement")),
        "zoneCount": len(zone_reports),
        "relocatedInteriors": len(assignments),
        "relocatedExplicitLights": relocated_lights,
        "relocatedStems": sorted(source_stems[key] for key in assignments),
        "zones": zone_reports,
        "zoneExteriorOverlapCount": 0,
        "interZoneOverlapCount": 0,
        "assertionPassed": True,
        "applicationOrder": (
            "door seam matching in authored coordinates, then source-parent translation"
        ),
    }
    return path, layout, assignments, report


def _casefold_unique(values: Iterable[str], label: str) -> None:
    seen: dict[str, str] = {}
    for value in values:
        key = value.casefold()
        if key in seen:
            raise CompositionError(
                f"duplicate {label} differing only by case: {seen[key]!r}, {value!r}"
            )
        seen[key] = value


def _manifest_sources(
    report_or_directory: Path,
) -> tuple[Path, dict[str, Any], list[tuple[str, Path, dict[str, Any]]]]:
    report_path = _resolve_interior_report(report_or_directory)
    report = _read_json_object(report_path)
    if report.get("$schema") != INTERIOR_REPORT_SCHEMA:
        raise CompositionError(
            f"unsupported interior report schema: {report.get('$schema')!r}"
        )
    if report.get("level") != "Anekbah" or report.get("valid") is not True:
        raise CompositionError("interior report is not a valid Anekbah build")
    options = report.get("conversionOptions")
    if not isinstance(options, dict):
        raise CompositionError("interior report has no conversionOptions object")
    if (
        options.get("lighting") != "baked"
        or options.get("include_cameras") is not False
        or options.get("include_lights") is not True
    ):
        raise CompositionError(
            "interiors must be baked exports with cameras omitted and decoded "
            "explicit lights retained"
        )
    entries = report.get("interiors")
    if not isinstance(entries, list) or not entries:
        raise CompositionError("interior report contains no selected interiors")
    expected = report.get("selection", {}).get("resolvedUniqueInteriors")
    total = report.get("totals", {}).get("interiors")
    if expected != len(entries) or total != len(entries):
        raise CompositionError(
            "interior report selection/totals do not match its ordered interior list"
        )

    stems: list[str] = []
    resolved: list[tuple[str, Path, dict[str, Any]]] = []
    local_glb_directory = report_path.parent / "glb"
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise CompositionError(f"interior entry {number} is not an object")
        stem = entry.get("decorStem")
        output = entry.get("output")
        if not isinstance(stem, str) or not stem.strip() or not isinstance(output, dict):
            raise CompositionError(f"interior entry {number} is missing decorStem/output")
        if stem.casefold() == "anekbah":
            raise CompositionError("interior report must not contain the base exterior")
        stems.append(stem)
        local_path = local_glb_directory / f"{stem}.glb"
        recorded_path = output.get("file")
        candidates = [local_path]
        if isinstance(recorded_path, str) and recorded_path:
            candidates.append(Path(recorded_path).expanduser())
        glb_path = next((item.resolve() for item in candidates if item.is_file()), None)
        if glb_path is None:
            raise CompositionError(
                f"manifest-selected interior GLB is missing for {stem}: {local_path}"
            )
        if glb_path.stem.casefold() != stem.casefold():
            raise CompositionError(
                f"interior {stem} resolves to mismatched GLB {glb_path.name}"
            )
        data = glb_path.read_bytes()
        recorded_bytes = output.get("bytes")
        recorded_hash = output.get("sha256")
        if recorded_bytes != len(data):
            raise CompositionError(
                f"interior {stem} byte count differs from its build report"
            )
        if not isinstance(recorded_hash, str) or _sha256_bytes(data) != recorded_hash:
            raise CompositionError(f"interior {stem} SHA-256 differs from its build report")
        if output.get("valid") is not True:
            raise CompositionError(f"interior {stem} is not marked valid")
        resolved.append((stem, glb_path, entry))
    _casefold_unique(stems, "interior decor stem")
    return report_path, report, resolved


def _parse_glb(
    path: Path,
    role: str,
    decor_stem: str,
    manifest_entry: dict[str, Any] | None = None,
) -> GlbSource:
    source_path = path.expanduser().resolve(strict=True)
    data = source_path.read_bytes()
    document = converter.validate_glb_bytes(data)
    if document.get("asset", {}).get("version") != "2.0":
        raise CompositionError(f"{source_path.name} is not glTF 2.0")
    used = set(document.get("extensionsUsed", []))
    required = set(document.get("extensionsRequired", []))
    unsupported = sorted((used | required) - SUPPORTED_EXTENSIONS)
    if unsupported:
        raise CompositionError(
            f"{source_path.name} uses unsupported extensions: {unsupported}"
        )
    buffers = document.get("buffers")
    if not isinstance(buffers, list) or len(buffers) != 1:
        raise CompositionError(f"{source_path.name} must contain exactly one GLB buffer")
    if "uri" in buffers[0]:
        raise CompositionError(f"{source_path.name} contains an external buffer URI")
    scenes = document.get("scenes")
    if not isinstance(scenes, list) or len(scenes) != 1 or document.get("scene", 0) != 0:
        raise CompositionError(f"{source_path.name} must contain one default scene")
    for image in document.get("images", []):
        if "uri" in image:
            raise CompositionError(f"{source_path.name} contains an external image URI")

    json_length = struct.unpack_from("<I", data, 12)[0]
    json_end = 20 + json_length
    bin_length = struct.unpack_from("<I", data, json_end)[0]
    bin_start = json_end + 8
    declared = int(buffers[0]["byteLength"])
    if declared > bin_length:
        raise CompositionError(f"{source_path.name} declares a truncated BIN payload")
    binary = data[bin_start : bin_start + declared]
    _validate_document_indices(document, len(binary), source_path.name)
    return GlbSource(
        path=source_path,
        role=role,
        decor_stem=decor_stem,
        data=data,
        document=document,
        binary=binary,
        sha256=_sha256_bytes(data),
        manifest_entry=manifest_entry,
    )


def _lights(document: dict[str, Any]) -> list[dict[str, Any]]:
    extension = document.get("extensions", {}).get("KHR_lights_punctual", {})
    lights = extension.get("lights", [])
    if not isinstance(lights, list):
        raise CompositionError("KHR_lights_punctual.lights is not an array")
    return lights


def _offsets(document: dict[str, Any]) -> IndexOffsets:
    return IndexOffsets(
        accessors=len(document.get("accessors", [])),
        buffer_views=len(document.get("bufferViews", [])),
        samplers=len(document.get("samplers", [])),
        images=len(document.get("images", [])),
        textures=len(document.get("textures", [])),
        materials=len(document.get("materials", [])),
        meshes=len(document.get("meshes", [])),
        cameras=len(document.get("cameras", [])),
        skins=len(document.get("skins", [])),
        nodes=len(document.get("nodes", [])),
        lights=len(_lights(document)),
    )


def _add_index(value: Any, offset: int, label: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise CompositionError(f"invalid {label} index: {value!r}")
    return value + offset


def _tag_extras(value: Any, source: GlbSource) -> dict[str, Any]:
    tag = {
        "role": source.role,
        "decorStem": source.decor_stem,
        "sourceGlb": source.path.name,
        "sourceGlbSha256": source.sha256,
        "authoredPlacement": (
            "source node transform retained; composition parent controls final placement"
        ),
    }
    if value is None:
        return {"omikronComposition": tag}
    if isinstance(value, dict):
        result = copy.deepcopy(value)
        if "omikronComposition" in result:
            raise CompositionError("source extras already use reserved omikronComposition key")
        result["omikronComposition"] = tag
        return result
    return {"omikronOriginalExtras": copy.deepcopy(value), "omikronComposition": tag}


def _rebase_material_textures(value: Any, texture_offset: int) -> None:
    if isinstance(value, list):
        for item in value:
            _rebase_material_textures(item, texture_offset)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key == "extras":
            continue
        if key.endswith("Texture") and isinstance(item, dict) and "index" in item:
            item["index"] = _add_index(item["index"], texture_offset, key)
        _rebase_material_textures(item, texture_offset)


def _copy_source_into(
    output: dict[str, Any],
    binary: bytearray,
    source: GlbSource,
    suppressed_door_nodes: dict[int, dict[str, Any]] | None = None,
    teleport_zone: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_document = source.document
    suppressed_door_nodes = suppressed_door_nodes or {}
    offsets = _offsets(output)
    binary.extend(b"\0" * ((-len(binary)) % 4))
    binary_offset = len(binary)
    binary.extend(source.binary)

    for view in source_document.get("bufferViews", []):
        item = copy.deepcopy(view)
        item["buffer"] = 0
        item["byteOffset"] = binary_offset + int(item.get("byteOffset", 0))
        output.setdefault("bufferViews", []).append(item)

    for accessor in source_document.get("accessors", []):
        item = copy.deepcopy(accessor)
        if "bufferView" in item:
            item["bufferView"] = _add_index(
                item["bufferView"], offsets.buffer_views, "accessor bufferView"
            )
        sparse = item.get("sparse")
        if isinstance(sparse, dict):
            for key in ("indices", "values"):
                if "bufferView" in sparse.get(key, {}):
                    sparse[key]["bufferView"] = _add_index(
                        sparse[key]["bufferView"],
                        offsets.buffer_views,
                        f"sparse {key} bufferView",
                    )
        output.setdefault("accessors", []).append(item)

    output.setdefault("samplers", []).extend(
        copy.deepcopy(source_document.get("samplers", []))
    )
    for image in source_document.get("images", []):
        item = copy.deepcopy(image)
        if "bufferView" in item:
            item["bufferView"] = _add_index(
                item["bufferView"], offsets.buffer_views, "image bufferView"
            )
        output.setdefault("images", []).append(item)
    for texture in source_document.get("textures", []):
        item = copy.deepcopy(texture)
        if "sampler" in item:
            item["sampler"] = _add_index(
                item["sampler"], offsets.samplers, "texture sampler"
            )
        if "source" in item:
            item["source"] = _add_index(
                item["source"], offsets.images, "texture image"
            )
        output.setdefault("textures", []).append(item)
    for material in source_document.get("materials", []):
        item = copy.deepcopy(material)
        _rebase_material_textures(item, offsets.textures)
        output.setdefault("materials", []).append(item)

    source_lights = _lights(source_document)
    if source_lights:
        lights = output.setdefault("extensions", {}).setdefault(
            "KHR_lights_punctual", {"lights": []}
        )["lights"]
        lights.extend(copy.deepcopy(source_lights))

    output.setdefault("cameras", []).extend(
        copy.deepcopy(source_document.get("cameras", []))
    )
    for mesh in source_document.get("meshes", []):
        item = copy.deepcopy(mesh)
        item["extras"] = _tag_extras(item.get("extras"), source)
        for primitive in item.get("primitives", []):
            if "indices" in primitive:
                primitive["indices"] = _add_index(
                    primitive["indices"], offsets.accessors, "primitive indices"
                )
            if "material" in primitive:
                primitive["material"] = _add_index(
                    primitive["material"], offsets.materials, "primitive material"
                )
            primitive["attributes"] = {
                semantic: _add_index(index, offsets.accessors, f"{semantic} accessor")
                for semantic, index in primitive.get("attributes", {}).items()
            }
            for target in primitive.get("targets", []):
                for semantic, index in list(target.items()):
                    target[semantic] = _add_index(
                        index, offsets.accessors, f"morph {semantic} accessor"
                    )
        output.setdefault("meshes", []).append(item)

    for skin in source_document.get("skins", []):
        item = copy.deepcopy(skin)
        if "inverseBindMatrices" in item:
            item["inverseBindMatrices"] = _add_index(
                item["inverseBindMatrices"], offsets.accessors, "inverseBindMatrices"
            )
        if "skeleton" in item:
            item["skeleton"] = _add_index(
                item["skeleton"], offsets.nodes, "skin skeleton"
            )
        item["joints"] = [
            _add_index(index, offsets.nodes, "skin joint")
            for index in item.get("joints", [])
        ]
        output.setdefault("skins", []).append(item)

    for source_node_index, node in enumerate(source_document.get("nodes", [])):
        item = copy.deepcopy(node)
        item["extras"] = _tag_extras(item.get("extras"), source)
        if "mesh" in item:
            if source_node_index in suppressed_door_nodes:
                pair = suppressed_door_nodes[source_node_index]
                source_mesh_index = item.pop("mesh")
                retained = pair.get("baseExterior") or pair.get("retainedInterior")
                item["extras"]["omikronComposition"]["doorSeamSuppression"] = {
                    "pairId": pair["pairId"],
                    "phase": pair["phase"],
                    "policy": "two-phase duplicate door seam suppression",
                    "rendering": "mesh binding removed",
                    "originalSourceMeshIndex": source_mesh_index,
                    "retainedRenderableDoor": copy.deepcopy(retained),
                    "distanceMeters": pair["distanceMeters"],
                    "anchorToleranceMeters": DOOR_SEAM_ANCHOR_TOLERANCE_METERS,
                }
            else:
                item["mesh"] = _add_index(item["mesh"], offsets.meshes, "node mesh")
        if "camera" in item:
            item["camera"] = _add_index(
                item["camera"], offsets.cameras, "node camera"
            )
        if "skin" in item:
            item["skin"] = _add_index(item["skin"], offsets.skins, "node skin")
        if "children" in item:
            item["children"] = [
                _add_index(index, offsets.nodes, "node child")
                for index in item["children"]
            ]
        light_ref = item.get("extensions", {}).get("KHR_lights_punctual")
        if isinstance(light_ref, dict) and "light" in light_ref:
            light_ref["light"] = _add_index(
                light_ref["light"], offsets.lights, "node light"
            )
        output.setdefault("nodes", []).append(item)

    for animation in source_document.get("animations", []):
        item = copy.deepcopy(animation)
        for sampler in item.get("samplers", []):
            sampler["input"] = _add_index(
                sampler["input"], offsets.accessors, "animation input"
            )
            sampler["output"] = _add_index(
                sampler["output"], offsets.accessors, "animation output"
            )
        for channel in item.get("channels", []):
            target = channel.get("target", {})
            if "node" in target:
                target["node"] = _add_index(
                    target["node"], offsets.nodes, "animation target node"
                )
        output.setdefault("animations", []).append(item)

    source_scene = source_document["scenes"][0]
    roots = [
        _add_index(index, offsets.nodes, "scene root node")
        for index in source_scene.get("nodes", [])
    ]
    group_index = len(output.setdefault("nodes", []))
    group_placement = (
        "teleport inspection-zone translation; child nodes retain authored transforms"
        if teleport_zone is not None
        else "identity; child nodes retain authored transforms"
    )
    group = {
        "name": f"OMIKRON_SOURCE__{source.decor_stem}",
        "children": roots,
        "extras": {
            "omikronComposition": {
                "role": source.role,
                "decorStem": source.decor_stem,
                "sourceGlb": str(source.path),
                "sourceGlbBytes": len(source.data),
                "sourceGlbSha256": source.sha256,
                "authoredPlacement": group_placement,
            },
            "sourceScene": copy.deepcopy(source_scene),
        },
    }
    if teleport_zone is not None:
        group["translation"] = [
            float(value) for value in teleport_zone["translationGlbMeters"]
        ]
        group["extras"]["omikronComposition"]["teleportInspectionZone"] = {
            "id": teleport_zone["id"],
            "label": teleport_zone.get("label"),
            "translationGlbMeters": copy.deepcopy(
                teleport_zone["translationGlbMeters"]
            ),
            "translationBlenderMeters": copy.deepcopy(
                teleport_zone["translationBlenderMeters"]
            ),
        }
    output["nodes"].append(group)
    output["scenes"][0].setdefault("nodes", []).append(group_index)
    return {
        "role": source.role,
        "decorStem": source.decor_stem,
        "path": str(source.path),
        "bytes": len(source.data),
        "sha256": source.sha256,
        "scene": copy.deepcopy(source_scene),
        "documentExtras": copy.deepcopy(source_document.get("extras")),
        "counts": {
            name: len(source_document.get(name, [])) for name in ARRAY_NAMES
        },
        "lights": len(source_lights),
        "binaryBytes": len(source.binary),
        "compositionRootNode": group_index,
        "suppressedDoorSeamNodes": len(suppressed_door_nodes),
        "teleportInspectionZone": (
            teleport_zone.get("id") if teleport_zone is not None else None
        ),
        "compositionRootTranslationGlbMeters": copy.deepcopy(
            group.get("translation", [0.0, 0.0, 0.0])
        ),
    }


def _pack_glb(document: dict[str, Any], binary: bytes) -> bytes:
    document["buffers"] = [{"name": "Anekbah composed BIN", "byteLength": len(binary)}]
    json_bytes = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    json_bytes += b" " * ((-len(json_bytes)) % 4)
    bin_chunk = binary + b"\0" * ((-len(binary)) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(bin_chunk)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<I4s", len(json_bytes), b"JSON")
        + json_bytes
        + struct.pack("<I4s", len(bin_chunk), b"BIN\0")
        + bin_chunk
    )


def _require_index(value: Any, length: int, label: str) -> int:
    if not isinstance(value, int) or value < 0 or value >= length:
        raise CompositionError(f"{label} index {value!r} is outside 0..{length - 1}")
    return value


def _material_texture_indices(value: Any) -> Iterable[int]:
    if isinstance(value, list):
        for item in value:
            yield from _material_texture_indices(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "extras":
                continue
            if key.endswith("Texture") and isinstance(item, dict) and "index" in item:
                yield item["index"]
            yield from _material_texture_indices(item)


def _validate_document_indices(
    document: dict[str, Any], binary_length: int, label: str
) -> None:
    accessors = document.get("accessors", [])
    views = document.get("bufferViews", [])
    samplers = document.get("samplers", [])
    images = document.get("images", [])
    textures = document.get("textures", [])
    materials = document.get("materials", [])
    meshes = document.get("meshes", [])
    cameras = document.get("cameras", [])
    skins = document.get("skins", [])
    nodes = document.get("nodes", [])
    lights = _lights(document)
    for number, view in enumerate(views):
        if view.get("buffer", 0) != 0:
            raise CompositionError(f"{label}: bufferView {number} does not use buffer 0")
        start = int(view.get("byteOffset", 0))
        length = int(view.get("byteLength", -1))
        if start < 0 or length < 0 or start + length > binary_length:
            raise CompositionError(f"{label}: bufferView {number} exceeds the BIN payload")
    for number, accessor in enumerate(accessors):
        if "bufferView" in accessor:
            _require_index(accessor["bufferView"], len(views), f"{label} accessor {number}")
        sparse = accessor.get("sparse", {})
        for key in ("indices", "values"):
            if "bufferView" in sparse.get(key, {}):
                _require_index(
                    sparse[key]["bufferView"], len(views), f"{label} sparse {key}"
                )
    for number, image in enumerate(images):
        if "bufferView" in image:
            _require_index(image["bufferView"], len(views), f"{label} image {number}")
    for number, texture in enumerate(textures):
        if "sampler" in texture:
            _require_index(texture["sampler"], len(samplers), f"{label} texture sampler")
        if "source" in texture:
            _require_index(texture["source"], len(images), f"{label} texture image")
    for number, material in enumerate(materials):
        for index in _material_texture_indices(material):
            _require_index(index, len(textures), f"{label} material {number} texture")
    for number, mesh in enumerate(meshes):
        for primitive in mesh.get("primitives", []):
            if "indices" in primitive:
                _require_index(primitive["indices"], len(accessors), f"{label} indices")
            if "material" in primitive:
                _require_index(primitive["material"], len(materials), f"{label} material")
            for index in primitive.get("attributes", {}).values():
                _require_index(index, len(accessors), f"{label} attribute")
            for target in primitive.get("targets", []):
                for index in target.values():
                    _require_index(index, len(accessors), f"{label} morph target")
    for number, skin in enumerate(skins):
        if "inverseBindMatrices" in skin:
            _require_index(
                skin["inverseBindMatrices"], len(accessors), f"{label} skin matrices"
            )
        if "skeleton" in skin:
            _require_index(skin["skeleton"], len(nodes), f"{label} skin skeleton")
        for index in skin.get("joints", []):
            _require_index(index, len(nodes), f"{label} skin joint")
    for number, node in enumerate(nodes):
        if "mesh" in node:
            _require_index(node["mesh"], len(meshes), f"{label} node {number} mesh")
        if "camera" in node:
            _require_index(node["camera"], len(cameras), f"{label} node {number} camera")
        if "skin" in node:
            _require_index(node["skin"], len(skins), f"{label} node {number} skin")
        for index in node.get("children", []):
            _require_index(index, len(nodes), f"{label} node {number} child")
        light_ref = node.get("extensions", {}).get("KHR_lights_punctual")
        if isinstance(light_ref, dict) and "light" in light_ref:
            _require_index(light_ref["light"], len(lights), f"{label} node light")
    for scene in document.get("scenes", []):
        for index in scene.get("nodes", []):
            _require_index(index, len(nodes), f"{label} scene root")
    for animation in document.get("animations", []):
        local_samplers = animation.get("samplers", [])
        for sampler in local_samplers:
            _require_index(sampler["input"], len(accessors), f"{label} animation input")
            _require_index(sampler["output"], len(accessors), f"{label} animation output")
        for channel in animation.get("channels", []):
            _require_index(channel["sampler"], len(local_samplers), f"{label} channel")
            if "node" in channel.get("target", {}):
                _require_index(
                    channel["target"]["node"], len(nodes), f"{label} animation node"
                )


def _triangle_count(document: dict[str, Any]) -> int:
    total = 0
    accessors = document.get("accessors", [])
    for mesh in document.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            if primitive.get("mode", 4) != 4 or "indices" not in primitive:
                continue
            accessor = accessors[primitive["indices"]]
            total += int(accessor.get("count", 0)) // 3
    return total


def _renderable_mesh_indices(document: dict[str, Any]) -> set[int]:
    return {
        node["mesh"]
        for node in document.get("nodes", [])
        if isinstance(node.get("mesh"), int)
    }


def _triangle_count_for_meshes(
    document: dict[str, Any], mesh_indices: Iterable[int]
) -> int:
    total = 0
    accessors = document.get("accessors", [])
    meshes = document.get("meshes", [])
    for mesh_index in mesh_indices:
        mesh = meshes[_require_index(mesh_index, len(meshes), "renderable mesh")]
        for primitive in mesh.get("primitives", []):
            if primitive.get("mode", 4) != 4 or "indices" not in primitive:
                continue
            accessor = accessors[primitive["indices"]]
            total += int(accessor.get("count", 0)) // 3
    return total


def merge_sources(
    sources: Sequence[GlbSource],
    *,
    zone_assignments: dict[str, dict[str, Any]] | None = None,
    zone_report: dict[str, Any] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    if not sources or sources[0].role != "base":
        raise CompositionError("the first composition source must be the base exterior")
    output: dict[str, Any] = {
        "asset": {
            "version": "2.0",
            "generator": "Independent Omikron GLB composer",
            "copyright": "Source game assets remain property of their owners",
        },
        "scene": 0,
        "scenes": [{"name": "Anekbah exterior and IAM-selected interiors", "nodes": []}],
        "nodes": [],
    }
    binary = bytearray()
    used: set[str] = set()
    required: set[str] = set()
    provenance: list[dict[str, Any]] = []
    zone_assignments = zone_assignments or {}
    if any(key != key.casefold() for key in zone_assignments):
        raise CompositionError("teleport zone assignment keys must be case-folded")
    available_interior_stems = {
        source.decor_stem.casefold()
        for source in sources
        if source.role == "interior"
    }
    absent_zone_stems = sorted(set(zone_assignments) - available_interior_stems)
    if absent_zone_stems:
        raise CompositionError(
            f"teleport zone assignments reference absent interiors: {absent_zone_stems}"
        )
    suppressed_nodes, seam_suppression = _door_seam_suppression_plan(sources)
    for source_index, source in enumerate(sources):
        used.update(source.document.get("extensionsUsed", []))
        required.update(source.document.get("extensionsRequired", []))
        provenance.append(
            _copy_source_into(
                output,
                binary,
                source,
                suppressed_nodes.get(source_index),
                zone_assignments.get(source.decor_stem.casefold()),
            )
        )
    if used:
        output["extensionsUsed"] = sorted(used)
    if required:
        output["extensionsRequired"] = sorted(required)
    for name in ARRAY_NAMES:
        if name in output and not output[name]:
            del output[name]

    output["extras"] = {
        "omikron": {
            "$schema": COMPOSITION_REPORT_SCHEMA,
            "level": "Anekbah",
            "composition": "source-faithful exterior plus IAM-selected interiors",
            "sourceCount": len(sources),
            "interiorCount": len(sources) - 1,
            "authoredPlacement": (
                "exterior-connected source parents are identity transforms; teleport-local "
                "interior parents receive inspection-zone translations; all OD3X child "
                "node transforms remain unchanged"
            ),
            "sourceDocuments": provenance,
            "doorSeamSuppression": copy.deepcopy(seam_suppression),
            "teleportInspectionZones": copy.deepcopy(zone_report),
        }
    }
    glb = _pack_glb(output, bytes(binary))
    validated = converter.validate_glb_bytes(glb)
    _validate_document_indices(validated, len(binary), "composed GLB")
    renderable_meshes = _renderable_mesh_indices(validated)
    stats = {
        "sources": len(sources),
        "interiors": len(sources) - 1,
        "objects": len(validated.get("nodes", [])),
        "sourceGroupNodes": len(validated["scenes"][0].get("nodes", [])),
        "meshes": len(validated.get("meshes", [])),
        "renderableMeshes": len(renderable_meshes),
        "primitives": sum(
            len(mesh.get("primitives", [])) for mesh in validated.get("meshes", [])
        ),
        "triangles": _triangle_count(validated),
        "renderableTriangles": _triangle_count_for_meshes(
            validated, renderable_meshes
        ),
        "materials": len(validated.get("materials", [])),
        "images": len(validated.get("images", [])),
        "textures": len(validated.get("textures", [])),
        "cameras": len(validated.get("cameras", [])),
        "lights": len(_lights(validated)),
        "bufferViews": len(validated.get("bufferViews", [])),
        "accessors": len(validated.get("accessors", [])),
        "binaryBytes": len(binary),
        "outputBytes": len(glb),
        "doorSeamSuppression": seam_suppression,
        "teleportInspectionZones": copy.deepcopy(zone_report),
    }
    return glb, stats


def compose(
    base_glb: Path,
    interior_report_or_directory: Path,
    output_glb: Path,
    *,
    report_path: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    output = output_glb.expanduser().resolve()
    composition_report = (
        report_path.expanduser().resolve()
        if report_path is not None
        else output.with_suffix(".composition.json")
    )
    existing = [path for path in (output, composition_report) if path.exists()]
    if existing and not overwrite:
        raise CompositionError(
            "output already exists (use --overwrite): " + ", ".join(map(str, existing))
        )
    interior_report_path, interior_report, entries = _manifest_sources(
        interior_report_or_directory
    )
    base = _parse_glb(base_glb, "base", "Anekbah")
    base_source_name = (
        base.document.get("extras", {}).get("omikron", {}).get("source3do", "")
    )
    if str(base_source_name).casefold() != "anekbah.3do":
        raise CompositionError(
            f"base GLB does not identify Anekbah.3DO in root provenance: {base_source_name!r}"
        )
    sources = [base]
    for stem, path, entry in entries:
        source = _parse_glb(path, "interior", stem, entry)
        source_name = (
            source.document.get("extras", {}).get("omikron", {}).get("source3do", "")
        )
        if str(source_name).casefold() != f"{stem}.3do".casefold():
            raise CompositionError(
                f"interior {stem} GLB provenance names {source_name!r}"
            )
        sources.append(source)
    _casefold_unique((source.decor_stem for source in sources), "composition source")
    expected_interior_lights = int(
        interior_report.get("totals", {}).get("explicitLights", -1)
    )
    actual_interior_lights = sum(
        len(_lights(source.document)) for source in sources[1:]
    )
    if actual_interior_lights != expected_interior_lights:
        raise CompositionError(
            "interior explicit-light total differs from its manifest: "
            f"{actual_interior_lights} != {expected_interior_lights}"
        )
    _zone_layout_path, _zone_layout, zone_assignments, zone_report = (
        _load_anekbah_zone_layout(sources)
    )
    glb, stats = merge_sources(
        sources,
        zone_assignments=zone_assignments,
        zone_report=zone_report,
    )
    seam_suppression = stats.pop("doorSeamSuppression")
    teleport_zones = stats.pop("teleportInspectionZones")
    output_record = {
        "file": str(output),
        "bytes": len(glb),
        "sha256": _sha256_bytes(glb),
        "valid": True,
    }
    report: dict[str, Any] = {
        "$schema": COMPOSITION_REPORT_SCHEMA,
        "level": "Anekbah",
        "valid": True,
        "method": "direct GLB JSON/BIN merge; no Blender import/export round trip",
        "authoredPlacement": (
            "connected source roots stay at identity; eight teleport-local interiors use "
            "documented parent translations; original node transforms remain unchanged"
        ),
        "base": {
            "file": str(base.path),
            "bytes": len(base.data),
            "sha256": base.sha256,
        },
        "interiorManifest": {
            "file": str(interior_report_path),
            "bytes": interior_report_path.stat().st_size,
            "sha256": _sha256_path(interior_report_path),
            "schema": interior_report.get("$schema"),
            "selection": copy.deepcopy(interior_report.get("selection")),
            "resolvedEntries": len(entries),
            "explicitLights": actual_interior_lights,
            "undecodedMeshLights": interior_report.get("totals", {}).get(
                "undecodedMeshLights"
            ),
            "entryOrder": [stem for stem, _path, _entry in entries],
        },
        "sources": [
            {
                "index": index,
                "role": source.role,
                "decorStem": source.decor_stem,
                "file": str(source.path),
                "bytes": len(source.data),
                "sha256": source.sha256,
            }
            for index, source in enumerate(sources)
        ],
        "doorSeamSuppression": seam_suppression,
        "teleportInspectionZones": teleport_zones,
        "statistics": stats,
        "output": output_record,
        "verification": {
            "builtInGlbValidation": True,
            "allIndicesRebasedAndRangeChecked": True,
            "sourceHashesCheckedAgainstInteriorManifest": True,
            "noRemainingRenderableDoorSeams": seam_suppression[
                "assertionPassed"
            ],
            "teleportZonesDoNotOverlap": teleport_zones["assertionPassed"],
            "relocatedTeleportInteriors": teleport_zones["relocatedInteriors"],
            "supportedExtensions": sorted(SUPPORTED_EXTENSIONS),
        },
    }
    _atomic_write(output, glb)
    _atomic_write(
        composition_report,
        (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    return report


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge Anekbah.glb with manifest-selected interior GLBs"
    )
    parser.add_argument("base_glb", type=Path)
    parser.add_argument(
        "interior_report_or_directory",
        type=Path,
        help=(
            "anekbah_interiors_report.json, its parent directory, or its glb directory"
        ),
    )
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        report = compose(
            args.base_glb,
            args.interior_report_or_directory,
            args.output,
            report_path=args.report,
            overwrite=args.overwrite,
        )
        print(
            json.dumps(
                {
                    "valid": report["valid"],
                    **report["statistics"],
                    "output": report["output"]["file"],
                    "sha256": report["output"]["sha256"],
                },
                indent=2,
            )
        )
        return 0
    except (OSError, CompositionError, converter.FormatError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
