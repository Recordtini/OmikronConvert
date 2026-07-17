# OD3X interior-light research

This note documents the light data used by the Anekbah conversion pipeline. It
is based on the installed PC assets, the independent Python parser in
`omikron_glb.py`, and the behavior of Abjab's historical MaxScript importer.

## What was missing

The 81 canonical Anekbah interior files contain 1,872 source light references:

| Source representation | Count | Current handling |
| --- | ---: | --- |
| Serialized 304-byte explicit-light records | 1,358 across 80 interiors | Decoded and emitted as `KHR_lights_punctual` spot lights |
| Header `numMeshLights` count | 514 across 48 interiors | Reported only; no independent record section has been found |

`ACScosas` is the only canonical interior with no explicit-light record. It has
one `numMeshLights` reference.

The previous canonical interior exporter set `include_lights=False`, so all
1,358 decoded records were omitted even though the exterior retained its 155
explicit lights.

## The 304-byte explicit-light record

| Offset | Size | Parsed value |
| ---: | ---: | --- |
| `0x00` | 2 | flag 1, unsigned |
| `0x02` | 2 | flag 2, unsigned |
| `0x04` | 20 | null-terminated CP850 name |
| `0x18` | 4 | float 1; legacy far-attenuation end |
| `0x1C` | 4 | float 2; legacy far-attenuation start |
| `0x20` | 4 | source intensity |
| `0x24` | 4 | float 4; unresolved secondary value |
| `0x28` | 4 | float 5; unresolved secondary value |
| `0x2C` | 4 | BGRA color bytes |
| `0x30` | 192 | six 32-byte point slots: XYZ float vector plus 20 unknown bytes |
| `0xF0` | 64 | unresolved trailing bytes |

The unknown 184 bytes are zero in all installed Anekbah interior explicit-light
records. All parsed floats are finite.

Four flag combinations occur:

| `(flag1, flag2)` | Records |
| --- | ---: |
| `(2, 0)` | 493 |
| `(2, 0x4000)` | 432 |
| `(18, 0)` | 244 |
| `(18, 0x4000)` | 189 |

Their runtime meaning remains unresolved. Some same-position records differ by
these flags, suggesting state, receiver, or volume classification. They must
not be discarded or merged without engine evidence.

## Legacy importer interpretation

The MaxScript reads each explicit record and creates a hidden free spotlight:

- point 1 is the light position;
- point 2 is its target;
- hotspot is 40 degrees full angle;
- falloff is 120 degrees full angle;
- multiplier is the raw source intensity;
- float 2 is far-attenuation start;
- float 1 is far-attenuation end;
- flags are ignored.

The script contains a disabled experiment that aims additional spotlights at
points 3 through 6 using the two secondary floats. In 1,319 of 1,358 installed
records, points 3 through 6 form a rectangular target plane whose center agrees
with point 2 within 0.01 game unit. This strongly suggests cone/frustum-shape
data rather than four additional runtime lights. The converter preserves all
six points as metadata but does not synthesize five lights from one record.

## glTF mapping

The converter follows the active legacy interpretation while preserving every
known raw field in `light.extras.omikron`:

- game coordinates become right-handed glTF Y-up coordinates using
  `(X, -Y, -Z) * 0.025`;
- point 1 supplies translation and point 2 supplies the local `-Z` direction;
- the 40/120-degree full cones become 20/60-degree glTF half-angles;
- float 1 becomes `range` after the 0.025 metres-per-unit scale;
- intensity becomes candela using the configurable multiplier, default `100`;
- source BGRA becomes normalized RGB;
- near attenuation, flags, secondary floats, and all six points remain extras.

Across the installed interiors, both the mapped median and 90th-percentile
range are 19.69 metres because 787.4016 game units is a common authored cutoff;
the maximum is 283.64 metres. Mapped intensities span 50–1,000 candela with the
default scale.

`KHR_lights_punctual` cannot express the source near-attenuation start and uses
physical inverse-square/candela semantics that the 1999 engine did not. The
mapping is therefore deliberately labeled best-effort.

## Baked-world policy

Canonical exports retain original baked vertex colors and use
`KHR_materials_unlit`. Explicit light nodes are included for source
completeness, inspection, and possible actor/runtime work, but do not relight
the static decor a second time. Switching the final scene wholesale to dynamic
PBR would produce a less faithful result.

The eight teleport-local interiors contain 201 explicit lights. Their source
nodes remain children of the same composition parents as their geometry, so the
inspection-zone translations move lights and meshes together.

Representative source counts:

| Interior | Explicit | Header-only mesh lights | Notes |
| --- | ---: | ---: | --- |
| `Aapden` | 34 | 15 | Mostly warm amber/white, intensity 2.5–5 |
| `Aapjenna` | 33 | 16 | Intensity 5; 11 repeated semantic records |
| `ACSgrot` | 50 | 0 | Red records, intensity 1–5 |
| `ACSgrots` | 7 | 0 | Mostly cyan |
| `Shunabku` | 7 | 0 | Orange/white |

## Why `numMeshLights` is not synthesized

No second 304-byte light section follows the explicit records:
`lightsOffset + numLights * 304` reaches EOF in checked exterior and interior
files. The legacy importer calls `numMeshLights` “Other/Unknown Lights” but
never parses a location or record for them. They are likely mesh/editor/static
lighting already represented by baked vertex colors, but that remains an
inference.

The count also does not match any mesh or mover-flag subset; for example,
`AHall30` reports 22 mesh lights but contains only 17 meshes. The pipeline
reports all 514 references as `undecodedMeshLights` and creates no invented
punctual nodes for them.

## Duplicate records

The 1,358 records reduce to 1,191 unique parsed records before composition.
There are 165 redundant records in 99 semantic groups after inspection-zone
placement. These are retained because some differ only by unresolved flags and
may represent runtime lighting states or receiver classes. No exact semantic
match was found between the interior records and the exterior's 155 lights.

## Remaining research

- Determine both flag fields from engine code or runtime state changes.
- Identify the intended role of points 3 through 6 and the secondary floats.
- Establish the original attenuation equation and intensity/color space.
- Identify exactly what contributes to `numMeshLights`.
- Test whether explicit records primarily light actors, particles, or world
  geometry at runtime.
