# Omikron OD3X to GLB

This is an independent Python pipeline for converting original-PC *Omikron: The
Nomad Soul* `.3DO` scenes and paired `.3DT` texture containers to GLB 2.0.
Anekbah is the reference level and the first complete build target.

The final Anekbah output now has two deliberately different forms:

- `Anekbah_complete.glb` is the portable, source-oriented level: the exterior
  plus 81 IAM-selected interiors, loading halls, connectors, and tunnels. Its
  duplicate interior door seams are non-rendering while their source nodes and
  resources remain available for provenance. Eight disconnected teleport-local
  interiors are separated into seven labeled inspection zones.
- `Anekbah_complete.blend` is the calibrated presentation scene: the same
  authored geometry plus the opening blue atmosphere and persistent billboard
  effects needed to resemble the original game in Blender. Duplicate door
  objects and their orphaned mesh datablocks are removed from this form.

The large `Asky` plane is **not** included in either final scene. It can still be
converted to `Asky.glb` as a standalone research artifact, but importing its raw
bounded plane as a sky was visually and behaviorally wrong. Anekbah's final
presentation instead uses the reference-matched world color and distance haze.

## Requirements

- Python 3.9 or newer.
- A legally obtained local installation of the PC game.
- Blender for the calibrated preview and final `.blend`; the pipeline has been
  exercised with Blender 5.2.0 LTS.
- Optional: the official [Khronos glTF Validator][validator] for independent
  conformance checking beyond the built-in structural verifier.

All examples below are PowerShell commands run from the repository root:

```powershell
$Repo = (Get-Location).Path
$Game = 'C:\Program Files (x86)\Steam\steamapps\common\Omikron'
$Out = Join-Path $Repo 'exports\Anekbah'
$Tool = Join-Path $Repo 'omikron_glb\omikron_glb.py'
$EffectsTool = Join-Path $Repo 'omikron_glb\anekbah_effects.py'
$InteriorsTool = Join-Path $Repo 'omikron_glb\anekbah_interiors.py'
$ComposeTool = Join-Path $Repo 'omikron_glb\anekbah_compose.py'
$Preview = Join-Path $Repo 'omikron_glb\blender_preview.py'
$Build = Join-Path $Repo 'omikron_glb\build_anekbah.ps1'
$Python = 'C:\path\to\python.exe'
$Blender = 'X:\SteamLibrary\steamapps\common\Blender\blender.exe'
$Validator = 'C:\path\to\gltf_validator.exe'
```

Change `$Blender` and `$Validator` for the current installations. The validator
is optional. The build finds `python.exe` on `PATH` and common Blender installs
automatically; pass `-PythonPath $Python` or `-BlenderPath $Blender` when either
program is elsewhere.

## One-command Anekbah build

`build_anekbah.ps1` is the reproducible entry point:

```powershell
& $Build `
  -GameRoot $Game `
  -OutputDirectory $Out `
  -BlenderPath $Blender `
  -ValidatorPath $Validator
```

The build performs, in order:

1. The converter, installed-data, interior-selection, and direct-composition
   regression suites in `test_omikron_glb.py` and
   `test_anekbah_compose.py`.
2. Baked conversion of the source Anekbah exterior.
3. Standalone conversion of `Asky` for research; it is not passed into the
   final scene as sky geometry.
4. Extraction of the smoke, glow, and explosion assets embedded in
   `anekbah.SCX`.
5. IAM-driven conversion of 81 canonical interiors, including all 27 available
   `AHall` loading halls, connector/tunnel assets, and 1,358 decoded explicit
   light records.
6. Direct JSON/BIN composition of the exterior and manifest-listed interiors
   into `Anekbah_complete.glb`, including deterministic two-phase door-seam
   suppression, deterministic teleport-local inspection zones, and no Blender
   import/export round trip.
7. Built-in verification of the exterior, effects, standalone sky research
   asset, combined level, and all 81 interior GLBs.
8. The same full set through Khronos validation when `-ValidatorPath` is
   supplied.
9. Blender composition and three source-camera renders under
   `previews_complete`, ending in `Anekbah_complete.blend`.
10. `anekbah_build_report.json`, with artifact roles, byte sizes, SHA-256
    hashes, and verifier counts.

Use `-SkipValidator` when the independent validator is unavailable. Use
`-SkipBlender` for conversion and validation without the final Blend/renders.

## Loading halls and interiors

