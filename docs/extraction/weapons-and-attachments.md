## Contents

1. [Scope and module map](#1-scope-and-module-map)
2. [Where a weapon lives: templates, prefabs, CAB dependencies](#2-where-a-weapon-lives-templates-prefabs-cab-dependencies)
3. [What gets installed: presets, slots, filters, and the roll](#3-what-gets-installed-presets-slots-filters-and-the-roll)
4. [How the geometry is assembled](#4-how-the-geometry-is-assembled)
5. [The `.eftweap` container: exact layout](#5-the-eftweap-container-exact-layout)
6. [The aim block: optics, eye relief, field of view](#6-the-aim-block-optics-eye-relief-field-of-view)
7. [Attachment to the character: `Weapon_root` and the `q4` undo](#7-attachment-to-the-character-weapon_root-and-the-q4-undo)
8. [Materials on the consumer side](#8-materials-on-the-consumer-side)
9. [What is dropped](#9-what-is-dropped)
10. [Invariants and their failure signatures](#10-invariants-and-their-failure-signatures)
11. [Old patterns](#11-old-patterns)

Every number below was measured on 2026-08-10 against this repo and the live install at
`<game>/EscapeFromTarkov_Data` (bundle tree: 7,561 files,
40,683,386,221 bytes, newest mtime 2026-08-05T07:16:23, per the CAB index stamp in
`packs/shared/unity_cabs.json`). The reference pack is `out/weapons/weapon_colt_m4a1_556x45`,
built 2026-07-31. Companion document for the rig this hangs on:
[characters-and-animation.md](characters-and-animation.md).

---

## 1. Scope and module map

A weapon in this repo is not an asset, it is a **build**: an item template plus a tree of installed
mods, each contributing one prefab, all baked down to ONE merged mesh with per-submesh materials
and written as an `.eftweap` pack. The same assembler also bakes non-weapon equipment (a helmet, a
rig) when handed an empty install map, which is why the module is a general "assemble an item"
routine that happens to be named after guns.

| file | role |
|---|---|
| `extraction/characters/loadout.py` | rolls WHICH weapon and WHICH mods, from the game's own tables |
| `extraction/characters/build_weapon.py` | prefab tree → merged mesh, materials, textures, aim block |
| `extraction/characters/unity_deps.py` | CAB → bundle index, so a prefab's mesh in a sibling bundle resolves |
| `extraction/characters/build_loadouts.py` | drives the roll + the bake per bot, writes `out/kits/<bot>_<seed>/kit.json` |
| `extraction/characters/fetch_bot_db.py` | banks `bot_loadouts.json`, `customization.json`, `globals.json` into `packs/shared/` |
| `tools/blender/import_eftweap.py` | `.eftweap` → Blender mesh, optionally bone-parented |
| `viewer/src/character/weapon.rs` | `.eftweap` → Bevy meshes + materials |
| `viewer/src/character/mod.rs` | attaches the player's weapon and publishes `PlayerAim` |
| `viewer/src/npc.rs` | attaches the shared NPC weapon |
| `viewer/src/character/drive.rs` | the aim-down-sights solve that consumes the aim block |

Two entry points, and they differ in what they install:

```
build_weapon.py --item <tpl>            # BSG's FACTORY preset for that weapon
build_loadouts.py --bot pmcusec --count 4   # loadout.roll(): the bot's OWN rolled mod tree
```

The shipped `out/weapons/weapon_colt_m4a1_556x45` is the second kind. It is exactly
`loadout.roll("pmcusec", 0)` - verified by rebuilding from that roll and byte-comparing `mesh.bin`
(identical SHA-256 prefix `d2e4104f0891a970`, 7.3 s). The factory M4 preset is a *different* gun:
12 mods, a 370 mm barrel, a carry-handle rear sight and a 30-round STANAG, where the rolled one has
15 mods, a 260 mm barrel, an HHS-1 and a PMAG D-60.

---

## 2. Where a weapon lives: templates, prefabs, CAB dependencies

### 2.1 Item id → prefab bundle

`packs/shared/item_templates.json` is BSG's item table, keyed by the 24-hex template id. Measured:
**4,441 templates, 4,248 of them carrying `_props.Prefab.path`, 1,137 carrying a non-empty
`_props.Slots[]`.** The path is relative to `StreamingAssets/Windows` and is used verbatim
(`build_weapon.py:716-719`):

```
5447a9cd4bdc2dbd208b4567  weapon_colt_m4a1_556x45
  _props.Prefab.path = assets/content/weapons/m4a1/weapon_colt_m4a1_556x45_container.bundle
```

There is no search, no name matching, no fallback: a missing file prints `[skip] <name>: bundle
missing (<rel>)` and that subtree contributes nothing (`build_weapon.py:720-722`).

`item_templates.json` is NOT refreshed by `fetch_bot_db.py` - its download loop covers only
`templates/customization.json` and `globals.json` (`fetch_bot_db.py:104-105`), and the
`--skip-items` flag it declares at `:77` is never read. The item table must be supplied out of
band (the source repo ships it under LFS).

### 2.2 The container bundle is a prefab, not a mesh

Measured on the M4 container: **321 Transforms, 20 renderers, 69 MonoBehaviours, 5 roots.** The
geometry is not in it. Renderer mesh PPtrs carry `m_FileID > 0`, i.e. an index into that serialized
file's externals list, each naming a CAB. `unity_deps.resolve_into` reads the container's
`AssetBundle.m_Dependencies`, maps each CAB through the prebuilt index and loads the providers into
the SAME `UnityPy.Environment` (`unity_deps.py:118-150`, capped at 64 deps per container). It
returns only the objects the container itself introduced, because the dependency bundles carry
unrelated assets and baking them merges other guns into yours.

Measured for the full rolled M4 build: **16 distinct templates baked, 78 declared CAB
dependencies, 0 unresolvable.** The index itself holds **14,738 CAB entries over 7,560 bundle
files**, of which **1,160 files have no extension at all and provide 2,286 of those entries** - the
reason `is_bundle` tests the `UnityFS`/`UnityWeb`/`UnityRaw`/`UnityArchive` header rather than the
filename (`unity_deps.py:33-46`).

Objects are addressed within their own serialized file, so a same-file PPtr is resolved by the
`(assets_file identity, path_id)` pair, never by path id alone - once dependency bundles share the
environment, bare path ids collide (`build_weapon.py:109-118`).

### 2.3 A container ships several model variants, and they are in different frames

The M4 container's five roots, with their subtree sizes:

| root GameObject | nodes |
|---|---:|
| `weapon_colt_m4a1_556x45_model.generated` | 83 |
| `weapon_colt_m4a1_556x45_model_simple.generated` | 80 |
| `weapon_colt_m4a1_556x45_simple.generated` | 78 |
| `weapon_colt_m4a1_556x45.generated` | 78 |
| `weapon_colt_m4a1_556x45_container` | 2 |

`Bundle.variant()` scores each root: `+100,000` if the subtree contains a node named `Weapon_root`,
`−50,000` if the root's name contains `simple`, plus the node count as a tiebreak
(`build_weapon.py:288-297`). Measured: `weapon_colt_m4a1_556x45_model.generated` wins with 83
nodes. Every later query - socket lookup, renderer filter, subtree tests - is restricted to that
one variant. **The scoring rule is AUTHORED**; that the game loads exactly one variant is an
inference from the data shape, not read out of a component, and is *unverified*.

The chosen variant's top three levels, read out of the bundle:

```
weapon_colt_m4a1_556x45_model.generated
  Base HumanLCollarbone / Base HumanLUpperarm / Base HumanLForearm1
  Base HumanRCollarbone / Base HumanRUpperarm / Base HumanRForearm1
  Camera_animated
  Weapon_root / Weapon_root_anim / weapon
```

That is the whole attachment contract, stated by the game's own data: the container ships a partial
arm rig whose bone names are the character rig's names, and the weapon hangs under a node called
`Weapon_root` - which is bone index 68 of the shared 79-bone rig. §7 uses this.

### 2.4 Socket nodes carry the slot names

`_props.Slots[]._name` and the prefab's GameObject names agree. The M4 template declares six slots;
the chosen variant carries seven `mod_*` nodes:

| template slot | required | filter size | node present |
|---|---|---:|---|
| `mod_pistol_grip` | yes | 30 | yes |
| `mod_magazine` | no | 20 | yes |
| `mod_reciever` | yes | 6 | yes |
| `mod_stock` | yes | 15 | yes |
| `mod_charge` | yes | 10 | yes |
| `mod_charge_001` | no | 1 | yes |
| - | - | - | `mod_magazine_new` (node with no template slot) |

The prefab is a **superset** of the template. Measured across the whole rolled M4 build: all 15
installed edges found a node of the same name (the build printed no `[warn] no node ... in prefab`
lines). A missing node is warned about and the subtree is dropped (`build_weapon.py:730-733`).

---

## 3. What gets installed: presets, slots, filters, and the roll

### 3.1 The factory build (`ItemPresets`)

`packs/shared/globals.json` → `ItemPresets` is BSG's own table of factory builds. Measured: **372
presets, 247 flagged `_encyclopedia`, covering 248 distinct root templates.** Each preset is a flat
list of item *instances* with `_id`, `_tpl`, `parentId`, `slotId`. Flattening to
`{parentTpl: {slot: childTpl}}` is four lines (`build_weapon.py:794-808`, duplicated in
`loadout.py:105-112`); where two presets share a root, the `_encyclopedia` one wins
(`build_weapon.py:822-823`).

The flattening is keyed by **template**, not by preset instance id. Two instances of one template
inside a preset therefore collapse onto one entry and the last write wins (`loadout.py:112`). That
same keying propagates all the way into the baker (§4.3).

Measured: the M4's encyclopedia preset is 13 items → 12 installed mods over 5 parent templates, and
**none of the 248 factory maps contains a cycle**.

### 3.2 The roll (`loadout.build_weapon_tree`)

`packs/shared/bot_loadouts.json` holds, per bot type (63 measured), the two tables that matter:

```
inventory.equipment.FirstPrimaryWeapon = {itemId: WEIGHT}   the game's own spawn frequencies
inventory.mods[parentTpl][slot]        = [allowed child ids]  what THAT bot may bolt on
```

The weapon is a weighted pick (`loadout.py:54-68, 160`). The tree is then walked from the weapon
template down (`loadout.py:114-141`). Per slot, in the template's own `Slots[]` order:

1. `allowed = _props.Slots[i]._props.filters[0].Filter` - the legality set (`loadout.py:71-78`).
2. `cands = [c for c in bot_mods[parentTpl][slot] if c in allowed]`. If non-empty, pick one with
   `rng.randrange(len(cands))` - **uniform**, not weighted, because the mods table is a plain list
   in BSG's data and carries no weights (`loadout.py:121-124`).
3. Else the factory part for that slot, **if and only if it is in `allowed`** (`loadout.py:125-126`).
4. Else nothing - *including for a required slot* (`loadout.py:132-133`).

**Rule 4 is the load-bearing one.** Filling a required slot with `sorted(allowed)[0]` picks by
alphabet, which is how a fixed A2 rear sight once ended up installed under an HHS-1 - a build the
game would never produce. An incomplete gun is honest; an invented one is not.

Legality is per parent, so the check is the *parent's* filter and not a global whitelist. Measured
on the pmcusec table and the M4 receiver: BSG offers 2 barrels, 3 handguards, 3 scopes and 4 rear
sights, against a receiver `mod_sight_rear` filter of 11 entries - the bot table is the narrower of
the two, and the intersection is what actually varies. Note that the A2 rear sight *does* appear in
that bot table, so the shipped M4 legitimately carries both an HHS-1 and a fixed A2 rear: the
folding logic in §4.4 only reaches sights that carry an `AutoFoldableSight` component, and the A2
does not.

Measured over **63 bot types × 4 seeds = 252 rolls, 208 of which name a weapon** (11 bot types ship
no `FirstPrimaryWeapon` table at all): 4 to 22 mods each (mean 11.8), tree
depth 1 to **6** (mean 3.08), **zero cycles**, and **0 of the 780 reached required slots left
empty**.

### 3.3 Recursion, guards, and what "one install map" implies

`build_weapon_tree` guards `depth > 8` and a `seen` set at entry (`loadout.py:99-101`); the inner
`rec` guards `d > 8` and `tpl in install` (`loadout.py:115`). Measured maximum depth is 6, so the
guard has never fired on shipped data.

`install` is `{parentTpl: {slot: childTpl}}` - **keyed by template id, not by position in the
tree.** A template appearing at two places is expanded once and *both* places inherit the same
children. This is not theoretical: measured, **16 of 208 rolled trees** repeat a template, e.g.
`arenafighter` seed 3 puts `mount_vector_kriss_side_rail` on a Kriss Vector twice and both rails
receive the same `tactical_all_steiner_9021_dbal_pl`.

The consumer of that map, `build_weapon.build`, has **no guard of its own**: its `depth` parameter
is accepted and never read (`build_weapon.py:697`), and its inner `rec` (`:712-736`) carries
neither a depth counter nor a visited set. Safety rests entirely on the map being acyclic, which is
measured true for all 248 factory maps and all 208 rolled trees but is not enforced. A cyclic map
would recurse until Python's stack limit.

### 3.4 Determinism

One `random.Random(f"{bot_type}:{seed}")` serves the whole kit (`loadout.py:158`): the weapon pick,
every mod choice, then every equipment slot. The stream is therefore **shared** - changing the mod
tree shifts every later equipment draw, so a bot's backpack depends on its gun. Deterministic
across runs, reloads and machines; not stable across a change to the tables or to the walk order.

---

## 4. How the geometry is assembled

`build_weapon.bake` (`:433-694`) appends one prefab's renderers to the growing vertex/index/submesh
arrays under a caller-supplied matrix. `build_weapon.build.rec` (`:712-736`) walks the install map
and supplies those matrices.

### 4.1 The mount frame (`root_inv`) - DERIVED geometry, AUTHORED rule

Every prefab is baked **relative to an anchor node**, not to bundle-absolute space. The anchor is
chosen in this order (`build_weapon.py:333-366`):

1. the node named `Weapon_root` inside the chosen variant - a weapon container;
2. else the first renderer whose GameObject name contains `_lod0` **and whose mesh actually
   resolves** - a mod prefab;
3. else the first renderer with no `_lod` in its name whose mesh resolves;
4. else the variant root with the most descendants.

Then the 3×3 is **orthonormalised** (columns divided by their norms) and only the translation is
kept, before inverting (`build_weapon.py:374-387`). A mount is a *frame*: it says where a part sits
and which way it faces, not how big it is. Inverting a scaled anchor rescales every vertex.

Why the anchor matters, measured:

- The M4 container's `Weapon_root` sits at translation **(0.1568, 1.4140, 0.0174)** in bundle
  space. Baking absolute would put the assembled gun 1.4 m from the hand.
- A mod prefab's LOD node carries a **−90° X rotation** relative to its root. Measured exactly, on
  `reciever_ar15_colt_m4a1_std` and on `scope_all_eotech_hhs_1_tan`, the LOD0 node's local-to-root
  matrix is the permutation with columns `(+X, −Z, +Y)` and zero translation - i.e. the DCC-Z-up →
  Unity-Y-up fixup. Anchoring on the prefab root bakes it in and every mod comes out turned 90°
  from the weapon body, whose own chain is rotation-free relative to `Weapon_root`. Anchoring on
  the mesh node cancels it: for the anchor renderer itself, `root_inv @ world_of(anchor)` reduces
  to the node's scale alone.
- Rule 2's "and whose mesh actually resolves" is structural, not cosmetic. Measured on the LA-5,
  its laser emitters are Unity primitive gizmos named `Sphere`/`sphere` carrying scales of
  **0.0040, 0.0090 and 0.0016** (1/252 ≈ 0.00397). Those primitives live in Unity's built-in
  library, which the game does not ship, so the mesh read raises and the candidate is excluded
  structurally.

**DERIVED**: every transform in the chain. **AUTHORED**: which node counts as the mount frame for a
mod, and the orthonormalisation. The game's own answer lives in runtime code that instantiates the
prefab at the socket; it is not captured, so the rule above is this repo's reconstruction, adopted
because the alternative was measured wrong.

### 4.2 The slot chain

For the root item, `base_M = I`. Then, for every renderer of a bundle:

```
M = base_M @ bundle.root_inv() @ bundle.world_of(renderer_transform)
```

and for every installed child:

```
base_M_child = base_M @ bundle.root_inv() @ bundle.world_of(slot_node)
```

(`build_weapon.py:524` and `:736`.) So a mod at depth *k* is placed by

```
( Π over ancestors  root_inv_i @ world_of(socket_i) ) @ root_inv_child @ world_of(renderer)
```

**Yes, the baker composes each mod prefab's transform down the slot chain, and a sight's position
is entirely DERIVED** - it is the receiver's `mod_scope` node transform, composed under the
weapon's own anchor, with the sight's internal node tree on top. No item carries an authored
offset anywhere in this pipeline.

The M4 pack is the proof. Per-material centroids, in the pack's own space where `Weapon_root` is
the origin and downrange is −Y:

| part (by material) | centroid | extent |
|---|---|---|
| `stock_ar15_lmt_sopmod_LOD0` | (0.000, −0.106, −0.047) | 0.063 × 0.199 × 0.130 |
| `stock_ar15_colt_stock_tube_std_LOD0` | (−0.000, −0.137, −0.004) | 0.035 × 0.196 × 0.040 |
| `pistolgrip_ar15_damage_industries_ecs_fde_LOD0` | (−0.000, −0.237, −0.100) | 0.028 × 0.088 × 0.098 |
| `sight_rear_ar15_colt_a2_LOD0` | (0.004, −0.279, 0.045) | 0.047 × 0.069 × 0.050 |
| `scope_all_eotech_g33_LOD0_tan` | (−0.000, −0.292, 0.060) | 0.053 × 0.099 × 0.078 |
| `reciever_ar15_colt_m4a1_std_LOD0` | (−0.012, −0.326, −0.000) | 0.059 × 0.206 × 0.058 |
| `mag_stanag_magpul_pmag_d-60_556x45_60_LOD0` | (−0.000, −0.393, −0.101) | 0.111 × 0.113 × 0.191 |
| `handguard_ar15_geissele_smr_mk16_95_inch_LOD0` | (−0.000, −0.534, −0.001) | 0.043 × 0.243 × 0.061 |
| `tactical_all_insight_la5_LOD0` | (0.001, −0.559, 0.042) | 0.080 × 0.122 × 0.043 |
| `gas_block_..._front_sight_gas_block_std_LOD0` | (−0.000, −0.632, 0.020) | 0.022 × 0.053 × 0.089 |
| `muzzle_ar15_surefire_sf4p_fh556rc_..._LOD0` | (0.000, −0.701, −0.001) | 0.025 × 0.067 × 0.025 |
| `silencer_socom_surefire_socom556_rc2_..._LOD0` | (0.000, −0.745, −0.000) | 0.036 × 0.154 × 0.040 |

Buttstock nearest the socket, silencer furthest, magazine hanging at −Z, optics riding at +Z. Whole
pack bounding box **0.1108 × 0.8158 × 0.2953 m**, Y spanning −0.8220 to −0.0062 - the socket sits
6 mm behind the buttpad and the muzzle is 82 cm downrange. Nothing in that table was authored.

### 4.3 Coordinate handling

Identical to the map and character pipelines: `G3 = diag(−1, 1, 1)` applied to positions and to
normals (`build_weapon.py:43, 574, 582`), triangle winding swapped `(a,b,c) → (a,c,b)`
(`:687-688`), and UV `v → 1 − v` because Unity's origin is bottom-left and wgpu's is top-left
(`:588-593`). Units are metres; the manifest declares
`{"world": "viewer (X-flipped from Unity)", "windingFlipped": true}` and nothing else.

Two details that bite:

- `MeshHandler` decodes the vertex/index **streams**; the typetree attributes are empty for these
  bundles (`m_Vertices == []`), so a raw typetree read assembles nothing (`:557-564`).
- `SubMesh.firstByte` is a **byte** offset. The index width comes from
  `MeshHandler.m_Use16BitIndices`, never a hardcoded `/2` (`:605-609`).

Measured on the shipped M4: 0 degenerate triangles, normals unit to within 3.5 × 10⁻⁴, UV range
(−0.901, −0.000) to (1.998, 1.000) - out-of-unit UVs are real tiling, and the sampler must repeat.

### 4.4 The five culls, and what each one prevents

A prefab ships more geometry than exists at any one moment. `bake` removes four classes of it
structurally and one by measurement.

| cull | authority | anchor | measured on the M4 |
|---|---|---|---|
| model variant | `Weapon_root` presence + name scoring | `:274-300`, `:514` | 5 roots → 1 (83 of 321 nodes) |
| LOD | GameObject name suffix `_lod<N>` | `:441`, `:519` | LOD 0 only; `lod` is a `bake` parameter fixed at 0 by `build` |
| scope mode | `ScopePrefabCache._scopeModeInfos` | `:120-206`, `:457-463` | HHS-1 declares **2** modes (`mode_000` = G33 inline and optic-bearing, `mode_001` = magnifier flipped aside); template agrees, `ModesCount: [2]`, `sightModType: "hybrid"`. `EFT_SCOPE_MODE` selects; default 0 |
| folded iron sight | `AutoFoldableSight`, state read from the node name (`"unfolded" not in name`) | `:208-234`, `:453-456` | MBUS gen2 ships **4** state node sets, 2 folded and 2 unfolded. `fold_sights` is true because the build has a slot whose name contains `scope`, so the 2 unfolded are hidden and the 2 folded kept - hence two MBUS submeshes (3,018 and 3,735 indices) |
| duplicate state of a moving part | measurement, not naming | `:659-684` | LA-5 selector `switch_000..004`, all five renderers at 113 verts each and all centred at (−0.02, −0.53, 0.06); 4 dropped, 1 kept. Template says `ModesCount: 4` |

The duplicate-state test is `(material name, exact triangle count)` plus a centre within **2 cm**.
It is a distance test rather than an exact key because the switch positions differ by millimetres.
Parts that merely overlap - the barrel inside its handguard, the G33's lens against its backing
disc - carry different materials and are untouched.

`fold_sights` is computed globally over the whole install map: any slot name containing `"scope"`
anywhere in the build folds every foldable sight in it (`:703-707`). In the M4 that slot is on the
receiver, not on the weapon. **AUTHORED**: the substring test, and the fact that it does not check
whether the optic is actually over the sight.

**The dedupe drops the submesh but keeps the vertices**, because vertices are appended before the
submesh loop runs (`:601-603` vs `:682`). Measured on the M4: `vertexCount` 64,549, distinct
referenced indices 64,097, **452 orphaned vertices - exactly the 4 × 113 of the dropped LA-5
switch states.** Harmless (14 KB), and a consumer that trusts `vertexCount` is still correct.

### 4.5 Repeatability

Two consecutive rebuilds of the rolled M4 produce byte-identical `mesh.bin` and identical
manifests. Rebuilding today against the **shipped** pack from 2026-07-31 gives byte-identical
geometry but a manifest that differs in two materials' `_Cube` slot
(`patron_cubemap_metall_matte.png` → `patron_cubemap_metall.png`, on the MBUS front sight and the
Geissele handguard) and in four materials' float blocks. The game tree was patched on 2026-08-05.
**Geometry survived the patch; material properties did not.** A pack is therefore stamped by
nothing - there is no `gameBuild` field in an `.eftweap` manifest, unlike `.eftchar` - and drift of
this kind is invisible until you diff.

---

## 5. The `.eftweap` container: exact layout

```
<pack>/manifest.json
<pack>/mesh.bin
<pack>/textures/*.png
```

All binary little-endian. **There is no `version` key** and no schema block beyond `conventions`.

### 5.1 `manifest.json` keys

| key | type | meaning | shipped M4 |
|---|---|---|---|
| `item` | string | the 24-hex template id of the ROOT item | `5447a9cd4bdc2dbd208b4567` |
| `name` | string | `_name` of that template; also the output directory name | `weapon_colt_m4a1_556x45` |
| `vertexCount` | int | vertices in `mesh.bin`, including orphans | **64,549** |
| `indexCount` | int | `u32` indices | **190,044** (63,348 triangles) |
| `vertex` | object | `{stride, fields[{name, fmt, offset}]}` | stride **32** |
| `submeshes` | array | `{material, idxStart, idxCount}`, `idxStart` in INDICES | **30** |
| `materials` | array | material names; `submesh.material` indexes it | **23** |
| `materialTextures` | object | `{material: {slotName: "textures/<file>.png"}}` | 22 entries, 80 (material, slot) pairs → **62** distinct files |
| `materialProps` | object | `{material: {floats:{}, colors:{}}}`, raw, shader-named | 23 entries |
| `aim` | object or null | the optic's eye anchor; §6 | present |
| `conventions` | object | `{"world": "viewer (X-flipped from Unity)", "windingFlipped": true}` | - |

Emission is `build_weapon.py:768-789`.

### 5.2 `mesh.bin`

Two blocks, no header:

```
[ vertexCount * 32 bytes of vertices ][ indexCount * 4 bytes of u32 indices ]
```

Vertex layout, interleaved, **stride 32** (`build_weapon.py:743-747, 774-777`):

| offset | size | format | attribute |
|---:|---:|---|---|
| 0 | 12 | `f32x3` | `pos` (viewer space, metres) |
| 12 | 12 | `f32x3` | `nrm` (viewer space, unit) |
| 24 | 8 | `f32x2` | `uv` (V already flipped) |

Measured: 64,549 × 32 + 190,044 × 4 = **2,825,744 bytes**, which is the file's exact size.

There is no tangent, no vertex colour, no second UV, no joint index and no joint weight. A weapon
is rigid: it rides one bone, so it needs none of them (compare `.eftchar`, stride 72). A normal-map
consumer must therefore derive tangents itself - `import_eftweap` lets Blender do it, and Bevy's
`StandardMaterial` gets none, which is a real difference from the character path.

Indices are **global to the pack**, not per submesh: `idxStart` is an offset into the single index
array, and vertex indices address the single vertex block. Measured: the 30 submeshes' `idxCount`
sum to exactly 190,044, contiguously from 0, and the maximum index is 64,548 = `vertexCount − 1`.

### 5.3 Submesh → material

One submesh per (renderer, Unity submesh) pair, in bake order, recorded as
`(materials.index(mat_name), start, count)` (`build_weapon.py:657-689`). Material names are
**deduplicated pack-wide**, so one material can own several submeshes and the mapping is
many-to-one. Measured on the M4:

- `weapon_colt_m4a1_556x45_LOD0` owns **5** submeshes - the receiver shell, the bolt catch, the
  trigger, the release and the selector are separate renderers of the container sharing one
  material.
- `scope_all_eotech_g33_LOD0_tan` owns 2 (the magnifier body and its mount).
- `sight_front_all_magpul_mbus_gen2_LOD0_fde` owns 2 (the folded blade and the base).
- `tactical_all_insight_la5_LOD0` owns 2 (the kept selector switch, 321 indices, and the body,
  26,544).
- `mag_glass` owns 1 submesh of **6 indices** - two triangles, the PMAG's round-count window.

**Nothing in the pack records which mod a submesh came from.** Provenance survives only as a
material name, and only because BSG names materials after their items. A consumer that wants
"detach the suppressor" cannot do it from an `.eftweap`.

### 5.4 Textures

Every `Material.m_SavedProperties.m_TexEnvs` slot with a resolvable texture is written as
`textures/<sanitised m_Name>.png` and recorded under its slot name **verbatim, as the shader names
it** (`build_weapon.py:620-631, 749-767`). Slot occurrences across the M4's 23 materials:

| slot | materials |
|---|---:|
| `_Cube` | 20 |
| `_MainTex` | 19 |
| `_BumpMap` | 18 |
| `_SpecMap` | 18 |
| `_SpecTex`, `_EnvTex`, `_MarkTex`, `_MaskTex`, `_MaskTex2` | 1 each |

`_MainTex` on 19 of 23 is the interesting number: four materials have no albedo at all
(`back_linza` has no texture entry whatsoever), and §8 turns on that fact.

Normal maps are repacked from Unity's DXT5nm at write time - X out of ALPHA, Z reconstructed - and
the trigger is a **measurement**, `r.std() > 0.02` passes an already-standard map through unchanged
(`build_weapon.py:850-866`, the same rule as `extraction/characters/pack.py`). Candidates are
selected by slot (`_BumpMap`, `_NormalMap`) or by an `_n`/`_normal` filename suffix (`:761`).

One cosmetic defect: the write guard is `if rel not in written.values()`, comparing a string
against dict values (`:755`), so it never fires and a shared texture is re-encoded once per
material that references it. Measured cost on the M4: **80 PNG encodes for 62 distinct files**. The
output is correct; only the build time is wasted.

### 5.5 What the consumers actually assert

`import_eftweap.py` reads the declared `vertex.fields[]` and honours `offset`/`fmt` per attribute
(`:136-160`), and refuses a `mesh.bin` shorter than `vertexCount * stride + indexCount * 4`
(`:146-149`).

`viewer/src/character/weapon.rs` reads `stride` from the manifest but then reads position, normal
and UV at **hardcoded offsets 0, 12 and 24** (`weapon.rs:126-136`). It checks only the blob length
(`:122-125`) and skips a submesh whose range runs past the index array (`:174`). It does not read
`conventions`, `fields`, or any version. **The declared layout and the Rust consumer's assumed
layout are joined by nothing.** Change the field order in the emitter and Blender stays correct
while the viewer silently draws garbage.

Neither consumer checks the UV convention, because the manifest does not state it: `conventions`
carries no `uvVFlipBaked`, so `import_eftweap` hardcodes `vflip=True` (`:180-184`) and a pack built
before the flip was added would be re-flipped with nothing to detect it.

### 5.6 The five shipped packs

| pack | verts | tris | submeshes | materials | textures | bbox (m) | aim |
|---|---:|---:|---:|---:|---:|---|---|
| `weapon_colt_m4a1_556x45` | 64,549 | 63,348 | 30 | 23 | 62 | 0.111 × 0.816 × 0.295 | fov 5.03°, relief 0.1090 |
| `weapon_izhmash_ak74m_545x39` | 75,844 | 74,577 | 31 | 25 | 61 | 0.100 × 0.956 × 0.330 | none |
| `weapon_izhmash_akm_762x39` | 77,927 | 75,895 | 27 | 22 | 58 | 0.095 × 0.890 × 0.315 | none |
| `weapon_kbp_9a91_9x39` | 57,587 | 61,382 | 23 | 15 | 44 | 0.079 × 0.886 × 0.251 | none |
| `weapon_tochmash_val_9x39` | 80,895 | 84,251 | 38 | 22 | 60 | 0.091 × 0.883 × 0.274 | fov 5.75°, relief 0.0864 |

`aim: null` is the normal case, not a failure: iron sights and unmagnified red dots ship no
`OpticSight`, hence no `ScopeTransform`.

---

## 6. The aim block: optics, eye relief, field of view

Emitted at `build_weapon.py:466-509`, consumed at `weapon.rs:44-72` and `drive.rs:486-586`.

### 6.1 Where each field comes from

`ScopePrefabCache._scopeModeInfos` is the game's own list of a sight's mutually exclusive modes.
Each entry names a `ModeGameObject` and, for a magnified optic, an `OpticSight`. From the selected
mode's `OpticSight`:

| manifest field | game source | anchor |
|---|---|---|
| `position`, `forward`, `up` | `OpticSight.ScopeTransform`, composed into assembled-weapon space | `:156, :471-477` |
| `fov` | `ScopeData.FieldOfView`, else `OpticSight.FieldOfView`, else `ScopeCameraData.FieldOfView` on any node in the mode subtree | `:180-193, :244-257` |
| `eyeRelief` | `OpticSight.DistanceToCamera` | `:170-171` |
| `nearClip` | `ScopeData.NearClipPlane` or `OpticSight.NearClipPlane` | `:172-179` |
| `lensMaterial` | first material on `OpticSight.LensRenderer` | `:160-164` |
| `decorMaterial` | first material on `OpticSight.DecorLensRenderer` | `:165-167` |
| `opticMaterials` | every material on a renderer inside the mode subtree | `:194-200` |
| `source` | the literal string `"OpticSight.ScopeTransform"` | `:508` |

**All DERIVED.** Aiming needs no authored offsets anywhere.

### 6.2 The shipped M4's numbers

```json
"aim": {
  "position":  [3.4753254759341834e-05, -0.14628688130969614, 0.072392919932666],
  "forward":   [-1.9470736619662756e-07, -0.9999999999999651, -1.780701592120723e-07],
  "up":        [1.3522650513456577e-07, -1.7807018554165493e-07, 0.9999999999999751],
  "fov": 5.03000020980835,
  "eyeRelief": 0.10898952186107635,
  "nearClip": null,
  "lensMaterial":  "scope_all_eotech_g33_LOD0_linza",
  "decorMaterial": "scope_all_eotech_g33_LOD0_glass",
  "opticMaterials": ["back_linza", "scope_all_eotech_g33_LOD0_glass",
                     "scope_all_eotech_g33_LOD0_linza", "scope_all_eotech_g33_LOD0_tan"],
  "source": "OpticSight.ScopeTransform"
}
```

The `ScopeTransform` node is named **`mod_aim_camera`** and the lens node is **`linza_mode_000`**,
read directly out of `scope_all_eotech_hhs_1_tan.bundle`.

**What `eyeRelief` means, measured rather than asserted.** In the sight's own prefab space the
anchor sits at (−0.00003, 0.21849, 0.04323) and the lens at (−0.00003, 0.10950, 0.04323). The
distance between them is **0.108990 m - bit-for-bit the `DistanceToCamera` value.** So
`OpticSight.DistanceToCamera` is exactly the anchor-to-lens standoff: the anchor is where your eye
goes, one eye relief BEHIND the glass. Placing the optic at the eye instead puts it inside the near
plane, where it is clipped away; `drive.rs:581-584` pulls the near plane to
`clamp(eyeRelief * 0.15, 0.005, 0.05)` while aiming for exactly that reason. When the pack carries
no relief the viewer falls back to **0.11 m**, which is AUTHORED (`mod.rs:207`).

**Which way the eye faces is derived, not hardcoded.** A Unity camera looks down +Z, but this
node's +Z points back toward the shoulder - measured, its basis column 2 in prefab space is +Y
while the lens is at *lower* Y. Rather than hardcode a flip, the sign is taken from the optic's own
`LensRenderer`: you look THROUGH the lens, so forward is whichever of ±Z agrees with the direction
from the anchor to it (`build_weapon.py:485-489`). On the M4 the flip fires, and the emitted
`forward` is −Y, i.e. downrange, agreeing with §4.2's part ordering. In pack space the anchor is
0.1090 m behind the G33 lens centroid (−0.1463 vs −0.2553 on Y) and 0.6757 m behind the
muzzle-most vertex along `forward`.

**What `fov` means.** 5.03° is the G33's 3× magnification expressed the way the game stores it: the
field of view of the optic's OWN camera, whose image EFT renders into the lens circle while the
rest of the screen keeps its normal FOV. Applying it to the main camera is roughly a 12× zoom of
everything, so the viewer leaves it off by default and only honours it under `EFT_ADS_ZOOM=1`
(`drive.rs:563-580`). Measured consequence of turning it on: at 5.03° the reticle plane - 11 cm
from the eye - fills the entire screen.

### 6.3 Only the first optic survives

`bake` appends one record per emitting mode into `out_aim`, but the manifest keeps `aim[0]`
(`build_weapon.py:786`). A build with two magnified optics (a scope plus an offset canted sight)
loses the second silently. **AUTHORED**, and a limitation rather than a decision.

### 6.4 How the viewer aims

The weapon hangs off the socket bone through an **offset node** that is identity at rest
(`mod.rs:190-198`). Aiming solves that node so the anchor lands on the camera:

```
offset = bone_world⁻¹ · camera_world · anchor_local⁻¹
```

eased from identity by the blend (`drive.rs:549-562`). **The weapon moves to the eye, not the eye
to the weapon** - the game plays an additive aim pose (`Additive_Aiming`, `Additive_ISaim` are real
layers in this controller) rather than dragging your head to the optic, and moving the camera
instead puts it wherever the gun currently is, which at low ready is inside the receiver.

NPCs get no offset node: `npc.rs:132-137` parents the parts straight to the bone with
`Transform::IDENTITY`, so **NPCs cannot aim down sights** - the aim block is read only for the
player. The doc comment at `drive.rs:507` names an `EFT_ADS_EYE=1` mode; no code in `viewer/src`
reads that variable, so it does not exist.

---

## 7. Attachment to the character: `Weapon_root` and the `q4` undo

This is the section to read if you read only one.

### 7.1 The socket

The rig's bone 68 is named `Weapon_root` (`weapon.rs:21`, measured index in §4 of the character
document). The weapon container ships a node of the **same name**, with the weapon under
`Weapon_root / Weapon_root_anim / weapon` and a partial arm rig (`Base HumanLCollarbone`,
`Base HumanRCollarbone`, `Camera_animated`) beside it - all four names verified in
`weapon_colt_m4a1_556x45_container.bundle` in §2.3. Because `root_inv` puts that node at the
origin, the baked pack is already expressed in the socket's frame, and the attachment is an
**identity transform**: `mod.rs:195-198, 219-224`, `npc.rs:132-137`,
`import_eftweap.py:215-221`. Any offset would be inventing a grip the game does not apply.

The rig also ships `weapon_holster` (75) and `weapon_holster1` (76) for the slung pose; nothing
uses them yet.

That the runtime aligns the two nodes with identity is an **inference from the name match and the
shared arm-bone names**, strong but not read out of a component: *unverified*.

### 7.2 The `q4` undo, and why rigid equipment does the opposite

Blender bones must point down their own local **+Y**. `import_eftchar.py` therefore measures which
local axis actually runs down the bone (`_derive_bone_axis`, `:369-392` - a majority vote over
parent→child directions, no assumption), builds the signed permutation `q4` that carries it to +Y
(`_q_matrix`, `:395-403`, asserting `det = +1`), and builds the rest pose as
`rest = bind_world @ q4` (`:1078`). It then **publishes** the matrix on the armature as
`arm_obj["eft_q4"]` (`:1089`) precisely so consumers can undo it.

This rig is **+X-down-the-bone**, so `q4` is a 90° rotation and the engine bone frame and the
Blender bone frame differ by exactly that.

| thing | authored in | consumer composes | anchor |
|---|---|---|---|
| the weapon (`.eftweap`) | the **ENGINE** bone frame | `armature.matrix_world @ pose.matrix @ q4⁻¹` | `import_eftweap.py:227-239` |
| rigid equipment (`.eftchar` attachment) | **UNITY Y-UP** | `armature.matrix_world @ pose.matrix @ socket @ local` - no undo | `import_eftchar.py:711-721` |

Measured on `out/characters/kit_bear_5`, head bone at bind pose:

```
ENGINE  (pose.matrix @ q4⁻¹)   +X -> world (0.00, -0.36, +0.93)   up the skull
                               +Y -> world (0.00, -0.93, -0.36)   forward and down
BLENDER (pose.matrix)          +Y -> world (0.00, -0.36, +0.93)   up the skull
```

**Why they differ.** The weapon is authored in the engine's bone frame - its own prefab literally
carries a node named `Weapon_root` with the same arm bones around it, so its origin *is* the
engine's socket and the correct frame is the one the engine has, `pose.matrix @ q4⁻¹`. A rigid
equipment prefab is authored the ordinary Unity way: a prefab root at identity above a mesh node
carrying a single −90° X rotation, the DCC-Z-up → Unity-Y-up fixup (verified identical on
`cap_BEAR`, `item_equipment_helmet_LSHZ` and `item_equipment_helmet_ULACH_coyote`). Its own up is
therefore **+Y** - which is the convention `q4` was constructed to produce, so the correct frame
there is `pose.matrix` itself, with no undo.

The rule is one rule: **attach each thing in the frame it was authored in.** The two opposite
answers follow from the two different authorings, not from two different policies.

Getting up right is only half of it. A bone frame has three axes and the other two are fixed by
bone **roll**, a rigging convention with nothing to do with which way the face points, so an item
can sit crown-up and still be yawed 90°. `_socket_basis` (`import_eftchar.py:725-747`) therefore
DERIVES the remaining rotation from the bind pose: `up = pack +Y`, `forward = characterForward`
(itself derived from a walk clip's root motion), `right = up × forward`, and the socket is
`rest_rotation⁻¹ @ desired`. The weapon needs none of that, because its authored frame already
fixes all three axes.

**The same −90° X fixup appears on weapon mods and is handled the opposite way** (§4.1): a mod's
LOD node carries it and `root_inv` cancels it, because the assembled weapon must end up in the
engine bone frame. A rigid attachment's `localRot` *keeps* it - measured `(−0.7071, 0, 0, 0.7071)`
on the Tagilla welding mask - because that item ends up in the Unity Y-up frame. Same rotation, two
pipelines, two correct-but-opposite treatments. This is the single easiest thing in the repo to get
backwards.

### 7.3 Blender's bone-parent offset

Blender's `parent_type = 'BONE'` places a child at the bone's **tail**, so a bone parent plus a
cleared inverse still differs from the engine by a translation along the bone. Neither importer
derives that offset: both assign the world matrix they want and let Blender back-solve the local
basis, which is expressed in bone space and therefore stays correct for every animated frame
(`import_eftweap.py:222-239`, `import_eftchar.py:655-657`).

---

## 8. Materials on the consumer side

`materialProps` carries every `m_Floats` and `m_Colors` entry raw, named as the shader names them
(`build_weapon.py:638-654`). Measured on the M4: 23 material entries, up to 36 floats and 13
colours each, with `_Color` on all 23, `_EmissionColor` on 22, `_ReflectColor` and `_SpecColor` on
21, and a long tail of BSG-specific channels (`USEHEAT`, `_HeatColor1/2`, `_HeatTemp`,
`_BaseTintColor`, `_FresPow`, `_DropsSpec`).

`weapon.rs:209-300` classifies **by property, never by name** - BSG names the same shader `_glass`,
`_linza` and `mag_glass` interchangeably, and `mag_glass` is actually opaque
(`_Color.a = 1.0`, measured).

| class | test | shipped M4 example |
|---|---|---|
| reticle | has `_MarkTex` | `scope_all_eotech_g33_LOD0_linza` - drawn unlit and emissive at `_Color × _HDR`; a projected light, not a lit surface, so shadow must not darken it |
| optic glass | named by `aim.lensMaterial` / `decorMaterial`, or in `aim.opticMaterials` **with no textures at all** | `scope_all_eotech_g33_LOD0_glass`; and `back_linza`, `_Color` pure black with zero texture slots |
| stated-alpha glass | `_Color.a < 0.999` | `scope_all_eotech_exps3_LOD0_glass`, alpha **0.128** |
| env-lit glass | no `_MainTex` and an `_EnvTex`/`_Cube` present | the fresnel lens family; opacity from `_ReflectColor.a` |
| opaque | everything else | 18 of 23 |

`back_linza` is the case that teaches the rule. It is the disc the game's scope camera renders the
magnified image ONTO - a render target, not paint - so it is authored opaque black with no
textures. Drawn as authored it is a black disc that blocks the sight picture completely. The test
that catches it is "a material inside the optic's own mode subtree that carries no texture", which
is why `opticMaterials` is in the manifest at all.

Gloss handling is uniform across the repo and inverted from PBR: `_Shininess` / `_Glossiness` /
`_SpecMap` are **gloss** (high = shiny), so `roughness = 1 − gloss`
(`weapon.rs:266-268`, `import_eftweap.py:108-114`). `_SpecMap` is emitted but must never be bound
to occlusion - doing so on the character path crushed ambient to a fifth with hard seams.

`import_eftweap.py:91-106` additionally inverts the normal map's **green** channel: BSG's maps are
DirectX convention and Blender expects OpenGL. The Bevy path does not, which is a known asymmetry
between the two consumers.

Two consumer traps recorded in the source: the Rust `AimAnchor` needs
`#[serde(rename_all = "camelCase")]` (`weapon.rs:51-54`) or the multi-word fields deserialize to
their defaults and every lens falls back to half-opaque; and the lens tint must come from
`_MainColor` where the fresnel family provides it, falling back to `_Color` - falling back to
*white* renders every untextured lens as a solid white pane (`weapon.rs:255-265`).

---

## 9. What is dropped

- **Per-part identity.** The build is merged into one mesh. There are no node transforms, no part
  hierarchy, no slot names in the pack, and therefore no bolt cycle, no magazine change, no folding
  stock, no charging-handle animation. The only surviving provenance is material naming.
- **The LOD chain.** LOD 0 only. `bake` takes a `lod` parameter (`:433`) that `build` never passes,
  so the level is fixed at 0 for every pack.
- **Skinning.** A `SkinnedMeshRenderer` inside a weapon prefab is read for its `m_Mesh` and baked
  by its node's matrix (`:527-529`); bindposes and bone weights are ignored. Anything that actually
  deforms - a sling, a belt of ammunition - will bake in its bind pose.
- **Tangents, vertex colours, second UV set.** Not in the 32-byte vertex.
- **All non-selected scope modes and sight states**, by design (§4.4). The pack cannot be
  re-switched at runtime; rebuild with a different `EFT_SCOPE_MODE`.
- **Everything else in the item template.** Ergonomics, recoil, accuracy, ammo capacity, fire
  modes, weight, `ModesCount`, `sightModType` - none of it reaches the pack. `.eftweap` is
  geometry, materials and one aim anchor.
- **Colliders, muzzle-flash and shell-eject sockets, sound.** Not read.
- **The second and later optics.** `aim[0]` only (§6.3).
- **The game build stamp.** No `version`, no `source`, no `gameBuild` in the manifest, so a pack
  cannot be checked against the install that produced it (§4.5).

---

## 10. Invariants and their failure signatures

The "what you SEE" column records observations from the source notes taken when each invariant was
broken; except where a measurement is quoted, those signatures are not reproducible from the repo
as it stands.

| invariant | how it breaks | what you SEE |
|---|---|---|
| every prefab baked relative to its own anchor (`root_inv`) | baking in bundle-absolute space | the assembled gun is metres from the hand and stretched - measured cause: the M4 container's `Weapon_root` sits at (0.1568, 1.4140, 0.0174) in bundle space, and the recorded symptom was a 1.75 m weapon |
| a mod anchored on its **mesh** node, not its prefab root | anchoring on the root | every mod is turned 90° from the weapon body: the receiver runs across the barrel instead of along it, the magazine sticks out sideways. Measured: a mod's LOD0 node's local-to-root is exactly the −90° X permutation, columns (+X, −Z, +Y) |
| the anchor is a node that contributes baked geometry | matching any renderer by name | the LA-5's gizmo `Sphere` markers become the anchor; inverting their measured 0.0040 / 0.0090 / 0.0016 scale bakes an 8 cm device as a ~51 m object that swallows the map |
| the anchor's basis orthonormalised, scale dropped | inverting the anchor's scale too | the part is correctly placed and the wrong size, which reads as "that mod's model is broken" rather than as a transform bug |
| one model variant chosen | resolving sockets by name across the whole bundle | the full and simple `.generated` trees are in different frames, so parts resolve against whichever socket was found first and individual mods land on the wrong axis while the rest of the gun looks fine |
| one LOD kept | baking all of them | three shells of every part merged into one mesh; silhouettes look right, triangle count triples, and the low-LOD skin z-fights through the high one |
| one scope mode kept | baking `_scopeModeInfos` in full | the HHS-1's G33 magnifier is drawn inline AND flipped aside at once - one optic stacked on a copy of itself. Measured: the HHS-1 declares 2 modes, and its template agrees (`ModesCount: [2]`) |
| backup sight folded when the build carries an optic | drawing every `AutoFoldableSight` state | the MBUS front sight stands up THROUGH the scope tube. Measured: 4 state node sets, 2 folded and 2 unfolded |
| duplicate states of a moving part dropped | baking them all | the LA-5's selector is drawn in all five positions at once, a smear of overlapping switches. Measured: 5 renderers, 113 verts each, their centres spanning 3.65 mm and all rounding to (−0.02, −0.53, 0.06) |
| `firstByte` divided by the mesh's real index width | hardcoding `/2` | every 32-bit-indexed mesh reads garbled triangles - a shredded, spiky version of the part, still roughly in the right place |
| `G3` applied once, winding swapped with it | winding not swapped | every triangle faces inward; the weapon renders inside-out or vanishes under back-face culling |
| UV `v → 1 − v` baked exactly once | skipped, or repeated in the shader | the gun samples its texture upside down. This reads as a subtle wrong-placement, not as an obvious break - it shipped undetected until 2026-07-31. The manifest does not declare the flip, so nothing can detect it |
| normal maps repacked from DXT5nm | written raw | tangent normal ≈ (1, y, z), pointing along the surface; the receiver flips between lit and black as it turns |
| `materialProps` carried through | dropped | every material falls back to the default, which is opaque WHITE: every scope lens becomes a solid pane you cannot see through |
| optic surfaces classified by property, not by name | trusting the name | `mag_glass` (measured `_Color.a = 1.0`) is drawn transparent while `back_linza` (measured `_Color` = pure black, zero textures) is drawn opaque - you get a see-through magazine and a blind scope |
| `AimAnchor` deserialized with `rename_all = "camelCase"` | omitted | `lensMaterial` / `opticMaterials` silently become defaults, so no surface is recognised as optic glass and every lens renders half-opaque. Nothing errors |
| the weapon composed in the ENGINE bone frame (`pose.matrix @ q4⁻¹`) | using `pose.matrix` | the rifle is in the hand and rotated 90°: it reads as "sideways", muzzle out through the forearm. This rig is +X-down-the-bone, so the two frames differ by exactly that |
| rigid equipment composed in the BLENDER/Unity frame (`pose.matrix`, no undo) | applying the weapon's rule to it | the helmet is present, watertight, correctly textured and rotated 90°: the crown points forward out of the face. See §7.2 |
| a required slot left EMPTY when nothing legal is available | filling it with `sorted(allowed)[0]` | a build the game would never produce - an A2 rear sight fixed under an HHS-1. Structurally valid, quietly wrong, and impossible to spot without knowing the gun |
| a required slot that IS empty | - | the entire subtree beneath it disappears, because its children hang off its sockets: an empty `mod_reciever` takes the barrel, handguard, gas block, muzzle, suppressor and optic with it. Measured: 0 of 780 reached required slots were empty across 208 rolled trees |
| the install map acyclic | a cycle | `build_weapon.build.rec` has no depth or cycle guard (`:697, :712`) and recurses to Python's stack limit. Measured 0 cycles in 248 factory presets and 208 rolled trees, but nothing enforces it |
| CAB dependencies resolved through the prebuilt index | letting UnityPy fall back | a miss triggers a recursive scan of the ~40 GB game tree per lookup; and an index built by globbing `*.bundle` misses the 1,160 extensionless bundles that provide 2,286 of the 14,738 CAB entries, so the item assembles to nothing with no error |
| only the container's OWN objects baked | baking everything loaded into the environment | the dependency bundles carry unrelated assets; every other gun sharing a bundle merges into yours |
| the vertex layout the Rust consumer assumes | changing `vertex.fields[]` without changing `weapon.rs` | Blender stays correct and the viewer draws garbage: `weapon.rs:126-136` reads `stride` from the manifest but positions/normals/UVs at hardcoded 0/12/24 |

---

## 11. Old patterns

- **Filling a required slot with `sorted(allowed)[0]`.** Picked by alphabet and produced a fixed A2
  rear sight installed under an HHS-1. Replaced by "leave it empty" (`loadout.py:127-133`). The
  cost is measured and small: 0 of 780 reached required slots actually end up empty.
- **Anchoring a mod prefab on its root.** Killed by the measurement that a mod's LOD node carries a
  −90° X rotation relative to its root; every mod baked 90° off. Now the mesh node is the mount
  frame (`build_weapon.py:336-353`).
- **Anchoring on any renderer whose name lacks `_lod`.** Matched Unity primitive gizmos. Measured
  on the LA-5: `Sphere` nodes at 0.0040 / 0.0090 / 0.0016 scale, and inverting one baked the 8 cm
  device as a 51 m object. Candidates now require a mesh that resolves, which excludes built-ins
  structurally (`:389-421`).
- **Keeping the anchor's scale.** A mount is a frame, not a size. The basis is orthonormalised and
  a warning is printed when scale was present (`:374-382`); the M4 build prints none.
- **Baking in bundle-absolute space.** Recorded symptom: the assembled gun stretched to 1.75 m.
  Everything is now root-relative, including the socket matrix passed to a child (`:524, :736`).
- **Resolving sockets by name across the whole bundle.** The full and simple `.generated` variants
  each carry their own `Weapon_root` and `mod_*` nodes in different frames, so parts landed on the
  wrong axis. Replaced by `variant()` scoring (`:274-300`).
- **Baking every LOD.** Merged three copies of every part.
- **Baking every scope mode.** Stacked the HHS-1's magnifier on its own flipped-aside copy.
  Replaced by reading `ScopePrefabCache._scopeModeInfos` (`:120-206`).
- **Baking every state of a moving part.** The LA-5's selector drawn in all five positions.
  Replaced by a *measured* dedupe - same material, same triangle count, centres within 2 cm - which
  deliberately does not touch genuinely overlapping parts because those carry different materials
  (`:659-684`).
- **`firstByte // 2`.** Garbled every 32-bit-indexed mesh; the width now comes from
  `MeshHandler.m_Use16BitIndices` (`:605-609`).
- **Not flipping V.** The map pack declared `uvVFlipBaked: true` and the character pack baked it;
  weapons never did, so every gun sampled its texture upside down. Fixed 2026-07-31 (commit
  `e146211`, "weapon: flip V, like every other pipeline already did"). The `.eftweap` manifest
  still does not declare the flip.
- **Dropping `materialProps`.** Left the viewer with a default material - opaque white - so every
  scope lens rendered as a solid pane. The properties are now carried raw and interpreted by the
  consumer (`:638-654`).
- **Naming-based material classification.** BSG uses `_glass`, `_linza` and `mag_glass`
  interchangeably and `mag_glass` is opaque (measured `_Color.a = 1.0`). Classification is now
  purely by property (`weapon.rs:209-244`).
- **Falling back to white for an untextured lens tint.** Rendered `back_linza` - the black
  render-target disc - as a solid white pane. Tint now comes from `_MainColor`, else `_Color`
  (`weapon.rs:255-265`).
- **Snapping the eye onto the sight anchor.** Moving the camera to the weapon puts it wherever the
  gun currently is, which at low ready is inside the receiver. The weapon is moved to the eye
  instead, through an offset node (`drive.rs:549-562`).
- **Driving the main camera with `ScopeCameraData.FieldOfView`.** Measured: at 5.03° the reticle
  plane, 11 cm from the eye, fills the entire screen, because that FOV describes the image the
  optic renders INTO its lens and not the view of the world around it. Now opt-in via
  `EFT_ADS_ZOOM=1` (`drive.rs:563-580`).
- **`build_loadouts.py`'s equipment path is itself an old pattern that is still live.** It calls
  `classify()` to decide skinned-versus-rigid and then hands *every* item to `build_weapon.build`
  regardless (`build_loadouts.py:110-122`), which merges every LOD and every SHADOW/drop proxy
  because those culls are tuned for weapon prefabs. Measured: a WarTech backpack baked to a 1.78 m
  bounding box against a 0.56 m real mesh. The current path for worn equipment is
  `extraction/characters/kit_parts.py` + `skin.py`, which routes by what the prefab IS and takes
  geometry only from meshes the container's own renderers reference.
- **A CAB index built by globbing `*.bundle`.** Measured on this install: 1,160 of the 7,560 bundle
  files carry no extension and provide 2,286 of the 14,738 CAB entries. Bundles are now recognised
  by the `UnityFS`-family header (`unity_deps.py:33-46`). The specific claim in
  `characters-and-animation.md` that eleven submeshes of the M4A1 were lost this way is *unverified*
 - the counterfactual is not reproducible now that the index is correct.
