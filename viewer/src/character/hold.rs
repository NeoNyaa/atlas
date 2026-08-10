//! Keeping the hands on the weapon through a cross-fade.
//!
//! A cross-fade is a per-bone, per-channel weighted accumulation of LOCAL pos/quat/scale - what
//! Mecanim does, and what [`super::anim::PoseAccumulator`] does to match it. A local blend does NOT
//! preserve a world-space relationship between two different kinematic chains, and the weapon hold
//! is exactly such a relationship:
//!
//! ```text
//! Weapon_root      <- Base HumanRibcage                          4 joints from the root
//! Base HumanRPalm  <- RForearm3 <- .. <- RCollarbone <- Ribcage  6 joints further out
//! ```
//!
//! Every joint in the fade contributes its own interpolated rotation, so through the window the
//! weapon and the hands travel different arcs and separate. Measured on `out/characters/scav` with
//! the Blender port of this solver: 90 mm peak on a sprint-to-stop, 110 mm on a prone transition,
//! and 204 mm of left hand off the foregrip.
//!
//! The source data is not at fault. Within a single clip `Weapon_root` is rigid to the right palm to
//! under 1 mm in 573 of 662 clips, and to BOTH palms in 445. The 89 that are not rigid are the
//! grenade, holster, melee and vault clips, where the weapon genuinely leaves the hand.
//!
//! WHICH CORRECTION. Moving the WEAPON onto the right hand is the obvious one and it is wrong: it
//! drags the rifle away from wherever the left arm's blend put it, and measured, the left hand went
//! from 139 mm off the foregrip to 304 mm. Both arms are solved onto the weapon instead, which is
//! what the game does - the Player prefab runs `FullBodyBipedIK` / `SimpleTIK` AFTER the Mecanim
//! blend, against the `IK_S_LPalm` / `IK_S_RPalm` effectors and `Bend_Goal_*` poles the rig ships.
//! In the shipped curves those effectors are coincident with the palms to 0.0 mm, i.e. the clip data
//! already contains the solved result, so reproducing the solve at the blend is the whole job.
//!
//! Two properties make it safe to leave on: with one active clip the target IS that clip's own palm,
//! so an unfaded frame is untouched (and the work is skipped rather than relied on to cancel); and
//! the grip is read PER FRAME, so nothing assumes rigidity and the clips where the hand really does
//! leave the weapon reproduce as authored.
//!
//! Mirrors `tools/blender/weapon_hold.py`. The two must agree or a stitched shot looks different in
//! Blender than it does here.

use super::pack::{CharacterPack, ClipData};
use bevy::prelude::*;

const WEAPON_BONE: &str = "Weapon_root";

struct Arm {
    palm: usize,
    fore: usize,
    upper: usize,
    pole: usize,
    twist: Vec<usize>,
}

pub struct WeaponHold {
    weapon: usize,
    palm_r: usize,
    arms: Vec<Arm>,
}

/// Never drive the elbow fully straight: at full extension the IK plane is undefined and the joint
/// pops between frames.
const MAX_REACH: f32 = 0.999;

fn compose(t: (Vec3, Quat, Vec3)) -> Mat4 {
    Mat4::from_scale_rotation_translation(t.2, t.1, t.0)
}

impl WeaponHold {
    pub fn new(pack: &CharacterPack) -> Option<Self> {
        let b = |n: &str| pack.bone_by_name.get(n).copied();
        let weapon = b(WEAPON_BONE)?;
        let palm_r = b("Base HumanRPalm")?;
        let mut arms = Vec::new();
        for (palm, fore, upper, pole) in [
            ("Base HumanLPalm", "Base HumanLForearm1", "Base HumanLUpperarm", "Bend_Goal_Left"),
            ("Base HumanRPalm", "Base HumanRForearm1", "Base HumanRUpperarm", "Bend_Goal_Right"),
        ] {
            let (Some(palm), Some(fore), Some(upper), Some(pole)) =
                (b(palm), b(fore), b(upper), b(pole))
            else {
                continue;
            };
            // The forearm is three bones because the roll is spread over twist joints. IK treats the
            // chain as two segments and leaves the twist locals alone so their share rides along.
            let mut twist = Vec::new();
            let mut cur = palm;
            while cur != fore {
                twist.push(cur);
                match pack.bones[cur].parent {
                    Some(p) => cur = p,
                    None => break,
                }
            }
            arms.push(Arm { palm, fore, upper, pole, twist });
        }
        (!arms.is_empty()).then_some(Self { weapon, palm_r, arms })
    }

