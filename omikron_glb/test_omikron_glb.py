"""Regression tests for the dependency-free Omikron-to-GLB converter."""

from __future__ import annotations

import math
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

import anekbah_effects as effects
import anekbah_interiors as interiors
import omikron_glb as converter


GAME_ROOT = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Omikron")
ANEKBAH_3DO = GAME_ROOT / "MESHES" / "DECORS" / "Anekbah.3DO"


def _minimal_scene(*, horizontal_fov_degrees: float = 90.0) -> converter.Scene3DO:
    """Return an empty, valid scene with one camera for focused GLB tests."""
    header = converter.Header(
        magic="OD3X",
        version_major=4,
        version_minor=44,
        materials_offset=0,
        vertices_offset=0,
        triangles_offset=0,
        rectangles_offset=0,
        meshes_offset=0,
        doors_offset=0,
        cameras_offset=0,
        lights_offset=0,
        num_triangles=0,
        num_rectangles=0,
        num_vertices=0,
        num_materials=0,
        num_cameras=1,
        num_meshes=0,
        num_doors=0,
        total_lights=0,
        num_mesh_lights=0,
        num_lights=0,
    )
    camera = converter.SourceCamera(
        name="test_camera",
        position=(10.0, 20.0, 30.0),
        target=(10.0, 20.0, 29.0),
        unknown_float=0.0,
        field_of_view_degrees=horizontal_fov_degrees,
    )
    return converter.Scene3DO(
        path=Path("synthetic.3DO"),
        source_bytes=b"OD3X synthetic fixture",
        header=header,
        materials=[],
        vertices=[],
        triangles=[],
        rectangles=[],
        meshes=[],
        doors=[],
        cameras=[camera],
        lights=[],
        warnings=[],
    )


def _build_synthetic_glb(
    *, horizontal_fov_degrees: float = 90.0, aspect_ratio: float = 4.0 / 3.0
) -> bytes:
    scene = _minimal_scene(horizontal_fov_degrees=horizontal_fov_degrees)
    with tempfile.TemporaryDirectory() as temporary_directory:
        texture_source = Path(temporary_directory) / "synthetic.3DT"
        texture_source.write_bytes(b"")
        glb, _stats = converter.build_glb(
            scene,
            [],
            texture_source,
            converter.ConversionOptions(
                include_lights=False,
                camera_aspect_ratio=aspect_ratio,
            ),
        )
    return glb


