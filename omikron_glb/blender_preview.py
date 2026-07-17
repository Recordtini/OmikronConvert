#!/usr/bin/env python3
"""Blender-side GLB smoke test and source-camera preview renderer.

Run with Blender, not the system Python:
  blender --background --factory-startup --python blender_preview.py -- input.glb output_dir [output.blend] [sky.glb|sky.png] [effects_dir] [interiors_dir]
"""

from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path
import re
import sys

import bmesh
import bpy
from mathutils import Matrix, Vector


ANEKBAH_HAZE_START_METERS = 50.0
ANEKBAH_HAZE_END_METERS = 135.0
ANEKBAH_HAZE_TRANSITION_METERS = (
    ANEKBAH_HAZE_END_METERS - ANEKBAH_HAZE_START_METERS
)
ANEKBAH_HAZE_EXPONENT = 1.6
ANEKBAH_GLOW_EMISSION_STRENGTH = 4.0
ANEKBAH_GLOW_SATURATION = 0.05
ANEKBAH_GLOW_VALUE = 1.35
ANEKBAH_DOOR_SEAM_ANCHOR_TOLERANCE_METERS = 0.05
ANEKBAH_DOOR_SEAM_AUDIT_MAX_MATCH_DELTA_METERS = 0.0318975
ANEKBAH_DOOR_SEAM_AUDIT_NEXT_REJECTED_DELTA_METERS = 18.13
ANEKBAH_ZONE_LAYOUT_FILE = "anekbah_zone_layout.json"
ANEKBAH_SMOKE_FADE_START_METERS = 25.0
ANEKBAH_SMOKE_FADE_END_METERS = 180.0
ANEKBAH_SMOKE_FADE_TRANSITION_METERS = (
    ANEKBAH_SMOKE_FADE_END_METERS - ANEKBAH_SMOKE_FADE_START_METERS
)


def arguments() -> tuple[
    Path,
    Path,
    Path | None,
    Path | None,
    Path | None,
    Path | None,
]:
    usage = (
        "expected: -- INPUT.glb OUTPUT_DIR [OUTPUT.blend] "
        "[SKY.glb|SKY.png] [EFFECTS_DIR] [INTERIORS_DIR]"
    )
    try:
        marker = sys.argv.index("--")
    except ValueError as error:
        raise SystemExit(usage) from error
    values = sys.argv[marker + 1 :]
    if len(values) not in (2, 3, 4, 5, 6):
        raise SystemExit(usage)
    return (
        Path(values[0]),
        Path(values[1]),
        Path(values[2]) if len(values) >= 3 else None,
        Path(values[3]) if len(values) >= 4 else None,
        Path(values[4]) if len(values) >= 5 else None,
        Path(values[5]) if len(values) == 6 else None,
    )


def scene_bounds() -> tuple[Vector, Vector]:
    points: list[Vector] = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
    if not points:
        return Vector((0, 0, 0)), Vector((0, 0, 0))
    return (
        Vector(tuple(min(point[axis] for point in points) for axis in range(3))),
        Vector(tuple(max(point[axis] for point in points) for axis in range(3))),
    )


def configure_world(
    scene: bpy.types.Scene, first_arrival: bool
) -> dict[str, object]:
    if scene.world is None:
        scene.world = bpy.data.worlds.new("Omikron Preview World")
    world = scene.world
    world.use_nodes = True
    nodes = world.node_tree.nodes
    nodes.clear()
    background = nodes.new("ShaderNodeBackground")
    output = nodes.new("ShaderNodeOutputWorld")
    world.node_tree.links.new(background.outputs["Background"], output.inputs["Surface"])
    if first_arrival:
        # Sampled from the user-selected original-PC Anekbah reference frame:
        # sky RGB is about (77,115,149) sRGB.  Nearby geometry keeps its baked
        # material color; the reference blue-grey atmosphere takes over only
        # across the longer middle-to-far distance range.
        background.inputs["Color"].default_value = (0.0742, 0.1714, 0.3005, 1.0)
        return {
            "mode": "Anekbah delayed steep first-arrival blue distance haze",
            "referenceSrgb": [77, 115, 149],
            "linearRgb": [0.0742, 0.1714, 0.3005],
            "method": "powered smoothstep camera-distance material fog",
            "factorFormula": (
                "t=clamp((viewDistance-startMeters)/(endMeters-startMeters),0,1); "
                "smooth=t*t*(3-2*t); factor=pow(smooth,exponent)"
            ),
            "startMeters": ANEKBAH_HAZE_START_METERS,
            "endMeters": ANEKBAH_HAZE_END_METERS,
            "transitionMeters": ANEKBAH_HAZE_TRANSITION_METERS,
            "exponent": ANEKBAH_HAZE_EXPONENT,
            "factorSamples": {
                f"{distance:g}m": round(
                    _first_arrival_haze_factor_at_distance(distance), 6
                )
                for distance in (25.0, 50.0, 70.0, 90.0, 110.0, 135.0, 180.0)
            },
            "behavior": (
                "near geometry remains unfogged through the start distance; "
                "the powered smoothstep then transitions more steeply to the "
                "reference blue-grey background"
            ),
        }
    background.inputs["Color"].default_value = (0.002, 0.006, 0.025, 1.0)
    background.inputs["Strength"].default_value = 1.0
    return {"mode": "neutral dark validation background"}


def _build_first_arrival_haze_factor(
    nodes: bpy.types.Nodes,
    links: bpy.types.NodeLinks,
    view_distance: bpy.types.NodeSocket,
    name_prefix: str,
) -> bpy.types.NodeSocket:
    """Build the auditable powered-smoothstep first-arrival fog curve."""
    fog_offset = nodes.new("ShaderNodeMath")
    fog_offset.operation = "SUBTRACT"
    fog_offset.inputs[1].default_value = ANEKBAH_HAZE_START_METERS
    fog_offset.label = f"Distance - {ANEKBAH_HAZE_START_METERS:g} m"
    fog_offset.name = f"{name_prefix}_OFFSET"

    normalized = nodes.new("ShaderNodeMath")
    normalized.operation = "DIVIDE"
    normalized.inputs[1].default_value = ANEKBAH_HAZE_TRANSITION_METERS
    normalized.use_clamp = True
    normalized.label = "t = clamped normalized distance"
    normalized.name = f"{name_prefix}_T"

    squared = nodes.new("ShaderNodeMath")
    squared.operation = "MULTIPLY"
    squared.label = "t squared"
    squared.name = f"{name_prefix}_T2"

    doubled = nodes.new("ShaderNodeMath")
    doubled.operation = "MULTIPLY"
    doubled.inputs[1].default_value = 2.0
    doubled.label = "2t"
    doubled.name = f"{name_prefix}_2T"

    three_minus_double = nodes.new("ShaderNodeMath")
    three_minus_double.operation = "SUBTRACT"
    three_minus_double.inputs[0].default_value = 3.0
    three_minus_double.label = "3 - 2t"
    three_minus_double.name = f"{name_prefix}_3_MINUS_2T"

    smoothstep = nodes.new("ShaderNodeMath")
    smoothstep.operation = "MULTIPLY"
    smoothstep.label = "smoothstep = t squared x (3 - 2t)"
    smoothstep.name = f"{name_prefix}_SMOOTHSTEP"

    powered = nodes.new("ShaderNodeMath")
    powered.operation = "POWER"
    powered.inputs[1].default_value = ANEKBAH_HAZE_EXPONENT
    powered.use_clamp = True
    powered.label = f"smoothstep^{ANEKBAH_HAZE_EXPONENT:g}"
    powered.name = f"{name_prefix}_POWERED"

    links.new(view_distance, fog_offset.inputs[0])
    links.new(fog_offset.outputs[0], normalized.inputs[0])
    links.new(normalized.outputs[0], squared.inputs[0])
    links.new(normalized.outputs[0], squared.inputs[1])
    links.new(normalized.outputs[0], doubled.inputs[0])
    links.new(doubled.outputs[0], three_minus_double.inputs[1])
    links.new(squared.outputs[0], smoothstep.inputs[0])
    links.new(three_minus_double.outputs[0], smoothstep.inputs[1])
    links.new(smoothstep.outputs[0], powered.inputs[0])
    return powered.outputs[0]


def _first_arrival_haze_factor_at_distance(distance_meters: float) -> float:
    """Evaluate the documented node curve for validation/report samples."""
    t = max(
        0.0,
        min(
            1.0,
            (distance_meters - ANEKBAH_HAZE_START_METERS)
            / ANEKBAH_HAZE_TRANSITION_METERS,
        ),
    )
    smooth = t * t * (3.0 - 2.0 * t)
    return smooth**ANEKBAH_HAZE_EXPONENT


def apply_first_arrival_distance_haze(materials: list[bpy.types.Material]) -> int:
    fog_color = (0.0742, 0.1714, 0.3005, 1.0)
    modified = 0
    for material in materials:
        # Transparent persistent effects receive an alpha-preserving emissive
        # distance fade when imported.  Mixing an opaque fog shader over them
        # here would expose the rectangular billboard at long range.
        if material.get("omikronPersistentEffectMaterial"):
            continue
        if not material.use_nodes or material.node_tree is None:
            continue
        nodes = material.node_tree.nodes
        output = next((node for node in nodes if node.type == "OUTPUT_MATERIAL"), None)
        if output is None or not output.inputs["Surface"].links:
            continue
        source_link = output.inputs["Surface"].links[0]
        source_shader = source_link.from_socket
        material.node_tree.links.remove(source_link)
        camera_data = nodes.new("ShaderNodeCameraData")
        camera_data.label = "Omikron camera distance"
        fog_emission = nodes.new("ShaderNodeEmission")
        fog_emission.inputs["Color"].default_value = fog_color
        mix = nodes.new("ShaderNodeMixShader")
        mix.label = "Omikron first-arrival distance haze"
        fog_factor = _build_first_arrival_haze_factor(
            nodes,
            material.node_tree.links,
            camera_data.outputs["View Distance"],
            "OMIKRON_HAZE",
        )
        material.node_tree.links.new(fog_factor, mix.inputs[0])
        material.node_tree.links.new(source_shader, mix.inputs[1])
        material.node_tree.links.new(fog_emission.outputs["Emission"], mix.inputs[2])
        material.node_tree.links.new(mix.outputs[0], output.inputs["Surface"])
        modified += 1
    return modified


def add_spherical_preview_sky(
    minimum: Vector, maximum: Vector, sky_path: Path
) -> bpy.types.Object:
    if not sky_path.is_file():
        raise FileNotFoundError(f"sky texture does not exist: {sky_path}")
    center = (minimum + maximum) * 0.5
    radius = max((maximum - minimum).length * 1.25, 250.0)
    bpy.ops.mesh.primitive_uv_sphere_add(
        segments=64,
        ring_count=32,
        radius=radius,
        location=center,
    )
    sky = bpy.context.object
    sky.name = "OMIKRON_PREVIEW_SKY__CIEL3"
    sky.data.name = "OMIKRON_PREVIEW_SKY__CIEL3"
    sky["omikronPreviewOnly"] = True
    sky["sourceTexture"] = str(sky_path.resolve())

    material = bpy.data.materials.new("OMIKRON_PREVIEW_SKY__CIEL3")
    material.use_nodes = True
    material.use_backface_culling = False
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")
    texture = nodes.new("ShaderNodeTexImage")
    mapping = nodes.new("ShaderNodeMapping")
    coordinates = nodes.new("ShaderNodeTexCoord")
    texture.image = bpy.data.images.load(str(sky_path.resolve()), check_existing=True)
    texture.extension = "REPEAT"
    texture.interpolation = "Linear"
    mapping.inputs["Scale"].default_value = (4.0, 2.0, 1.0)
    material.node_tree.links.new(coordinates.outputs["UV"], mapping.inputs["Vector"])
    material.node_tree.links.new(mapping.outputs["Vector"], texture.inputs["Vector"])
    material.node_tree.links.new(texture.outputs["Color"], emission.inputs["Color"])
    material.node_tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    sky.data.materials.append(material)
    return sky