    fn fk(pack: &CharacterPack, locals: &[(Vec3, Quat, Vec3)], out: &mut Vec<Mat4>) {
        out.clear();
        for (i, bone) in pack.bones.iter().enumerate() {
            let l = compose(locals[i]);
            out.push(match bone.parent {
                Some(p) => out[p] * l,
                None => l,
            });
        }
    }

    /// Blend 4x4s on TRS: translation accumulates, rotation nlerps hemisphere-aligned. The same
    /// accumulation the pose blend uses, so a grip and the body it rides interpolate on one rule.
    fn blend_trs(items: &[(Mat4, f32)]) -> Option<Mat4> {
        let mut t = Vec3::ZERO;
        let mut q = Vec4::ZERO;
        let mut refq: Option<Vec4> = None;
        let mut wsum = 0.0f32;
        for (m, w) in items {
            let (_s, r, tr) = m.to_scale_rotation_translation();
            let mut v = Vec4::new(r.x, r.y, r.z, r.w);
            match refq {
                None => refq = Some(v),
                Some(rf) => {
                    if v.dot(rf) < 0.0 {
                        v = -v;
                    }
                }
            }
            t += tr * *w;
            q += v * *w;
            wsum += *w;
        }
        if wsum < 1e-6 || q.length_squared() < 1e-12 {
            return None;
        }
        let qn = q.normalize();
        Some(Mat4::from_rotation_translation(
            Quat::from_xyzw(qn.x, qn.y, qn.z, qn.w),
            t / wsum,
        ))
    }