Yes: the loading hallways remembered from the old 3DS Max tools are included.
The selection is derived from the installed game's `IAM/AREAS.TAG` labels and
the corresponding decor slot in `IAM/AREA`, not from a hand-written room list.
The canonical 81-asset set includes:

- 27 `AHall` transition/loading halls.
- `AImpasas`, a connector state associated with the impasse area.
- `TunelAQ1`, `TunelAQ2`, and `TunnelAS`.
- Apartments, bars, shops, hospital, morgue, pharmacy, restaurant, archive,
  police/security, cave, ventilation, and other Anekbah interiors and state
  sub-scenes referenced by the IAM.

The 73 exterior-connected interior models are already authored in Anekbah's
global coordinate space and retain identity source roots. Eight interiors have
no portal/door path to the exterior and use local coordinates around the
origin. The composer preserves every original node transform and moves only
those eight source parents into the inspection zones documented below.

The default canonical selection intentionally excludes:

- `Anekbah`, because it is the separately converted exterior.
- `AToit` and `AImpasse`, because they are outdoor sub-areas rather than
  interiors.
- `ABETSY`, because it is a mutually exclusive Betsy-specific state of the
  `Shunabku` apartment shell.
- `ACSGROTL`, because it is an alternate-light version occupying the same space
  as the Gandhar cave.

Two IAM references cannot be emitted because the installed PC data does not
contain their matching decor pairs: `ACSA4` (Archives04) and `CAVE-JEN` (Jenna's
cave). They remain explicit in `anekbah_interiors_report.json` rather than being
silently substituted.

### Teleport-local inspection zones

The portal connectivity audit identifies these disconnected sources:
`A_shootg`, `Aapden`, `Aapjenna`, `Aapkayl`, `ACSgrot`, `ACSgrots`, `AImpasas`,
and `Shunabku`. Their coincident local coordinate systems caused the cave and
several apartments to intersect near the origin even though gameplay reaches
them by teleport/area switching rather than a continuous exterior doorway.

`anekbah_zone_layout.json` moves them into a three-column inspection grid east
of the city. It defines seven zones because `ACSgrot` and its `ACSgrots` SAS
remain together. Each zone is lifted so its minimum Blender Z is zero. The GLB
uses translations on the eight `OMIKRON_SOURCE__*` parent nodes; Blender uses
seven labeled `OMIKRON_ZONE_ANCHOR__*` empties under
`OMIKRON_ANEKBAH_TELEPORT_ZONES`. The original child transforms remain
unchanged in both outputs.

The Blender geometry audit relocated 457 objects and measured zero
zone/exterior overlaps and zero inter-zone overlaps. Its measured zone bounds
agree with the layout declarations within `0.0005` meters.

### Duplicate door-seam suppression

Some loading halls and interiors repeat a portal already supplied by the city
or another interior. Rendering both copies causes the visible z-fighting seen
at `Porteg30`/`OMIKRON_INTERIOR__AHall30__Porteg30.001` and
`Ported42`/`OMIKRON_INTERIOR__AHall42__Ported42.001`.

The final composer resolves these seams using the normalized original portal
name and an authored/world anchor separation of at most `0.05` meters. The
policy is deterministic:

1. When an interior portal matches the exterior, the exterior copy wins.
2. For a remaining interior-to-interior match, the lower manifest/report index
   wins.

The deliberately narrow audited classifier accepts names containing `porte` or
`door`, plus the exact aliases `acoultoit`, `ba06eport4`, `csvdorb6`, and
`csvdorb7`. `GGrockx` is explicitly excluded because it is not a door. Phase 1
suppresses 90 exterior/interior duplicates and phase 2 suppresses 69
interior/interior duplicates: 159 suppressed copies in total, with zero
matching renderable seams left. Both reported user examples are covered by
phase 1.

The portable GLB keeps every suppressed source node and resource for audit and
provenance, but removes the suppressed node's mesh binding so only the winning
copy renders. Blender applies the same pair list more directly: it removes each
duplicate object and its now-unreferenced orphan mesh datablock. Both
`Anekbah_complete.composition.json` and
`previews_complete/blender_validation.json` list every winner/suppressed pair,
its phase, normalized name, anchors, and separation.

To inspect the selection without extracting payloads:

```powershell
python $InteriorsTool inspect $Game
```

To convert the canonical set:

```powershell
$InteriorsDir = Join-Path $Out 'interiors'
python $InteriorsTool convert $Game $InteriorsDir --overwrite
```