def load_camera_relative_sky(
    sky_path: Path,
) -> tuple[bpy.types.Object, Matrix, dict[str, object]]:
    """Import an authored sky GLB and retain its mesh-to-camera transform.

    Omikron renders Asky as a camera-centered sky layer.  Re-parenting its exact
    source mesh with the authored relative transform reproduces that behavior in
    Blender without warping CIEL3 onto an invented sphere.
    """
    if not sky_path.is_file():
        raise FileNotFoundError(f"sky GLB does not exist: {sky_path}")
    before = set(bpy.data.objects)
    result = bpy.ops.import_scene.gltf(filepath=str(sky_path.resolve()))
    if "FINISHED" not in result:
        raise RuntimeError(f"sky glTF import failed: {result}")
    imported = [obj for obj in bpy.data.objects if obj not in before]
    meshes = [obj for obj in imported if obj.type == "MESH"]
    cameras = [obj for obj in imported if obj.type == "CAMERA"]
    if len(meshes) != 1 or len(cameras) != 1:
        raise RuntimeError(
            f"expected one sky mesh and one sky camera, got {len(meshes)} and {len(cameras)}"
        )
    sky = meshes[0]
    source_camera = cameras[0]
    relative = source_camera.matrix_world.inverted() @ sky.matrix_world
    first_arrival_grade = None
    if sky_path.stem.casefold() == "asky":
        # The game's skybox path expands this authored plane beyond its literal
        # mesh bounds and presents CIEL3 as a subdued, animated overcast field.
        # A static 1.6x expansion removes the raw plane edge at the authored 4:3
        # FOV.  World distance haze performs the subdued cloud mix at render
        # time, leaving the source sky material and CIEL3 pixels untouched.
        relative = relative @ Matrix.Scale(1.6, 4)
        first_arrival_grade = {
            "meshScale": 1.6,
            "material": "source unmodified; world haze supplies distance blend",
            "basis": "original PC first-arrival screenshot/video calibration",
            "cloudAnimation": "not yet emulated",
        }
    source_camera_name = source_camera.name
    source_camera_data = source_camera.data
    bpy.data.objects.remove(source_camera, do_unlink=True)
    if source_camera_data.users == 0:
        bpy.data.cameras.remove(source_camera_data)
    sky.name = "OMIKRON_PREVIEW_SKY__ASKY"
    sky.data.name = "OMIKRON_PREVIEW_SKY__ASKY"
    sky["omikronPreviewOnly"] = True
    sky["sourceSkyGlb"] = str(sky_path.resolve())
    return (
        sky,
        relative,
        {
            "object": sky.name,
            "source": str(sky_path.resolve()),
            "sourceCamera": source_camera_name,
            "behavior": "authored mesh parented camera-relative with skybox presentation emulation",
            "firstArrivalGrade": first_arrival_grade,
            "portableGlbGeometry": False,
        },
    )


def attach_camera_relative_sky(
    sky: bpy.types.Object, camera: bpy.types.Object, relative: Matrix
) -> None:
    sky.parent = camera
    sky.matrix_parent_inverse = Matrix.Identity(4)
    sky.matrix_basis = relative


def _matrix_rows(matrix: Matrix) -> list[list[float]]:
    return [
        [float(matrix[row][column]) for column in range(4)]
        for row in range(4)
    ]


def _vector_values(vector: Vector) -> list[float]:
    return [float(value) for value in vector]


