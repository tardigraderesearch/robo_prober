# Scan → segmented links → URDF

This pipeline takes a triangulated OBJ scan of a robot, divides it into
candidate link regions, and, once an operator has confirmed scale, link
boundaries, joint pivots, axes and limits, exports a portable visual URDF.
No physical actuation is part of this workflow, and nothing about the
mechanism is inferred automatically.

## What the tools do and do not establish

Connectivity cannot identify mechanical links: scanner bridges, wires and
touching surfaces can join moving parts, and disconnected pieces can belong to
one rigid part. The automatic proposal (`model propose`) keeps small islands as
`residual` and divides the largest component at low triangle-density cross
sections along its principal axis. That is a starting point for an operator,
not joint discovery. Expect to correct it with the operations below.

Each output directory includes separate full-resolution OBJ meshes, original
face IDs in `.faces.npy`, a reproducible `segmentation.json`, a `manifest.json`,
a shaded three-view preview, and `articulation.unconfirmed.json`. The manifest
records every mesh and face-map hash and its `source_from_local` rigid transform.
The source OBJ is preserved. Triangles are **assigned by centroid**, not clipped
or capped: cut edges can be jagged/open. No triangles are dropped, repaired,
welded, or invented. Coverage checks establish original-face disjointness, not
solid-volume nonintersection or collision safety.

## Use the segmentation tools

Install the geometry extra (`pip install "robo-prober[geometry]"`), then:

```bash
robo-prober model prepare scan.obj --local work/scan.obj --out work/inspection
robo-prober model propose work/scan.obj --parts 4 --out work/proposal.json
robo-prober model segment --config work/proposal.json --out work/candidates
robo-prober model validate-segments work/candidates/manifest.json
```

`model prepare` copies the source mesh (never modifying it), records its hash,
and reports connected components, bounds and degenerate faces. `model propose`
writes an editable segmentation config. Edit its `operations`, rerun
`model segment` into a new directory, and inspect the generated `preview.png`
(X/Y, X/Z and Y/Z orthographic views with source-coordinate bounds and a color
legend). Keep `confirmed: false` in segmentation configs. Source hashes bind
every edit to the exact mesh. Re-running a segmentation does not overwrite an
edited articulation scaffold; a stale manifest hash must be resolved before export.

Operations execute in order. Each split must produce two nonempty regions.
Names must be unique identifiers; misspelled regions, empty selections, and
name collisions fail rather than silently discarding geometry.

```json
{"kind":"plane", "region":"body", "normal":[1,0,0], "offset":10,
 "negative":"near", "positive":"far"}
```

A plane selects triangles whose source-space centroid satisfies
`dot(normal, centroid) < offset`; equality goes to the positive side. A normal
need not be normalized, but its offset must use the same scaling. This is the
explicit cutting-plane control; use a region name to restrict the cut.

```json
{"kind":"box", "region":"far", "min":[50,-1000,-1000], "max":[200,1000,1000],
 "inside":"end", "outside":"arm"}
```

Box selection uses inclusive source-space centroid bounds. Multiple localized
boxes/planes let you isolate an elbow or remove a piece mistakenly assigned to
a neighboring link without cutting the entire scan.

```json
{"kind":"faces", "region":"arm", "face_ids":[100,101,102],
 "selected":"correction", "remainder":"arm_rest"}
```

Face IDs are **zero-based original triangular OBJ face order**, independent of
mesh export numbering. All selected faces must belong to the named region.
For a precise correction, select original IDs found in the `.faces.npy` maps
and inspect the new preview. Do not apply these example IDs blindly to your own scan.

```json
{"kind":"merge", "regions":["correction","end"], "name":"end_corrected"}
```

Merging is how you assign disconnected islands to a confirmed rigid part or
correct a previous split. Residual debris is deliberately retained: classify
or merge it explicitly. The exporter currently requires every region in the
kinematic graph; remove debris in a separately documented source revision if
it should truly be excluded, rather than claiming lossless coverage afterward.

## Confirm geometry and define articulation

Copy the selected output's `articulation.unconfirmed.json` to a new file and
fill it in.

A complete articulation config must provide:

- `scale.meters_per_source_unit`, `confirmed: true`, and `measurement` describing
  the named physical endpoints, measured distance/units, and corresponding scan
  points. Do not assume that source values are millimeters.
- Every region exactly once in `links`, with `confirmed: true` and a nonempty
  `confirmation` explaining the boundary decision. Optional `marker_ids` are
  unique across links; an empty list means tag mounting/mapping remains unassigned.
- Each link's `source_frame`: a proper 4×4 **source-from-link** transform at the
  scanned zero pose. Its translation is the link origin/pivot in source units;
  its rotation columns are the link axes expressed in source coordinates.
  Reflections, shear, embedded scale, NaNs and nonrigid matrices are rejected.
- A connected parent/child tree in `joints`, with unique joint names, `type`
  (`fixed`, `revolute`, `continuous`, or `prismatic`), explicit confirmation,
  and—for moving joints—a unit `axis` in the child/joint frame. The child frame
  origin is the pivot. Joint origin is computed as
  `inverse(source_from_parent) @ source_from_child`; it is not guessed.
- For revolute/prismatic joints, measured/manual `lower` and `upper` limits;
  moving joints also require explicit positive `effort` and `velocity` for URDF.
  Revolute angles use radians, prismatic travel uses meters, effort uses N·m or
  N, velocity uses rad/s or m/s. Continuous joints omit angular bounds. Do not
  copy synthetic effort/velocity/limits into the physical model.

The validation gate rejects unconfirmed scale, regions, axes, joints, stale
segmentation, graph cycles, multiple parents, duplicate tags, invalid limits,
and bad transforms. A confirmation string is an operator assertion, not
independent proof that a physical measurement was performed.

```bash
robo-prober model articulate --config work/my-confirmed-model.json --out work/urdf
robo-prober model articulate --config work/my-confirmed-model.json --positions work/joint-positions.json --out work/urdf-posed
```

The optional positions JSON maps moving joint names to radians/meters. Values
outside confirmed limits are rejected. Each bundle contains `robot.urdf`,
relative `meshes/*.obj` paths (mesh coordinates already in meters), the exact
articulation config, a validation report, and `articulated.png`. `yourdfpy`
actually reloads the XML and mesh files, validates them, and computes the
preview transforms. The URDF root frame may differ from the scan frame; the
report retains `source_from_root`. Move the complete bundle to preserve paths.

This is a portable **visual kinematic** URDF. No fabricated inertia, collision
model, controller bindings, dynamics, actuator safety, or physical movement is
claimed. A root rotation/translation changes the display frame, not physical
measurements.

## Synthetic example

`examples/build_synthetic_articulation.py` builds an explicitly **synthetic**
two-box arm. Each box is defined as one meter long; the hinge is at `[1,0,0]`
with axis Z. The distal endpoint moves analytically from `[2,0,0]` to `[1,1,0]`
at π/2. These numbers are fixture definitions, not measurements.

```bash
python examples/build_synthetic_articulation.py synthetic-arm
```

This writes the source OBJ, segmentation, confirmed articulation config, and
two URDF bundles (`zero/` and `bent/`) with previews and validation reports.
The test suite runs the same builder.
