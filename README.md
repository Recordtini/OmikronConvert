# OmikronConvert

Independent tools for converting original-PC *Omikron: The Nomad Soul* OD3X
scenes into glTF 2.0/GLB and calibrated Blender scenes.

Anekbah is the reference implementation. The pipeline converts its exterior,
81 IAM-selected interiors and loading halls, embedded textures, baked vertex
lighting, cameras, explicit lights, door metadata, and selected particle
assets. It also documents the remaining reverse-engineering gaps instead of
silently inventing data.

See [the converter guide](omikron_glb/README.md) for requirements, commands,
validation details, artifact descriptions, and fidelity notes. Interior-light
format research is in
[`omikron_glb/docs/OD3X_LIGHTS.md`](omikron_glb/docs/OD3X_LIGHTS.md).

This repository intentionally contains no game assets or generated exports.
Run the tools against your own legally obtained installation.