def _file_record(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "file": str(path.resolve()),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _object_bounds(objects: list[bpy.types.Object]) -> tuple[Vector, Vector]:
    points: list[Vector] = []
    for obj in objects:
        if obj.type != "MESH":
            continue
        points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
    if not points:
        return Vector((0, 0, 0)), Vector((0, 0, 0))
    return (
        Vector(tuple(min(point[axis] for point in points) for axis in range(3))),
        Vector(tuple(max(point[axis] for point in points) for axis in range(3))),
    )


def _aabb_overlaps(
    left_minimum: Vector,
    left_maximum: Vector,
    right_minimum: Vector,
    right_maximum: Vector,
    tolerance: float = 1.0e-5,
) -> bool:
    return all(
        min(left_maximum[axis], right_maximum[axis])
        - max(left_minimum[axis], right_minimum[axis])
        > tolerance
        for axis in range(3)
    )


def _load_anekbah_zone_layout() -> tuple[Path, dict[str, object]]:
    path = Path(__file__).resolve().with_name(ANEKBAH_ZONE_LAYOUT_FILE)
    if not path.is_file():
        raise FileNotFoundError(f"Anekbah zone layout does not exist: {path}")
    layout = json.loads(path.read_text(encoding="utf-8"))
    if layout.get("$schema") != "omikron-anekbah-zone-layout-v1":
        raise RuntimeError(
            f"unsupported Anekbah zone layout schema: {layout.get('$schema')!r}"
        )
    if layout.get("level") != "Anekbah" or layout.get("enabled") is not True:
        raise RuntimeError("Anekbah zone layout is not enabled for Anekbah")
    zones = layout.get("zones")
    if not isinstance(zones, list) or not zones:
        raise RuntimeError("Anekbah zone layout has no zones")
    ids: set[str] = set()
    stems: set[str] = set()
    for zone in zones:
        zone_id = str(zone.get("id", ""))
        zone_stems = zone.get("stems")
        translation_blender = zone.get("translationBlenderMeters")
        translation_glb = zone.get("translationGlbMeters")
        if not zone_id or zone_id.casefold() in ids:
            raise RuntimeError(f"blank or duplicate Anekbah zone id: {zone_id!r}")
        ids.add(zone_id.casefold())
        if not isinstance(zone_stems, list) or not zone_stems:
            raise RuntimeError(f"Anekbah zone {zone_id} has no stems")
        for stem in zone_stems:
            key = str(stem).casefold()
            if not key or key in stems:
                raise RuntimeError(
                    f"blank or duplicate Anekbah zone stem in {zone_id}: {stem!r}"
                )
            stems.add(key)
        if not (
            isinstance(translation_blender, list)
            and len(translation_blender) == 3
            and isinstance(translation_glb, list)
            and len(translation_glb) == 3
            and all(math.isfinite(float(value)) for value in translation_blender)
            and all(math.isfinite(float(value)) for value in translation_glb)
        ):
            raise RuntimeError(f"Anekbah zone {zone_id} has invalid translations")
        expected_blender = (
            float(translation_glb[0]),
            -float(translation_glb[2]),
            float(translation_glb[1]),
        )
        if max(
            abs(float(translation_blender[index]) - expected_blender[index])
            for index in range(3)
        ) > 1.0e-8:
            raise RuntimeError(
                f"Anekbah zone {zone_id} GLB/Blender translations disagree"
            )
    return path, layout


def apply_anekbah_teleport_zones(
    base_exterior_mesh_objects: list[bpy.types.Object],
) -> dict[str, object]:
    """Move disconnected local-coordinate interiors into inspection zones.

    The layout is applied only after authored-position door seam suppression, so
    connected rooms retain their original placement and the duplicate-door audit
    still compares the unmodified source anchors.
    """
    layout_path, layout = _load_anekbah_zone_layout()
    root_name = "OMIKRON_ANEKBAH_TELEPORT_ZONES"
    if bpy.data.collections.get(root_name) is not None:
        raise RuntimeError(f"collection already exists: {root_name}")
    root_collection = bpy.data.collections.new(root_name)
    root_collection["omikronTeleportZoneRoot"] = True
    root_collection["omikronZoneLayout"] = str(layout_path)
    bpy.context.scene.collection.children.link(root_collection)

    base_minimum, base_maximum = _object_bounds(base_exterior_mesh_objects)
    zone_reports: list[dict[str, object]] = []
    relocated_stems: set[str] = set()
    final_bounds: list[tuple[str, Vector, Vector]] = []
    relocated_objects = 0
    relocated_lights = 0
    for zone_index, zone in enumerate(layout["zones"]):
        zone_id = str(zone["id"])
        stems = [str(stem) for stem in zone["stems"]]
        stem_keys = {stem.casefold() for stem in stems}
        objects = [
            obj
            for obj in bpy.context.scene.objects
            if bool(obj.get("omikronInterior"))
            and str(obj.get("omikronInteriorStem", "")).casefold() in stem_keys
        ]
        found_stems = {
            str(obj.get("omikronInteriorStem", "")).casefold() for obj in objects
        }
        if found_stems != stem_keys:
            raise RuntimeError(
                f"Anekbah zone {zone_id} stem mismatch: "
                f"expected={sorted(stem_keys)}, found={sorted(found_stems)}"
            )
        object_pointers = {obj.as_pointer() for obj in objects}
        roots = [
            obj
            for obj in objects
            if obj.parent is None or obj.parent.as_pointer() not in object_pointers
        ]
        if not roots:
            raise RuntimeError(f"Anekbah zone {zone_id} has no root objects")
        before_minimum, before_maximum = _object_bounds(objects)

        zone_collection = bpy.data.collections.new(
            f"OMIKRON_ZONE__{_safe_name_component(zone_id)}"
        )
        zone_collection["omikronTeleportZone"] = True
        zone_collection["omikronTeleportZoneId"] = zone_id
        zone_collection["omikronTeleportZoneStems"] = ",".join(stems)
        root_collection.children.link(zone_collection)
        anchor = bpy.data.objects.new(
            f"OMIKRON_ZONE_ANCHOR__{_safe_name_component(zone_id)}", None
        )
        anchor.empty_display_type = "CUBE"
        anchor.empty_display_size = 10.0
        anchor["omikronTeleportZone"] = True
        anchor["omikronTeleportZoneId"] = zone_id
        anchor["omikronTeleportZoneIndex"] = zone_index
        anchor["omikronTeleportZoneStems"] = ",".join(stems)
        anchor["omikronZoneLayout"] = str(layout_path)
        zone_collection.objects.link(anchor)
        for obj in roots:
            world_matrix = obj.matrix_world.copy()
            obj.parent = anchor
            obj.matrix_world = world_matrix
        translation = Vector(
            tuple(float(value) for value in zone["translationBlenderMeters"])
        )
        anchor.location = translation
        bpy.context.view_layer.update()

        after_minimum, after_maximum = _object_bounds(objects)
        declared_bounds = zone.get("finalBoundsBlenderMeters", {})
        declared_minimum = Vector(
            tuple(float(value) for value in declared_bounds.get("minimum", []))
        )
        declared_maximum = Vector(
            tuple(float(value) for value in declared_bounds.get("maximum", []))
        )
        if len(declared_minimum) != 3 or len(declared_maximum) != 3:
            raise RuntimeError(f"Anekbah zone {zone_id} has invalid final bounds")
        bounds_delta = max(
            *(abs(after_minimum[index] - declared_minimum[index]) for index in range(3)),
            *(abs(after_maximum[index] - declared_maximum[index]) for index in range(3)),
        )
        if bounds_delta > 0.01:
            raise RuntimeError(
                f"Anekbah zone {zone_id} final bounds differ by {bounds_delta} m"
            )
        if _aabb_overlaps(after_minimum, after_maximum, base_minimum, base_maximum):
            raise RuntimeError(f"Anekbah zone {zone_id} still overlaps the exterior")
        for prior_id, prior_minimum, prior_maximum in final_bounds:
            if _aabb_overlaps(
                after_minimum, after_maximum, prior_minimum, prior_maximum
            ):
                raise RuntimeError(
                    f"Anekbah zones {prior_id} and {zone_id} overlap after placement"
                )
        final_bounds.append((zone_id, after_minimum, after_maximum))
        relocated_stems.update(stems)
        relocated_objects += len(objects)
        zone_light_objects = sum(obj.type == "LIGHT" for obj in objects)
        relocated_lights += zone_light_objects
        zone_reports.append(
            {
                "id": zone_id,
                "label": zone.get("label"),
                "stems": stems,
                "collection": zone_collection.name,
                "anchor": anchor.name,
                "rootObjects": len(roots),
                "objects": len(objects),
                "lightObjects": zone_light_objects,
                "translationBlenderMeters": _vector_values(translation),
                "translationGlbMeters": zone["translationGlbMeters"],
                "authoredBoundsBlenderMeters": {
                    "minimum": _vector_values(before_minimum),
                    "maximum": _vector_values(before_maximum),
                },
                "composedBoundsBlenderMeters": {
                    "minimum": _vector_values(after_minimum),
                    "maximum": _vector_values(after_maximum),
                },
                "declaredBoundsMaximumDeltaMeters": bounds_delta,
                "overlapsExterior": False,
            }
        )

    expected_relocated = int(
        layout.get("selection", {}).get("relocatedInteriors", -1)
    )
    if len(relocated_stems) != expected_relocated:
        raise RuntimeError(
            "Anekbah zone layout relocated stem count mismatch: "
            f"{len(relocated_stems)} != {expected_relocated}"
        )
    expected_relocated_lights = int(
        layout.get("selection", {}).get("relocatedExplicitLights", -1)
    )
    if relocated_lights != expected_relocated_lights:
        raise RuntimeError(
            "Anekbah zone layout relocated explicit-light count mismatch: "
            f"{relocated_lights} != {expected_relocated_lights}"
        )
    return {
        "enabled": True,
        "layout": _file_record(layout_path),
        "schema": layout["$schema"],
        "purpose": layout.get("purpose"),
        "selection": layout.get("selection"),
        "placement": layout.get("placement"),
        "rootCollection": root_collection.name,
        "zones": zone_reports,
        "zoneCount": len(zone_reports),
        "relocatedInteriors": len(relocated_stems),
        "relocatedStems": sorted(relocated_stems, key=str.casefold),
        "relocatedObjects": relocated_objects,
        "relocatedLightObjects": relocated_lights,
        "baseExteriorBoundsBlenderMeters": {
            "minimum": _vector_values(base_minimum),
            "maximum": _vector_values(base_maximum),
        },
        "zoneExteriorOverlapCount": 0,
        "interZoneOverlapCount": 0,
        "assertionPassed": True,
        "applicationOrder": (
            "after authored-position door seam suppression and before effects, "
            "haze, camera clipping, rendering, and save"
        ),
    }


def _safe_name_component(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in value
    ).strip("_")
    return cleaned or "UNNAMED"


def _tag_and_prefix_interior_id(
    datablock: bpy.types.ID,
    name_prefix: str,
    decor_stem: str,
    source_glb: Path,
    report_index: int,
    datablock_kind: str,
) -> None:
    original_name = datablock.name
    datablock["omikronInterior"] = True
    datablock["omikronInteriorStem"] = decor_stem
    datablock["omikronInteriorSourceGlb"] = str(source_glb.resolve())
    datablock["omikronInteriorReportIndex"] = report_index
    datablock["omikronInteriorDatablockKind"] = datablock_kind
    datablock["omikronInteriorSourceName"] = original_name
    datablock.name = f"{name_prefix}__{original_name}"


def _restore_imported_punctual_light_ranges(
    lights: list[bpy.types.Light],
    scale: float,
) -> dict[str, object]:
    if not math.isfinite(scale) or scale <= 0:
        raise RuntimeError(f"invalid interior light scale: {scale}")
    ranges: list[float] = []
    for light in lights:
        source = light.get("omikron")
        if source is None or not hasattr(source, "get"):
            raise RuntimeError(
                f"imported interior light has no Omikron source extras: {light.name}"
            )
        far_game_units = float(source.get("farAttenuationGameUnits", math.nan))
        if not math.isfinite(far_game_units) or far_game_units <= 0:
            raise RuntimeError(
                f"imported interior light has invalid far attenuation: {light.name}"
            )
        range_meters = far_game_units * scale
        light.use_custom_distance = True
        light.cutoff_distance = range_meters
        light["omikronRangeRestoredByPreview"] = True
        light["omikronRangeMeters"] = range_meters
        ranges.append(range_meters)
    return {
        "lights": len(lights),
        "source": "light.omikron.farAttenuationGameUnits",
        "scaleMetersPerGameUnit": scale,
        "customDistanceEnabled": len(lights),
        "minimumMeters": min(ranges) if ranges else None,
        "maximumMeters": max(ranges) if ranges else None,
        "reason": (
            "Blender 5.2 imports the KHR light but leaves use_custom_distance "
            "disabled; restore the authored cutoff without changing baked/unlit decor"
        ),
    }


def _matrix_maximum_component_delta(left: Matrix, right: Matrix) -> float:
    return max(
        abs(float(left[row][column]) - float(right[row][column]))
        for row in range(4)
        for column in range(4)
    )


def _material_has_first_arrival_haze(material: bpy.types.Material) -> bool:
    return bool(
        material.use_nodes
        and material.node_tree is not None
        and any(
            node.type == "MIX_SHADER"
            and node.label == "Omikron first-arrival distance haze"
            for node in material.node_tree.nodes
        )
    )


def import_anekbah_interiors(
    interiors_directory: Path,
) -> tuple[list[bpy.types.Material], dict[str, object]]:
    """Import only the GLBs enumerated by the audited Anekbah interior report.

    Every source object retains its imported world matrix.  Collection moves,
    naming, and provenance tags are presentation metadata only; no composition
    transform is introduced.
    """
    report_path = interiors_directory / "anekbah_interiors_report.json"
    if not report_path.is_file():
        raise FileNotFoundError(
            f"Anekbah interior report does not exist: {report_path}"
        )
    manifest = json.loads(report_path.read_text(encoding="utf-8"))
    if manifest.get("$schema") != "omikron-anekbah-interiors-build-v2":
        raise RuntimeError(
            f"unsupported Anekbah interior report schema: {manifest.get('$schema')!r}"
        )
    if str(manifest.get("level", "")).casefold() != "anekbah":
        raise RuntimeError(
            f"interior report level is not Anekbah: {manifest.get('level')!r}"
        )
    if manifest.get("valid") is not True:
        raise RuntimeError("Anekbah interior report is not marked valid")
    entries = manifest.get("interiors")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("Anekbah interior report has no interiors list")
    declared_selection_count = int(
        manifest.get("selection", {}).get("resolvedUniqueInteriors", -1)
    )
    declared_totals_count = int(manifest.get("totals", {}).get("interiors", -1))
    if declared_selection_count != len(entries) or declared_totals_count != len(entries):
        raise RuntimeError(
            "Anekbah interior report count mismatch: "
            f"entries={len(entries)}, selection={declared_selection_count}, "
            f"totals={declared_totals_count}"
        )
    conversion_options = manifest.get("conversionOptions", {})
    if conversion_options.get("include_cameras") is not False:
        raise RuntimeError("Anekbah interiors must be converted without cameras")
    if conversion_options.get("include_lights") is not True:
        raise RuntimeError(
            "Anekbah interiors must retain their decoded explicit lights"
        )

    stems = [str(entry.get("decorStem", "")) for entry in entries]
    folded_stems = [stem.casefold() for stem in stems]
    if any(not stem for stem in stems) or len(set(folded_stems)) != len(stems):
        raise RuntimeError("Anekbah interior report has blank or duplicate decor stems")
    declared_files = [
        str(entry.get("output", {}).get("file", "")) for entry in entries
    ]
    if any(not value for value in declared_files):
        raise RuntimeError("Anekbah interior report has a blank output.file")
    folded_files = [str(Path(value).resolve()).casefold() for value in declared_files]
    if len(set(folded_files)) != len(folded_files):
        raise RuntimeError("Anekbah interior report has duplicate output.file entries")

    root_collection_name = "OMIKRON_ANEKBAH_INTERIORS"
    if bpy.data.collections.get(root_collection_name) is not None:
        raise RuntimeError(f"collection already exists: {root_collection_name}")
    root_collection = bpy.data.collections.new(root_collection_name)
    root_collection["omikronInteriorRoot"] = True
    root_collection["omikronInteriorManifest"] = str(report_path.resolve())
    root_collection["omikronAuthoredPlacement"] = (
        "global Anekbah coordinates; no composition transform"
    )
    bpy.context.scene.collection.children.link(root_collection)

    imported_materials: list[bpy.types.Material] = []
    all_imported_objects: list[bpy.types.Object] = []
    asset_reports: list[dict[str, object]] = []
    ordered_verified_hashes: list[str] = []
    total_objects = 0
    total_mesh_objects = 0
    total_meshes = 0
    total_materials = 0
    total_images = 0
    total_lights = 0
    total_unique_triangles = 0
    total_instanced_triangles = 0
    total_verified_bytes = 0
    maximum_matrix_delta = 0.0

    for list_position, entry in enumerate(entries):
        decor_stem = str(entry["decorStem"])
        report_index = int(entry.get("index", list_position + 1))
        output = entry.get("output", {})
        if output.get("valid") is not True:
            raise RuntimeError(
                f"interior {decor_stem} output is not marked valid in the report"
            )
        source_glb = Path(str(output["file"]))
        if not source_glb.is_absolute():
            source_glb = report_path.parent / source_glb
        if not source_glb.is_file():
            raise FileNotFoundError(
                f"manifest-listed Anekbah interior GLB does not exist: {source_glb}"
            )
        verified_output = _file_record(source_glb)
        if verified_output["bytes"] != int(output.get("bytes", -1)):
            raise RuntimeError(
                f"interior {decor_stem} byte count differs from its report: "
                f"{verified_output['bytes']} != {output.get('bytes')}"
            )
        if str(verified_output["sha256"]).casefold() != str(
            output.get("sha256", "")
        ).casefold():
            raise RuntimeError(
                f"interior {decor_stem} SHA-256 differs from its report"
            )

        before_objects = {datablock.as_pointer() for datablock in bpy.data.objects}
        before_meshes = {datablock.as_pointer() for datablock in bpy.data.meshes}
        before_materials = {
            datablock.as_pointer() for datablock in bpy.data.materials
        }
        before_images = {datablock.as_pointer() for datablock in bpy.data.images}
        before_cameras = {datablock.as_pointer() for datablock in bpy.data.cameras}
        before_lights = {datablock.as_pointer() for datablock in bpy.data.lights}
        result = bpy.ops.import_scene.gltf(filepath=str(source_glb.resolve()))
        if "FINISHED" not in result:
            raise RuntimeError(
                f"Anekbah interior glTF import failed for {source_glb}: {result}"
            )
        objects = [
            datablock
            for datablock in bpy.data.objects
            if datablock.as_pointer() not in before_objects
        ]
        meshes = [
            datablock
            for datablock in bpy.data.meshes
            if datablock.as_pointer() not in before_meshes
        ]
        materials = [
            datablock
            for datablock in bpy.data.materials
            if datablock.as_pointer() not in before_materials
        ]
        images = [
            datablock
            for datablock in bpy.data.images
            if datablock.as_pointer() not in before_images
        ]
        new_cameras = [
            datablock
            for datablock in bpy.data.cameras
            if datablock.as_pointer() not in before_cameras
        ]
        new_lights = [
            datablock
            for datablock in bpy.data.lights
            if datablock.as_pointer() not in before_lights
        ]
        forbidden_objects = [obj for obj in objects if obj.type == "CAMERA"]
        unsupported_objects = [
            obj for obj in objects if obj.type not in {"MESH", "EMPTY", "LIGHT"}
        ]
        if forbidden_objects or new_cameras:
            raise RuntimeError(
                f"interior {decor_stem} attempted to import cameras: "
                f"objects={[obj.name for obj in forbidden_objects]}, "
                f"cameraData={[item.name for item in new_cameras]}"
            )
        if unsupported_objects:
            raise RuntimeError(
                f"interior {decor_stem} imported unsupported object types: "
                f"{[(obj.name, obj.type) for obj in unsupported_objects]}"
            )
        mesh_objects = [obj for obj in objects if obj.type == "MESH"]
        light_objects = [obj for obj in objects if obj.type == "LIGHT"]
        if not mesh_objects:
            raise RuntimeError(f"interior {decor_stem} imported no mesh objects")
        declared_conversion = entry.get("conversion", {})
        declared_explicit_lights = int(
            declared_conversion.get("explicitLights", -1)
        )
        if (
            len(light_objects) != declared_explicit_lights
            or len(new_lights) != declared_explicit_lights
        ):
            raise RuntimeError(
                f"interior {decor_stem} explicit-light count mismatch: "
                f"objects={len(light_objects)}, datablocks={len(new_lights)}, "
                f"declared={declared_explicit_lights}"
            )
        new_light_pointers = {light.as_pointer() for light in new_lights}
        reused_light_data = [
            obj.data
            for obj in light_objects
            if obj.data.as_pointer() not in new_light_pointers
        ]
        if reused_light_data:
            raise RuntimeError(
                f"interior {decor_stem} reused pre-existing light datablocks: "
                f"{[light.name for light in reused_light_data]}"
            )
        light_range_report = _restore_imported_punctual_light_ranges(
            new_lights,
            float(conversion_options.get("scale", 0.025)),
        )

        material_pointers = {material.as_pointer() for material in materials}
        referenced_materials = {
            slot.material
            for obj in mesh_objects
            for slot in obj.material_slots
            if slot.material is not None
        }
        reused_materials = [
            material
            for material in referenced_materials
            if material.as_pointer() not in material_pointers
        ]
        if reused_materials:
            raise RuntimeError(
                f"interior {decor_stem} reused pre-existing material datablocks: "
                f"{[material.name for material in reused_materials]}"
            )
        referenced_images = {
            node.image
            for material in materials
            if material.use_nodes and material.node_tree is not None
            for node in material.node_tree.nodes
            if node.type == "TEX_IMAGE" and node.image is not None
        }
        image_pointers = {image.as_pointer() for image in images}
        reused_images = [
            image
            for image in referenced_images
            if image.as_pointer() not in image_pointers
        ]
        if reused_images:
            raise RuntimeError(
                f"interior {decor_stem} reused pre-existing image datablocks: "
                f"{[image.name for image in reused_images]}"
            )

        bpy.context.view_layer.update()
        matrices_before_organization = {
            obj.as_pointer(): obj.matrix_world.copy() for obj in objects
        }
        name_prefix = f"OMIKRON_INTERIOR__{_safe_name_component(decor_stem)}"
        child_collection = bpy.data.collections.new(name_prefix)
        child_collection["omikronInterior"] = True
        child_collection["omikronInteriorStem"] = decor_stem
        child_collection["omikronInteriorSourceGlb"] = str(source_glb.resolve())
        child_collection["omikronInteriorReportIndex"] = report_index
        root_collection.children.link(child_collection)
        for obj in objects:
            for source_collection in tuple(obj.users_collection):
                source_collection.objects.unlink(obj)
            child_collection.objects.link(obj)

        for image in images:
            _tag_and_prefix_interior_id(
                image,
                name_prefix,
                decor_stem,
                source_glb,
                report_index,
                "image",
            )
        for material in materials:
            _tag_and_prefix_interior_id(
                material,
                name_prefix,
                decor_stem,
                source_glb,
                report_index,
                "material",
            )
        for mesh in meshes:
            _tag_and_prefix_interior_id(
                mesh,
                name_prefix,
                decor_stem,
                source_glb,
                report_index,
                "mesh",
            )
        for light in new_lights:
            _tag_and_prefix_interior_id(
                light,
                name_prefix,
                decor_stem,
                source_glb,
                report_index,
                "light",
            )
        for obj in objects:
            _tag_and_prefix_interior_id(
                obj,
                name_prefix,
                decor_stem,
                source_glb,
                report_index,
                "object",
            )

        bpy.context.view_layer.update()
        asset_matrix_delta = max(
            _matrix_maximum_component_delta(
                matrices_before_organization[obj.as_pointer()], obj.matrix_world
            )
            for obj in objects
        )
        if asset_matrix_delta > 1.0e-6:
            raise RuntimeError(
                f"interior {decor_stem} world matrix changed during organization: "
                f"maximum component delta {asset_matrix_delta}"
            )
        for mesh in meshes:
            mesh.calc_loop_triangles()
        unique_triangles = sum(len(mesh.loop_triangles) for mesh in meshes)
        instanced_triangles = sum(
            len(obj.data.loop_triangles) for obj in mesh_objects
        )
        asset_minimum, asset_maximum = _object_bounds(objects)
        asset_reports.append(
            {
                "manifestListPosition": list_position,
                "reportIndex": report_index,
                "decorStem": decor_stem,
                "areaReferences": entry.get("areaReferences", []),
                "source": entry.get("source"),
                "declaredBoundsMeters": entry.get("boundsMeters"),
                "declaredConversion": declared_conversion,
                "declaredOutput": output,
                "verifiedOutput": verified_output,
                "collection": child_collection.name,
                "namePrefix": name_prefix,
                "counts": {
                    "objects": len(objects),
                    "meshObjects": len(mesh_objects),
                    "meshDatablocks": len(meshes),
                    "materials": len(materials),
                    "images": len(images),
                    "lightObjects": len(light_objects),
                    "lightDatablocks": len(new_lights),
                    "uniqueMeshDatablockTriangles": unique_triangles,
                    "instancedMeshObjectTriangles": instanced_triangles,
                    "declaredEmittedMeshes": declared_conversion.get(
                        "emittedMeshes"
                    ),
                    "declaredEmittedTriangles": declared_conversion.get(
                        "emittedTriangles"
                    ),
                },
                "importedWorldBoundsMeters": {
                    "minimum": _vector_values(asset_minimum),
                    "maximum": _vector_values(asset_maximum),
                },
                "compositionTransform": _matrix_rows(Matrix.Identity(4)),
                "maxWorldMatrixComponentDeltaAfterOrganization": (
                    asset_matrix_delta
                ),
                "importedCameras": 0,
                "importedLights": len(new_lights),
                "lightRangeRestoration": light_range_report,
            }
        )
        imported_materials.extend(materials)
        all_imported_objects.extend(objects)
        ordered_verified_hashes.append(str(verified_output["sha256"]))
        total_objects += len(objects)
        total_mesh_objects += len(mesh_objects)
        total_meshes += len(meshes)
        total_materials += len(materials)
        total_images += len(images)
        total_lights += len(new_lights)
        total_unique_triangles += unique_triangles
        total_instanced_triangles += instanced_triangles
        total_verified_bytes += int(verified_output["bytes"])
        maximum_matrix_delta = max(maximum_matrix_delta, asset_matrix_delta)

    declared_total_lights = int(
        manifest.get("totals", {}).get("explicitLights", -1)
    )
    if total_lights != declared_total_lights:
        raise RuntimeError(
            "Anekbah interior manifest explicit-light total mismatch: "
            f"imported={total_lights}, declared={declared_total_lights}"
        )
    all_minimum, all_maximum = _object_bounds(all_imported_objects)
    ordered_set_digest = hashlib.sha256(
        "\n".join(ordered_verified_hashes).encode("ascii")
    ).hexdigest()
    return imported_materials, {
        "enabled": True,
        "manifest": _file_record(report_path),
        "schema": manifest["$schema"],
        "level": manifest["level"],
        "manifestValid": manifest["valid"],
        "providedDirectory": str(interiors_directory.resolve()),
        "manifestOutputDirectory": manifest.get("outputDirectory"),
        "authoredPlacement": manifest.get("authoredPlacement"),
        "placement": (
            "import phase: each report-listed GLB is loaded once with an identity "
            "composition transform; disconnected local-coordinate sources are moved "
            "only after authored-position door seam suppression"
        ),
        "compositionTransform": _matrix_rows(Matrix.Identity(4)),
        "rootCollection": root_collection.name,
        "childCollections": len(entries),
        "selection": manifest.get("selection"),
        "conversionOptions": conversion_options,
        "excluded": manifest.get("excluded", []),
        "missing": manifest.get("missing", []),
        "manifestDeclaredTotals": manifest.get("totals"),
        "actualImportTotals": {
            "interiors": len(asset_reports),
            "objects": total_objects,
            "meshObjects": total_mesh_objects,
            "meshDatablocks": total_meshes,
            "materials": total_materials,
            "images": total_images,
            "uniqueMeshDatablockTriangles": total_unique_triangles,
            "instancedMeshObjectTriangles": total_instanced_triangles,
            "verifiedGlbBytes": total_verified_bytes,
            "importedCameras": 0,
            "importedLights": total_lights,
            "boundsMeters": {
                "minimum": _vector_values(all_minimum),
                "maximum": _vector_values(all_maximum),
            },
            "maximumWorldMatrixComponentDeltaAfterOrganization": (
                maximum_matrix_delta
            ),
        },
        "orderedGlbSetSha256": ordered_set_digest,
        "orderedGlbSetHashMethod": (
            "SHA-256 of the report-order lowercase-independent verified GLB "
            "SHA-256 strings joined by newline"
        ),
        "traceability": (
            "every imported object, mesh, light, material, and image is prefixed and "
            "tagged with decor stem, source GLB, report index, source name, and kind"
        ),
        "cameraLightPolicy": (
            "fail if an interior creates a camera or if its decoded explicit-light "
            "object/datablock counts differ from the manifest; retain source lights "
            "without letting them relight KHR_materials_unlit baked world surfaces"
        ),
        "interiors": asset_reports,
    }


def _normalized_door_source_name(value: str) -> str:
    """Return a case-insensitive source name without Blender collision suffixes.

    Blender appends suffixes such as ``.001`` when an imported interior object
    collides with an already-imported exterior object.  Repeated suffixes are
    removed because the provenance property records the name as it existed
    immediately after import, before this script applies its own prefix.
    """
    return re.sub(r"(?:\.\d{3,})+$", "", value.strip()).casefold()


def _is_door_source_name(normalized_name: str) -> bool:
    return (
        "porte" in normalized_name
        or "door" in normalized_name
        or normalized_name in {"acoultoit", "ba06eport4", "csvdorb6", "csvdorb7"}
    )


def _mesh_object_triangle_count(obj: bpy.types.Object) -> int:
    obj.data.calc_loop_triangles()
    return len(obj.data.loop_triangles)


def _door_seam_composition_counts(
    base_exterior_mesh_objects: list[bpy.types.Object],
) -> dict[str, int]:
    scene_mesh_objects = [
        obj for obj in bpy.context.scene.objects if obj.type == "MESH"
    ]
    interior_mesh_objects = [
        obj for obj in scene_mesh_objects if bool(obj.get("omikronInterior"))
    ]
    base_door_candidates = [
        obj
        for obj in base_exterior_mesh_objects
        if obj.name in bpy.context.scene.objects
        and _is_door_source_name(_normalized_door_source_name(obj.name))
    ]
    interior_door_candidates = [
        obj
        for obj in interior_mesh_objects
        if _is_door_source_name(
            _normalized_door_source_name(
                str(obj.get("omikronInteriorSourceName", obj.name))
            )
        )
    ]
    return {
        "sceneObjects": len(bpy.context.scene.objects),
        "sceneMeshObjects": len(scene_mesh_objects),
        "sceneInstancedMeshObjectTriangles": sum(
            _mesh_object_triangle_count(obj) for obj in scene_mesh_objects
        ),
        "interiorObjects": sum(
            bool(obj.get("omikronInterior")) for obj in bpy.context.scene.objects
        ),
        "interiorMeshObjects": len(interior_mesh_objects),
        "interiorInstancedMeshObjectTriangles": sum(
            _mesh_object_triangle_count(obj) for obj in interior_mesh_objects
        ),
        "baseExteriorDoorCandidates": len(base_door_candidates),
        "interiorDoorCandidates": len(interior_door_candidates),
    }


def _find_exterior_interior_door_seam_matches(
    base_exterior_mesh_objects: list[bpy.types.Object],
) -> list[tuple[bpy.types.Object, bpy.types.Object, str, float]]:
    exterior_by_name: dict[str, list[bpy.types.Object]] = {}
    for exterior in base_exterior_mesh_objects:
        if exterior.name not in bpy.context.scene.objects:
            continue
        normalized_name = _normalized_door_source_name(exterior.name)
        if _is_door_source_name(normalized_name):
            exterior_by_name.setdefault(normalized_name, []).append(exterior)

    matches: list[tuple[bpy.types.Object, bpy.types.Object, str, float]] = []
    for interior in bpy.context.scene.objects:
        if interior.type != "MESH" or not bool(interior.get("omikronInterior")):
            continue
        source_name = str(interior.get("omikronInteriorSourceName", interior.name))
        normalized_name = _normalized_door_source_name(source_name)
        if not _is_door_source_name(normalized_name):
            continue
        candidates = []
        for exterior in exterior_by_name.get(normalized_name, []):
            anchor_delta = (
                interior.matrix_world.translation - exterior.matrix_world.translation
            ).length
            if anchor_delta <= ANEKBAH_DOOR_SEAM_ANCHOR_TOLERANCE_METERS:
                candidates.append((anchor_delta, exterior))
        if candidates:
            anchor_delta, winner = min(
                candidates,
                key=lambda item: (item[0], item[1].name.casefold()),
            )
            matches.append((interior, winner, normalized_name, anchor_delta))
    return matches


def _find_interior_interior_door_seam_matches(
) -> list[tuple[bpy.types.Object, bpy.types.Object, str, float]]:
    """Find interior door losers using manifest/report order as priority.

    Every returned loser has at least one same-name, spatially coincident door
    with a strictly lower report index.  Winners are selected only from the
    retained lower-index objects, so every reported winner survives the pass.
    """
    interior_by_name: dict[str, list[bpy.types.Object]] = {}
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or not bool(obj.get("omikronInterior")):
            continue
        source_name = str(obj.get("omikronInteriorSourceName", obj.name))
        normalized_name = _normalized_door_source_name(source_name)
        if _is_door_source_name(normalized_name):
            interior_by_name.setdefault(normalized_name, []).append(obj)

    matches: list[tuple[bpy.types.Object, bpy.types.Object, str, float]] = []
    for normalized_name, objects in interior_by_name.items():
        ordered = sorted(
            objects,
            key=lambda obj: (
                int(obj.get("omikronInteriorReportIndex", -1)),
                obj.name.casefold(),
            ),
        )
        if any(int(obj.get("omikronInteriorReportIndex", -1)) < 0 for obj in ordered):
            raise RuntimeError(
                f"interior door lacks manifest/report index: {normalized_name}"
            )
        retained: list[bpy.types.Object] = []
        for loser in ordered:
            loser_index = int(loser["omikronInteriorReportIndex"])
            candidates = []
            for winner in retained:
                winner_index = int(winner["omikronInteriorReportIndex"])
                anchor_delta = (
                    loser.matrix_world.translation - winner.matrix_world.translation
                ).length
                if anchor_delta <= ANEKBAH_DOOR_SEAM_ANCHOR_TOLERANCE_METERS:
                    if winner_index == loser_index:
                        raise RuntimeError(
                            "coincident interior doors have the same manifest/report "
                            f"index {loser_index}: {winner.name}, {loser.name}"
                        )
                    if winner_index > loser_index:
                        raise RuntimeError(
                            "interior door ordering invariant failed: "
                            f"{winner_index} > {loser_index}"
                        )
                    candidates.append((winner_index, anchor_delta, winner))
            if candidates:
                _winner_index, anchor_delta, winner = min(
                    candidates,
                    key=lambda item: (
                        item[0],
                        item[1],
                        item[2].name.casefold(),
                    ),
                )
                matches.append((loser, winner, normalized_name, anchor_delta))
            else:
                retained.append(loser)
    return matches


def suppress_duplicate_interior_door_seams(
    base_exterior_mesh_objects: list[bpy.types.Object],
) -> dict[str, object]:
    """Retain one authoritative mesh for each coincident authored door.

    Phase one keeps the exterior copy.  Phase two compares only the remaining
    interior doors and keeps the copy from the lower manifest/report index.  A
    name match alone is deliberately insufficient: the original/source name
    must identify a door and the object world origins must agree within 5 cm.
    The audited maximum exterior seam-pair delta is about 3.19 cm, while the
    next rejected same-name candidate is more than 18 m away.
    """
    bpy.context.view_layer.update()
    before = _door_seam_composition_counts(base_exterior_mesh_objects)
    exterior_matches = _find_exterior_interior_door_seam_matches(
        base_exterior_mesh_objects
    )
    exterior_suppressed: list[dict[str, object]] = []
    orphan_meshes_removed = 0
    for interior, exterior, normalized_name, anchor_delta in exterior_matches:
        interior_source_name = str(
            interior.get("omikronInteriorSourceName", interior.name)
        )
        interior_name = interior.name
        interior_mesh = interior.data
        interior_anchor = interior.matrix_world.translation.copy()
        exterior_anchor = exterior.matrix_world.translation.copy()
        record = {
            "phase": "exteriorPriority",
            "interiorStem": str(interior.get("omikronInteriorStem", "")),
            "interiorObject": interior_name,
            "interiorSourceName": interior_source_name,
            "interiorSourceGlb": str(
                interior.get("omikronInteriorSourceGlb", "")
            ),
            "interiorReportIndex": int(
                interior.get("omikronInteriorReportIndex", -1)
            ),
            "normalizedDoorName": normalized_name,
            "baseExteriorWinner": exterior.name,
            "baseExteriorSourceName": exterior.name,
            "interiorWorldAnchorMeters": _vector_values(interior_anchor),
            "baseExteriorWorldAnchorMeters": _vector_values(exterior_anchor),
            "anchorDeltaMeters": anchor_delta,
            "triangleCounts": {
                "suppressedInterior": _mesh_object_triangle_count(interior),
                "retainedBaseExterior": _mesh_object_triangle_count(exterior),
            },
            "winner": {
                "kind": "baseExterior",
                "object": exterior.name,
                "sourceName": exterior.name,
                "worldAnchorMeters": _vector_values(exterior_anchor),
                "triangles": _mesh_object_triangle_count(exterior),
            },
            "loser": {
                "kind": "interior",
                "stem": str(interior.get("omikronInteriorStem", "")),
                "object": interior_name,
                "sourceName": interior_source_name,
                "sourceGlb": str(
                    interior.get("omikronInteriorSourceGlb", "")
                ),
                "reportIndex": int(
                    interior.get("omikronInteriorReportIndex", -1)
                ),
                "worldAnchorMeters": _vector_values(interior_anchor),
                "triangles": _mesh_object_triangle_count(interior),
            },
            "interiorMeshDatablock": interior_mesh.name,
            "orphanMeshRemoved": False,
        }
        bpy.data.objects.remove(interior, do_unlink=True)
        if interior_mesh.users == 0:
            bpy.data.meshes.remove(interior_mesh)
            record["orphanMeshRemoved"] = True
            orphan_meshes_removed += 1
        exterior_suppressed.append(record)

    bpy.context.view_layer.update()
    after_exterior_priority = _door_seam_composition_counts(
        base_exterior_mesh_objects
    )
    remaining_exterior_matches = _find_exterior_interior_door_seam_matches(
        base_exterior_mesh_objects
    )
    if remaining_exterior_matches:
        raise RuntimeError(
            "exterior-priority door seam suppression left matched duplicates: "
            + ", ".join(
                f"{interior.name} -> {exterior.name} ({delta:.9g} m)"
                for interior, exterior, _normalized, delta
                in remaining_exterior_matches
            )
        )

    interior_matches = _find_interior_interior_door_seam_matches()
    prepared_interior_suppressions: list[
        tuple[bpy.types.Object, bpy.types.Mesh, dict[str, object]]
    ] = []
    for loser, winner, normalized_name, anchor_delta in interior_matches:
        loser_mesh = loser.data
        loser_anchor = loser.matrix_world.translation.copy()
        winner_anchor = winner.matrix_world.translation.copy()
        loser_source_name = str(
            loser.get("omikronInteriorSourceName", loser.name)
        )
        winner_source_name = str(
            winner.get("omikronInteriorSourceName", winner.name)
        )
        loser_triangles = _mesh_object_triangle_count(loser)
        winner_triangles = _mesh_object_triangle_count(winner)
        record = {
            "phase": "interiorManifestPriority",
            "normalizedDoorName": normalized_name,
            "anchorDeltaMeters": anchor_delta,
            "winner": {
                "kind": "interior",
                "stem": str(winner.get("omikronInteriorStem", "")),
                "object": winner.name,
                "sourceName": winner_source_name,
                "sourceGlb": str(
                    winner.get("omikronInteriorSourceGlb", "")
                ),
                "reportIndex": int(winner["omikronInteriorReportIndex"]),
                "worldAnchorMeters": _vector_values(winner_anchor),
                "triangles": winner_triangles,
            },
            "loser": {
                "kind": "interior",
                "stem": str(loser.get("omikronInteriorStem", "")),
                "object": loser.name,
                "sourceName": loser_source_name,
                "sourceGlb": str(
                    loser.get("omikronInteriorSourceGlb", "")
                ),
                "reportIndex": int(loser["omikronInteriorReportIndex"]),
                "worldAnchorMeters": _vector_values(loser_anchor),
                "triangles": loser_triangles,
            },
            "triangleCounts": {
                "retainedLowerReportIndexInterior": winner_triangles,
                "suppressedHigherReportIndexInterior": loser_triangles,
            },
            "loserMeshDatablock": loser_mesh.name,
            "orphanMeshRemoved": False,
        }
        if record["winner"]["reportIndex"] >= record["loser"]["reportIndex"]:
            raise RuntimeError(
                "interior door suppression selected a non-lower report index: "
                f"{winner.name} -> {loser.name}"
            )
        prepared_interior_suppressions.append((loser, loser_mesh, record))

    interior_suppressed: list[dict[str, object]] = []
    for loser, loser_mesh, record in prepared_interior_suppressions:
        bpy.data.objects.remove(loser, do_unlink=True)
        if loser_mesh.users == 0:
            bpy.data.meshes.remove(loser_mesh)
            record["orphanMeshRemoved"] = True
            orphan_meshes_removed += 1
        interior_suppressed.append(record)

    bpy.context.view_layer.update()
    remaining_exterior_matches = _find_exterior_interior_door_seam_matches(
        base_exterior_mesh_objects
    )
    remaining_interior_matches = _find_interior_interior_door_seam_matches()
    if remaining_exterior_matches or remaining_interior_matches:
        descriptions = [
            f"exterior/interior {interior.name} -> {exterior.name} "
            f"({delta:.9g} m)"
            for interior, exterior, _normalized, delta
            in remaining_exterior_matches
        ]
        descriptions.extend(
            f"interior/interior {loser.name} -> {winner.name} "
            f"({delta:.9g} m)"
            for loser, winner, _normalized, delta in remaining_interior_matches
        )
        raise RuntimeError(
            "door seam suppression left matched duplicates: "
            + ", ".join(descriptions)
        )
    after = _door_seam_composition_counts(base_exterior_mesh_objects)

    reported_examples = ("porteg30", "ported42")
    suppressed_names = {
        str(record["normalizedDoorName"]) for record in exterior_suppressed
    }
    example_coverage = {
        example: example in suppressed_names for example in reported_examples
    }
    suppressed = exterior_suppressed + interior_suppressed
    ggrockx_suppressed = any(
        str(record["normalizedDoorName"]) == "ggrockx" for record in suppressed
    )
    if ggrockx_suppressed:
        raise RuntimeError("non-door GGrockx entered door seam suppression")
    return {
        "enabled": True,
        "policy": (
            "two-phase door-only suppression: exterior wins over coincident "
            "interior copies, then the lower manifest/report index wins among "
            "remaining coincident interior copies"
        ),
        "nameNormalization": (
            "trim whitespace, remove one or more trailing Blender .NNN collision "
            "suffixes, then Unicode casefold"
        ),
        "doorNameRestriction": (
            "normalized source name contains 'porte' or 'door', or is one of "
            "the audited portal aliases acoultoit, ba06eport4, csvdorb6, csvdorb7"
        ),
        "anchor": "object matrix_world translation in authored Anekbah meters",
        "anchorToleranceMeters": ANEKBAH_DOOR_SEAM_ANCHOR_TOLERANCE_METERS,
        "auditedMaximumMatchedAnchorDeltaMeters": (
            ANEKBAH_DOOR_SEAM_AUDIT_MAX_MATCH_DELTA_METERS
        ),
        "auditedNextRejectedSameNameAnchorDeltaMeters": (
            ANEKBAH_DOOR_SEAM_AUDIT_NEXT_REJECTED_DELTA_METERS
        ),
        "auditedExteriorInteriorMatchedPairs": 90,
        "auditedAmbiguousExteriorCandidates": 0,
        "toleranceRationale": (
            "5 cm covers every audited exterior/interior door seam pair: the "
            "largest accepted delta is 0.0318975 m, while the next rejected "
            "same-name candidate is 18.13 m away; no exterior winner is ambiguous"
        ),
        "before": before,
        "afterExteriorPriority": after_exterior_priority,
        "after": after,
        "phases": {
            "exteriorPriority": {
                "policy": "base exterior winner; interior copy removed",
                "matchedPairs": len(exterior_matches),
                "suppressedCount": len(exterior_suppressed),
                "suppressedInteriorObjects": exterior_suppressed,
                "remainingMatchedPairsAfterPhase": 0,
            },
            "interiorManifestPriority": {
                "policy": (
                    "same normalized door source name and <=0.05 m world-anchor "
                    "delta; lower manifest/report index winner"
                ),
                "auditedOriginalDisjointPairsBeforeExteriorPriority": 72,
                "auditedPairsWhoseBothCopiesExteriorPhaseRemoved": [
                    "ported06",
                    "porteg06",
                ],
                "auditedExcludedNonDoorPair": "ggrockx",
                "auditedExpectedAdditionalSuppressions": 69,
                "matchedPairs": len(interior_matches),
                "suppressedCount": len(interior_suppressed),
                "winnerLoserPairs": interior_suppressed,
                "remainingMatchedPairsAfterPhase": 0,
            },
        },
        "auditedExpectedTotalSuppressions": 159,
        "suppressedCount": len(suppressed),
        "orphanMeshDatablocksRemoved": orphan_meshes_removed,
        "suppressedInteriorObjects": suppressed,
        "remainingMatchedPairs": {
            "exteriorInterior": len(remaining_exterior_matches),
            "interiorInterior": len(remaining_interior_matches),
            "total": len(remaining_exterior_matches)
            + len(remaining_interior_matches),
        },
        "assertNoRemainingMatchedPairs": (
            not remaining_exterior_matches and not remaining_interior_matches
        ),
        "explicitNonDoorAudit": {
            "name": "GGrockx",
            "suppressed": ggrockx_suppressed,
        },
        "userReportedExamples": {
            "normalizedNames": list(reported_examples),
            "suppressed": example_coverage,
        },
    }


def _scene_totals(scene: bpy.types.Scene) -> dict[str, object]:
    object_types: dict[str, int] = {}
    mesh_objects: list[bpy.types.Object] = []
    unique_meshes: dict[int, bpy.types.Mesh] = {}
    for obj in scene.objects:
        object_types[obj.type] = object_types.get(obj.type, 0) + 1
        if obj.type == "MESH":
            mesh_objects.append(obj)
            obj.data.calc_loop_triangles()
            unique_meshes[obj.data.as_pointer()] = obj.data
    minimum, maximum = scene_bounds()
    return {
        "objects": len(scene.objects),
        "objectTypes": dict(sorted(object_types.items())),
        "meshObjects": len(mesh_objects),
        "usedMeshDatablocks": len(unique_meshes),
        "blenderMeshDatablocks": len(bpy.data.meshes),
        "materials": len(bpy.data.materials),
        "images": len(bpy.data.images),
        "lights": len(bpy.data.lights),
        "cameras": len(bpy.data.cameras),
        "collections": len(bpy.data.collections),
        "uniqueMeshDatablockTriangles": sum(
            len(mesh.loop_triangles) for mesh in unique_meshes.values()
        ),
        "instancedMeshObjectTriangles": sum(
            len(obj.data.loop_triangles) for obj in mesh_objects
        ),
        "interiorObjects": sum(
            bool(obj.get("omikronInterior")) for obj in scene.objects
        ),
        "persistentEffectObjects": sum(
            bool(obj.get("omikronPersistentEffect")) for obj in scene.objects
        ),
        "previewSkyObjects": sum(
            obj.name.startswith("OMIKRON_PREVIEW_SKY") for obj in scene.objects
        ),
        "firstArrivalHazeMaterials": sum(
            _material_has_first_arrival_haze(material)
            for material in bpy.data.materials
        ),
        "boundsMinMeters": _vector_values(minimum),
        "boundsMaxMeters": _vector_values(maximum),
        "boundsSizeMeters": _vector_values(maximum - minimum),
    }


def _configure_effect_material(
    material: bpy.types.Material, effect_key: str
) -> dict[str, object]:
    """Retain imported alpha with a halo-safe emissive sprite approximation.

    The portable effect GLBs preserve alpha blend and unlit emission.  The
    exact Runtime.exe sprite blend equation remains unresolved, so this Eevee
    treatment prevents dark halos and remains explicitly an unverified visual
    approximation.  Emission fades with camera distance so transparent pixels
    stay transparent over the level's separately fogged background.
    """
    emission_strength = (
        1.0 if effect_key == "smoke" else ANEKBAH_GLOW_EMISSION_STRENGTH
    )
    material["omikronPersistentEffectMaterial"] = True
    material["omikronSourceBlendSemantics"] = (
        "alpha_test alpha_blend camera_facing_3d_sprite"
    )
    material["omikronSpriteBlendEquation"] = "unresolved"
    material["omikronAdditiveLookingApproximation"] = (
        "unverified halo-safe luminance-normalized emission"
    )
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "BLENDED"
    material.use_backface_culling = False

    emission_nodes: list[bpy.types.Node] = []
    additive_luminance_gates = 0
    color_grade_nodes = 0
    if material.use_nodes and material.node_tree is not None:
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        emission_nodes = [node for node in nodes if node.type == "EMISSION"]
        for index, emission in enumerate(emission_nodes):
            color_socket = emission.inputs.get("Color")
            if (
                effect_key == "glow"
                and color_socket is not None
                and color_socket.is_linked
            ):
                source_color = color_socket.links[0].from_socket
                links.remove(color_socket.links[0])
                corona_grade = nodes.new("ShaderNodeHueSaturation")
                corona_grade.name = f"OMIKRON_GLOW_CORONA_GRADE_{index}"
                corona_grade.label = (
                    f"Near-white corona: saturation {ANEKBAH_GLOW_SATURATION:g}, "
                    f"value {ANEKBAH_GLOW_VALUE:g}"
                )
                corona_grade.inputs["Fac"].default_value = 1.0
                corona_grade.inputs["Saturation"].default_value = (
                    ANEKBAH_GLOW_SATURATION
                )
                corona_grade.inputs["Value"].default_value = ANEKBAH_GLOW_VALUE
                links.new(source_color, corona_grade.inputs["Color"])
                links.new(corona_grade.outputs["Color"], color_socket)
                color_grade_nodes += 1

            strength_socket = emission.inputs.get("Strength")
            if strength_socket is None:
                continue
            for link in tuple(strength_socket.links):
                links.remove(link)
            camera_data = nodes.new("ShaderNodeCameraData")
            camera_data.label = "Omikron effect camera distance"
            camera_data.name = f"OMIKRON_FX_DISTANCE_{effect_key}_{index}"
            inverse = nodes.new("ShaderNodeMath")
            inverse.operation = "SUBTRACT"
            inverse.inputs[0].default_value = 1.0
            inverse.label = "Alpha-preserving haze fade"
            strength = nodes.new("ShaderNodeMath")
            strength.operation = "MULTIPLY"
            strength.inputs[1].default_value = emission_strength
            strength.label = f"{effect_key} emission strength"
            if effect_key == "glow":
                # The game keeps the white lamp corona legible into the abrupt
                # blue-distance transition.  Use the same audited curve as the
                # opaque world so it does not begin fading across clear near geometry.
                fade_factor = _build_first_arrival_haze_factor(
                    nodes,
                    links,
                    camera_data.outputs["View Distance"],
                    f"OMIKRON_GLOW_HAZE_{index}",
                )
            else:
                # The vent-smoke approximation predates this Lampe88 calibration.
                # Keep its established linear fade untouched; this pass calibrates
                # only the world haze and streetlight glow.
                fog_offset = nodes.new("ShaderNodeMath")
                fog_offset.operation = "SUBTRACT"
                fog_offset.inputs[1].default_value = ANEKBAH_SMOKE_FADE_START_METERS
                fog_offset.label = (
                    f"Distance - {ANEKBAH_SMOKE_FADE_START_METERS:g} m"
                )
                fog_factor = nodes.new("ShaderNodeMath")
                fog_factor.operation = "DIVIDE"
                fog_factor.inputs[1].default_value = (
                    ANEKBAH_SMOKE_FADE_TRANSITION_METERS
                )
                fog_factor.use_clamp = True
                fog_factor.label = (
                    f"Clamp haze over {ANEKBAH_SMOKE_FADE_TRANSITION_METERS:g} m"
                )
                links.new(
                    camera_data.outputs["View Distance"], fog_offset.inputs[0]
                )
                links.new(fog_offset.outputs[0], fog_factor.inputs[0])
                fade_factor = fog_factor.outputs[0]
            links.new(fade_factor, inverse.inputs[1])
            links.new(inverse.outputs[0], strength.inputs[0])
            links.new(strength.outputs[0], strength_socket)

        # The runtime sprite blend equation is unresolved, while plain Eevee
        # alpha blending visibly lays dark atlas texels over the scene.  Gate
        # imported alpha by emitted luminance for a halo-safe, additive-looking
        # preview; do not claim this is the exact game equation.
        output = next(
            (
                node
                for node in nodes
                if node.type == "OUTPUT_MATERIAL"
                and node.inputs["Surface"].is_linked
            ),
            None,
        )
        emission = emission_nodes[0] if emission_nodes else None
        if output is not None and emission is not None:
            final_shader = output.inputs["Surface"].links[0].from_node
            factor = (
                final_shader.inputs[0]
                if final_shader.type == "MIX_SHADER" and final_shader.inputs[0].is_linked
                else None
            )
            color = emission.inputs.get("Color")
            if factor is not None and color is not None and color.is_linked:
                alpha_source = factor.links[0].from_socket
                color_source = color.links[0].from_socket
                links.remove(factor.links[0])
                luminance = nodes.new("ShaderNodeRGBToBW")
                luminance.label = "Additive contribution luminance"
                luminance.name = f"OMIKRON_FX_LUMINANCE_{effect_key}"
                additive_alpha = nodes.new("ShaderNodeMath")
                additive_alpha.operation = "MULTIPLY"
                additive_alpha.label = "Source alpha x additive luminance"
                additive_alpha.name = f"OMIKRON_FX_ADDITIVE_ALPHA_{effect_key}"
                links.new(color_source, luminance.inputs[0])
                links.new(alpha_source, additive_alpha.inputs[0])
                links.new(luminance.outputs[0], additive_alpha.inputs[1])
                links.new(additive_alpha.outputs[0], factor)

                # Standard alpha blending multiplies emission by opacity.  Since
                # opacity above is multiplied by luminance, divide emission
                # strength by the same (epsilon-clamped) luminance.  Their
                # product preserves source RGB contribution without dark
                # occlusion; this remains an unverified runtime approximation.
                strength_socket = emission.inputs.get("Strength")
                if strength_socket is not None and strength_socket.is_linked:
                    distance_strength = strength_socket.links[0].from_socket
                    links.remove(strength_socket.links[0])
                    safe_luminance = nodes.new("ShaderNodeMath")
                    safe_luminance.operation = "MAXIMUM"
                    safe_luminance.inputs[1].default_value = 0.01
                    safe_luminance.label = "Max(luminance, 0.01)"
                    reciprocal = nodes.new("ShaderNodeMath")
                    reciprocal.operation = "DIVIDE"
                    reciprocal.inputs[0].default_value = 1.0
                    reciprocal.label = "Additive emission normalization"
                    normalized_strength = nodes.new("ShaderNodeMath")
                    normalized_strength.operation = "MULTIPLY"
                    normalized_strength.label = "Distance strength / luminance"
                    links.new(luminance.outputs[0], safe_luminance.inputs[0])
                    links.new(safe_luminance.outputs[0], reciprocal.inputs[1])
                    links.new(distance_strength, normalized_strength.inputs[0])
                    links.new(reciprocal.outputs[0], normalized_strength.inputs[1])
                    links.new(normalized_strength.outputs[0], strength_socket)
                additive_luminance_gates += 1

    return {
        "material": material.name,
        "surfaceRenderMethod": getattr(material, "surface_render_method", None),
        "doubleSided": not material.use_backface_culling,
        "emissionStrengthAtCamera": emission_strength,
        "emissionNodesModified": len(emission_nodes),
        "colorGradeNodes": color_grade_nodes,
        "coronaColorGrade": (
            {
                "saturation": ANEKBAH_GLOW_SATURATION,
                "value": ANEKBAH_GLOW_VALUE,
                "purpose": "near-white high-energy streetlight corona",
            }
            if effect_key == "glow"
            else None
        ),
        "additiveLuminanceGates": additive_luminance_gates,
        "distanceFadeStartMeters": (
            ANEKBAH_HAZE_START_METERS
            if effect_key == "glow"
            else ANEKBAH_SMOKE_FADE_START_METERS
        ),
        "distanceFadeEndMeters": (
            ANEKBAH_HAZE_END_METERS
            if effect_key == "glow"
            else ANEKBAH_SMOKE_FADE_END_METERS
        ),
        "distanceFadeTransitionMeters": (
            ANEKBAH_HAZE_TRANSITION_METERS
            if effect_key == "glow"
            else ANEKBAH_SMOKE_FADE_TRANSITION_METERS
        ),
        "distanceFadeCurve": (
            f"powered smoothstep exponent {ANEKBAH_HAZE_EXPONENT:g}"
            if effect_key == "glow"
            else "unchanged clamped linear"
        ),
        "blendTreatment": (
            "source alpha is multiplied by emitted luminance so black texels are "
            "transparent; emission is inversely luminance-normalized so the final "
            "RGB contribution is an unverified additive-looking Blender preview"
        ),
        "runtimeBlendEquation": "unresolved",
        "hazeTreatment": (
            "emission fades to transparent with view distance so the separately "
            "fogged scene shows through without opaque billboard rectangles"
        ),
    }


def _import_effect_prototype(
    effect_path: Path, effect_key: str, source_frame_count: int
) -> tuple[bpy.types.Object, list[bpy.types.Mesh], dict[str, object]]:
    if not effect_path.is_file():
        raise FileNotFoundError(f"persistent effect GLB does not exist: {effect_path}")
    before = set(bpy.data.objects)
    result = bpy.ops.import_scene.gltf(filepath=str(effect_path.resolve()))
    if "FINISHED" not in result:
        raise RuntimeError(f"persistent effect glTF import failed: {result}")
    imported = [obj for obj in bpy.data.objects if obj not in before]
    mesh_objects = [obj for obj in imported if obj.type == "MESH"]
    imported_cameras = [obj for obj in imported if obj.type == "CAMERA"]
    if len(mesh_objects) != 1:
        raise RuntimeError(
            f"expected one mesh in {effect_path.name}, got {len(mesh_objects)}"
        )
    prototype = mesh_objects[0]
    source_mesh = prototype.data
    source_mesh.calc_loop_triangles()
    source_matrix = prototype.matrix_world.copy()
    source_translation = source_matrix.translation.copy()
    source_linear = source_matrix.copy()
    source_linear.translation = Vector((0.0, 0.0, 0.0))

    source_minimum = Vector(
        tuple(
            min(vertex.co[axis] for vertex in source_mesh.vertices)
            for axis in range(3)
        )
    )
    source_maximum = Vector(
        tuple(
            max(vertex.co[axis] for vertex in source_mesh.vertices)
            for axis in range(3)
        )
    )
    pivot = (source_minimum + source_maximum) * 0.5

    material_reports = []
    for material in source_mesh.materials:
        if material is None:
            continue
        material.name = f"OMIKRON_FX__{effect_key.upper()}__{material.name}"
        material_reports.append(_configure_effect_material(material, effect_key))

    # Each source animation frame is one quad (two triangles).  Runtime.exe
    # selects exactly one quad over particle age; adjacent quads in the model
    # are not simultaneous geometry.  Both triangles of a quad share the same
    # local-X span, so this groups exact source polygons without rebuilding UVs,
    # colors, normals, or material assignments.
    frame_polygons_by_x_span: dict[tuple[float, float], list[int]] = {}
    for polygon in source_mesh.polygons:
        xs = [source_mesh.vertices[index].co.x for index in polygon.vertices]
        span = (round(min(xs), 5), round(max(xs), 5))
        frame_polygons_by_x_span.setdefault(span, []).append(polygon.index)
    frame_groups = [
        frame_polygons_by_x_span[span]
        for span in sorted(frame_polygons_by_x_span)
    ]
    if len(frame_groups) != source_frame_count:
        raise RuntimeError(
            f"{effect_path.name}: manifest declares {source_frame_count} frames, "
            f"but geometry contains {len(frame_groups)} quad X spans"
        )
    if any(len(polygons) != 2 for polygons in frame_groups):
        raise RuntimeError(
            f"{effect_path.name}: every animation frame must contain exactly two triangles"
        )

    billboard_basis = Matrix.Rotation(math.radians(90.0), 4, "X")
    frame_meshes: list[bpy.types.Mesh] = []
    frame_reports: list[dict[str, object]] = []
    for frame_index, source_polygon_indices in enumerate(frame_groups):
        frame_mesh = source_mesh.copy()
        frame_mesh.name = (
            f"OMIKRON_FX__{effect_key.upper()}__FRAME_{frame_index:02d}__LINKED_MESH"
        )
        mesh_builder = bmesh.new()
        mesh_builder.from_mesh(frame_mesh)
        mesh_builder.faces.ensure_lookup_table()
        keep = set(source_polygon_indices)
        bmesh.ops.delete(
            mesh_builder,
            geom=[face for face in mesh_builder.faces if face.index not in keep],
            context="FACES_ONLY",
        )
        unused_vertices = [
            vertex for vertex in mesh_builder.verts if not vertex.link_faces
        ]
        if unused_vertices:
            bmesh.ops.delete(mesh_builder, geom=unused_vertices, context="VERTS")
        mesh_builder.to_mesh(frame_mesh)
        mesh_builder.free()
        frame_mesh.update()

        frame_source_minimum = Vector(
            tuple(
                min(vertex.co[axis] for vertex in frame_mesh.vertices)
                for axis in range(3)
            )
        )
        frame_source_maximum = Vector(
            tuple(
                max(vertex.co[axis] for vertex in frame_mesh.vertices)
                for axis in range(3)
            )
        )
        frame_pivot = (frame_source_minimum + frame_source_maximum) * 0.5
        normalization = (
            billboard_basis @ source_linear @ Matrix.Translation(-frame_pivot)
        )
        frame_mesh.transform(normalization)
        frame_mesh.update()
        frame_mesh.calc_loop_triangles()
        if len(frame_mesh.loop_triangles) != 2:
            raise RuntimeError(
                f"{effect_path.name}: frame {frame_index} emitted "
                f"{len(frame_mesh.loop_triangles)} triangles instead of 2"
            )
        normalized_points = [vertex.co.copy() for vertex in frame_mesh.vertices]
        normalized_minimum = Vector(
            tuple(
                min(point[axis] for point in normalized_points) for axis in range(3)
            )
        )
        normalized_maximum = Vector(
            tuple(
                max(point[axis] for point in normalized_points) for axis in range(3)
            )
        )
        frame_meshes.append(frame_mesh)
        frame_reports.append(
            {
                "frameIndex": frame_index,
                "sourcePolygonIndices": source_polygon_indices,
                "vertices": len(frame_mesh.vertices),
                "triangles": len(frame_mesh.loop_triangles),
                "sourceBoundsMinMeters": _vector_values(frame_source_minimum),
                "sourceBoundsMaxMeters": _vector_values(frame_source_maximum),
                "sourceBoundsCenterMeters": _vector_values(frame_pivot),
                "normalizationMatrix": _matrix_rows(normalization),
                "normalizedBoundsMinMeters": _vector_values(normalized_minimum),
                "normalizedBoundsMaxMeters": _vector_values(normalized_maximum),
                "dimensionsMeters": _vector_values(
                    normalized_maximum - normalized_minimum
                ),
            }
        )

    removed_camera_names = []
    for camera in imported_cameras:
        removed_camera_names.append(camera.name)
        camera_data = camera.data
        bpy.data.objects.remove(camera, do_unlink=True)
        if camera_data.users == 0:
            bpy.data.cameras.remove(camera_data)

    report = {
        **_file_record(effect_path),
        "sourceMeshObject": prototype.name,
        "sourceVertices": len(source_mesh.vertices),
        "sourceTriangles": len(source_mesh.loop_triangles),
        "sourceFrameCount": source_frame_count,
        "renderedTrianglesPerInstance": 2,
        "sourceObjectMatrix": _matrix_rows(source_matrix),
        "sourceObjectTranslationDiscardedMeters": _vector_values(source_translation),
        "sourceGeometryBoundsMinMeters": _vector_values(source_minimum),
        "sourceGeometryBoundsMaxMeters": _vector_values(source_maximum),
        "sourceGeometryBoundsCenterMeters": _vector_values(pivot),
        "normalizationPolicy": (
            "each selected frame's bounds center becomes the decor anchor; source "
            "object translation is discarded; source rotation/scale and Blender "
            "billboard basis are baked into shared per-frame mesh data"
        ),
        "normalizationDecision": (
            "Runtime selects one source quad over particle age.  Raw frame offsets "
            "are atlas-layout coordinates, not world placement; each exact quad is "
            "isolated and centered without rebuilding its attributes."
        ),
        "billboardBasisCorrectionDegreesXYZ": [90.0, 0.0, 0.0],
        "frames": frame_reports,
        "removedSourceCameras": removed_camera_names,
        "materials": material_reports,
    }
    return prototype, frame_meshes, report


def add_persistent_effects(
    effects_directory: Path,
    decor_mesh_objects: list[bpy.types.Object],
    cameras: list[bpy.types.Object],
) -> tuple[list[bpy.types.Object], dict[str, object]]:
    if not effects_directory.is_dir():
        raise FileNotFoundError(
            f"persistent effects directory does not exist: {effects_directory}"
        )
    manifest_path = Path(__file__).resolve().with_name("anekbah_composition.json")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"composition manifest does not exist: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    persistent_groups = [
        group
        for group in manifest["sfx"]["verified_anchor_groups"]
        if group.get("layer") == "persistent"
    ]
    effect_metadata = {
        str(effect["key"]): effect
        for effect in manifest["scx_embedded_effects"]["effects"]
    }
    definitions_by_id = {
        int(definition["id"]): definition
        for definition in manifest["sfx"]["definition_table"]["definitions"]
    }
    supported_effects = {"smoke", "glow"}
    unexpected = sorted(
        {
            str(group.get("effect"))
            for group in persistent_groups
            if group.get("effect") not in supported_effects
        }
    )
    if unexpected:
        raise RuntimeError(f"unsupported persistent effect groups: {unexpected}")

    effect_paths = {
        "smoke": effects_directory / "Anekbah_smoke.glb",
        "glow": effects_directory / "Anekbah_glow.glb",
    }
    prototypes: dict[str, bpy.types.Object] = {}
    frame_meshes_by_effect: dict[str, list[bpy.types.Mesh]] = {}
    asset_reports: dict[str, dict[str, object]] = {}
    for effect_key, effect_path in effect_paths.items():
        source_frame_count = int(
            effect_metadata[effect_key]["animation_frames"]["count"]
        )
        (
            prototypes[effect_key],
            frame_meshes_by_effect[effect_key],
            asset_reports[effect_key],
        ) = _import_effect_prototype(effect_path, effect_key, source_frame_count)

    collection = bpy.data.collections.new("OMIKRON_PERSISTENT_EFFECTS")
    bpy.context.scene.collection.children.link(collection)
    instances: list[bpy.types.Object] = []
    instance_counts = {key: 0 for key in supported_effects}
    expected_counts = {key: 0 for key in supported_effects}
    group_reports: list[dict[str, object]] = []
    unmatched_groups: list[dict[str, object]] = []
    mismatch_groups: list[dict[str, object]] = []
    maximum_location_delta = 0.0
    frame_distribution = {
        effect_key: {
            str(index): 0 for index in range(len(frame_meshes_by_effect[effect_key]))
        }
        for effect_key in supported_effects
    }

    for group in persistent_groups:
        effect_key = str(group["effect"])
        for binding in group["bindings"]:
            effect_id = int(binding["effect_id"])
            definition = definitions_by_id.get(effect_id)
            if definition is None:
                raise RuntimeError(
                    f"persistent binding {effect_key}/{binding['prefix']} refers to "
                    f"missing SFX definition {effect_id}"
                )
            if str(definition["effect"]) != effect_key:
                raise RuntimeError(
                    f"SFX definition {effect_id} is {definition['effect']}, not {effect_key}"
                )
            initial_scale = float(definition["initial_scale"])
            prefix = str(binding["prefix"])
            expected = int(binding["anchor_count"])
            matches = sorted(
                (
                    obj
                    for obj in decor_mesh_objects
                    if obj.name[:4].casefold() == prefix.casefold()
                ),
                key=lambda obj: obj.name.casefold(),
            )
            expected_counts[effect_key] += expected
            status = "matched"
            if not matches:
                status = "unmatched-expected-zero" if expected == 0 else "unmatched"
            if len(matches) != expected:
                status = "count-mismatch"
            group_report = {
                "effect": effect_key,
                "effectId": effect_id,
                "definitionName": str(definition["name"]),
                "prefix": prefix,
                "initialUniformScale": initial_scale,
                "expectedCount": expected,
                "matchedCount": len(matches),
                "anchorNames": [anchor.name for anchor in matches],
                "status": status,
            }
            group_reports.append(group_report)
            if not matches:
                unmatched_groups.append(group_report.copy())
            if len(matches) != expected:
                mismatch_groups.append(group_report.copy())

            group_frame_distribution = {
                str(index): 0
                for index in range(len(frame_meshes_by_effect[effect_key]))
            }
            for anchor in matches:
                source_frame_count = len(frame_meshes_by_effect[effect_key])
                if source_frame_count == 1:
                    frame_index = 0
                else:
                    phase_key = f"{effect_key}:{anchor.name.casefold()}".encode("utf-8")
                    digest = hashlib.sha256(phase_key).digest()
                    frame_index = (
                        int.from_bytes(digest[:4], byteorder="little")
                        % source_frame_count
                    )
                instance = bpy.data.objects.new(
                    f"OMIKRON_FX__{effect_key.upper()}__{anchor.name}",
                    frame_meshes_by_effect[effect_key][frame_index],
                )
                collection.objects.link(instance)
                anchor_location = anchor.matrix_world.translation.copy()
                instance.matrix_world = (
                    Matrix.Translation(anchor_location)
                    @ Matrix.Scale(initial_scale, 4)
                )
                instance["omikronPersistentEffect"] = effect_key
                instance["omikronAnchorObject"] = anchor.name
                instance["omikronAnchorPrefix"] = prefix
                instance["omikronSfxDefinitionId"] = effect_id
                instance["omikronInitialUniformScale"] = initial_scale
                instance["omikronStaticSourceFrame"] = frame_index
                instance["omikronBillboard"] = "screen-aligned COPY_ROTATION"
                instance["omikronSourceAsset"] = str(effect_paths[effect_key].resolve())
                constraint = instance.constraints.new(type="COPY_ROTATION")
                constraint.name = "OMIKRON_BILLBOARD_CAMERA_FACING"
                constraint.target = cameras[0]
                constraint.owner_space = "WORLD"
                constraint.target_space = "WORLD"
                constraint.mix_mode = "REPLACE"
                if hasattr(instance, "visible_shadow"):
                    instance.visible_shadow = False
                maximum_location_delta = max(
                    maximum_location_delta,
                    (instance.matrix_world.translation - anchor_location).length,
                )
                instances.append(instance)
                instance_counts[effect_key] += 1
                frame_distribution[effect_key][str(frame_index)] += 1
                group_frame_distribution[str(frame_index)] += 1
            group_report["chosenFrameDistribution"] = group_frame_distribution

    for effect_key, prototype in prototypes.items():
        source_mesh = prototype.data
        bpy.data.objects.remove(prototype, do_unlink=True)
        if source_mesh.users == 0:
            bpy.data.meshes.remove(source_mesh)
        asset_reports[effect_key]["linkedFrameMeshes"] = [
            {
                "frameIndex": index,
                "mesh": mesh.name,
                "users": mesh.users,
                "trianglesPerInstance": 2,
            }
            for index, mesh in enumerate(frame_meshes_by_effect[effect_key])
        ]

    if mismatch_groups:
        raise RuntimeError(
            "persistent effect anchor counts do not match the verified manifest: "
            + json.dumps(mismatch_groups, separators=(",", ":"))
        )

    total_expected = sum(expected_counts.values())
    report = {
        "enabled": True,
        "manifest": _file_record(manifest_path),
        "effectsDirectory": str(effects_directory.resolve()),
        "layerSelection": ["persistent"],
        "anchorMatchRule": (
            "case-insensitive comparison of each manifest prefix with the first "
            "four characters of imported decor mesh object names"
        ),
        "placement": (
            "each linked-frame instance receives the exact matched decor anchor "
            "world position plus its SFX definition initial_uniform_scale; decor "
            "rotation/scale and SCX effect-scene transforms are not inherited"
        ),
        "normalizationDecision": (
            "runtime selects one exact source quad over particle age; each isolated "
            "frame is bounds-centered because raw offsets are atlas-layout coordinates"
        ),
        "expectedInstanceCounts": {
            **expected_counts,
            "total": total_expected,
        },
        "matchedAnchorCounts": {
            **instance_counts,
            "total": len(instances),
        },
        "totalInstances": len(instances),
        "linkedMeshInstanceCounts": instance_counts,
        "sourceFrameCounts": {
            effect_key: len(frame_meshes_by_effect[effect_key])
            for effect_key in sorted(supported_effects)
        },
        "renderedTrianglesPerInstance": {"smoke": 2, "glow": 2},
        "chosenFrameDistribution": frame_distribution,
        "staticPhaseApproximation": {
            "enabled": True,
            "algorithm": (
                "SHA-256 of '<effect>:<casefolded anchor name>'; first four bytes "
                "interpreted little-endian modulo source frame count"
            ),
            "behavior": (
                "one deterministic source frame is shown per anchor for this static "
                "first-arrival preview; the game selects frames over particle age"
            ),
        },
        "definitionScaleSource": (
            "manifest sfx.definition_table.definitions selected by each binding effect_id"
        ),
        "billboardConstraints": len(instances),
        "billboardBehavior": (
            "screen-aligned COPY_ROTATION constraints are retargeted to each source "
            "camera before rendering and left targeting the saved active camera"
        ),
        "maxAnchorLocationDeltaMeters": maximum_location_delta,
        "groups": group_reports,
        "unmatchedGroups": unmatched_groups,
        "countMismatchGroups": mismatch_groups,
        "effectAssets": asset_reports,
        "particleSystemCaveat": (
            "This is static persistent dressing, not a particle simulation: launch "
            "vectors, acceleration, lifetime/frame animation, color evolution, roll, "
            "scale-over-life, delays, and burst behavior are not evaluated."
        ),
        "materialTreatment": {
            "portableSource": "KHR_materials_unlit plus alphaMode BLEND",
            "blender": (
                "source alpha is luminance-gated so black texels contribute no "
                "occlusion; double-sided unlit emission is inversely normalized "
                "by luminance as an unverified additive-looking preview treatment"
            ),
            "runtimeSpriteBlendEquation": "unresolved",
            "distanceHaze": (
                f"glow uses the powered-smoothstep world curve from "
                f"{ANEKBAH_HAZE_START_METERS:g} to {ANEKBAH_HAZE_END_METERS:g} m; "
                f"smoke retains its prior linear {ANEKBAH_SMOKE_FADE_START_METERS:g} "
                f"to {ANEKBAH_SMOKE_FADE_END_METERS:g} m fade; persistent-effect "
                "materials are excluded from the opaque scene-haze shader mix"
            ),
            "glowCalibration": {
                "emissionStrength": ANEKBAH_GLOW_EMISSION_STRENGTH,
                "saturation": ANEKBAH_GLOW_SATURATION,
                "value": ANEKBAH_GLOW_VALUE,
                "geometryScaleChanged": False,
                "placementChanged": False,
            },
        },
        "excluded": {
            "explosion": "scripted layer; intentionally not imported or instanced",
            "cargoVariants": "scripted layer; intentionally not instanced",
        },
    }
    return instances, report