This writes each GLB beneath `interiors/glb` and an audited
`interiors/anekbah_interiors_report.json` containing source IAM references,
exclusions, missing assets, hashes, bounds, conversion statistics, the decoded
explicit-light inventory, and verification status. The canonical exports retain
all 1,358 explicit interior light records. Optional `--include-outdoor` and
`--include-alternate-states` switches are useful for research, but their output
is not the canonical no-overlap final level.

## Manual Anekbah workflow

### 1. Convert the exterior

The converter finds the `.3DT` beside its `.3DO` and embeds decoded textures as
lossless PNG images:

```powershell
New-Item -ItemType Directory -Force $Out | Out-Null

python $Tool convert (Join-Path $Game 'MESHES\DECORS\Anekbah.3DO') `
  --lighting baked `
  --camera-aspect-ratio (4 / 3) `
  -o (Join-Path $Out 'Anekbah.glb')
```

`--include-hidden` includes source meshes marked hidden. Other useful switches
are `--no-cameras`, `--no-lights`, `--texture-filter nearest`, and `--scale`.
Run `python $Tool convert --help` for the complete interface.

For the installed PC data used during development, Anekbah is OD3X 4.44 with
31,232 source vertices, 15,467 triangles, 15,482 quads, 860 meshes (one hidden),
20 texture materials, 53 doors, three cameras, 155 explicit lights, and 14
not-yet-decoded mesh-light records. Its source faces triangulate to 46,431
triangles before the hidden mesh is omitted.

### 2. Extract the embedded effect assets

```powershell
$EffectsDir = Join-Path $Out 'effects'
python $EffectsTool extract $Game $EffectsDir `
  --lighting baked `
  --camera-aspect-ratio (4 / 3) `
  --overwrite
```

This writes the exact embedded `.3DO/.3DT` pairs under `effects/sources`, one raw
GLB per effect, and `effects/anekbah_effects_report.json` with source offsets,
hashes, inspections, conversion statistics, and verification results.

| Raw asset | Source frames | Raw GLB triangles | Classification |
| --- | ---: | ---: | --- |
| `Anekbah_smoke.glb` | 8 quads in one row | 16 | Persistent |
| `Anekbah_glow.glb` | 1 quad | 2 | Persistent |
| `Anekbah_explosion.glb` | 16 quads in two rows | 32 | Scripted |

These are faithful source assets, not placed effects or a complete particle
runtime.

### 3. Convert and compose interiors

```powershell
$InteriorsDir = Join-Path $Out 'interiors'
$CompleteGlb = Join-Path $Out 'Anekbah_complete.glb'

python $InteriorsTool convert $Game $InteriorsDir --overwrite

python $ComposeTool `
  (Join-Path $Out 'Anekbah.glb') `
  $InteriorsDir `
  -o $CompleteGlb `
  --report (Join-Path $Out 'Anekbah_complete.composition.json') `
  --overwrite
```

The composer reads only the ordered, hash-checked files named by the interior
report. It rebases every glTF index and BIN offset directly, verifies the merged
document, and preserves each source root and node transform. It also performs
the audited two-phase portal suppression described above. The result does not
depend on Blender's glTF importer/exporter.

### 4. Build the final Blender presentation

`blender_preview.py` accepts the exterior, output directory, optional Blend
path, optional sky slot, effects directory, and interiors directory in that
order. The sky slot is positional; for Anekbah pass an omitted sentinel, not
`Asky.glb`:

```powershell
$PreviewDir = Join-Path $Out 'previews_complete'
$Blend = Join-Path $Out 'Anekbah_complete.blend'
$NoSky = Join-Path $Out '_OMITTED_ANEKBAH_SKY_'

& $Blender --background --factory-startup `
  --python $Preview -- `
  (Join-Path $Out 'Anekbah.glb') `
  $PreviewDir `
  $Blend `
  $NoSky `
  $EffectsDir `
  $InteriorsDir
```

This renders all three source cameras at 1280x960, writes
`previews_complete/blender_validation.json`, and saves the final Blend. The
report audits every report-listed interior and its unchanged transform, the
absence of preview-sky geometry, atmosphere nodes, material coverage, persistent
effect anchors, and render paths.

### 5. Verify the portable outputs

