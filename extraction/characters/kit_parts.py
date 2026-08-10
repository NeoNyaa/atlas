#!/usr/bin/env python3
"""Fold a bot's rolled EQUIPMENT into the character build spec.

WHY THIS EXISTS.  A character pack was clothing and a face: `appearance.py` rolls body / feet /
hands / head and nothing else, so every pack this repo built shipped `"attachments": []` and every
render showed an unequipped man.  The kit was not missing from the data - `loadout.py` has rolled it
from the game's own weighted tables all along - it was only ever baked by `build_loadouts.py`, which
hands EVERY item to `build_weapon.build()`.

That is the bug this module exists to route around.  `build_loadouts.classify()` correctly reports
whether an item's prefab is a `SkinnedMeshRenderer` or a `MeshRenderer`, and then the result is
discarded and the rigid weapon baker runs regardless.  For a skinned item that baker merges every
LOD and every SHADOW/drop proxy in the prefab and applies the prefab's own transforms, so the
geometry comes out scattered: measured on `out/kits/_parts`, a WarTech backpack baked to a 1.78 m
bounding box and a nomex balaclava to 1.95 m, against raw asset meshes of 0.56 m and 0.31 m.

The character builder already has BOTH correct paths and has had them all along:

    build_character.build() step 2   `skin.py::load_part` - bone remap, weights, a 79-entry
                                     inverse-bindpose table.  This is what a vest or a backpack
                                     needs: they deform with the body.
    build_character.build() step 2b  `skin.py::Attachment` - rigid geometry pinned to one bone,
                                     with the local transform composed down from the prefab root.
                                     This is what a cap, goggles or a headset needs.

So equipment does not need a new format, a new baker or a new importer.  It needs to be appended to
the spec the builder already consumes, on the correct one of those two paths.  A skinned item then
skins with the body for free, and a rigid item rides its bone for free.

THE BONE IS AUTHORED, AND IS FLAGGED AS SUCH.  A rigid item's prefab does not say where it goes:
its `Dress` component lists renderers and a decal type, and the slot -> bone mapping lives in the
runtime's `PlayerBody.SlotView`, which is code and is not captured.  `characters.json` already
records this choice per item for Tagilla's welding mask and states plainly that it is authored.
`SLOT_BONE` below is the same choice made once per SLOT instead of once per item; everything else
here is game-derived.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_loadouts
import loadout as loadout_mod
import unity_deps

#: AUTHORED, not game-derived - see the module docstring.  A rigid item in one of these slots hangs
#: off this rig bone.  Everything on the head rides `Base HumanHead` so it turns with the look;
#: `Base HumanBackpack` exists in the rig for exactly one purpose and this is it.
SLOT_BONE = {
    "Headwear": "Base HumanHead",
    "FaceCover": "Base HumanHead",
    "Eyewear": "Base HumanHead",
    "Earpiece": "Base HumanHead",
    "Backpack": "Base HumanBackpack",
    "ArmorVest": "Base HumanSpine3",
    "TacticalVest": "Base HumanSpine3",
    "ArmBand": "Base HumanLForearm3",
}

#: Slots that are worn geometry.  A weapon is not equipment: it is assembled and attached
#: separately, and a secured container has no worn mesh at all.
WORN_SLOTS = tuple(SLOT_BONE)

CHAR_ROOT = "assets/content/characters"


def _char_relative(rel):
    """The part loader resolves against the characters root; item prefabs are StreamingAssets
    relative.  Strip the prefix when it is there and leave the path alone when it is not."""
    if rel and rel.startswith(CHAR_ROOT + "/"):
        return rel[len(CHAR_ROOT) + 1:]
    return rel


def roll(bot_type, seed=0, tables=None, cabs=None, only_slots=None, skip_slots=None,
         prefer=None, verbose=True):
    """Roll `bot_type`'s kit and split it into (skinned_parts, rigid_equipment, report).

    prefer      {slot: [item name substrings]} - bias a slot toward specific items.  The weights
                stay the game's; this only filters WHICH of the game's own options are eligible, so
                a montage can ask for BEAR caps without inventing an item that does not spawn.
    only_slots  restrict to these slots entirely.
    skip_slots  drop these slots. Dropping a slot is not the same as the item failing to bake: the
                bot simply is not wearing one, which is a legitimate roll the game also produces.
    """
    if tables is None:
        tables = loadout_mod.load_tables()
    items, bots, presets, cust = tables
    if cabs is None:
        cabs = unity_deps.load(verbose=False)

    import fetch_bot_db
    kit = loadout_mod.roll(bot_type, seed, items, bots, presets, cust,
                           fetch_bot_db.load_config())
    worn = kit.get("worn") or {}
    if prefer:
        # Bias a slot toward particular items WITHOUT inventing one. The weights and the candidate
        # set stay the game's; this re-rolls the slot restricted to options whose name matches, and
        # leaves the slot exactly as rolled when nothing matches, so a montage can ask for BEAR caps
        # and still only ever get something the bot actually spawns wearing.
        import random
        eq = ((bots.get(bot_type) or {}).get("inventory") or {}).get("equipment") or {}
        for slot, wants in prefer.items():
            table = eq.get(slot) or {}
            elig = {}
            for iid, w in table.items():
                nm = (items.get(iid) or {}).get("_name") or ""
                if any(s.lower() in nm.lower() for s in wants):
                    elig[iid] = w
            if not elig:
                continue
            rng = random.Random("prefer:%s:%s:%d" % (bot_type, slot, seed))
            iid = loadout_mod.weighted_pick(rng, elig)
            t = items.get(iid) or {}
            pr = (t.get("_props") or {}).get("Prefab")
            worn[slot] = {"id": iid, "name": t.get("_name"),
                          "prefab": pr.get("path") if isinstance(pr, dict) else None}
    parts, equipment, report = [], [], []
    for slot, v in sorted(worn.items()):
        if slot not in WORN_SLOTS:
            continue
        if only_slots and slot not in only_slots:
            continue
        if skip_slots and slot in skip_slots:
            report.append((slot, v.get("name") or v.get("id"), "skip", "slot excluded by request"))
            continue
        rel, name = v.get("prefab"), (v.get("name") or v.get("id"))
        if not rel:
            report.append((slot, name, "skip", "item has no prefab"))
            continue
        kind, _bone_hint = build_loadouts.classify(rel, cabs)
        if kind is None:
            # Real item, no worn geometry (ammo, a container). Recorded, never silently swallowed.
            report.append((slot, name, "skip", "no renderer in prefab"))
            continue
        if kind == "skinned":
            parts.append({"path": _char_relative(rel), "slot": slot, "view": "third"})
            report.append((slot, name, "skinned", "deforms with the body"))
        else:
            bone = SLOT_BONE.get(slot)
            if not bone:
                report.append((slot, name, "skip", "no authored bone for this slot"))
                continue
            equipment.append({"bundle": rel, "bone": bone, "slot": slot, "name": name})
            report.append((slot, name, "rigid", "rides %s" % bone))
    if verbose:
        for slot, name, kind, why in report:
            print("[kit] %-14s %-42s %-8s %s" % (slot, str(name)[:42], kind, why))
    return parts, equipment, report


def apply(spec, bot_type, seed=0, **kw):
    """Merge a rolled kit into a build spec in place, and return the report."""
    parts, equipment, report = roll(bot_type, seed, **kw)
    spec.setdefault("parts", []).extend(parts)
    spec.setdefault("equipment", []).extend(equipment)
    spec["kit"] = [{"slot": s, "item": n, "kind": k} for s, n, k, _ in report if k != "skip"]
    return report
