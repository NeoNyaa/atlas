## Contents

1. [Scope, module map, and what was measured](#1-scope-module-map-and-what-was-measured)
2. [The appearance roll: who a character is](#2-the-appearance-roll-who-a-character-is)
3. [The kit roll: what he is wearing](#3-the-kit-roll-what-he-is-wearing)
4. [The skinned / rigid split](#4-the-skinned--rigid-split)
5. [What is AUTHORED here, and why the two roll flags are not](#5-what-is-authored-here-and-why-the-two-roll-flags-are-not)
6. [Garment variants and the combination problem](#6-garment-variants-and-the-combination-problem)
7. [From spec to pack: build steps 2 and 2b](#7-from-spec-to-pack-build-steps-2-and-2b)
8. [The three ways a worn item breaks](#8-the-three-ways-a-worn-item-breaks)
9. [What is dropped](#9-what-is-dropped)
10. [Invariants and their failure signatures](#10-invariants-and-their-failure-signatures)
11. [Old patterns](#11-old-patterns)

---

## 1. Scope, module map, and what was measured

`docs/extraction/characters-and-animation.md` covers the rig, the skinning and the clips: how one
biped, one part bundle and one `AnimationClip` become a `.eftchar` pack. It does not say **who** the
character is or **what he has on**. Its §10 covers the mechanics of a single attachment record - one
mesh, one bone, one local transform - and stops there. This document covers the two rolls that decide
the answer: the **appearance** roll (body, feet, hands, head) and the **kit** roll (armour, rig,
backpack, helmet, mask, glasses, headset), and everything that follows from a character being dressed
rather than naked.

| file | role |
|---|---|
| `extraction/characters/fetch_bot_db.py` | banks BSG's bot / customization / item / preset tables into `packs/shared/` |
| `extraction/characters/appearance.py` | rolls WHICH body prefabs a bot wears, from the game's weighted `appearance` tables |
| `extraction/characters/loadout.py` | rolls the full kit - weapon plus mod tree plus worn equipment - from the game's `inventory.equipment` weights |
| `extraction/characters/kit_parts.py` | routes each worn item into the character build spec as a skinned part or a rigid attachment |
| `extraction/characters/build_character.py` | `--bot / --seed / --kit / --prefer / --skip-slot`, and build steps 2 and 2b |
| `extraction/characters/build_loadouts.py` | the OLD `.eftkit` baker; still the home of `classify()`, which `kit_parts` calls |
| `extraction/characters/skin.py` | `load_part` (skinned) and `load_attachment` (rigid), plus the CAB dependency repair both need |
| `tools/blender/import_eftchar.py` | the consumer: bindpose merge, the derived socket basis, `mesh_filter` |

The data all sits under `packs/shared/`: `bot_loadouts.json`, `customization.json`,
`item_templates.json`, `globals.json`. None of it is authored by this repo - `fetch_bot_db.py`
mirrors it and trims each bot type to `inventory`, `appearance`, `chances`
(`fetch_bot_db.py:93-97`). The roster is **listed from the source, never enumerated**
(`fetch_bot_db.py:64-71`), so a game update that adds a boss is picked up by re-running the fetch.

**Everything numeric below was measured**, on this workstation, against:

- `packs/shared/` as banked: **63 bot types**, **4,441 item templates**, **597 customization
  entries**.
- `out/characters/kit_bear_0` … `kit_bear_11` - twelve built packs, `bot: pmcbear`, seeds 0..11,
  each with a `kit.json` sidecar.
- `out/kits/_parts/` - 34 items baked by the OLD `build_loadouts.py` path, used in §11.
- The live EFT install, for prefab structure (object graphs, `m_FileID` values, bindpose counts).

Numbers quoted from another file's comments rather than re-measured are marked as such.

---

## 2. The appearance roll: who a character is

### 2.1 The tables and the four slots

A bot type's entry in `bot_loadouts.json` carries an `appearance` block: one `{customizationId:
weight}` table per slot. `appearance.py:34` fixes the slots this repo reads to `("body", "feet",
"hands", "head")` and `appearance.py:111` rolls each one with `weighted_pick`, resolves the winning
id through `customization.json` to `_props.Prefab.path`, and returns a build spec shaped exactly like
a `characters.json` entry so the existing builder consumes it unchanged.

Measured on `pmcbear`:

| slot | options | notable weights |
|---|---:|---|
| `body` | 18 | `bear_upper_SpNa` 8, `bear_upper_summerfield` 7, `top_bear_voin` 7, `top_bear_sumrak` 7 … `bear_top_borey` 2 |
| `feet` | 15 | `bear_lower_zaslon` 9, `bear_lower_srvv_surpat` 8 … `bear_lower_g99` 2 |
| `hands` | 1 | `SavageHands1` → `assets/content/hands/wild/wild_body_1_firsthands.bundle` |
| `head` | 5 | `DefaultBearHead` 10, `bear_head_eug` 7, `bear_head_5` 5, `bear_head_3` 4, `bear_head_6` 3 |
| `voice` | 5 | **never read** - not in `SLOTS` |

The `voice` table exists in the source data with five weighted entries and no `Prefab`; this
pipeline ignores it. `fetch_bot_db.py:40` also declares an `appearanceSlots` config key with the
same four names, and **nothing reads it** - `appearance.SLOTS` is a module constant. The config key
is dead weight, not a second source of truth.

The pick is a plain cumulative-weight walk over `{id: weight}` (`appearance.py:71-84`): sum the
weights, draw `rng.random() * total`, walk until the accumulator passes it. A table whose weights sum
to zero degenerates to `rng.choice`. Weights are clamped at zero, never normalised, and never
edited - **the distribution is the game's**.

### 2.2 Seeding, and why the kit and the body can never disagree

`appearance.py:127` seeds from the string `f"appearance:{bot_type}:{seed}"`. `loadout.py:158` seeds
its own stream from `f"{bot_type}:{seed}"` and then, at `loadout.py:185`, calls
`appearance.resolve(bot_type, seed, …)` - the same call the character builder makes, with the same
seed. The two streams are independent, so rolling a kit does not perturb the body, and the body a kit
believes in is the body the pack actually contains **by construction**, not by convention.

Measured, re-rolling `pmcbear` seeds 0..11 from the banked tables and diffing against the twelve
built packs: every appearance slot and every equipment slot reproduces exactly, with two explained
exceptions (§5.3). Seed 2 rolls `bear_upper_SpNa` / `bear_lower_ops_windshirt` / `bear_head_3`, and
`out/characters/kit_bear_2` contains `Top_Bear_SpNa_*`, `Pants_bear_OPSWindshirp_lod0` and
`Bear_Head_3_*`. Determinism holds across processes and across runs because the seed is a **string**,
not `hash()`.

### 2.3 Views

`appearance.py:48` tags each slot with a view: `body`, `feet`, `head` are `"third"`, `hands` is
`"first"`. The hands slot points at the first-person prefabs, which bind the same 79-bone rig but
belong to a different draw set - a third-person body already has arms, so drawing both puts two pairs
of hands on one skeleton. Every built pack carries `Wild_Body_1_firstHands` (8,870 vertices, measured
identical in all twelve) tagged `view: "first"`, and the consumer shows one view at a time.

This tag is load-bearing beyond drawing: `import_eftchar.py:258-259` ranks first-person meshes
**last** when merging bindpose tables, precisely so a mesh that is never drawn with the body cannot
define the reference frame for a bone the body also binds (§8.2).

### 2.4 Why hand-authored part lists were abandoned

`characters.json` used to carry hand-picked prefab lists per character. `appearance.py:9-12` records
the measurement that killed the practice: the authored scav wore `head_civilian_1`, which **does not
appear in the scav appearance table at all** (the game rolls `wild_head_1/2/3/drozd/misha`), and it
had no hands slot. The authored roster was not a simplification of the game's answer, it was a
different answer.

The rule that replaced it: **anything the game can decide, the game decides.** `characters.json`
survives for the handful of facts the tables genuinely do not carry - named one-offs (`tagilla`,
`glukhar`), controller overrides, clip sets, and the equipment slot→bone choice, which
`characters.json:74-82` states plainly is authored. The failure mode of the old approach was not that
it errored; it produced a complete, well-formed, plausible character that was simply not one the game
spawns.

---

## 3. The kit roll: what he is wearing

### 3.1 Source and structure

`loadout.py` reads four banked tables (`loadout.py:33-51`):

- `bot_loadouts.json` → `inventory.equipment.<Slot> = {itemId: weight}` - this bot type's own spawn
  frequencies, and `inventory.mods[parentTpl][slot] = [allowed child ids]` - what this bot may bolt
  onto a gun.
- `bot_loadouts.json` → `chances.equipment.<Slot>` - the **percent chance the slot is filled at
  all**.
- `item_templates.json` → `_props.Prefab.path` (the bundle) and `_props.Slots[].filters[0].Filter`
  (the legality check for weapon assembly).
- `globals.json` → `ItemPresets`, BSG's factory weapon builds.

`loadout.roll` (`loadout.py:145`) draws the primary weapon, builds its mod tree, then walks the
configured wearable slots (`loadout.py:169-181`): roll the fill chance first, and only then roll the
item. Without the fill chance every bot wore every slot, which is not how the game spawns them.

Measured on `pmcbear` - option counts, weight totals, and fill chances, all read from the banked
table:

| slot | options | Σ weights | `chances.equipment` |
|---|---:|---:|---:|
| `Headwear` | 22 | 67 | 84 |
| `TacticalVest` | 19 | 79 | 100 |
| `ArmorVest` | 13 | 32 | 40 |
| `Backpack` | 11 | 52 | 65 |
| `Eyewear` | 8 | 68 | 86 |
| `FaceCover` | 7 | 60 | 75 |
| `Earpiece` | 7 | 45 | 56 |
| `ArmBand` | 0 | 0 | 0 |

The BEAR-specific items are exactly where a BEAR PMC's table would put them. `cap_BEAR` - the green
BEAR ballcap, prefab `.../head_bear/item_equipment_head_bear_green.bundle` - is the **second-heaviest
Headwear option at weight 7**, behind only `item_equipment_helmet_LSHZ` at 8, and its black sibling
`item_equipment_head_bear_black` sits at 4. `bear_rig_6h112` is the **second-heaviest TacticalVest at
weight 8**, behind `item_equipment_rig_tv115` at 17. Those numbers are BSG's; this repo has never
touched a weight.

The wearable slot list itself is configuration, not code: `loadout.py:169` iterates
`cfg["wearableSlots"]`, which `fetch_bot_db.py:37-38` defaults to seven slots and
`extraction/characters/bot_db.json:7-15` currently pins to the same seven. `ArmBand` is **not** in
that list, so it is never rolled even though `kit_parts.SLOT_BONE` has an entry for it - and
`pmcbear`'s `ArmBand` table is empty anyway.

### 3.2 The weapon, and the one place a roll refuses to guess

`build_weapon_tree` (`loadout.py:89-142`) starts from BSG's factory preset for the rolled weapon and,
per slot, prefers a candidate from **this bot's own mod table** intersected with the parent
template's `Filter`. When neither the bot table nor the factory preset names a part for a required
slot, the slot **stays empty**. `loadout.py:127-133` records why: filling it with `sorted(allowed)[0]`
picks by alphabet, which is how a fixed A2 rear sight ended up installed under an HHS-1 - a build the
game would never produce. An incomplete gun is honest; an invented one is not. This is the same
principle §5.3 applies to `--prefer`.

Weapon assembly itself is out of scope here; it produces an `.eftweap` pack and hangs off
`Weapon_root` by a different mechanism entirely (`characters-and-animation.md` §10).

### 3.3 What a rolled kit looks like

The twelve built packs are `pmcbear` seeds 0..11. Filled-slot counts across them, against the table's
own fill chances:

| slot | filled in packs | chance | expected of 12 |
|---|---:|---:|---:|
| `TacticalVest` | 12 | 100 | 12 |
| `Headwear` | 11 | 84 | 10.1 |
| `Eyewear` | 11 | 86 | 10.3 |
| `Backpack` | 10 | 65 | 7.8 |
| `Earpiece` | 9 | 56 | 6.7 |
| `ArmorVest` | 3 | 40 | 4.8 |
| `FaceCover` | **0** | 75 | 9.0 |

`FaceCover` at 0 of 12 is not chance - it is `--skip-slot FaceCover`. Re-rolling the seeds shows a
`FaceCover` item on 10 of the 12 (`balaclava_cg` six times, `item_equipment_facecover_nomexBalaclava`
three, `item_equipment_facecover_shemagh_02` once), none of which reached a pack. §5.3 explains why
that is a legitimate thing to do.

---

## 4. The skinned / rigid split

### 4.1 The test is what the prefab IS

`build_loadouts.classify` (`build_loadouts.py:36-54`) resolves an item prefab with its CAB
dependencies, looks at the renderer components the **container itself** introduced, and returns:

```
SkinnedMeshRenderer present  ->  "skinned"
else MeshRenderer present    ->  "rigid"
else                         ->  None      (a real item with no worn geometry)
```

`kit_parts.roll` (`kit_parts.py:132-146`) routes on that result and on nothing else. A `"skinned"`
item is appended to `spec["parts"]` and goes through `skin.load_part`, deforming with the body for
free. A `"rigid"` item is appended to `spec["equipment"]` with an authored bone and goes through
`skin.load_attachment`. `None` is **recorded, never swallowed** - the report line reads
`skip / no renderer in prefab`, and the item is a real one that simply has no worn mesh (ammunition, a
secured container).

The test is a property of the asset, so it cannot drift as BSG adds items, and it cannot be fooled by
a name.

### 4.2 It is nearly slot-aligned, and the exception is the whole argument

Measured by running `classify()` over **all 87 items** in `pmcbear`'s seven wearable tables:

| slot | skinned | rigid |
|---|---:|---:|
| `ArmorVest` | 13 | 0 |
| `TacticalVest` | 19 | 0 |
| `Backpack` | 11 | 0 |
| `Headwear` | 0 | 22 |
| `Eyewear` | 0 | 8 |
| `Earpiece` | 0 | 7 |
| `FaceCover` | **6** | **1** |

Six of the seven slots are pure. `FaceCover` is not: `mask`, `balaclava_cg`, `balaclava_skull`,
`shemagh`, `item_equipment_facecover_shemagh_02` and `item_equipment_facecover_nomexBalaclava` are
skinned cloth, while `item_equipment_facecover_beard_red` - a beard - is rigid. A routing table keyed
on slot name would be right 86 of 87 times and would put a beard through the skinned path, where
`load_part` raises on a mesh with no weights unless `skip_unskinned` swallows it (`skin.py:526-538`),
i.e. the item would vanish. One counterexample is enough; the structural test costs nothing and has
no counterexamples.

### 4.3 What each path costs and guarantees

**Skinned** (`skin.load_part`, `skin.py:347`). The item's own `Mesh.m_BoneNameHashes` are resolved
against the canonical rig, weights are renormalised to a partition of unity, and a **rig-sized**
inverse-bindpose table is emitted - 79 × 64 bytes ≈ 5 KB per mesh, identity everywhere the item does
not bind. The payoff is that the item shares the character's single joint palette: no per-item bone
mapping at runtime, and armour that follows the spine when the character leans. Measured bind widths
across the twelve packs: a chest rig binds **5 to 10** rig bones, an `item_equipment_armor_*` vest **8 to 10** (kora kulon 8, both 6B23 10), a
backpack **7 to 9**, against **12** for trousers and **49 to 61** for a torso garment
(`top_bear_blacklynx` 49, `top_bear_spna` 52, `tshirt_bear_black` 58, `top_bear_polevoi` 61). On this
path the kit build passes `skip_unskinned=True` (`build_character.py:272-279`),
because an equipment prefab ships rigid proxies next to the worn renderer and those must be dropped
rather than fail the whole item.

**Rigid** (`skin.load_attachment`, `skin.py:639`). Geometry is taken verbatim, every vertex is pinned
to one bone with full weight - `jointIndex = (0,0,0,0)`, `jointWeight = (1,0,0,0)`
(`skin.py:820-824`) - so the same vertex layout and the same shader serve both paths, and the mesh's
transform within the prefab is composed up to the prefab root and stored as a local TRS
(`skin.py:745-776`). The guarantee is that the item is rigid: it turns with the head and never
deforms, which is correct for a helmet and wrong for cloth. The cost is that **the bone and the frame
are choices** (§5).

Measured across the twelve packs, every one of the 31 emitted attachments targets **bone 24,
`Base HumanHead`**, with `localPos = [0,0,0]`, `localScale = [1,1,1]` and
`localRot = (−0.7071, 0, 0, 0.7071)` - the −90° X fixup that a DCC-Z-up asset carries into Unity's
Y-up. Not one attachment in twelve packs deviates.

---

## 5. What is AUTHORED here, and why the two roll flags are not

The distinction is load-bearing in this project, so it is worth stating flatly: in the whole outfit
and kit pipeline there are **exactly two authored values**, and both are on the rigid path.

### 5.1 `SLOT_BONE` - authored

`kit_parts.py:50-59`:

```
Headwear, FaceCover, Eyewear, Earpiece -> Base HumanHead
Backpack                               -> Base HumanBackpack
ArmorVest, TacticalVest                -> Base HumanSpine3
ArmBand                                -> Base HumanLForearm3
```

This is a choice, flagged as one at `kit_parts.py:30-35` and `skin.py:99-101`. A rigid item's prefab
does not say where it goes: its `Dress` component lists renderers and a decal type, and the real
slot→`SlotView` mapping lives in `PlayerBody`, which is IL2CPP code and is not captured.
`characters.json:74-82` already made the same choice once per item for Tagilla's welding mask;
`SLOT_BONE` makes it once per slot instead.

Measured scope of the authored surface: of the eight entries, only `Base HumanHead` is ever reached
for `pmcbear`, because all 43 `ArmorVest` + `TacticalVest` + `Backpack` items classify skinned (§4.2)
and `ArmBand` is never rolled (§3.1). Whether `Base HumanBackpack`, `Base HumanSpine3` or
`Base HumanLForearm3` are the right answers is **unverified**: no shipped `pmcbear` item routes
through them. What would verify it is a bot type whose rigid table reaches those slots, built and
rendered.

### 5.2 The socket frame - authored in kind, derived in value

Storing an attachment's `localRot` says nothing about what it is local *to*, and on this rig the two
plausible answers differ by exactly 90°. `characters-and-animation.md` §10 establishes the halves:
the weapon is authored in the ENGINE bone frame so `import_eftweap` undoes the `q4` bone-axis
permutation, while rigid equipment is authored **Unity Y-up** - a prefab root at identity above a
mesh node carrying one −90° X fixup - so it does not. This rig is +X-down-the-bone, hence the 90°.

Getting up right is only half of it. A bone frame has three axes and the other two are fixed by the
bone's **roll**, a rigging convention with nothing to do with which way the face points, so an item
can sit crown-up and still be yawed 90° - a headset across the skull sideways, a ballcap with its
peak over the ear. Rather than author a second hand-picked rotation, `import_eftchar._socket_basis`
(`import_eftchar.py:725-768`) **derives** the socket from the bind pose:

```
up      = pack +Y
forward = manifest characterForward     (itself derived from a walk clip's root motion)
right   = up x forward
socket  = rest_rotation^-1 @ [right | up | forward]
```

and composes `pose.matrix @ socket @ local` (`import_eftchar.py:721`). Being bone-local it rides the
animation unchanged; being derived per bone it is equally right for a cap on the head, a pack on
`Base HumanBackpack` and an armband on a forearm, none of which share a roll convention. **The
decision to compose in the item's authoring frame is authored; the rotation that gets there is
derived.** That is the smallest authored surface the problem admits.

### 5.3 `--prefer` and `--skip-slot` are not authoring

Both flags exist for staging renders, and both were used to build the twelve `kit_bear_*` packs. They
are legitimate for the same reason and it is worth being precise about it.

**`--skip-slot SLOT` drops a slot** (`kit_parts.py:125-127`, `build_character.py:541-542`). The
resulting character is not wearing that item. That is a state the game itself produces on every roll
where the fill chance fails: `pmcbear` wears a `FaceCover` 75 % of the time, so a bare-faced BEAR PMC
is 25 % of the population. Skipping the slot moves the character within the distribution; it does not
move him outside it.

**`--prefer SLOT=SUBSTR` biases a slot without inventing an item** (`kit_parts.py:97-118`,
`build_character.py:543-546`). It filters the bot's own table to entries whose `_name` contains one
of the substrings, and re-rolls with `weighted_pick` on the **surviving game weights**. Nothing is
added to the table, no weight is edited, and if nothing matches the slot is left exactly as rolled - 
measured: `--prefer Headwear=no_such_item_xyz` on seed 2 leaves `cap_BEAR` untouched. Every item it
can produce is one the bot genuinely spawns wearing.

Writing an item id into the spec directly would be a different act: it could name an item that is not
in that bot's table at all - the `head_civilian_1` mistake of §2.4 in a new costume - and nothing
downstream would notice, because the pipeline has no check that a worn item is in-distribution. The
guardrail is that `--prefer` cannot express that.

Three properties of `--prefer` that are not obvious and are all measured:

1. **The match is against `_name`, not the prefab path.** `cap_BEAR`'s prefab is
   `item_equipment_head_bear_green.bundle`, but its `_name` is `cap_BEAR`, so
   `--prefer Headwear=head_bear` narrows the 22-option table to exactly one item - 
   `item_equipment_head_bear_black` - and the green cap is not eligible. Measured: that flag returns
   `item_equipment_head_bear_black` for all twelve seeds.
2. **A narrow filter is deterministic, not weighted.** The weights are still the game's, but one
   surviving candidate makes the roll a foregone conclusion. `--prefer` narrows a distribution; it
   does not preserve one.
3. **It can fill a slot the fill-chance roll declined**, because `kit_parts.py:117` assigns
   `worn[slot]` unconditionally. Measured: `pmcbear` seed 11 rolls no Headwear at all, and
   `--prefer Headwear=head_bear` gives it `item_equipment_head_bear_black` riding `Base HumanHead`.
   That is still an in-distribution item, but the *slot occupancy* is no longer the game's.

Reconciling the twelve packs against a clean re-roll leaves exactly two classes of difference, both
explained by these flags: `FaceCover` missing everywhere (`--skip-slot`), and `Headwear` forced to
`item_equipment_head_bear_black` on seeds 0, 3, 6 and 9 (`--prefer Headwear=head_bear`). Every other
slot on every other seed matches the roll exactly.

---

## 6. Garment variants and the combination problem

### 6.1 The game ships one garment per combination

A top is not a single mesh. The game authors it **cut for each combination of things worn over it**,
and ships all of them in the one prefab. Measured across the twelve packs:

| kind | variants shipped | example |
|---|---|---|
| torso garment | `base`, `_CR_`, `_AR_` | `Top_Bear_SpNa_base_lod0`, `_CR_lod0`, `_AR_lod0` |
| backpack | `_Body_`, `_CR_`, `_AR_`, `_CR_AR_` | `BP_WarTech_Body_lod0`, `_CR_lod0`, `_AR_lod0`, `_CR_AR_lod0` |
| chest rig | `_body_`, `_AR_` | `CR_Triton_body_lod0`, `CR_Triton_AR_lod0` |
| armour vest | one | `AR_Kora_kulon_LOD0` |
| trousers, t-shirt | one | `Pants_bear_Voin_lod0`, `Tshirt_bear_black_lod0` |
| head | `_base`, `_custom` | `Bear_Head_3_lod0_base`, `Bear_Head_3_custom_lod0` |

The token names **what else is present**, not what the mesh is: a top's `_AR_` is the version cut to
be worn under body armour, a backpack's `_CR_` is the version whose straps are cut for a chest rig.
Armour is outermost of its pair and needs no variant; trousers and a t-shirt have nothing to cut for.

**They occupy the same space.** Measured centroid separation between the variants of one part:
`Top_bear_BlackLynx` base/CR/AR - **7.0 mm**; `BP_WarTech` Body/CR/AR/CR_AR - **17.0 mm**;
`Bear_Head_3` base/custom - **0.1 mm**. The torso garment's variants agree to about two centimetres; the backpacks' straps are recut, so `BP_WarTech` and `BP_6SH118` differ by 47 mm and 45 mm corner to corner. The point rests on the centroids, not the boxes. These
are alternatives, not layers.

### 6.2 Why the extractor ships all of them

Only ONE variant is referenced by a renderer. Measured on the live install:

- `top_bear_blacklynx.bundle` owns six meshes (3 variants × 2 LODs). `Top_bear_BlackLynx_Base_lod0`
  and `_lod1` are addressed by `SkinnedMeshRenderer`s; the four `_AR_` / `_CR_` meshes are addressed
  by **nothing**.
- `item_equipment_backpack_wartech.bundle` owns thirteen: `BP_WarTech_Body_lod0/lod1` on
  `SkinnedMeshRenderer`s, six unreferenced `_AR_ / _CR_ / _CR_AR_` meshes, and five zero-bindpose
  `BP_WarTech_Drop_lod0/lod1`, `Drop_SHADOW_lod0/lod1` and `Drop_COLLIDER` on `MeshFilter`s.
- `bear_head_eug.bundle` owns four: `_base` on renderers, `_custom` unreferenced.

The runtime swaps `SkinnedMeshRenderer.sharedMesh` to the right variant when it dresses the character.
So a renderer-driven read would ship only the base cut and there would be no way to dress anyone;
`load_part` therefore takes **every mesh the container owns** (`skin.py:423-428`), which is what makes
the variants available at all. The rigid proxies are what `skip_unskinned` exists to drop
(`skin.py:526-538`), and the `_lodN` regex searches anywhere in the name rather than at the end
(`skin.py:144-152`) because equipment names are `item_..._lod1_base`, not `..._lod1`.

The consequence is that **the pack ships the whole variant set and the consumer must choose.**

### 6.3 The choice is a function of the kit, which is why `kit.json` exists

Drawing them all is three coats at once, in the same 7 mm of space: z-fighting across the whole torso
wherever two cuts are coplanar, and each garment's cut edges poking through the others. Keeping the
`base` unconditionally is the opposite error - a jacket cut for a bare torso under a chest rig, so the
rig's straps sink into a sleeve that was never trimmed for them.

Neither is fixable from the mesh names alone, because the correct answer depends on which slots the
kit filled. `build_character.py:591-602` therefore writes a `kit.json` beside the pack:

```json
{"bot": "pmcbear", "seed": 2,
 "slots": ["ArmorVest", "Backpack", "Earpiece", "Eyewear", "Headwear", "TacticalVest"],
 "items": [{"slot": "ArmorVest", "item": "item_equipment_armor_kora_kulon_black", "kind": "skinned"}, …]}
```

`slots` is the selector. The manifest itself carries **no** slot information - a mesh entry's `part`
is the bundle stem and `source.bundles` lists the item bundles in load order, neither of which says
what the item was worn as. Note also that `kit_parts.apply` sets `spec["kit"]`
(`kit_parts.py:158`), and nothing consumes it: the manifest has no `kit` key. The sidecar is the
record.

Worked example, `out/characters/kit_bear_2` (`ArmorVest` and `TacticalVest` both filled). The pack
ships **16 meshes, 15 of them third-person**. The correct third-person draw set is **six**: one top
variant, the trousers, one head, the armour, one backpack variant, one rig variant. The other nine
are three alternatives that should not be drawn (two spare top cuts, one spare head), three spare
backpack cuts, one spare rig cut, and two exact duplicates (§8.3). `kit_bear_0` (rig, no armour) is
the same shape: 13 meshes, 12 third-person, **five** correct.

`kit_bear_2` also shows that the variant set is **not complete**. Both `ArmorVest` and `TacticalVest`
are filled, so the top wants `_CR_AR_` - and `top_bear_spna` ships only `base` / `_CR_` / `_AR_`.
Backpacks ship the four-way set; tops in these twelve packs never do. A selector must therefore
degrade rather than assume, and `_AR_` is the honest fallback when both are present, because armour is
the outer layer against the top.

### 6.4 The naming is not uniform, and a substring rule breaks

Traps, all measured in the twelve packs:

- **Case varies within one convention**: `_Base_` (BlackLynx) vs `_base_` (SpNa, Sumrak, Borey);
  `_body_` (Triton, Azimut, tv110) vs `_Body_` (WarTech, Gr99T20); `_AR_` vs `_Ar_` (`CR_6h112_Ar_lod0`).
- **The token is not always the same word**: `top_bear_polevoi` names its armour cut
  `Top_bear_Polevoi_Armor_lod0`, not `_AR_`. A strict `_AR_` match misses it and falls back to base.
- **The prefix is part of the asset name, not a variant marker**: `AR_strandhogg_lod0` is a
  `TacticalVest` with no variants at all, and `CR_Ars_Arma_A18_*` is also a `TacticalVest`. A
  substring search for `AR` matches both, and matches the wrong thing in both.
- **The token can sit on either side of the LOD token**: `Bear_Head_3_custom_lod0` vs
  `Bear_Head_0_lod0_custom`, in two heads from the same table.
- **One part can change its own prefix mid-set**: `item_equipment_backpack_gr99t30_black` ships
  `BP_Gr99_T30_Body_LOD0` and `BP_Gr99_T30_CR_LOD0` alongside `Backpack_Gr99_T30_AR_LOD0` and
  `Backpack_Gr99_T30_CR_AR_LOD0`.

A selector must therefore be case-insensitive, token-based on the segments around `lodN`, and scoped
to the meshes of **one part** rather than the whole pack.

### 6.5 The head pair is a different question

`_base` / `_custom` is not kit-dependent. The two differ by roughly 2 vertices and 0.1 mm of
centroid (`Bear_Head_3`: 3,376 vs 3,374; `BEAR_head_Eug`: 2,728 vs 2,728), and the appearance table
resolves to the whole prefab, not to one of the pair, so nothing in the data this pipeline reads
selects between them. What the runtime uses `_custom` for is **unverified**; what would verify it is
the `Dress`/`Skin` MonoBehaviour payload or the `PlayerBody` code. In the meantime the SMR-referenced
`_base` is the defensible default, and `load_attachment` already applies exactly that rule on the
rigid path - it skips any mesh whose name ends `_custom` (`skin.py:793-796`), because taking both
draws the item twice. `load_part` does **not**, so every pack whose head prefab ships the pair
carries both: all twelve kit packs, and `assault_0` / `player_0` / `pmcbear_0` / `pmcusec_0`. The
boss heads (Glukhar, Killa) and the scav civilian head ship no `_custom` variant, and `tagilla`
carries no head mesh at all.

### 6.6 Who selects, and on what signal

`import_eftchar.py:70-72` still records the importer's own position: it imports every mesh at the
selected LOD, and `mesh_filter` (`import_eftchar.py:1105`, `:1123`) is the lever a caller pulls. The
selection itself lives in the consumers, and there are two, which must agree or a pack reads as a
different character in each:

| consumer | where |
|---|---|
| the reference render harness | `renders/kit_sheet.py::make_keep` (untracked - `renders/` is gitignored) |
| the viewer | `viewer/src/character/rig.rs::meshes_to_draw` |

**Select on `part`, not on the name.** Both started out grouping by the mesh name with the variant
tag stripped, and both were wrong, because the bare cut is not untagged - it is spelled `_Base_`,
`_Body_` or `_body_` - and because the names are not even internally consistent. `assault_0` ships
`Top_Wildman_Russia_Armor_lod0`, `Top_Wildman_Russia_CR_lod0` and `Top_wild_Russia_base_lod0`: three
cuts of one garment under two different stems, which no token rule groups. The consequence was
silent and shipped: `pmcbear_0` drew `Top_bear_BlackLynx_CR_lod0` **and**
`Top_bear_BlackLynx_Base_lod0`, two coats at once, in every render made before this was caught.

Every mesh carries the `part` it came from (§11), and every variant of a garment shares it by
construction, whatever it is called. Grouping on `part` is therefore structural in the way the rest
of this pipeline prefers: it cannot drift as items are added.

The guard against over-merging is geometric. A part MAY ship two genuinely separate pieces rather
than two cuts of one, so a member whose AABB centre is more than 0.25 m from the chosen mesh is kept
as well - alternatives are co-located by definition, and measured, the variants of one garment sit
within 17 mm of each other (§6.5).

---

## 7. From spec to pack: build steps 2 and 2b

`build_character.py --bot pmcbear --seed 2 --kit` runs:

1. `appearance.resolve` returns a spec with `parts[]`, `controller`, `rootMotion`, `defaultClipSet`,
   `appearance{}` (`build_character.py:566-570`).
2. `--prefer` is parsed into `{slot: [substrings]}` and `kit_parts.apply` merges the kit into that
   same spec in place (`build_character.py:580-588`): skinned items **appended to `parts`**, rigid
   items appended to `equipment`, and a report returned.
3. `build()` runs unchanged. **Step 2** (`build_character.py:266-293`) loads every entry of `parts`
   through `skin.load_part`, marking an entry `is_kit` when its `slot` is in `kit_parts.WORN_SLOTS`
   and passing `skip_unskinned=True` for those. **Step 2b** (`build_character.py:295-314`) resolves
   each `equipment` entry's bone by name against the rig and loads it through `skin.load_attachment`;
   a bone name the rig does not have is a warning and a skip, not a crash.
4. `kit.json` is written beside the pack (§6.3).

Two properties of the merge are worth naming. First, **equipment does not need a new format, a new
baker or a new importer** - it reuses the two paths the character builder already had, so a skinned
item skins with the body for free and a rigid one rides its bone for free. Second, the ORDER matters
for bindposes: `parts` are loaded body-first and equipment is appended after, and
`import_eftchar.merged_inverse_bindposes` estimates each mesh's correction only over bones some
**other** mesh already defined (`import_eftchar.py:278`), so the body defines the reference frame and
the equipment is carried into it, never the reverse.

Path handling: item prefabs from `item_templates.json` are StreamingAssets-relative
(`assets/content/items/...`), while the part loader resolves against the characters root.
`kit_parts._char_relative` (`kit_parts.py:68-73`) strips the prefix when present and leaves the path
alone when it is not, which is why an equipment bundle path survives into `source.bundles` at full
length while a body prefab appears as `character/prefabs/...`.

---

## 8. The three ways a worn item breaks

### 8.1 An unresolved dependency - the pure white helmet, the item that vanishes

**An equipment prefab is often not self-contained**, and a body prefab always is, which is why this
never surfaced until the kit path existed. Both `load_part` (`skin.py:364-394`) and
`load_attachment` (`skin.py:653-668`) resolve the container's CAB dependencies into one `UnityPy`
environment before reading anything.

Measured on the live install, loading each bundle alone and then with its dependencies:

| bundle | alone | with deps | consequence of loading alone |
|---|---|---|---|
| `item_equipment_helmet_ulach_coyote` | 4 Mesh, 1 Material, **0 Texture2D** | 88 Texture2D reachable, 4 deps | the material's `_MainTex`, `_BumpMap` and `_SpecMap` all carry `m_FileID: 2`; none resolve. **A pure white helmet over the operative's face.** |
| `item_equipment_armor_6b23_mflora` | **0 Mesh**, 2 SkinnedMeshRenderers | 6 Mesh reachable, 5 deps | both renderers point at `m_FileID: 1`; the loader raises `no Mesh objects` and **the whole item is lost** |
| `item_equipment_glasses_6b34` | 4 Mesh, 2 Material, 3 Texture2D | 89 Texture2D, 4 deps | the FRAME material resolves in-container; the GLASS material's `_MainTex` and `_SpecTex` are `m_FileID: 2`. **A white lens in a correctly textured frame** - not a white item. (`skin.py:364-372` describes this item as carrying no textures at all; measured, that is imprecise.) |

The two failures are silent in different ways: one raises and takes the item with it, one produces a
material with an empty texture set that renders as untextured white. The second is the dangerous one,
because the geometry is present, watertight and correctly placed.

Two rules keep the repair from becoming a new bug (`skin.py:374-380`):

- **Geometry comes from the container only** - plus, precisely, the meshes the container's own
  renderers reference by `path_id` (`skin.py:407-421`). Baking everything in the environment drags a
  neighbour's meshes into the item; taking only the container's own loses `6b23_mflora` entirely.
- **A dependency entry is consulted only for a `path_id` the container did not already define**
  (`skin.py:433-437`). `path_id`s are per-file, so preferring the container's own is what stops a
  collision silently binding a stranger's texture.

The repair has a benign side effect worth knowing so it is not mistaken for corruption: dependency
`Material`s are emitted too. Measured, `kit_bear_2` carries **111 materials of which 11 are
referenced by any submesh**, and in every one of the twelve packs the referenced set and the
*has-any-texture* set are identical - the unreferenced ones come out empty. Material count is
therefore not a useful sanity signal on a kit pack.

### 8.2 A disagreeing bindpose - long spikes

Blender has one armature with one rest pose and skins `Pose(j) · Rest(j)⁻¹ · v`, while the pack skins
`Pose(j) · InverseBindpose_mesh(j) · v`. Those agree only where every mesh shares a bindpose table.
Body parts do. **Equipment does not always.**

Measured on the twelve packs, taking each mesh's implied correction `C = IBP_ref(j)⁻¹ · IBP_mesh(j)`
over the bones an earlier mesh already defined:

| mesh | shared bones | max element of `C − I` | spread of `C` across those bones | verdict |
|---|---:|---:|---:|---|
| `BP_Gr99T20_*` (kit_bear_1) | 7–8 | 3.1e-07 | 6.7e-07 | agrees with the body |
| `AR_Kora_kulon_LOD0` (kit_bear_2) | 8 | 2.7e-07 | 6.7e-07 | agrees |
| `BP_6SH118_*` (kit_bear_2) | 8–9 | **1.0** | 6.1e-07 | **constant → bakeable** |
| `Wild_Body_1_firstHands` (all) | 40 | 1.005 | **0.145** | not constant → excluded |

`BP_6SH118`'s `C` is not noise and not a rig error. Rounded, it is exactly

```
[ 1  0  0 ]
[ 0  0 -1 ]      a +90 degree rotation about X, translation 3.8e-07 m
[ 0  1  0 ]
```

 -  a Z-up authoring frame that the item's bindpose kept and the body's did not. Because it is a
**constant right factor**, `IBP_mesh(j) = IBP_ref(j) · C` on every shared bone to 6.1e-07, it factors
out of `Σⱼ wⱼ Pose(j) IBP(j) v` and can be baked into the item's vertices exactly, for any number of
influences (`import_eftchar.py:231-307`). Bones the item alone binds are then stored pre-corrected as
`own[b] · C⁻¹`, or the bake would displace them.

Left uncorrected, the mesh tears into long spikes - the `import_eftchar` docstring records the same
disagreement as **1.245** under its own metric and states the visual signature
(`import_eftchar.py:236-238`). Two structural traps make a naive merge unable to see it, and both are
measured above: the **first-person hands** bind the same rig with a different binding and a
non-constant offset (spread 0.145), so passing them first would let never-drawn geometry define
shared bones; and a mesh that is the **first to claim** a bone becomes its own reference there, so a
backpack claiming `Base HumanBackpack` alone compares as identity on that bone and as 90° on the
spine, making a genuinely constant offset read as non-constant.

### 8.3 A duplicate mesh - z-fighting, and it is live

**Ten of the twelve shipped packs contain a duplicated mesh**, 12 extra entries in total:

| pack | duplicated name |
|---|---|
| kit_bear_0 | `AR_strandhogg_lod0` |
| kit_bear_2 | `AR_Kora_kulon_LOD0`, `CR_Triton_body_lod0` |
| kit_bear_3 | `CR_Azimut_body_lod0` |
| kit_bear_4 | `AR_6B23_lod0`, `CR_tv110_body_lod0` |
| kit_bear_5 | `CR_Wartech_TV-115_lod0` |
| kit_bear_6 | `CR_6h112_body_lod0` |
| kit_bear_7 | `CR_tv110_body_lod0` |
| kit_bear_8 | `CR_arscpc_lod0` |
| kit_bear_9 | `CR_Ars_Arma_A18_Body_lod0` |
| kit_bear_10 | `AR_6B23_lod0` |

The cause is structural, and it is the same drop-proxy pattern §6.2 describes - with one difference
that defeats the existing guard. An equipment prefab ships two parallel trees: the **worn** tree
(`SkinnedMeshRenderer` + `LODGroup`) and the **dropped-on-the-ground** tree (`MeshFilter` +
`MeshRenderer` + its own `LODGroup`). Measured on `item_equipment_rig_strandhogg.bundle`: four own
meshes, two named `AR_strandhogg_lod0` and two named `_lod1`; one of each pair is on a
`SkinnedMeshRenderer`, the other on a `MeshFilter` - and **the `MeshFilter` copy carries 8 bindposes
and full skin weights**, so `skip_unskinned` does not drop it the way it drops
`BP_WarTech_Drop_SHADOW_lod0` (0 bindposes). `load_part` admits every mesh the container owns, so
both are baked.

The signature in the manifest is exact. The worn `AR_strandhogg_lod0` carries materials 55…63 across
its nine submeshes; its twin carries **material 55 on all nine**, because `smr_by_mesh` is keyed by
mesh `path_id` (`skin.py:481-486`) and the `MeshFilter` copy has no `SkinnedMeshRenderer`, so
`mat_index.get(0, material_base)` falls through to the part's base index. Vertex bytes, indices,
bound bones and bindposes are byte-identical between the two.

`kit_bear_9` shows the other flavour: `CR_Ars_Arma_A18_Body_lod0` appears twice with **different**
vertex bytes, 7 bound bones against 6, and bindposes differing by up to 2.0 - measured on the live
prefab, the `SkinnedMeshRenderer` copy has 7 bindposes and the `MeshFilter` copy 6. Two genuinely
different meshes sharing one name.

**Neither the extractor nor either consumer deduplicates.** What you get is the item drawn twice,
coincident: z-fighting over its whole surface where the copies are identical, and on the A18 a second
copy skinned against a different bindpose set on top of the first.

The fix implied by the measurement - take only the meshes the container's own **renderers** reference,
which is already computed as `wanted_mesh_ids` (`skin.py:412-421`) but currently used only to admit
dependency meshes - would also remove every unreferenced variant, so it cannot be applied without
first implementing variant selection (§6.6). That is why it is documented here rather than fixed.

---

## 9. What is dropped

Stated plainly, because a reimplementer will look for these.

- **`voice`.** `pmcbear`'s appearance table has five weighted voice entries. `appearance.SLOTS`
  does not include the slot.
- **`WatchPrefab` / `WatchPosition` / `WatchRotation`.** Measured, **35 of 597** customization
  entries name a wristwatch bundle (`assets/content/hands/bear/bear_watch_traserp66.bundle` and
  friends) with a position and a rotation. `appearance.py:143-145` reads only `_name`,
  `_props.Prefab.path` and `_props.BodyPart`. The watch is the one piece of equipment that lives in
  the customization table rather than the item table, and it is not extracted. It is carried by
  `*_hands` entries, and `pmcbear`'s hands table has exactly one option, so it would never appear for
  this bot type in any case.
- **`IntegratedArmorVest`.** A per-customization boolean, true on exactly **one** of the 597
  entries (`wild_Gluhar_body`). It is precisely the flag a variant selector would want - a body that
  already includes its armour - and nothing reads it.
- **`chances` beyond `equipment`.** `loadout.py:157` reads `chances.equipment` and nothing else.
- **`Pockets`, `SecuredContainer`, `Holster`, `Scabbard`, `SecondPrimaryWeapon`.** Present in
  `pmcbear`'s equipment tables (1, 1, 7, 0, 0 options), excluded from `wearableSlots`. A holster
  weapon is real geometry the game shows on the hip; it is not built.
- **`ArmBand`.** In `SLOT_BONE` but not in `wearableSlots`, and empty for `pmcbear`.
- **The `Dress` component's contents.** `load_attachment` never reads it; the slot→bone mapping it
  does not contain is the reason `SLOT_BONE` is authored (§5.1).
- **LOD1 and below, by default.** `--lod 0` is the normal build and only the requested set survives
  (`skin.py:501-502`). Every prefab inspected on the live install - six garment, backpack, rig and
  headwear bundles - ships exactly two LODs under one `LODGroup` per tree; that this holds for all
  equipment is unverified.

---

## 10. Invariants and their failure signatures

The "what you SEE" column mixes signatures recorded in the source notes when each invariant was
broken with consequences derived from the measurements above; the latter are marked *(predicted)* and
have not been observed on a render in this repo as it stands.

| invariant | how it breaks | what you SEE |
|---|---|---|
| every worn item comes from THIS bot type's own table | an item is written into the spec by hand | a complete, well-formed, plausible character the game never spawns. Nothing errors and no check exists: the `head_civilian_1` scav passed review for months. `--prefer` cannot express this, which is the guardrail |
| the weights are the game's | a table is edited, or a "sensible" uniform pick replaces the weighted one | the population is wrong in aggregate and right in every instance, so no single render reveals it. Measured reference: `pmcbear` Headwear is 22 options summing to 67, `item_equipment_helmet_LSHZ` 8 and `cap_BEAR` 7 |
| the kit and the body are seeded together | rolling appearance separately from the kit | the pack contains a body the kit sidecar does not describe, so variant selection picks against the wrong garment. `loadout.py:185` makes this impossible by calling `appearance.resolve` with the same seed |
| the skinned/rigid split is decided by the prefab's renderer | routing by slot name | 86 of 87 `pmcbear` items still work and `item_equipment_facecover_beard_red` - a rigid beard in a slot that is otherwise cloth - goes down the skinned path, where `skip_unskinned` drops its only mesh and `load_part` then raises `no meshes survived the LOD filter` (`skin.py:634-635`), which `build_character.py:276-283` does not catch. The build DIES rather than shipping a beardless bot - loud, not silent, which is the better failure but not the documented one |
| a rigid item's bone is AUTHORED and flagged | treated as derived | nothing breaks today, and the next slot that produces a rigid item inherits an untested guess. Measured: only `Base HumanHead` is exercised in twelve `pmcbear` packs; the `Backpack` / `Spine3` / `ArmBand` entries are unverified |
| an attachment is composed in the frame it was AUTHORED in | using the weapon's rule (undoing `q4`) on rigid equipment | the item is present, watertight and correctly textured, and rotated 90 degrees: a helmet's crown points forward out of the face, a cap sits on the cheek |
| the socket's remaining two axes come from the bind pose, not a second guess | fixing "up" only | the item is crown-up and yawed: a headset lies across the skull sideways, a ballcap's peak points out over the ear. `_socket_basis` derives it from `up = +Y`, `forward = characterForward`, `right = up × forward` |
| CAB dependencies resolved before reading materials | single-bundle load | **a pure white helmet.** Measured: `item_equipment_helmet_ulach_coyote` owns zero `Texture2D` and all three of its material's texture PPtrs carry `m_FileID: 2`. The geometry is perfect, which is why it survives a structural check |
| geometry taken from the container's own renderers, not the whole environment | baking every mesh in `env` | a neighbour's assets appear on the character. The opposite error - container-only - loses `item_equipment_armor_6b23_mflora` outright, since it owns **no `Mesh`** and both its renderers point at `m_FileID: 1` |
| a disagreeing bindpose is factored and baked into the vertices | using the pack's per-mesh table against one shared rest pose | the item tears into long spikes. Measured: `BP_6SH118`'s correction is a +90° X rotation, constant to 6.1e-07 across 8-9 shared bones, while `BP_Gr99T20` agrees to 3.1e-07 and needs nothing |
| the correction is estimated only over bones ANOTHER mesh defined, third-person first | letting the first claimant or the first-person hands set the reference | a constant, bakeable offset reads as non-constant and is refused; the item spikes anyway. The hands' own offset has spread **0.145** and must never define a shared bone |
| one mesh per item per LOD | the `MeshFilter` drop copy baked alongside the worn mesh | the item is drawn twice, coincident: z-fighting over its whole surface. **Live in 10 of 12 shipped packs.** Manifest tell-tale: the twin's submeshes all carry the part's `material_base` while the worn copy carries a distinct material per submesh |
| exactly one garment variant drawn | all of them | three coats in the same 7 mm of space *(predicted)* - z-fighting across the torso and each cut's trimmed edges poking through the others. `kit_bear_2` ships 15 third-person meshes where 6 belong |
| the variant chosen from the FILLED SLOTS | keeping `base` unconditionally | a jacket cut for a bare torso worn under a chest rig *(predicted)*: the rig's straps sink into an untrimmed sleeve. The filled slots are in `kit.json`; the manifest does not carry them |
| variant matching is token-based and case-insensitive, scoped to one part | a substring search for `AR` / `CR` | `AR_strandhogg_lod0` (a rig with no variants) and `CR_Ars_Arma_A18_*` match on their prefix, and `Top_bear_Polevoi_Armor_lod0` does not match `_AR_` at all, so the polevoi top silently falls back to base |
| `--skip-slot` used only for slots the game also leaves empty | used to hide a slot that always fills | for `pmcbear` every wearable slot except `TacticalVest` has a fill chance below 100, so skipping any of the other six stays in-distribution. Skipping `TacticalVest` (chance 100) would not |
| `--prefer` narrows, never invents | writing an item id directly | see row 1. Note that narrowing to a single survivor makes the roll deterministic, and that `kit_parts.py:117` will fill a slot the chance roll had declined |

---

## 11. Old patterns

- **Hand-authored part lists in `characters.json`.** Measured against the game, the authored scav
  wore `head_civilian_1`, which does not appear in the scav appearance table at all (the game rolls
  `wild_head_1/2/3/drozd/misha`), and it had no hands slot. Appearance is now rolled from the game's
  own weighted tables; `characters.json` survives for named one-offs and for the few facts the tables
  do not carry. Recorded at `appearance.py:9-12` and in `characters-and-animation.md` §16.

- **`build_loadouts.py`'s routing: classify, then ignore it.** This is the defect `kit_parts.py`
  exists to route around, and it is worth recording precisely because the module looks correct.
  `build_loadouts.classify()` (`build_loadouts.py:36-54`) reports skinned-versus-rigid accurately - 
  `kit_parts` still calls it - and then `build_loadouts.py:110-119` classifies and builds
  regardless, writing `kind` into the kit record at `build_loadouts.py:126-130`. **Every** item goes
  to `build_weapon.build()` whatever `classify` said. The rigid
  weapon assembler merges every mesh in the prefab: both LODs, every unworn variant, and the
  dropped-on-the-ground proxy with its own transform.

  Measured on `out/kits/_parts`, against the same items on the skinned path in `out/characters`:

  | item | old rigid bake | skinned path (worn mesh) |
  |---|---|---|
  | `item_equipment_rig_strandhogg` | 21,194 v, 18 submeshes, bbox **1.813 m** | 10,597 v, 9 submeshes, bbox **0.613 m** |
  | `item_equipment_backpack_wartech` | 9,505 v, bbox **1.782 m** | 4,324 v, bbox **0.565 m** |
  | `item_equipment_armor_kora_kulon_black` | 5,090 v, bbox **1.788 m** | 2,545 v, bbox **0.472 m** |
  | `item_equipment_rig_tv115` | 11,728 v, bbox **1.835 m** | 5,864 v, bbox **0.470 m** |
  | `backpack_Raid_6SH118` | 17,551 v, bbox **2.262 m** | 8,268 v, bbox **1.010 m** |
  | `item_equipment_facecover_nomexBalaclava` | 2,562 v, bbox **1.983 m** | - (never built skinned) |

  Several totals are **exact doubles** of the worn mesh - 21,194 = 2 × 10,597 (strandhogg),
  5,090 = 2 × 2,545 (kora kulon), 11,728 = 2 × 5,864 (tv115), 9,154 = 2 × 4,577 (triton) - because
  the merge takes both LODs of the worn variant. Where the total is not an exact double (WarTech
  9,505 against 2 × 4,324; 6SH118 17,551 against 2 × 8,268) the prefab's drop and shadow proxies are
  in the sum too. The bounding box inflates to metres only when one of those proxies sits away from
  the origin, which is why `item_equipment_rig_triton` lands at a correct 0.532 m - identical to its
  worn mesh - while `strandhogg` lands at 1.813 m. `kit_parts.py:14-15` records 1.78 m for the
  WarTech backpack and 1.95 m for the balaclava; re-measured here the WarTech is 1.782 m on its longest
  axis and the balaclava 1.953 m on Y, 1.983 m on the longest.

  **The same baker is correct for rigid items**, which is the reason the bug survived: `cap_BEAR`
  bakes to `0.175 × 0.313 × 0.132`, byte-for-byte the bounding box of the `item_equipment_head_BEAR_LOD0_base`
  attachment in the character packs, and every helmet, headset and pair of glasses in
  `out/kits/_parts` lands between 0.18 m and 0.31 m. Only skinned items are wrong, and only skinned
  items were never rendered on a body.

  `build_loadouts.py` also exits non-zero on any rolled slot that produced no geometry
  (`build_loadouts.py:136-144`), which is the right instinct - a bot spawns wearing that item - and
  which is why the failure presented as "the pipeline passes" rather than as a visible drop.

- **Filling a required weapon slot with `sorted(allowed)[0]`.** Recorded at `loadout.py:127-133`: it
  picks by alphabet, which installed a fixed A2 rear sight under an HHS-1. The slot is now left empty.
  Same principle as §5.3.

- **Treating the first-person hands as a foreign skeleton.** `appearance.py:38-47` records the
  earlier reading and its correction: the hands bind the same biped, all 40 of their bone paths
  resolving as suffixes of canonical rig paths. They are tagged `view: "first"` for a different
  reason - a third-person body already has arms. The tag then turned out to be load-bearing a second
  time, in the bindpose merge (§8.2), where the hands' non-constant 0.145-spread offset must not be
  allowed to define a shared bone.

- **A CAB index built by globbing `*.bundle`.** Carried over from the map and character pipelines and
  equally fatal here: an item whose geometry lives in an extensionless dependency assembles to
  nothing. Bundles are recognised by their UnityFS-family header (`unity_deps.py:35`).