```powershell
$Glbs = @(
  (Join-Path $Out 'Anekbah.glb'),
  (Join-Path $Out 'Anekbah_complete.glb'),
  (Join-Path $EffectsDir 'Anekbah_smoke.glb'),
  (Join-Path $EffectsDir 'Anekbah_glow.glb'),
  (Join-Path $EffectsDir 'Anekbah_explosion.glb')
) + @(
  Get-ChildItem (Join-Path $InteriorsDir 'glb') -Filter '*.glb' -File |
    ForEach-Object { $_.FullName }
)

foreach ($Glb in $Glbs) {
  python $Tool verify $Glb
  & $Validator $Glb
}
```

The validator writes `<asset>.report.json` beside each input.

## Artifact roles

| Artifact | Role |
| --- | --- |
| `exports/Anekbah/Anekbah.glb` | Source-faithful exterior baseline with embedded textures, baked vertex lighting, cameras, mapped explicit lights, and source metadata. |
| `exports/Anekbah/interiors/glb/*.glb` | 81 individually reusable canonical interior/loading-area exports with baked/unlit decor, no cameras, and all 1,358 decoded explicit-light nodes retained. |
| `exports/Anekbah/interiors/anekbah_interiors_report.json` | IAM-derived ordered selection, provenance, exclusions, missing references, source/output hashes, bounds, and aggregate statistics. |
| `omikron_glb/anekbah_zone_layout.json` | Authoritative seven-zone layout, source-stem assignments, GLB/Blender translations, and non-overlapping declared bounds for the eight teleport-local interiors. |
| `exports/Anekbah/Anekbah_complete.glb` | **Final portable GLB:** exterior plus all 81 canonical report-listed interiors. Seventy-three connected interiors retain identity parents; eight teleport-local interiors use documented inspection-zone parent translations. Duplicate door nodes/resources remain for provenance, but their mesh bindings are suppressed. It deliberately excludes Blender-only atmosphere and effect instances. |
| `exports/Anekbah/Anekbah_complete.composition.json` | Direct-composition proof, source order and hashes, index-rebase verification, teleport-zone transforms/overlap assertions, all door-seam winner/suppressed pairs, totals, and final GLB hash. |
| `exports/Anekbah/Anekbah_complete.blend` | **Final Blender scene:** exterior, canonical interiors, seven labeled teleport inspection zones, duplicate door objects removed, calibrated steep distance haze, and persistent billboard effects; no raw Asky plane. |
| `exports/Anekbah/previews_complete/blender_validation.json` | Machine-readable proof of Blender imports, parent-only zone transforms and measured bounds, door-seam removals, material treatment, atmosphere/effects, and renders. |
| `exports/Anekbah/effects/Anekbah_{smoke,glow,explosion}.glb` | Separate raw source effect assets extracted from SCX. |
| `exports/Anekbah/Asky.glb` | Standalone research conversion of the bounded authored plane. It is not included or passed as the final sky. |
| `exports/Anekbah/anekbah_build_report.json` | One-command report with final artifact paths, roles, sizes, SHA-256 hashes, and verification counts. |

Generated GLB, Blend, PNG, and extracted source files remain game payloads; see
the copyright section before sharing them.

## First-arrival atmosphere and street lights

The final Blender scene targets Anekbah's iconic blue opening rather than the
complete day/night cycle. The selected original-PC frame gives a distant-sky
sample near sRGB `(77, 115, 149)`, converted to linear
`(0.0742, 0.1714, 0.3005)`.

Nearby geometry is deliberately left colorful. The haze begins at 50 meters and
reaches the reference background at 135 meters using a powered smoothstep:

```text
t = clamp((viewDistance - 50) / (135 - 50), 0, 1)
smooth = t*t*(3 - 2*t)
hazeFactor = smooth^1.6
```

This gives the comparison frame's sharper camera-distance falloff instead of
washing the whole street with a heavy uniform fog. It is a reproducible visual
calibration, not a claim that the original engine's exact fog equation or
day/night controller has been decoded.

The 102 persistent `neon`/street-light billboards now use a near-white,
low-saturation emissive preview treatment with strength `4.0`, matching the
bright coronas visible in the original more closely than the earlier dim
billboards. The exact game blend equation remains unresolved, so this is clearly
labeled as a Blender approximation. No new vent-steam plume or particle
simulation was added in response to the comparison; the existing raw smoke
asset and static persistent-frame preview remain available for provenance.

The final scene contains zero `OMIKRON_PREVIEW_SKY` objects. The world background
and haze carry the distant blue field; the raw `Asky` plane is omitted.

Visual calibration references:

- [Original-PC opening credits at the first Anekbah streets][opening-video]
- [Original-PC Anekbah street walkthrough][street-video]
- [Original-PC wide city views][wide-video]
- [Original-PC water behavior][water-video]
- [MobyGames Windows screenshot][moby-shot]

## Source fidelity and preview boundaries

The portable converter preserves geometry, per-corner UVs, decoded textures,
baked vertex colors, source normals and alpha data, cameras, doors and flags as
metadata, and the best currently known mapping of explicit lights. Coordinates
are right-handed, Y-up glTF at `0.025` meters per game unit (40 game units per
meter).

Special `0x40` mirror-pass meshes such as Abank's `AB_mirror` use an opaque
portable base surface. Their original vertex-alpha floats remain available in
`_OD3_ALPHA`, but are not copied into `COLOR_0.a`: without the game's missing
mirror/reflection pass, treating those values as ordinary opacity incorrectly
punches holes through the mirror. Ordinary glass, masked geometry, water, and
non-mirror effects retain the existing alpha policy.

`--lighting baked` is the fidelity target. Materials use
`KHR_materials_unlit`, with decoded texture multiplied by original `COLOR_0`
baked-light RGB. Source punctual-light nodes remain in the exterior GLB, but
unlit materials prevent them from lighting decor a second time. Interior GLBs
use the same policy: they omit cameras, retain all 1,358 decoded explicit
lights, and preserve authored baked world lighting without applying a second
dynamic pass.

See [`docs/OD3X_LIGHTS.md`](docs/OD3X_LIGHTS.md) for the 304-byte record layout,
legacy Max mapping, flag/range statistics, target-plane evidence, duplicate
audit, and the 514 unresolved header-only mesh-light references.

`--lighting dynamic` remains experimental. It enables PBR response, uses neutral
`COLOR_0`, retains baked RGB in `_OD3_BAKED_COLOR`, and lets heuristically mapped
`KHR_lights_punctual` lights affect the scene. Use baked mode for historical
appearance.

OD3X stores horizontal field of view in degrees; glTF stores vertical `yfov` in
radians. The converter preserves the PC game's authored 4:3 presentation:

```text
yfov = 2 * atan(tan(horizontal_fov / 2) / (4 / 3))
```

The enhanced Blend adds presentation behavior that core glTF cannot express
portably: camera-distance material haze, billboard constraints, and the
near-white emissive corona approximation. `Anekbah_complete.glb` intentionally
does not bake those Blender-only additions into the source-oriented geometry.

## Validation snapshot

The current `Anekbah_complete.glb` is 54,556,184 bytes and contains:

- 82 sources: one exterior plus 81 canonical interiors.
- 3,459 resource meshes and 5,730 resource primitives, retained for complete
  source provenance.
- 159,999 resource triangles.
- 3,300 renderable meshes and 158,354 renderable triangles after the 159
  duplicate portal mesh bindings are suppressed.
- 1,039 materials and 661 embedded images.
- Three exterior cameras and 1,513 mapped explicit lights: 155 exterior plus
  1,358 interior.
- Eight translated source parents in seven teleport-local inspection zones;
  the exterior and remaining 73 interior parents stay at identity. Those eight
  parents carry 201 of the interior lights with them.

The current build passes the built-in glTF container, index, and range checks
for all 87 emitted GLBs. Independent Khronos validation was not run for this
snapshot; supply `-ValidatorPath` to generate a fresh official report rather
than relying on an older report.
The composition report records source/manifest SHA-256 checks, complete index
rebasing, the 90 phase-1 and 69 phase-2 door suppressions, zero remaining
renderable seam matches, all seven non-overlap assertions, and the final output
hash. The Blender validation report confirms 1,513 light objects, restores the
authored cutoff distance on all 1,358 imported interior lights, moves 201 lights
with the teleport-local zones, and reports zero zone/exterior or inter-zone
overlaps.

The exterior baseline contains 859 emitted meshes, 63,770 emitted vertices,
46,415 GLB triangles, 47 material variants, and 20 embedded images. Its
built-in verification passes while retaining 13 source-degenerate triangles
for fidelity. The 81 interiors contribute 2,600 emitted meshes and 113,584
triangles.

## Known gaps

- **Missing installed assets:** IAM references `ACSA4` and `CAVE-JEN`, but their
  decor pairs are absent from the installed PC data. The report preserves those
  gaps explicitly.
