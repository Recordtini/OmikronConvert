#!/usr/bin/env python3
"""Extract Anekbah's embedded effect assets and convert them to separate GLBs.

The installed ``SCPTDATA/anekbah.SCX`` contains three verified OD3X/texture
pairs.  Their offsets and lengths live in ``anekbah_composition.json`` rather
than in this program, so extraction remains auditable and install-relative.
Only the declared model and texture byte ranges are copied; script data and
the 12-byte SCX blob descriptors are not included in the extracted files.

This module has no third-party dependencies.  It delegates OD3X parsing,
texture decoding, GLB conversion, and structural GLB validation to the sibling
``omikron_glb.py`` converter.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
import sys
from typing import Any, Sequence

import omikron_glb as converter


EXPECTED_EFFECT_KEYS = ("smoke", "glow", "explosion")
EXPECTED_DESCRIPTOR_LAYOUT = (
    "self_offset_u32",
    "model_3do_length_u32",
    "texture_3dt_length_u32",
)
DEFAULT_MANIFEST = Path(__file__).with_name("anekbah_composition.json")
REPORT_NAME = "anekbah_effects_report.json"


class EffectsError(ValueError):
    """Raised when the composition manifest or embedded SCX data is invalid."""


@dataclasses.dataclass(frozen=True)
class EffectSpec:
    key: str
    external_name: str
    descriptor_offset: int
    model_offset: int
    model_length: int
    texture_offset: int
    texture_length: int
    end_exclusive: int
    material: str
    embedded_bitmap: str
    classification: str


@dataclasses.dataclass(frozen=True)
class ValidatedEffect:
    spec: EffectSpec
    model_bytes: bytes
    texture_bytes: bytes
    descriptor_values: tuple[int, int, int]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EffectsError(f"invalid JSON manifest {path}: {error}") from error
    if not isinstance(value, dict):
        raise EffectsError("composition manifest root must be a JSON object")
    return value, raw


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise EffectsError(f"{label} must be an integer or base-prefixed string")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value, 0)
        except ValueError as error:
            raise EffectsError(f"{label} is not an integer: {value!r}") from error
    else:
        raise EffectsError(f"{label} must be an integer or base-prefixed string")
    if result < 0:
        raise EffectsError(f"{label} must not be negative")
    return result


def _text(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise EffectsError(f"{label}.{key} must be a non-empty string")
    return value


def _effect_spec(value: Any, index: int) -> EffectSpec:
    if not isinstance(value, dict):
        raise EffectsError(f"effects[{index}] must be an object")
    label = f"effects[{index}]"
    key = _text(value, "key", label)
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", key):
        raise EffectsError(f"{label}.key is not a safe output name: {key!r}")
    return EffectSpec(
        key=key,
        external_name=_text(value, "external_name", label),
        descriptor_offset=_integer(value.get("blob_descriptor_offset"), f"{label}.blob_descriptor_offset"),
        model_offset=_integer(value.get("model_offset"), f"{label}.model_offset"),
        model_length=_integer(value.get("model_length"), f"{label}.model_length"),
        texture_offset=_integer(value.get("texture_offset"), f"{label}.texture_offset"),
        texture_length=_integer(value.get("texture_length"), f"{label}.texture_length"),
        end_exclusive=_integer(value.get("blob_end_exclusive"), f"{label}.blob_end_exclusive"),
        material=_text(value, "material", label),
        embedded_bitmap=_text(value, "embedded_bitmap", label),
        classification=_text(value, "classification", label),
    )


def _load_specs(manifest: dict[str, Any]) -> tuple[str, list[EffectSpec]]:
    if manifest.get("$schema") != "omikron-composition-manifest-v1":
        raise EffectsError("unsupported or missing composition manifest schema")
    if manifest.get("level") != "Anekbah":
        raise EffectsError("composition manifest is not for Anekbah")
    if manifest.get("path_base") != "game_install_root":
        raise EffectsError("composition paths must be relative to game_install_root")

    section = manifest.get("scx_embedded_effects")
    if not isinstance(section, dict):
        raise EffectsError("manifest is missing scx_embedded_effects")
    container = _text(section, "container", "scx_embedded_effects")
    source_files = manifest.get("source_files")
    if not isinstance(source_files, dict):
        raise EffectsError("manifest source_files must be an object")
    source_script = source_files.get("script")
    if source_script != container:
        raise EffectsError("SCX container does not match source_files.script")
    if tuple(section.get("blob_descriptor_layout", ())) != EXPECTED_DESCRIPTOR_LAYOUT:
        raise EffectsError("unexpected SCX blob descriptor layout")
    raw_effects = section.get("effects")
    if not isinstance(raw_effects, list):
        raise EffectsError("scx_embedded_effects.effects must be an array")
    if section.get("external_scene_count") != len(raw_effects):
        raise EffectsError("external_scene_count does not match the effect array")
    specs = [_effect_spec(value, index) for index, value in enumerate(raw_effects)]
    keys = tuple(spec.key for spec in specs)
    if keys != EXPECTED_EFFECT_KEYS:
        raise EffectsError(
            f"expected the verified effects {EXPECTED_EFFECT_KEYS}, found {keys}"
        )
    return container, specs


def _resolve_install_relative(game_root: Path, posix_path: str) -> Path:
    relative = PurePosixPath(posix_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or any(":" in part for part in relative.parts)
    ):
        raise EffectsError(f"unsafe install-relative path: {posix_path!r}")
    candidate = (game_root / Path(*relative.parts)).resolve(strict=True)
    try:
        candidate.relative_to(game_root)
    except ValueError as error:
        raise EffectsError(f"install-relative path escapes the game root: {posix_path!r}") from error
    return candidate


def _validate_effects(data: bytes, specs: Sequence[EffectSpec]) -> list[ValidatedEffect]:
    validated: list[ValidatedEffect] = []
    previous_end = -1
    for spec in specs:
        if spec.model_length == 0 or spec.texture_length == 0:
            raise EffectsError(f"{spec.key}: model and texture lengths must be positive")
        if spec.descriptor_offset < previous_end:
            raise EffectsError(f"{spec.key}: embedded blob overlaps the previous blob")
        if spec.model_offset != spec.descriptor_offset + 12:
            raise EffectsError(f"{spec.key}: model does not immediately follow its descriptor")
        if spec.texture_offset != spec.model_offset + spec.model_length:
            raise EffectsError(f"{spec.key}: texture does not immediately follow its model")
        if spec.end_exclusive != spec.texture_offset + spec.texture_length:
            raise EffectsError(f"{spec.key}: declared blob end does not match its lengths")
        if spec.end_exclusive > len(data):
            raise EffectsError(
                f"{spec.key}: blob ends at 0x{spec.end_exclusive:X}, "
                f"beyond SCX size 0x{len(data):X}"
            )

        descriptor = struct.unpack_from("<III", data, spec.descriptor_offset)
        expected_descriptor = (
            spec.descriptor_offset,
            spec.model_length,
            spec.texture_length,
        )
        if descriptor != expected_descriptor:
            raise EffectsError(
                f"{spec.key}: SCX descriptor {descriptor!r} does not match "
                f"the manifest {expected_descriptor!r}"
            )
        model = data[spec.model_offset : spec.texture_offset]
        texture = data[spec.texture_offset : spec.end_exclusive]
        if len(model) != spec.model_length or len(texture) != spec.texture_length:
            raise EffectsError(f"{spec.key}: extracted byte counts do not match the descriptor")
        if model[:4] != b"OD3X":
            raise EffectsError(f"{spec.key}: embedded model does not start with OD3X magic")
        if len(model) < 12:
            raise EffectsError(f"{spec.key}: embedded OD3X header is truncated")
        version = struct.unpack_from("<II", model, 4)
        if version not in {(3, 44), (4, 44)}:
            raise EffectsError(f"{spec.key}: unsupported embedded OD3X version {version[0]}.{version[1]}")

        validated.append(
            ValidatedEffect(
                spec=spec,
                model_bytes=model,
                texture_bytes=texture,
                descriptor_values=descriptor,
            )
        )
        previous_end = spec.end_exclusive
    return validated


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _glb_verification(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    document = converter.validate_glb_bytes(data)
    return {
        "validator": "omikron_glb.validate_glb_bytes",
        "file": str(path.resolve()),
        "bytes": len(data),
        "sha256": _sha256(data),
        "meshes": len(document.get("meshes", [])),
        "nodes": len(document.get("nodes", [])),
        "materials": len(document.get("materials", [])),
        "images": len(document.get("images", [])),
        "accessors": len(document.get("accessors", [])),
        "extensionsUsed": document.get("extensionsUsed", []),
        "valid": True,
    }


def inspect_install(game_root: Path, manifest_path: Path) -> dict[str, Any]:
    root = game_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise EffectsError(f"game root is not a directory: {root}")
    manifest_file = manifest_path.expanduser().resolve(strict=True)
    manifest, manifest_bytes = _read_json(manifest_file)
    container_relative, specs = _load_specs(manifest)
    container = _resolve_install_relative(root, container_relative)
    data = container.read_bytes()
    effects = _validate_effects(data, specs)
    return {
        "$schema": "omikron-anekbah-effects-inspection-v1",
        "level": "Anekbah",
        "valid": True,
        "gameRoot": str(root),
        "manifest": {
            "file": str(manifest_file),
            "bytes": len(manifest_bytes),
            "sha256": _sha256(manifest_bytes),
        },
        "container": {
            "installRelativePath": container_relative,
            "file": str(container),
            "bytes": len(data),
            "sha256": _sha256(data),
        },
        "effects": [
            {
                "key": effect.spec.key,
                "externalName": effect.spec.external_name,
                "classification": effect.spec.classification,
                "material": effect.spec.material,
                "embeddedBitmap": effect.spec.embedded_bitmap,
                "descriptorOffset": f"0x{effect.spec.descriptor_offset:X}",
                "descriptorValues": [f"0x{value:X}" for value in effect.descriptor_values],
                "modelRange": [
                    f"0x{effect.spec.model_offset:X}",
                    f"0x{effect.spec.texture_offset:X}",
                ],
                "modelBytes": len(effect.model_bytes),
                "modelSha256": _sha256(effect.model_bytes),
                "textureRange": [
                    f"0x{effect.spec.texture_offset:X}",
                    f"0x{effect.spec.end_exclusive:X}",
                ],
                "textureBytes": len(effect.texture_bytes),
                "textureSha256": _sha256(effect.texture_bytes),
                "od3xVersion": f"{struct.unpack_from('<I', effect.model_bytes, 4)[0]}."
                f"{struct.unpack_from('<I', effect.model_bytes, 8)[0]}",
            }
            for effect in effects
        ],
    }


def extract_and_convert(
    game_root: Path,
    output_directory: Path,
    manifest_path: Path,
    options: converter.ConversionOptions,
    overwrite: bool,
) -> dict[str, Any]:
    inspection = inspect_install(game_root, manifest_path)
    root = Path(inspection["gameRoot"])
    manifest_file = Path(inspection["manifest"]["file"])
    manifest, _ = _read_json(manifest_file)
    container_relative, specs = _load_specs(manifest)
    container = _resolve_install_relative(root, container_relative)
    source_data = container.read_bytes()
    effects = _validate_effects(source_data, specs)

    output = output_directory.expanduser().resolve()
    sources = output / "sources"
    targets: list[Path] = [output / REPORT_NAME]
    for effect in effects:
        stem = f"Anekbah_{effect.spec.key}"
        targets.extend((sources / f"{stem}.3DO", sources / f"{stem}.3DT", output / f"{stem}.glb"))
    existing = [path for path in targets if path.exists()]
    if existing and not overwrite:
        shown = ", ".join(str(path) for path in existing[:3])
        suffix = " ..." if len(existing) > 3 else ""
        raise EffectsError(f"output already exists (use --overwrite): {shown}{suffix}")
    output.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "$schema": "omikron-anekbah-effects-extraction-v1",
        "level": "Anekbah",
        "valid": True,
        "separateEffects": True,
        "sceneInstancing": False,
        "gameRoot": inspection["gameRoot"],
        "manifest": inspection["manifest"],
        "container": inspection["container"],
        "outputDirectory": str(output),
        "conversionOptions": dataclasses.asdict(options),
        "effects": [],
    }
    for effect in effects:
        stem = f"Anekbah_{effect.spec.key}"
        model_path = sources / f"{stem}.3DO"
        texture_path = sources / f"{stem}.3DT"
        glb_path = output / f"{stem}.glb"
        _atomic_write(model_path, effect.model_bytes)
        _atomic_write(texture_path, effect.texture_bytes)

        # Full OD3X section validation occurs here before conversion.  Texture
        # length/content is validated by decode_3dt inside convert_file.
        source_inspection = converter.inspect_scene(model_path)
        conversion = converter.convert_file(model_path, glb_path, options)
        verification = _glb_verification(glb_path)
        report["effects"].append(
            {
                "key": effect.spec.key,
                "externalName": effect.spec.external_name,
                "classification": effect.spec.classification,
                "material": effect.spec.material,
                "embeddedBitmap": effect.spec.embedded_bitmap,
                "sourceRanges": {
                    "descriptorOffset": f"0x{effect.spec.descriptor_offset:X}",
                    "descriptorValues": [
                        f"0x{value:X}" for value in effect.descriptor_values
                    ],
                    "model": [
                        f"0x{effect.spec.model_offset:X}",
                        f"0x{effect.spec.texture_offset:X}",
                    ],
                    "texture": [
                        f"0x{effect.spec.texture_offset:X}",
                        f"0x{effect.spec.end_exclusive:X}",
                    ],
                },
                "extraction": {
                    "descriptorBytesCopied": 0,
                    "model": {
                        "file": str(model_path.resolve()),
                        "bytes": len(effect.model_bytes),
                        "sha256": _sha256(effect.model_bytes),
                    },
                    "texture": {
                        "file": str(texture_path.resolve()),
                        "bytes": len(effect.texture_bytes),
                        "sha256": _sha256(effect.texture_bytes),
                    },
                },
                "sourceInspection": source_inspection,
                "conversion": conversion,
                "verification": verification,
            }
        )

    report_path = output / REPORT_NAME
    report_bytes = (json.dumps(report, indent=2) + "\n").encode("utf-8")
    _atomic_write(report_path, report_bytes)
    return report


def _conversion_options(args: argparse.Namespace) -> converter.ConversionOptions:
    return converter.ConversionOptions(
        scale=args.scale,
        lighting=args.lighting,
        include_hidden=args.include_hidden,
        include_cameras=not args.no_cameras,
        include_lights=not args.no_lights,
        texture_filter=args.texture_filter,
        light_intensity_scale=args.light_intensity_scale,
        camera_aspect_ratio=args.camera_aspect_ratio,
    )


def _add_manifest_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"composition manifest (default: {DEFAULT_MANIFEST.name} beside this script)",
    )


def _command_inspect(args: argparse.Namespace) -> int:
    print(json.dumps(inspect_install(args.game_root, args.manifest), indent=2))
    return 0


def _command_extract(args: argparse.Namespace) -> int:
    report = extract_and_convert(
        args.game_root,
        args.output_directory,
        args.manifest,
        _conversion_options(args),
        args.overwrite,
    )
    summary = {
        "valid": report["valid"],
        "effects": [
            {
                "key": effect["key"],
                "glb": effect["verification"]["file"],
                "sha256": effect["verification"]["sha256"],
            }
            for effect in report["effects"]
        ],
        "report": str((Path(report["outputDirectory"]) / REPORT_NAME).resolve()),
    }
    print(json.dumps(summary, indent=2))
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract and convert Anekbah's three embedded SCX effects"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="validate and describe the three embedded SCX effect blobs"
    )
    inspect_parser.add_argument("game_root", type=Path, help="Omikron installation root")
    _add_manifest_argument(inspect_parser)
    inspect_parser.set_defaults(func=_command_inspect)

    extract_parser = subparsers.add_parser(
        "extract", help="extract the three pairs and convert each to its own GLB"
    )
    extract_parser.add_argument("game_root", type=Path, help="Omikron installation root")
    extract_parser.add_argument("output_directory", type=Path, help="destination directory")
    _add_manifest_argument(extract_parser)
    extract_parser.add_argument("--overwrite", action="store_true")
    extract_parser.add_argument("--scale", type=float, default=0.025)
    extract_parser.add_argument("--lighting", choices=("baked", "dynamic"), default="baked")
    extract_parser.add_argument("--include-hidden", action="store_true")
    extract_parser.add_argument("--no-cameras", action="store_true")
    extract_parser.add_argument("--no-lights", action="store_true")
    extract_parser.add_argument("--texture-filter", choices=("linear", "nearest"), default="linear")
    extract_parser.add_argument("--light-intensity-scale", type=float, default=100.0)
    extract_parser.add_argument("--camera-aspect-ratio", type=float, default=4.0 / 3.0)
    extract_parser.set_defaults(func=_command_extract)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, EffectsError, converter.FormatError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