    /// `active` is each contributing clip with its own time and its NORMALISED weight, plus the
    /// per-clip locals the caller already sampled. Returns true if anything was corrected.
    /// `carry` is the grip being held across clips that key no weapon. It belongs to the CHARACTER,
    /// not to the solver: one solver serves every NPC, and a grip carried from one man's sprint into
    /// another man's prone transition would be worse than not carrying at all.
    pub fn solve(
        &self,
        pack: &CharacterPack,
        active: &[(&ClipData, f32, Vec<(Vec3, Quat, Vec3)>)],
        locals: &mut [(Vec3, Quat, Vec3)],
        carry: &mut Option<Mat4>,
    ) -> bool {
        if active.is_empty() {
            return false;
        }
        let keys_weapon = |c: &ClipData| c.tracks.iter().any(|t| t.bone == self.weapon);
        let all_key = active.iter().all(|(c, _, _)| keys_weapon(c));
        if active.len() < 2 && all_key {
            return false; // no-op by construction; skipping saves the FKs
        }

        let mut world = Vec::new();
        Self::fk(pack, locals, &mut world);
        let mut per_clip: Vec<(Vec<Mat4>, f32, bool)> = Vec::with_capacity(active.len());
        for (c, w, l) in active {
            let mut wm = Vec::new();
            Self::fk(pack, l, &mut wm);
            per_clip.push((wm, *w, keys_weapon(c)));
        }

        // ---- the grip, carried across clips that key no weapon ----
        // 33 of the scav pack's 662 clips have NO `Weapon_root` track: the game's controller runs
        // ten layers and weapon handling is its own, so a base-locomotion clip keys the body and
        // lets another layer place the weapon. There is no such layer here, and an unkeyed bone
        // falls back to the rest local, which parks the rifle by the ribcage for the clip's whole
        // length. Every clip votes with its own weight: one that keys the weapon votes for the grip
        // it authored, one that does not votes for the grip already being carried, so the value
        // eases between neighbours instead of snapping when the last keyed clip leaves the blend.
        let mut votes: Vec<(Mat4, f32)> = Vec::new();
        for (wm, w, keyed) in &per_clip {
            if *keyed {
                votes.push((wm[self.palm_r].inverse() * wm[self.weapon], *w));
            } else if let Some(g) = *carry {
                votes.push((g, *w));
            }
        }
        let grip = Self::blend_trs(&votes).or(*carry);
        if let Some(g) = grip {
            *carry = Some(g);
        }

        if !all_key {
            let Some(g) = grip else { return false };
            let target = world[self.palm_r] * g;
            let parent = pack.bones[self.weapon].parent.unwrap_or(self.weapon);
            let l = world[parent].inverse() * target;
            let (s, r, t) = l.to_scale_rotation_translation();
            locals[self.weapon] = (t, r, s);
            world[self.weapon] = target;
        }

        if active.len() < 2 {
            return true; // weapon repaired; the palms are this clip's own and need no solve
        }

        for ai in 0..self.arms.len() {
            let (upper, fore, palm, pole) = {
                let a = &self.arms[ai];
                (a.upper, a.fore, a.palm, a.pole)
            };
            // Each clip's palm read against ITS OWN socket - synthesised the same way for a clip
            // that keys none - which is what makes the solve exact at w=1 for every clip.
            let mut gs: Vec<(Mat4, f32)> = Vec::new();
            for (wm, w, keyed) in &per_clip {
                let wp = if *keyed {
                    wm[self.weapon]
                } else if let Some(g) = grip {
                    wm[self.palm_r] * g
                } else {
                    wm[self.weapon]
                };
                gs.push((wp.inverse() * wm[palm], *w));
            }
            let Some(g) = Self::blend_trs(&gs) else { continue };
            let target = world[self.weapon] * g;

            let a = world[upper].w_axis.truncate();
            let b = world[fore].w_axis.truncate();
            let e = world[palm].w_axis.truncate();
            let l1 = (b - a).length();
            let l2 = (e - b).length();
            let goal = target.w_axis.truncate();
            let d = goal - a;
            let dist = d.length();
            if dist < 1e-6 || l1 < 1e-6 || l2 < 1e-6 {
                continue;
            }
            let reach = dist.min((l1 + l2) * MAX_REACH);
            let dhat = d / dist;

            // Law of cosines fixes how far along a->goal the elbow sits; the pole fixes which way
            // it swings out of that axis.
            let cos1 = ((l1 * l1 + reach * reach - l2 * l2) / (2.0 * l1 * reach)).clamp(-1.0, 1.0);
            let proj = l1 * cos1;
            let h = (l1 * l1 - proj * proj).max(0.0).sqrt();
            let pv = world[pole].w_axis.truncate() - a;
            let mut perp = pv - dhat * pv.dot(dhat);
            if perp.length_squared() < 1e-12 {
                let bb = b - a;
                perp = bb - dhat * bb.dot(dhat); // pole collinear: keep the blend's own bend plane
            }
            if perp.length_squared() < 1e-12 {
                continue;
            }
            let perp = perp.normalize();
            let b_new = a + dhat * proj + perp * h;
            let e_new = a + dhat * reach;

            // Both corrections are pure world rotations about a joint, so the rest of the chain
            // rides along and only two locals need rewriting.
            let swing = |v0: Vec3, v1: Vec3| -> Quat {
                let (v0, v1) = (v0.normalize_or_zero(), v1.normalize_or_zero());
                if v0.length_squared() < 0.5 || v1.length_squared() < 0.5 {
                    return Quat::IDENTITY;
                }
                Quat::from_rotation_arc(v0, v1)
            };
            let r1 = swing(b - a, b_new - a);
            let w_up = Mat4::from_quat(r1) * world[upper];
            let w_up = Mat4::from_cols(
                w_up.x_axis, w_up.y_axis, w_up.z_axis,
                world[upper].w_axis,
            );
            let e_mid = a + r1 * (e - a);
            let r2 = swing(e_mid - b_new, e_new - b_new);
            let rot_fore = Mat4::from_quat(r2 * r1) * world[fore];
            let w_fore = Mat4::from_cols(
                rot_fore.x_axis, rot_fore.y_axis, rot_fore.z_axis,
                b_new.extend(1.0),
            );

            let set = |locals: &mut [(Vec3, Quat, Vec3)], bone: usize, m: Mat4| {
                let (s, r, t) = m.to_scale_rotation_translation();
                locals[bone] = (t, r, s);
            };
            let up_parent = pack.bones[upper].parent.unwrap_or(upper);
            set(locals, upper, world[up_parent].inverse() * w_up);
            set(locals, fore, w_up.inverse() * w_fore);

            // The wrist: re-FK the twist stub off the corrected forearm, then set the palm's local
            // so it lands on the authored grip ORIENTATION as well as its position.
            let mut cur = w_fore;
            let twist = self.arms[ai].twist.clone();
            for &bb in twist.iter().skip(1).rev() {
                cur = cur * compose(locals[bb]);
            }
            set(locals, palm, cur.inverse() * target);
        }
        true
    }
}
