"""Focused regression tests for the direct Anekbah GLB composer."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import anekbah_compose as compose
import omikron_glb as converter


def _fixture_document(name: str) -> dict[str, object]:
    return {
        "asset": {"version": "2.0", "generator": "composition test"},
        "scene": 0,
        "scenes": [{"name": name, "nodes": [0, 1, 2]}],
        "nodes": [
            {"name": f"{name}_mesh", "mesh": 0},
            {"name": f"{name}_camera", "camera": 0},
            {
                "name": f"{name}_light",
                "extensions": {"KHR_lights_punctual": {"light": 0}},
            },
        ],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 8}],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5123,
                "count": 3,
                "type": "SCALAR",
            }
        ],
        "samplers": [{}],
        "images": [{"bufferView": 0, "mimeType": "image/png", "name": name}],
        "textures": [{"sampler": 0, "source": 0}],
        "materials": [
            {
                "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}},
                "extensions": {"KHR_materials_unlit": {}},
            }
        ],
        "meshes": [
            {
                "name": name,
                "primitives": [
                    {
                        "attributes": {"POSITION": 0},
                        "indices": 0,
                        "material": 0,
                        "mode": 4,
                    }
                ],
                "extras": {"fixture": name},
            }
        ],
        "cameras": [
            {
                "type": "perspective",
                "perspective": {"yfov": 1.0, "znear": 0.1},
            }
        ],
        "extensionsUsed": ["KHR_lights_punctual", "KHR_materials_unlit"],
        "extensions": {
            "KHR_lights_punctual": {
                "lights": [{"type": "point", "intensity": 1.0}]
            }
        },
        "extras": {"omikron": {"source3do": f"{name}.3DO"}},
    }


def _door_fixture_document(
    name: str,
    doors: list[tuple[str, tuple[float, float, float]]],
) -> dict[str, object]:
    document = _fixture_document(name)
    mesh_template = document["meshes"][0]
    document["meshes"] = []
    document["nodes"] = []
    document["scenes"][0]["nodes"] = []
    for original_name, anchor in doors:
        mesh = copy.deepcopy(mesh_template)
        mesh["name"] = original_name
        mesh["extras"] = {"omikron": {"originalName": original_name}}
        mesh_index = len(document["meshes"])
        node_index = len(document["nodes"])
        document["meshes"].append(mesh)
        document["nodes"].append(
            {"name": original_name, "mesh": mesh_index, "translation": list(anchor)}
        )
        document["scenes"][0]["nodes"].append(node_index)
    return document


class GlbCompositionTests(unittest.TestCase):
    def test_merge_rebases_binary_resources_nodes_and_lights(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / "Anekbah.glb", root / "Room.glb"]
            sources = []
            for index, (path, stem, role) in enumerate(
                zip(paths, ("Anekbah", "Room"), ("base", "interior"))
            ):
                document = _fixture_document(stem)
                glb = compose._pack_glb(document, bytes((index + 1,)) * 8)
                path.write_bytes(glb)
                sources.append(compose._parse_glb(path, role, stem))

            glb, stats = compose.merge_sources(sources)
            document = converter.validate_glb_bytes(glb)
            json_length = int.from_bytes(glb[12:16], "little")
            binary_start = 20 + json_length + 8
            binary = glb[binary_start : binary_start + document["buffers"][0]["byteLength"]]

            self.assertEqual(binary, b"\x01" * 8 + b"\x02" * 8)
            self.assertEqual(
                [view["byteOffset"] for view in document["bufferViews"]], [0, 8]
            )
            self.assertEqual(document["accessors"][1]["bufferView"], 1)
            self.assertEqual(document["images"][1]["bufferView"], 1)
            self.assertEqual(document["textures"][1], {"sampler": 1, "source": 1})
            self.assertEqual(
                document["materials"][1]["pbrMetallicRoughness"][
                    "baseColorTexture"
                ]["index"],
                1,
            )
            self.assertEqual(document["meshes"][1]["primitives"][0]["indices"], 1)
            self.assertEqual(document["meshes"][1]["primitives"][0]["material"], 1)
            self.assertEqual(document["nodes"][4]["mesh"], 1)
            self.assertEqual(
                document["nodes"][6]["extensions"]["KHR_lights_punctual"][
                    "light"
                ],
                1,
            )
            self.assertEqual(document["scenes"][0]["nodes"], [3, 7])
            self.assertEqual(
                document["nodes"][4]["extras"]["omikronComposition"]["decorStem"],
                "Room",
            )
            self.assertEqual(
                len(
                    document["extensions"]["KHR_lights_punctual"]["lights"]
                ),
                2,
            )
            self.assertEqual(stats["sources"], 2)
            self.assertEqual(stats["sourceGroupNodes"], 2)
            self.assertEqual(stats["triangles"], 2)

    def test_exterior_priority_suppresses_matching_interior_door_seams(self) -> None:
        porteg30 = (26.627194213867188, 0.08756566643714905, 54.66345825195313)
        ported42 = (199.95913085937502, 4.085088729858398, -49.402383422851564)
        fixtures = [
            (
                "Anekbah",
                "base",
                [
                    ("Porteg30", porteg30),
                    ("Ported42", ported42),
                ],
            ),
            (
                "AHall30",
                "interior",
                [("PORTEG30.001", (porteg30[0] - 0.0000031, porteg30[1], porteg30[2]))],
            ),
            (
                "AHall42",
                "interior",
                [(" ported42 ", (ported42[0], ported42[1] - 0.0000004, ported42[2]))],
            ),
            (
                "NearbyButDistinct",
                "interior",
                [("Porteg30", (porteg30[0] + 0.06, porteg30[1], porteg30[2]))],
            ),
            (
                "FirstSharedInterior",
                "interior",
                [
                    ("PorteInterior", (1.0, 2.0, 3.0)),
                    ("GGrockx", (4.0, 5.0, 6.0)),
                ],
            ),
            (
                "SecondSharedInterior",
                "interior",
                [
                    ("porteinterior.001", (1.01, 2.0, 3.0)),
                    ("GGrockx", (4.0, 5.0, 6.0)),
                ],
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = []
            for stem, role, doors in fixtures:
                path = root / f"{stem}.glb"
                path.write_bytes(
                    compose._pack_glb(_door_fixture_document(stem, doors), b"12345678")
                )
                sources.append(compose._parse_glb(path, role, stem))

            glb, stats = compose.merge_sources(sources)
            document = converter.validate_glb_bytes(glb)

        suppression = stats["doorSeamSuppression"]
        self.assertEqual(suppression["anchorToleranceMeters"], 0.05)
        self.assertEqual(suppression["matchedPairCount"], 3)
        self.assertEqual(
            suppression["phases"]["exteriorPriority"]["matchedPairCount"], 2
        )
        self.assertEqual(
            suppression["phases"]["manifestInteriorPriority"]["matchedPairCount"],
            1,
        )
        self.assertEqual(suppression["remainingRenderableSeamMatches"], 0)
        self.assertTrue(suppression["assertionPassed"])
        self.assertEqual(
            {
                (
                    pair["normalizedOriginalName"],
                    pair["suppressedInterior"]["decorStem"],
                )
                for pair in suppression["pairs"]
            },
            {
                ("porteg30", "AHall30"),
                ("ported42", "AHall42"),
                ("porteinterior", "SecondSharedInterior"),
            },
        )

        source_nodes = {
            (
                node.get("extras", {})
                .get("omikronComposition", {})
                .get("decorStem"),
                node.get("name"),
            ): node
            for node in document["nodes"]
            if "omikronComposition" in node.get("extras", {})
        }
        self.assertIn("mesh", source_nodes[("Anekbah", "Porteg30")])
        self.assertIn("mesh", source_nodes[("Anekbah", "Ported42")])
        self.assertNotIn("mesh", source_nodes[("AHall30", "PORTEG30.001")])
        self.assertNotIn("mesh", source_nodes[("AHall42", " ported42 ")])
        self.assertIn("mesh", source_nodes[("NearbyButDistinct", "Porteg30")])
        self.assertIn(
            "mesh", source_nodes[("FirstSharedInterior", "PorteInterior")]
        )
        self.assertNotIn(
            "mesh", source_nodes[("SecondSharedInterior", "porteinterior.001")]
        )
        self.assertIn("mesh", source_nodes[("FirstSharedInterior", "GGrockx")])
        self.assertIn("mesh", source_nodes[("SecondSharedInterior", "GGrockx")])
        self.assertEqual(
            source_nodes[("AHall30", "PORTEG30.001")]["extras"]
            ["omikronComposition"]["doorSeamSuppression"]["rendering"],
            "mesh binding removed",
        )
        self.assertEqual(stats["meshes"], 9)
        self.assertEqual(stats["renderableMeshes"], 6)
        self.assertEqual(stats["triangles"], 9)
        self.assertEqual(stats["renderableTriangles"], 6)

    def test_teleport_zone_uses_only_the_source_parent_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = []
            for index, (stem, role) in enumerate(
                (("Anekbah", "base"), ("Aapden", "interior"))
            ):
                path = root / f"{stem}.glb"
                path.write_bytes(
                    compose._pack_glb(
                        _fixture_document(stem), bytes((index + 1,)) * 8
                    )
                )
                sources.append(compose._parse_glb(path, role, stem))

            zone = {
                "id": "apartment_den",
                "label": "Den Apartment",
                "translationGlbMeters": [883.0, 0.13, 319.0],
                "translationBlenderMeters": [883.0, -319.0, 0.13],
            }
            glb, stats = compose.merge_sources(
                sources,
                zone_assignments={"aapden": zone},
                zone_report={"assertionPassed": True, "relocatedInteriors": 1},
            )
            document = converter.validate_glb_bytes(glb)

        base_group = document["nodes"][3]
        den_group = document["nodes"][7]
        self.assertNotIn("translation", base_group)
        self.assertEqual(den_group["translation"], [883.0, 0.13, 319.0])
        self.assertNotIn("translation", document["nodes"][4])
        self.assertEqual(
            den_group["extras"]["omikronComposition"]
            ["teleportInspectionZone"]["id"],
            "apartment_den",
        )
        self.assertEqual(stats["teleportInspectionZones"]["relocatedInteriors"], 1)

    def test_manifest_order_is_authoritative_and_directory_is_not_globbed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "interiors"
            glb_directory = root / "glb"
            glb_directory.mkdir(parents=True)
            outputs = {}
            for stem, payload in (("Two", b"two"), ("One", b"one")):
                path = glb_directory / f"{stem}.glb"
                path.write_bytes(payload)
                outputs[stem] = {
                    "file": str(path),
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "valid": True,
                }
            (glb_directory / "Stray.glb").write_bytes(b"must not be selected")
            report = {
                "$schema": compose.INTERIOR_REPORT_SCHEMA,
                "level": "Anekbah",
                "valid": True,
                "conversionOptions": {
                    "lighting": "baked",
                    "include_cameras": False,
                    "include_lights": True,
                },
                "selection": {"resolvedUniqueInteriors": 2},
                "totals": {"interiors": 2},
                "interiors": [
                    {"decorStem": stem, "output": outputs[stem]}
                    for stem in ("Two", "One")
                ],
            }
            report_path = root / compose.INTERIOR_REPORT_NAME
            report_path.write_text(json.dumps(report), encoding="utf-8")

            resolved_report, _report, entries = compose._manifest_sources(root)

            self.assertEqual(resolved_report, report_path.resolve())
            self.assertEqual([entry[0] for entry in entries], ["Two", "One"])
            self.assertNotIn("Stray", [entry[0] for entry in entries])


if __name__ == "__main__":
    unittest.main()