def retarget_billboards(
    instances: list[bpy.types.Object], camera: bpy.types.Object
) -> None:
    for instance in instances:
        constraint = instance.constraints.get("OMIKRON_BILLBOARD_CAMERA_FACING")
        if constraint is None:
            raise RuntimeError(f"effect instance lost billboard constraint: {instance.name}")
        constraint.target = camera
    bpy.context.view_layer.update()


def main() -> None:
    (
        input_path,
        output_directory,
        blend_path,
        sky_path,
        effects_directory,
        interiors_directory,
    ) = arguments()
    output_directory.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    result = bpy.ops.import_scene.gltf(filepath=str(input_path.resolve()))
    if "FINISHED" not in result:
        raise RuntimeError(f"glTF import failed: {result}")

    scene = bpy.context.scene
    # Blender 5.2 renamed the post-Next engine identifier back to BLENDER_EEVEE.
    try:
        scene.render.engine = "BLENDER_EEVEE"
    except TypeError:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    # Authored OD3X cameras use the original PC game's 4:3 horizontal FOV.
    scene.render.resolution_x = 1280
    scene.render.resolution_y = 960
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    scene.render.image_settings.color_mode = "RGBA"
    atmosphere_report = configure_world(
        scene, first_arrival=input_path.stem.casefold() == "anekbah"
    )
    scene.view_settings.view_transform = "Standard"
    try:
        scene.view_settings.look = "None"
    except TypeError:
        pass
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0

    cameras = sorted(
        (obj for obj in scene.objects if obj.type == "CAMERA"),
        key=lambda obj: obj.name.casefold(),
    )
    if not cameras:
        raise RuntimeError("imported scene has no cameras")
    decor_mesh_objects = [obj for obj in scene.objects if obj.type == "MESH"]
    imported_image_count = len(bpy.data.images)

    minimum, maximum = scene_bounds()
    bounds_size = maximum - minimum
    for mesh in bpy.data.meshes:
        mesh.calc_loop_triangles()
    imported_object_count = len(bpy.data.objects)
    imported_mesh_object_count = sum(obj.type == "MESH" for obj in bpy.data.objects)
    imported_mesh_count = len(bpy.data.meshes)
    imported_material_count = len(bpy.data.materials)
    imported_triangle_count = sum(len(mesh.loop_triangles) for mesh in bpy.data.meshes)
    preview_sky = None
    preview_sky_relative = None
    preview_sky_report = None
    if input_path.stem.casefold() == "anekbah":
        # Asky is authored as a bounded plane.  The original engine expands and
        # animates it through a sky-specific render path that core glTF/Blender
        # import cannot reproduce faithfully.  Do not present the raw plane as
        # a sky; the calibrated world background and distance haze provide the
        # honest first-arrival atmosphere until that runtime path is decoded.
        preview_sky_report = {
            "omitted": True,
            "requestedSource": (
                str(sky_path.resolve()) if sky_path is not None else None
            ),
            "reason": (
                "authored Asky is a bounded floating plane without the game's "
                "sky-specific expansion and animation behavior"
            ),
            "replacement": (
                "reference-matched world background plus lighter camera-distance haze"
            ),
            "worldReferenceSrgb": [77, 115, 149],
            "portableGlbGeometry": False,
        }
    elif sky_path is not None:
        if sky_path.suffix.casefold() == ".glb":
            preview_sky, preview_sky_relative, preview_sky_report = (
                load_camera_relative_sky(sky_path)
            )
        else:
            preview_sky = add_spherical_preview_sky(minimum, maximum, sky_path)
            preview_sky_report = {
                "object": preview_sky.name,
                "texture": str(sky_path.resolve()),
                "behavior": "preview-only repeated spherical fallback",
                "portableGlbGeometry": False,
            }
    interior_materials: list[bpy.types.Material] = []
    interiors_report: dict[str, object] | None = None
    if interiors_directory is not None:
        if input_path.stem.casefold() != "anekbah":
            raise RuntimeError(
                "the audited interior manifest currently applies only to Anekbah"
            )
        interior_materials, interiors_report = import_anekbah_interiors(
            interiors_directory
        )
        interiors_report["doorSeamSuppression"] = (
            suppress_duplicate_interior_door_seams(decor_mesh_objects)
        )
        teleport_zones = apply_anekbah_teleport_zones(decor_mesh_objects)
        interiors_report["teleportInspectionZones"] = teleport_zones
        interiors_report["finalPlacement"] = (
            "73 portal-connected interiors retain authored global coordinates; "
            "8 teleport-local interiors use parent-only translations across 7 "
            "non-overlapping inspection zones"
        )
    elif input_path.stem.casefold() == "anekbah":
        interiors_report = {
            "enabled": False,
            "reason": "no INTERIORS_DIR argument was supplied",
        }
    effect_instances: list[bpy.types.Object] = []
    persistent_effects_report = None
    if effects_directory is not None:
        if input_path.stem.casefold() != "anekbah":
            raise RuntimeError(
                "the adjacent persistent-effects manifest currently applies only to Anekbah"
            )
        effect_instances, persistent_effects_report = add_persistent_effects(
            effects_directory, decor_mesh_objects, cameras
        )
    if input_path.stem.casefold() == "anekbah":
        atmosphere_report["materialsModified"] = apply_first_arrival_distance_haze(
            list(bpy.data.materials)
        )
        if interiors_report is not None and interiors_report.get("enabled"):
            interiors_report["firstArrivalHaze"] = {
                "appliedBySharedAnekbahMaterialPass": True,
                "importedMaterials": len(interior_materials),
                "materialsWithDistanceHaze": sum(
                    _material_has_first_arrival_haze(material)
                    for material in interior_materials
                ),
                "curve": {
                    "method": atmosphere_report.get("method"),
                    "startMeters": ANEKBAH_HAZE_START_METERS,
                    "endMeters": ANEKBAH_HAZE_END_METERS,
                    "exponent": ANEKBAH_HAZE_EXPONENT,
                },
            }
    composition_minimum, composition_maximum = scene_bounds()
    composition_bounds_size = composition_maximum - composition_minimum
    for index, camera in enumerate(cameras):
        if preview_sky is not None and preview_sky_relative is not None:
            attach_camera_relative_sky(preview_sky, camera, preview_sky_relative)
        camera.data.clip_start = min(camera.data.clip_start, 0.025)
        camera.data.clip_end = max(
            camera.data.clip_end, composition_bounds_size.length * 4.0
        )
        scene.camera = camera
        if effect_instances:
            retarget_billboards(effect_instances, camera)
        safe_name = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in camera.name
        )
        scene.render.filepath = str(
            (output_directory / f"preview_{index:02d}_{safe_name}.png").resolve()
        )
        bpy.ops.render.render(write_still=True)

    preferred_camera = next(
        (camera for camera in cameras if camera.name.upper().startswith("CAMERA1__")),
        cameras[0],
    )
    scene.camera = preferred_camera
    if preview_sky is not None and preview_sky_relative is not None:
        attach_camera_relative_sky(preview_sky, preferred_camera, preview_sky_relative)
    if effect_instances:
        retarget_billboards(effect_instances, preferred_camera)

    final_scene_totals = _scene_totals(scene)
    if (
        input_path.stem.casefold() == "anekbah"
        and final_scene_totals["previewSkyObjects"] != 0
    ):
        raise RuntimeError(
            "Anekbah final scene unexpectedly contains OMIKRON_PREVIEW_SKY objects"
        )

    source_record = _file_record(input_path)
    provenance: dict[str, object] = {
        "baseExteriorGlb": source_record,
        "interiorsManifest": (
            interiors_report.get("manifest")
            if interiors_report is not None and interiors_report.get("enabled")
            else None
        ),
        "persistentEffectsManifest": (
            persistent_effects_report.get("manifest")
            if persistent_effects_report is not None
            else None
        ),
        "previewSkyIncluded": preview_sky is not None,
        "requestedPreviewSky": (
            str(sky_path.resolve()) if sky_path is not None else None
        ),
        "compositionRule": (
            "base exterior and portal-connected interiors retain authored global "
            "coordinates; eight disconnected local-coordinate interiors receive "
            "explicit inspection-zone translations; preview atmosphere/effects are "
            "adjacent Blender-only presentation layers"
        ),
    }

    report = {
        "blenderVersion": bpy.app.version_string,
        "source": str(input_path.resolve()),
        "countScope": "base exterior GLB immediately after import",
        "objects": imported_object_count,
        "meshObjects": imported_mesh_object_count,
        "meshes": imported_mesh_count,
        "materials": imported_material_count,
        "images": imported_image_count,
        "blenderImagesAfterRender": len(bpy.data.images),
        "lights": len(bpy.data.lights),
        "cameras": len(bpy.data.cameras),
        "blenderImportedTriangles": imported_triangle_count,
        "triangleCountCaveat": (
            "Blender may discard zero-area source triangles during glTF import; "
            "the converter and Khronos validator reports are authoritative for GLB indices"
        ),
        "boundsMinMeters": list(minimum),
        "boundsMaxMeters": list(maximum),
        "boundsSizeMeters": list(bounds_size),
        "baseExteriorImport": {
            "source": source_record,
            "objects": imported_object_count,
            "meshObjects": imported_mesh_object_count,
            "meshDatablocks": imported_mesh_count,
            "materials": imported_material_count,
            "images": imported_image_count,
            "blenderImportedTriangles": imported_triangle_count,
            "boundsMinMeters": _vector_values(minimum),
            "boundsMaxMeters": _vector_values(maximum),
            "boundsSizeMeters": _vector_values(bounds_size),
            "capturedBeforeAdjacentImports": True,
        },
        "compositionBoundsBeforeCameraBillboardRetarget": {
            "minimum": _vector_values(composition_minimum),
            "maximum": _vector_values(composition_maximum),
            "size": _vector_values(composition_bounds_size),
        },
        "finalScene": final_scene_totals,
        "provenance": provenance,
        "cameraNames": [camera.name for camera in cameras],
        "savedActiveCamera": preferred_camera.name,
        "sourceCameraAspectRatio": 4.0 / 3.0,
        "atmosphere": atmosphere_report,
        "previewSky": preview_sky_report,
        "persistentEffects": persistent_effects_report,
        "interiors": interiors_report,
        "renders": [
            str(path.resolve()) for path in sorted(output_directory.glob("preview_*.png"))
        ],
    }
    if blend_path is not None:
        blend_path.parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(blend_path.resolve()))
        provenance["savedBlend"] = _file_record(blend_path)
    (output_directory / "blender_validation.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print("OMIKRON_BLENDER_VALIDATION " + json.dumps(report, separators=(",", ":")))


if __name__ == "__main__":
    main()