def _read_png_chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    """Parse PNG chunks and assert their CRCs while doing so."""
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError("missing PNG signature")
    chunks: list[tuple[bytes, bytes]] = []
    offset = 8
    while offset < len(png):
        if offset + 12 > len(png):
            raise AssertionError("truncated PNG chunk")
        length = struct.unpack_from(">I", png, offset)[0]
        chunk_type = png[offset + 4 : offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        if payload_end + 4 > len(png):
            raise AssertionError("PNG chunk extends beyond file")
        payload = png[payload_start:payload_end]
        stored_crc = struct.unpack_from(">I", png, payload_end)[0]
        calculated_crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        if stored_crc != calculated_crc:
            raise AssertionError(f"bad CRC for PNG chunk {chunk_type!r}")
        chunks.append((chunk_type, payload))
        offset = payload_end + 4
        if chunk_type == b"IEND":
            break
    if offset != len(png):
        raise AssertionError("trailing bytes after PNG IEND")
    return chunks


class ConverterUnitTests(unittest.TestCase):
    def test_coordinate_transform_is_right_handed_y_up(self) -> None:
        self.assertEqual(
            converter._transform_vec3((1.0, 2.0, 3.0)),
            (1.0, -2.0, -3.0),
        )
        self.assertEqual(
            converter._transform_vec3((40.0, -80.0, 120.0), 0.025),
            (1.0, 2.0, -3.0),
        )

    def test_horizontal_fov_is_written_as_vertical_gltf_fov(self) -> None:
        horizontal_fov = 90.0
        aspect_ratio = 4.0 / 3.0
        document = converter.validate_glb_bytes(
            _build_synthetic_glb(
                horizontal_fov_degrees=horizontal_fov,
                aspect_ratio=aspect_ratio,
            )
        )
        perspective = document["cameras"][0]["perspective"]
        expected_yfov = 2.0 * math.atan(
            math.tan(math.radians(horizontal_fov) / 2.0) / aspect_ratio
        )
        self.assertAlmostEqual(perspective["yfov"], expected_yfov, places=12)
        self.assertEqual(perspective["aspectRatio"], aspect_ratio)
        self.assertEqual(
            document["cameras"][0]["extras"]["omikronSourceFovAxis"],
            "horizontal",
        )

    def test_explicit_light_preserves_source_record_and_legacy_spot_mapping(
        self,
    ) -> None:
        scene = _minimal_scene()
        scene.lights = [
            converter.SourceLight(
                flags=(18, 0x4000),
                name="ROOM_LIGHT",
                values=(400.0, 200.0, 2.5, 90.0, 80.0),
                bgra=(10, 20, 30, 255),
                points=(
                    (40.0, 80.0, -120.0),
                    (40.0, 40.0, -120.0),
                    (1.0, 2.0, 3.0),
                    (4.0, 5.0, 6.0),
                    (7.0, 8.0, 9.0),
                    (10.0, 11.0, 12.0),
                ),
            )
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            texture_source = Path(temporary_directory) / "synthetic.3DT"
            texture_source.write_bytes(b"")
            glb, stats = converter.build_glb(
                scene,
                [],
                texture_source,
                converter.ConversionOptions(
                    include_cameras=False,
                    include_lights=True,
                    light_intensity_scale=100.0,
                ),
            )
        document = converter.validate_glb_bytes(glb)
        lights = document["extensions"]["KHR_lights_punctual"]["lights"]
        self.assertEqual(stats["explicitLights"], 1)
        self.assertEqual(len(lights), 1)
        light = lights[0]
        self.assertEqual(light["type"], "spot")
        self.assertEqual(light["range"], 10.0)
        self.assertEqual(light["intensity"], 250.0)
        self.assertEqual(
            light["color"],
            [30 / 255.0, 20 / 255.0, 10 / 255.0],
        )
        self.assertAlmostEqual(
            light["spot"]["innerConeAngle"], math.radians(20.0)
        )
        self.assertAlmostEqual(
            light["spot"]["outerConeAngle"], math.radians(60.0)
        )
        source = light["extras"]["omikron"]
        self.assertEqual(source["recordSizeBytes"], 304)
        self.assertEqual(source["flags"], [18, 0x4000])
        self.assertEqual(
            source["pointsGameCoordinates"][0],
            [40.0, 80.0, -120.0],
        )
        self.assertEqual(source["secondaryAttenuationValues"], [90.0, 80.0])
        node = next(
            item
            for item in document["nodes"]
            if "KHR_lights_punctual" in item.get("extensions", {})
        )
        self.assertEqual(node["matrix"][12:15], [1.0, -2.0, 3.0])
        self.assertEqual(
            node["extras"]["omikron"]["sourceKind"],
            "decoded explicit light",
        )

    def test_alpha_flag_policy(self) -> None:
        self.assertEqual(converter._alpha_mode((0, 0, 0, 0)), ("OPAQUE", False))
        self.assertEqual(converter._alpha_mode((0, 0x08, 0, 0)), ("MASK", True))
        self.assertEqual(converter._alpha_mode((0, 0x10, 0, 0)), ("BLEND", False))
        self.assertEqual(converter._alpha_mode((0, 0, 0, 0x20)), ("BLEND", False))
        self.assertEqual(converter._alpha_mode((0, 0x18, 0, 0)), ("BLEND", True))

    def test_png_encoding_has_valid_chunks_orientation_and_color_key(self) -> None:
        texture = converter.IndexedTexture(
            name="fixture.bmp",
            width=2,
            height=2,
            palette_rgb=((0, 0, 0), (255, 0, 0)),
            # Bottom row then top row, matching the source format.
            pixels_bottom_up=bytes((0, 1, 1, 0)),
            bits_per_pixel=8,
        )
        chunks = _read_png_chunks(
            converter.texture_to_png(texture, transparent_black=True)
        )
        self.assertEqual([chunk_type for chunk_type, _ in chunks], [b"IHDR", b"IDAT", b"IEND"])
        width, height, bit_depth, color_type, compression, filtering, interlace = (
            struct.unpack(">IIBBBBB", chunks[0][1])
        )
        self.assertEqual(
            (width, height, bit_depth, color_type, compression, filtering, interlace),
            (2, 2, 8, 6, 0, 0, 0),
        )
        scanlines = zlib.decompress(
            b"".join(payload for chunk_type, payload in chunks if chunk_type == b"IDAT")
        )
        self.assertEqual(
            scanlines,
            bytes(
                (
                    0,
                    255, 0, 0, 255,
                    0, 0, 0, 0,
                    0,
                    0, 0, 0, 0,
                    255, 0, 0, 255,
                )
            ),
        )

    def test_glb_internal_validation_accepts_output_and_rejects_bad_length(self) -> None:
        glb = _build_synthetic_glb()
        document = converter.validate_glb_bytes(glb)
        self.assertEqual(document["asset"]["version"], "2.0")
        self.assertEqual(struct.unpack_from("<I", glb, 8)[0], len(glb))

        corrupted = bytearray(glb)
        struct.pack_into("<I", corrupted, 8, len(corrupted) + 4)
        with self.assertRaisesRegex(converter.FormatError, "invalid GLB header"):
            converter.validate_glb_bytes(bytes(corrupted))


class InstalledAnekbahIntegrationTests(unittest.TestCase):
    def test_default_conversion_matches_known_anekbah_inventory(self) -> None:
        if not ANEKBAH_3DO.is_file() or not ANEKBAH_3DO.with_suffix(".3DT").is_file():
            self.skipTest(f"installed Anekbah assets not found below {GAME_ROOT}")

        scene = converter.parse_3do(ANEKBAH_3DO)
        texture_path = ANEKBAH_3DO.with_suffix(".3DT")
        textures = converter.decode_3dt(scene, texture_path)
        glb, stats = converter.build_glb(
            scene,
            textures,
            texture_path,
            converter.ConversionOptions(),
        )
        document = converter.validate_glb_bytes(glb)

        self.assertEqual(stats["emittedMeshes"], 859)
        self.assertEqual(stats["emittedTriangles"], 46_415)
        self.assertEqual(stats["embeddedImages"], 20)
        self.assertEqual(stats["outputMaterials"], 47)
        self.assertEqual(stats["cameraAspectRatio"], 4.0 / 3.0)
        self.assertEqual(stats["warnings"], [])
        self.assertEqual(len(document["meshes"]), 859)
        self.assertEqual(len(document["images"]), 20)
        self.assertEqual(len(document["materials"]), 47)
        self.assertEqual(len(document["cameras"]), 3)
        for material in document["materials"]:
            decoded = material.get("extras", {}).get("decodedEffects", [])
            self.assertNotIn("additive", decoded)
            self.assertNotIn("subtractive", decoded)
        for camera in document["cameras"]:
            self.assertEqual(camera["perspective"]["aspectRatio"], 4.0 / 3.0)

    def test_embedded_effect_descriptors_match_installed_scx(self) -> None:
        scx_path = GAME_ROOT / "SCPTDATA" / "anekbah.SCX"
        if not scx_path.is_file():
            self.skipTest(f"installed Anekbah SCX not found below {GAME_ROOT}")

        inspection = effects.inspect_install(GAME_ROOT, effects.DEFAULT_MANIFEST)
        self.assertTrue(inspection["valid"])
        self.assertEqual(inspection["container"]["bytes"], 3_624_025)
        self.assertEqual(
            inspection["container"]["sha256"],
            "8abff2559fc12a4cb4c646c2bcef14c9f7dc068743ce9cfb372ec68451750d32",
        )
        self.assertEqual(
            [
                (
                    effect["key"],
                    effect["modelBytes"],
                    effect["textureBytes"],
                    effect["od3xVersion"],
                )
                for effect in inspection["effects"]
            ],
            [
                ("smoke", 1_476, 17_278, "4.44"),
                ("glow", 856, 17_278, "4.44"),
                ("explosion", 2_020, 26_591, "4.44"),
            ],
        )

    def test_iam_interior_inventory_matches_installed_anekbah(self) -> None:
        area_path = GAME_ROOT / "IAM" / "AREA"
        if not area_path.is_file():
            self.skipTest(f"installed IAM/AREA not found below {GAME_ROOT}")

        inspection = interiors.inspect_interiors(GAME_ROOT)
        self.assertTrue(inspection["valid"])
        self.assertEqual(inspection["selection"]["resolvedUniqueInteriors"], 81)
        self.assertEqual(inspection["selection"]["excludedAreaReferences"], 5)
        self.assertEqual(inspection["selection"]["missingUniqueInteriors"], 2)
        self.assertEqual(inspection["sourceLighting"]["totalLights"], 1_872)
        self.assertEqual(inspection["sourceLighting"]["explicitLights"], 1_358)
        self.assertEqual(inspection["sourceLighting"]["meshLights"], 514)
        self.assertEqual(
            inspection["sourceLighting"]["interiorsWithExplicitLights"],
            80,
        )
        self.assertEqual(
            {item["decorStem"].casefold() for item in inspection["excluded"]},
            {"anekbah", "atoit", "aimpasse", "acsgrotl", "abetsy"},
        )
        self.assertEqual(
            {item["decorStem"] for item in inspection["missing"]},
            {"ACSA4", "CAVE-JEN"},
        )
        shunabku = next(
            item
            for item in inspection["interiors"]
            if item["decorStem"].casefold() == "shunabku"
        )
        self.assertEqual(len(shunabku["areaReferences"]), 3)


if __name__ == "__main__":
    unittest.main()
