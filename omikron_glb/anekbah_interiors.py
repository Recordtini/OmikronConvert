#!/usr/bin/env python3
"""Discover and convert Anekbah's authored interior DECORS layer.

The game's IAM/AREAS.TAG names every area while IAM/AREA stores an index of
area records.  Each record's decor slot identifies the .3DO/.3DT pair used for
that room.  Anekbah interiors are authored in the district's global coordinate
space, so their converted GLBs can be composed with the exterior without an
invented placement transform.

The default selection excludes the already-converted exterior, two outdoor
sub-areas, and two mutually exclusive same-space state variants.  Missing
referenced assets remain explicit in the report rather than being silently
replaced.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Any, Sequence

import omikron_glb as converter


DEFAULT_REPORT_NAME = "anekbah_interiors_report.json"
INTERIOR_REPORT_SCHEMA = "omikron-anekbah-interiors-build-v2"
AREA_DECOR_SLOT_OFFSET = 0x58
AREA_RESOURCE_NAME_BYTES = 9
DEFAULT_EXCLUSIONS = {
    "anekbah": "base exterior is converted separately",
    "atoit": "outdoor rooftop combat area, not an interior",
    "aimpasse": "outdoor alley area, not an interior",
    "acsgrotl": "alternate-light duplicate of the Gandhar cave",
    "abetsy": (
        "Betsy-specific state of the Shunabku apartment shell; importing both "
        "at once would overlap and z-fight"
    ),
}


class InteriorsError(ValueError):
    """Raised when IAM metadata or the requested interior build is invalid."""


@dataclasses.dataclass(frozen=True)
class AreaReference:
    index: int
    label: str
    archive_offset: int
    archive_length: int
    decor_stem: str


@dataclasses.dataclass(frozen=True)
class InteriorAsset:
    decor_stem: str
    model_path: Path
    texture_path: Path
    areas: tuple[AreaReference, ...]


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


def _read_area_tags(path: Path) -> dict[int, str]:
    try:
        lines = path.read_text(encoding="cp1252").splitlines()
    except UnicodeDecodeError as error:
        raise InteriorsError(f"cannot decode area tags {path}: {error}") from error
    tags: dict[int, str] = {}
    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("["):
            continue
        if "=" not in stripped:
            raise InteriorsError(f"invalid AREAS.TAG line {line_number}: {line!r}")
        raw_index, label = stripped.split("=", 1)
        try:
            index = int(raw_index)
        except ValueError as error:
            raise InteriorsError(
                f"invalid AREAS.TAG index on line {line_number}: {raw_index!r}"
            ) from error
        if index in tags:
            raise InteriorsError(f"duplicate AREAS.TAG index {index}")
        tags[index] = label.strip()
    if not tags:
        raise InteriorsError(f"no area tags found in {path}")
    expected = list(range(max(tags) + 1))
    if sorted(tags) != expected:
        raise InteriorsError("AREAS.TAG indices are not contiguous from zero")
    return tags


def _decode_resource_name(payload: bytes) -> str:
    raw = payload.split(b"\0", 1)[0]
    return raw.decode("cp850", errors="replace").strip()


def _read_area_references(game_root: Path) -> list[AreaReference]:
    tags_path = game_root / "IAM" / "AREAS.TAG"
    archive_path = game_root / "IAM" / "AREA"
    if not tags_path.is_file() or not archive_path.is_file():
        raise InteriorsError("game root is missing IAM/AREAS.TAG or IAM/AREA")
    tags = _read_area_tags(tags_path)
    archive = archive_path.read_bytes()
    table_size = len(tags) * 8
    if table_size > len(archive):
        raise InteriorsError("IAM/AREA index table is truncated")

    references: list[AreaReference] = []
    for index, label in tags.items():
        offset, length = struct.unpack_from("<II", archive, index * 8)
        if length <= 0 or offset < table_size or offset + length > len(archive):
            raise InteriorsError(
                f"invalid IAM/AREA entry {index}: offset=0x{offset:X}, length=0x{length:X}"
            )
        required = AREA_DECOR_SLOT_OFFSET + AREA_RESOURCE_NAME_BYTES
        if length < required:
            raise InteriorsError(f"IAM/AREA entry {index} is too short for its decor slot")
        stem = _decode_resource_name(
            archive[
                offset + AREA_DECOR_SLOT_OFFSET :
                offset + AREA_DECOR_SLOT_OFFSET + AREA_RESOURCE_NAME_BYTES
            ]
        )
        references.append(
            AreaReference(
                index=index,
                label=label,
                archive_offset=offset,
                archive_length=length,
                decor_stem=stem,
            )
        )
    return references


def _decor_file_map(directory: Path, suffix: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in directory.iterdir():
        if path.is_file() and path.suffix.casefold() == suffix.casefold():
            key = path.stem.casefold()
            if key in result:
                raise InteriorsError(f"case-insensitive duplicate decor asset: {path.name}")
            result[key] = path
    return result


def discover_interiors(
    game_root: Path,
    *,
    include_outdoor: bool = False,
    include_alternate_states: bool = False,
) -> tuple[list[InteriorAsset], list[dict[str, Any]], list[dict[str, Any]]]:
    root = game_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise InteriorsError(f"game root is not a directory: {root}")
    references = _read_area_references(root)
    decor_directory = root / "MESHES" / "DECORS"
    if not decor_directory.is_dir():
        raise InteriorsError(f"DECORS directory does not exist: {decor_directory}")
    models = _decor_file_map(decor_directory, ".3DO")
    textures = _decor_file_map(decor_directory, ".3DT")

    exclusions = dict(DEFAULT_EXCLUSIONS)
    if include_outdoor:
        exclusions.pop("atoit", None)
        exclusions.pop("aimpasse", None)
    if include_alternate_states:
        exclusions.pop("acsgrotl", None)
        exclusions.pop("abetsy", None)

    grouped: dict[str, list[AreaReference]] = {}
    original_stem: dict[str, str] = {}
    excluded: list[dict[str, Any]] = []
    for reference in references:
        if "anekbah" not in reference.label.casefold():
            continue
        if not reference.decor_stem:
            excluded.append(
                {
                    "areaIndex": reference.index,
                    "areaLabel": reference.label,
                    "decorStem": "",
                    "reason": "area has no decor resource",
                }
            )
            continue
        key = reference.decor_stem.casefold()
        if key in exclusions:
            excluded.append(
                {
                    "areaIndex": reference.index,
                    "areaLabel": reference.label,
                    "decorStem": reference.decor_stem,
                    "reason": exclusions[key],
                }
            )
            continue
        grouped.setdefault(key, []).append(reference)
        original_stem.setdefault(key, reference.decor_stem)

    assets: list[InteriorAsset] = []
    missing: list[dict[str, Any]] = []
    for key in sorted(grouped):
        model_path = models.get(key)
        texture_path = textures.get(key)
        areas = tuple(grouped[key])
        if model_path is None or texture_path is None:
            missing.append(
                {
                    "decorStem": original_stem[key],
                    "areaReferences": [dataclasses.asdict(area) for area in areas],
                    "missingModel": model_path is None,
                    "missingTexture": texture_path is None,
                }
            )
            continue
        assets.append(
            InteriorAsset(
                decor_stem=model_path.stem,
                model_path=model_path,
                texture_path=texture_path,
                areas=areas,
            )
        )
    return assets, excluded, missing


def _scene_bounds(
    scene: converter.Scene3DO, scale: float
) -> tuple[list[float], list[float]]:
    minimum = [math.inf, math.inf, math.inf]
    maximum = [-math.inf, -math.inf, -math.inf]
    for mesh in scene.meshes:
        if mesh.hidden_in_game or mesh.num_triangles + mesh.num_rectangles == 0:
            continue
        for vertex in scene.vertices[
            mesh.vertex_start : mesh.vertex_start + mesh.num_vertices
        ]:
            game_position = tuple(
                vertex.position[axis] + mesh.position[axis] for axis in range(3)
            )
            position = converter._transform_vec3(game_position, scale)
            for axis in range(3):
                minimum[axis] = min(minimum[axis], position[axis])
                maximum[axis] = max(maximum[axis], position[axis])
    if not all(math.isfinite(value) for value in (*minimum, *maximum)):
        return [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
    return minimum, maximum


def _od3x_light_counts(path: Path) -> dict[str, int]:
    with path.open("rb") as stream:
        header = stream.read(0x120)
    if len(header) < 0x120 or header[:4] != b"OD3X":
        raise InteriorsError(f"invalid OD3X header while reading lights: {path}")
    total_lights, mesh_lights, explicit_lights = struct.unpack_from(
        "<3I", header, 0x114
    )
    if total_lights != mesh_lights + explicit_lights:
        raise InteriorsError(
            f"OD3X light counts disagree for {path.name}: "
            f"{total_lights} != {mesh_lights} + {explicit_lights}"
        )
    return {
        "totalLights": total_lights,
        "meshLights": mesh_lights,
        "explicitLights": explicit_lights,
    }


def inspect_interiors(
    game_root: Path,
    *,
    include_outdoor: bool = False,
    include_alternate_states: bool = False,
) -> dict[str, Any]:
    root = game_root.expanduser().resolve(strict=True)
    assets, excluded, missing = discover_interiors(
        root,
        include_outdoor=include_outdoor,
        include_alternate_states=include_alternate_states,
    )
    interior_records: list[dict[str, Any]] = []
    total_lights = 0
    mesh_lights = 0
    explicit_lights = 0
    interiors_with_explicit_lights = 0
    for asset in assets:
        lighting = _od3x_light_counts(asset.model_path)
        total_lights += lighting["totalLights"]
        mesh_lights += lighting["meshLights"]
        explicit_lights += lighting["explicitLights"]
        interiors_with_explicit_lights += int(lighting["explicitLights"] > 0)
        interior_records.append(
            {
                "decorStem": asset.decor_stem,
                "model": str(asset.model_path),
                "texture": str(asset.texture_path),
                "areaReferences": [
                    dataclasses.asdict(area) for area in asset.areas
                ],
                "sourceLighting": lighting,
            }
        )
    return {
        "$schema": "omikron-anekbah-interiors-inspection-v2",
        "level": "Anekbah",
        "valid": True,
        "gameRoot": str(root),
        "selection": {
            "rule": "AREAS.TAG label contains Anekbah; unique non-excluded decor stems",
            "includeOutdoor": include_outdoor,
            "includeAlternateStates": include_alternate_states,
            "resolvedUniqueInteriors": len(assets),
            "excludedAreaReferences": len(excluded),
            "missingUniqueInteriors": len(missing),
        },
        "sourceLighting": {
            "totalLights": total_lights,
            "meshLights": mesh_lights,
            "explicitLights": explicit_lights,
            "interiorsWithExplicitLights": interiors_with_explicit_lights,
            "interiorsWithoutExplicitLights": (
                len(assets) - interiors_with_explicit_lights
            ),
            "decodedExportPolicy": (
                "explicit 304-byte records are emitted; mesh-light semantics remain "
                "unresolved and are count-only"
            ),
        },
        "interiors": interior_records,
        "excluded": excluded,
        "missing": missing,
    }


def convert_interiors(
    game_root: Path,
    output_directory: Path,
    options: converter.ConversionOptions,
    *,
    overwrite: bool,
    include_outdoor: bool = False,
    include_alternate_states: bool = False,
) -> dict[str, Any]:
    if options.include_cameras:
        raise InteriorsError(
            "interior composition exports must omit authored cameras"
        )
    if not options.include_lights:
        raise InteriorsError(
            "canonical interior composition exports must retain decoded explicit "
            "lights; baked vertex lighting remains in COLOR_0 to avoid double-lighting"
        )
    root = game_root.expanduser().resolve(strict=True)
    assets, excluded, missing = discover_interiors(
        root,
        include_outdoor=include_outdoor,
        include_alternate_states=include_alternate_states,
    )
    output = output_directory.expanduser().resolve()
    glb_directory = output / "glb"
    report_path = output / DEFAULT_REPORT_NAME
    targets = [glb_directory / f"{asset.decor_stem}.glb" for asset in assets]
    existing = [path for path in (*targets, report_path) if path.exists()]
    if existing and not overwrite:
        shown = ", ".join(str(path) for path in existing[:3])
        suffix = " ..." if len(existing) > 3 else ""
        raise InteriorsError(f"output already exists (use --overwrite): {shown}{suffix}")
    glb_directory.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "$schema": INTERIOR_REPORT_SCHEMA,
        "level": "Anekbah",
        "valid": True,
        "authoredPlacement": "global Anekbah coordinates; no composition transform",
        "gameRoot": str(root),
        "outputDirectory": str(output),
        "conversionOptions": dataclasses.asdict(options),
        "lightingPolicy": {
            "worldSurfaces": (
                "baked texture multiplied by COLOR_0 via KHR_materials_unlit"
            ),
            "explicitLights": (
                "retain every decoded 304-byte source light as KHR_lights_punctual"
            ),
            "effectOnBakedWorld": (
                "none; unlit materials prevent a second lighting pass"
            ),
            "undecodedMeshLights": (
                "counted from the OD3X header and preserved in reports, but their "
                "record semantics are not yet decoded"
            ),
        },
        "selection": {
            "rule": "AREAS.TAG label contains Anekbah; unique non-excluded decor stems",
            "includeOutdoor": include_outdoor,
            "includeAlternateStates": include_alternate_states,
            "resolvedUniqueInteriors": len(assets),
            "excludedAreaReferences": len(excluded),
            "missingUniqueInteriors": len(missing),
        },
        "excluded": excluded,
        "missing": missing,
        "interiors": [],
    }

    aggregate_minimum = [math.inf, math.inf, math.inf]
    aggregate_maximum = [-math.inf, -math.inf, -math.inf]
    totals = {
        "sourceMeshes": 0,
        "emittedMeshes": 0,
        "emittedTriangles": 0,
        "sourceMaterials": 0,
        "embeddedImages": 0,
        "explicitLights": 0,
        "undecodedMeshLights": 0,
        "outputBytes": 0,
    }
    for index, asset in enumerate(assets, 1):
        scene = converter.parse_3do(asset.model_path)
        textures = converter.decode_3dt(scene, asset.texture_path)
        glb_path = glb_directory / f"{asset.decor_stem}.glb"
        glb, stats = converter.build_glb(scene, textures, asset.texture_path, options)
        if int(stats["explicitLights"]) != scene.header.num_lights:
            raise InteriorsError(
                f"interior {asset.decor_stem} emitted "
                f"{stats['explicitLights']} explicit lights, expected "
                f"{scene.header.num_lights}"
            )
        converter.validate_glb_bytes(glb)
        _atomic_write(glb_path, glb)
        minimum, maximum = _scene_bounds(scene, options.scale)
        for axis in range(3):
            aggregate_minimum[axis] = min(aggregate_minimum[axis], minimum[axis])
            aggregate_maximum[axis] = max(aggregate_maximum[axis], maximum[axis])
        for key in totals:
            totals[key] += int(stats[key])
        report["interiors"].append(
            {
                "index": index,
                "decorStem": asset.decor_stem,
                "areaReferences": [dataclasses.asdict(area) for area in asset.areas],
                "source": {
                    "model": str(asset.model_path),
                    "modelBytes": asset.model_path.stat().st_size,
                    "modelSha256": _sha256_path(asset.model_path),
                    "texture": str(asset.texture_path),
                    "textureBytes": asset.texture_path.stat().st_size,
                    "textureSha256": _sha256_path(asset.texture_path),
                },
                "boundsMeters": {"minimum": minimum, "maximum": maximum},
                "sourceLighting": {
                    "totalLights": scene.header.total_lights,
                    "meshLights": scene.header.num_mesh_lights,
                    "explicitLights": scene.header.num_lights,
                },
                "conversion": stats,
                "output": {
                    "file": str(glb_path.resolve()),
                    "bytes": len(glb),
                    "sha256": hashlib.sha256(glb).hexdigest(),
                    "valid": True,
                },
            }
        )
        print(
            f"[{index:02d}/{len(assets):02d}] {asset.decor_stem}: "
            f"{stats['emittedMeshes']} meshes, {stats['emittedTriangles']} triangles, "
            f"{stats['explicitLights']} explicit lights"
        )

    report["totals"] = {
        **totals,
        "sourceTotalLights": (
            totals["explicitLights"] + totals["undecodedMeshLights"]
        ),
        "interiors": len(assets),
        "boundsMeters": {
            "minimum": aggregate_minimum,
            "maximum": aggregate_maximum,
        },
    }
    _atomic_write(
        report_path,
        (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    return report


def _conversion_options(args: argparse.Namespace) -> converter.ConversionOptions:
    return converter.ConversionOptions(
        scale=args.scale,
        lighting="baked",
        include_hidden=args.include_hidden,
        include_cameras=False,
        include_lights=True,
        texture_filter=args.texture_filter,
        light_intensity_scale=args.light_intensity_scale,
        camera_aspect_ratio=4.0 / 3.0,
    )


def _add_selection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--include-outdoor", action="store_true")
    parser.add_argument("--include-alternate-states", action="store_true")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover and convert Anekbah's IAM-referenced interior DECORS"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("game_root", type=Path)
    _add_selection_options(inspect_parser)

    convert_parser = subparsers.add_parser("convert")
    convert_parser.add_argument("game_root", type=Path)
    convert_parser.add_argument("output_directory", type=Path)
    convert_parser.add_argument("--overwrite", action="store_true")
    convert_parser.add_argument("--include-hidden", action="store_true")
    convert_parser.add_argument("--scale", type=float, default=0.025)
    convert_parser.add_argument(
        "--texture-filter", choices=("linear", "nearest"), default="linear"
    )
    convert_parser.add_argument(
        "--light-intensity-scale",
        type=float,
        default=100.0,
        help=(
            "best-effort source-intensity to glTF candela multiplier for decoded "
            "explicit lights (default: 100)"
        ),
    )
    _add_selection_options(convert_parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            report = inspect_interiors(
                args.game_root,
                include_outdoor=args.include_outdoor,
                include_alternate_states=args.include_alternate_states,
            )
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0
        report = convert_interiors(
            args.game_root,
            args.output_directory,
            _conversion_options(args),
            overwrite=args.overwrite,
            include_outdoor=args.include_outdoor,
            include_alternate_states=args.include_alternate_states,
        )
        print(
            json.dumps(
                {
                    "valid": report["valid"],
                    "interiors": report["totals"]["interiors"],
                    "meshes": report["totals"]["emittedMeshes"],
                    "triangles": report["totals"]["emittedTriangles"],
                    "explicitLights": report["totals"]["explicitLights"],
                    "undecodedMeshLights": report["totals"][
                        "undecodedMeshLights"
                    ],
                    "outputBytes": report["totals"]["outputBytes"],
                    "report": str(
                        (Path(report["outputDirectory"]) / DEFAULT_REPORT_NAME).resolve()
                    ),
                },
                indent=2,
            )
        )
        return 0
    except (OSError, InteriorsError, converter.FormatError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