- **Runtime area switching:** the complete GLB contains canonical authored room
  geometry together for inspection. Its separated teleport-local zones are an
  offline organization aid, not decoded runtime destinations. It does not
  reproduce loading triggers, room visibility streaming, portals, mutually
  exclusive state switching, or gameplay scripts.
- **Full particle runtime:** raw smoke/glow/explosion assets are extracted and
  persistent still-frame smoke/glow are composed in Blender. The preview does
  not simulate timing, motion, acceleration, animation over particle age, roll,
  color evolution, sounds, or a new vent-steam plume. Scripted explosions remain
  uninstanced.
- **Sprite rendering:** the exact runtime blend equation and sprite-type
  meanings remain unresolved. Blender's visible, dark-halo-safe treatment is an
  approximation and is labeled accordingly.
- **Mirror rendering:** core glTF does not reproduce the game's special mirror
  pass or planar reflection. Special mirror surfaces use their opaque textured
  base as a conservative fallback, with raw source alpha and flags retained for
  a future reflection implementation.
- **Sky/day-night behavior:** `Asky.glb` is exact bounded source geometry, but
  the engine's expansion, cloud motion, and full day/night controller are not
  decoded. The final scene omits the plane rather than presenting it incorrectly.
- **Lights:** 155 exterior and 1,358 interior explicit records are preserved as
  heuristic punctual spots with raw parsed fields retained in `extras`.
  Four flag classes, near attenuation, exact intensity semantics, and target
  plane behavior remain unresolved. The exterior reports 14 and the interiors
  report 514 additional header-only mesh-light references with no serialized
  records to decode. Baked/unlit mode remains the fidelity path.
- **NPCs and gameplay dressing:** actors, passer animation, trajectories,
  narrative overlays, character shadows, collision, triggers, and door
  animation are outside the static level conversion.

## Development checks

Run both regression suites directly. The current combined suite contains 14
tests:

```powershell
Push-Location (Join-Path $Repo 'omikron_glb')
try {
  python -m unittest -v test_omikron_glb.py test_anekbah_compose.py
}
finally {
  Pop-Location
}
```

Syntax-check every Python entry point and parse the PowerShell build script:

```powershell
python -m py_compile `
  (Join-Path $Repo 'omikron_glb\omikron_glb.py') `
  (Join-Path $Repo 'omikron_glb\anekbah_effects.py') `
  (Join-Path $Repo 'omikron_glb\anekbah_interiors.py') `
  (Join-Path $Repo 'omikron_glb\anekbah_compose.py') `
  (Join-Path $Repo 'omikron_glb\blender_preview.py') `
  (Join-Path $Repo 'omikron_glb\test_omikron_glb.py') `
  (Join-Path $Repo 'omikron_glb\test_anekbah_compose.py')

[scriptblock]::Create(
  (Get-Content (Join-Path $Repo 'omikron_glb\build_anekbah.ps1') -Raw)
) | Out-Null
```

For artifact-level regression, rerun `build_anekbah.ps1` and inspect
`anekbah_build_report.json`, `Anekbah_complete.composition.json`,
`interiors/anekbah_interiors_report.json`, every `*.glb.report.json`, and
`previews_complete/blender_validation.json`.

## Copyright and provenance

The tools do not ship game payloads. Manifests and reports contain only names,
offsets, decoded values, counts, relationships, and hashes. Generated GLB, PNG,
Blend, and extracted `.3DO/.3DT` files embed or reproduce copyrighted *Omikron*
geometry and textures. Keep them local and do not commit, package, or
redistribute them without the necessary rights. Every user should generate
artifacts from their own legally obtained installation.

The [Chevluh Blender importer][chevluh] was used only as an external behavioral
oracle while checking independent results and coordinate/scale assumptions. This
converter is not a copy or redistribution of that GPL project's code. Format
claims should ultimately be judged against installed game bytes, the
[glTF 2.0 specification][gltf-spec], and validator output.

[validator]: https://github.com/KhronosGroup/glTF-Validator
[gltf-spec]: https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html
[chevluh]: https://github.com/Chevluh/Omikron_Blender_Importer
[opening-video]: https://youtu.be/cdOnreFAsr0?t=140
[street-video]: https://youtu.be/UoSTK-hvq5U?t=30
[wide-video]: https://youtu.be/WQZiWnZipuc?t=6690
[water-video]: https://youtu.be/WQZiWnZipuc?t=13448
[moby-shot]: https://www.mobygames.com/game/1431/omikron-the-nomad-soul/screenshots/windows/30901/
