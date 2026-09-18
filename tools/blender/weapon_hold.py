"""Keep the hands on the weapon through an animation cross-fade.

THE PROBLEM.  A cross-fade between two clips is a per-bone, per-channel weighted accumulation of
LOCAL pos/quat/scale -- that is what Mecanim does, what `viewer/src/character/anim.rs` does, and
what `renders/anim/seq_action.py::blend_pose` does to match them.  A local blend does NOT preserve
a world-space relationship between two different kinematic chains, and the weapon hold is exactly
such a relationship::

    Weapon_root        <- Base HumanRibcage                          4 joints from the root
    Base HumanRPalm    <- RForearm3 <- .. <- RCollarbone <- Ribcage  6 joints further out

Every joint in the fade contributes its own interpolated rotation, so through a fade window the
weapon and the hands travel different arcs and separate.  Measured on this pack's own shots, with
`out/characters/scav` (79 bones, 662 clips):

    shot               rifle off the right hand      left hand off the rifle
    a02_sprint-stop        90 mm peak                    139 mm peak
    a03_prone-crawl       110 mm peak                    204 mm peak
    a01_firing-beat        41 mm peak                    144 mm peak

The source data is not at fault: within a single clip `Weapon_root` is rigid to the right palm to
under 1 mm in 573 of the 662 clips, and rigid to BOTH palms in 445 of them.  The 89 that are not
rigid are the grenade (`rgd5_*`), holster (`idle_weapon_in`/`_out`), melee (`axe_look`,
`knife_look`) and vault (`Start_Top`/`End_Top`) clips, where the weapon genuinely leaves the hand.

THE FIX, and why it is this one.  Two corrections are possible and only one of them is right:

  * move the WEAPON onto the right hand.  Pins the rifle to the right palm exactly, but it drags
    the rifle away from wherever the left arm's blend put it -- measured, the left hand went from
    139 mm off the foregrip to 304 mm off it.  It converts one artefact into a worse one.

  * leave the weapon on its blended local and solve BOTH ARMS onto it.  The weapon rides a short
    chain so its blended pose is already close to the authored one; the palms are what the blend
    throws off, so the palms are what to correct.  This is also what the game itself does: the
    Player prefab runs `FullBodyBipedIK` / `SimpleTIK` AFTER the Mecanim blend, and the rig ships
    `IK_S_LPalm` / `IK_S_RPalm` effectors and `Bend_Goal_Left` / `Bend_Goal_Right` pole targets for
    precisely this pass.  (In the clip data those effectors are coincident with the palms to 0.0 mm,
    i.e. the shipped curves already contain the solved result -- so reproducing the solve at the
    blend is the whole job.)

The target for each palm is the grip THAT CLIP AUTHORED -- the palm expressed in the weapon's frame
-- weight-blended across the active clips, then hung off the blended weapon.  With a single active
clip the target is that clip's own palm and the solve is a no-op, so an unfaded shot is bit-for-bit
untouched and nothing outside a fade window can regress.  It also needs no rigidity assumption: the
grip is read per frame, so the grenade and holster clips, where the hand really does leave the
weapon, reproduce exactly as authored.

Result on the same three shots: both hands land on the authored grip to 0.000 mm on every frame,
with 29 to 190 mm of arm reach still in hand.

Pure numpy on purpose -- no `bpy`, no `mathutils` -- so the solve can be verified outside Blender.
Quaternions are XYZW throughout, matching the pack and `seq_action`.
"""

import math

import numpy as np

# The forearm is three bones (`Forearm1..3`) because the roll is distributed over twist joints.
# IK treats the chain as two segments, upper arm and forearm, and leaves the twist bones' locals
# alone so their share of the roll rides along.
ARM_CHAINS = (
    dict(side="L", palm="Base HumanLPalm", fore="Base HumanLForearm1",
         upper="Base HumanLUpperarm", pole="Bend_Goal_Left"),
    dict(side="R", palm="Base HumanRPalm", fore="Base HumanRForearm1",
         upper="Base HumanRUpperarm", pole="Bend_Goal_Right"),
)

WEAPON_BONE = "Weapon_root"
FALLBACK_REFERENCE = "Base HumanRibcage"

# Never drive the elbow fully straight: at full extension the IK plane is undefined and the joint
# pops between frames. 0.999 of reach keeps a solvable bend and is far below what is visible.
MAX_REACH = 0.999


def _mat3_from_quat(q):
    x, y, z, w = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _quat_from_mat3(m):
    """XYZW.  Shepperd's method: branch on the largest diagonal term so the divisor is never small."""
    t = m[0, 0] + m[1, 1] + m[2, 2]
    if t > 0.0:
        s = math.sqrt(t + 1.0) * 2.0
        w, x, y, z = (0.25 * s, (m[2, 1] - m[1, 2]) / s,
                      (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s)
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        w, x, y, z = ((m[2, 1] - m[1, 2]) / s, 0.25 * s,
                      (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s)
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        w, x, y, z = ((m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s,
                      0.25 * s, (m[1, 2] + m[2, 1]) / s)
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        w, x, y, z = ((m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s,
                      (m[1, 2] + m[2, 1]) / s, 0.25 * s)
    q = np.array([x, y, z, w])
    return q / max(np.linalg.norm(q), 1e-12)


def _compose(p, q, s):
    m = np.eye(4)
    m[:3, :3] = _mat3_from_quat(q) * np.asarray(s, float)[None, :]
    m[:3, 3] = p
    return m


def _axis_angle(axis, ang):
    n = np.linalg.norm(axis)
    if n < 1e-12 or abs(ang) < 1e-12:
        return np.eye(3)
    k = axis / n
    K = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return np.eye(3) + math.sin(ang) * K + (1.0 - math.cos(ang)) * (K @ K)


def _swing(v0, v1):
    """Shortest rotation taking v0 onto v1."""
    v0 = v0 / max(np.linalg.norm(v0), 1e-12)
    v1 = v1 / max(np.linalg.norm(v1), 1e-12)
    ax = np.cross(v0, v1)
    return _axis_angle(ax, math.atan2(float(np.linalg.norm(ax)), float(v0 @ v1)))


class WeaponHold(object):
    """Post-blend hand solve for one skeleton.  Build once, call `solve` per frame.

    `bone_names` / `parents` are the pack's own tables; parents precede children, which is what
    makes a single forward pass a valid FK.
    """

    def __init__(self, bone_names, parents, reference=WEAPON_BONE, verbose=True):
        self.names = list(bone_names)
        self.parents = list(parents)
        self.n = len(self.names)
        idx = {n: i for i, n in enumerate(self.names)}
        self.idx = idx

        if any(parents[b] >= b for b in range(self.n) if parents[b] >= 0):
            raise ValueError("weapon_hold needs parents ordered before children for a one-pass FK")

        ref = reference if reference in idx else FALLBACK_REFERENCE
        self.ref = idx.get(ref)
        self.ref_name = ref
        self.arms = []
        for a in ARM_CHAINS:
            if all(a[k] in idx for k in ("palm", "fore", "upper", "pole")):
                self.arms.append(dict(side=a["side"], palm=idx[a["palm"]], fore=idx[a["fore"]],
                                      upper=idx[a["upper"]], pole=idx[a["pole"]]))
        self.enabled = self.ref is not None and len(self.arms) > 0
        # twist bones between the forearm root and the palm, palm-first
        self.twist = {}
        for a in self.arms:
            chain, cur = [], a["palm"]
            while cur != a["fore"] and self.parents[cur] >= 0:
                chain.append(cur)
                cur = self.parents[cur]
            self.twist[a["side"]] = chain
        self.palm_r = idx.get("Base HumanRPalm")
        self.weapon = idx.get(WEAPON_BONE)
        self.last_grip = None
        self.stats = {"frames": 0, "solved": 0, "clamped": 0, "max_residual_mm": 0.0,
                      "weapon_synthesised": 0, "no_grip_yet": 0}
        if verbose:
            print("[hold] reference %s, arms %s%s"
                  % (self.ref_name, [a["side"] for a in self.arms],
                     "" if self.enabled else "  -- DISABLED, rig lacks the bones"))

    # -------------------------------------------------------------------------------- internals
    def _fk(self, p, q, s):
        W = np.empty((self.n, 4, 4))
        for b in range(self.n):
            L = _compose(p[b], q[b], s[b])
            par = self.parents[b]
            W[b] = L if par < 0 else W[par] @ L
        return W

    def _set_local(self, p, q, s, b, L):
        sc = np.linalg.norm(L[:3, :3], axis=0)
        sc = np.where(sc < 1e-12, 1.0, sc)
        p[b] = L[:3, 3]
        q[b] = _quat_from_mat3(L[:3, :3] / sc[None, :])
        s[b] = sc

    def _blend_trs(self, mats_and_weights):
        """Weighted TRS blend of 4x4s: translation accumulates, rotation nlerps hemisphere-aligned.

        The same accumulation the pose blend uses, so a grip and the body it rides are interpolated
        on one convention.
        """
        acc_t = np.zeros(3)
        acc_q = np.zeros(4)
        ref = None
        for M, w in mats_and_weights:
            gq = _quat_from_mat3(M[:3, :3] / np.linalg.norm(M[:3, :3], axis=0)[None, :])
            if ref is None:
                ref = gq.copy()
            elif float(gq @ ref) < 0.0:
                gq = -gq
            acc_t += M[:3, 3] * w
            acc_q += gq * w
        n = float(np.linalg.norm(acc_q))
        if n < 1e-12:
            return None
        out = np.eye(4)
        out[:3, :3] = _mat3_from_quat(acc_q / n)
        out[:3, 3] = acc_t / max(sum(w for _, w in mats_and_weights), 1e-12)
        return out

    def _carry_grip(self, poses, worlds):
        """The weapon's place in the RIGHT PALM's frame, carried across clips that do not key it.

        33 of this pack's 662 clips have NO `Weapon_root` track at all -- `Fall`, `Jump_*_Sprint`,
        the `Prone_Turn_*` fan, `Start/Move/End_Top` (vault), `Stand_Kick`, `T_Pose`,
        `stand_Idle_no_weapons_0`, and `Transition_Sprint_to_Stand`.  That is not corruption: the
        game's controller runs ten layers and weapon handling is its own, so a base-locomotion clip
        legitimately keys the body and lets another layer place the weapon.  Offline there is no
        such layer, and the importer fills an unkeyed bone from the REST local, which leaves the
        rifle pinned near the ribcage while the arms move -- measured on `Transition_Sprint_to_Stand`
        (used by the a02 shot), 148 mm from where `Weapon_root_3rd_anim` puts it and wandering 28 mm
        RMS against the right palm across the segment.  For the length of that clip the weapon is
        simply not in the hand, fade or no fade.

        `Weapon_root_3rd_anim` is NOT the substitute it looks like: across the 629 clips that key
        both, the two bones diverge by up to 420 mm, so it is a different bone with its own job and
        not a mirror of the socket.

        What IS available is the grip the neighbouring clips authored.  Every clip votes with its
        own weight: one that keys the weapon votes for the grip it authored, one that does not votes
        for the grip being carried.  So the value eases from the outgoing clip's grip into the
        incoming clip's over the fade and holds flat in between, instead of snapping when the last
        keyed clip drops out of the blend.
        """
        if self.palm_r is None or self.weapon is None:
            return None
        votes = []
        for (Wi, w, authored) in worlds:
            if authored:
                votes.append((np.linalg.inv(Wi[self.palm_r]) @ Wi[self.weapon], w))
            elif self.last_grip is not None:
                votes.append((self.last_grip, w))
        if not votes:
            self.stats["no_grip_yet"] += 1
            return None
        g = self._blend_trs(votes)
        if g is not None:
            self.last_grip = g
        return g

    def _blended_grip(self, wworlds, arm):
        """The grip each active clip authored -- its palm in ITS OWN weapon frame -- weight-blended.

        `wworlds` is [(world_matrices, weight, weapon_world), ..].  Reading the palm against each
        clip's own socket is what makes the solve exact at w=1 and what lets it stay correct on the
        89 clips where the hand genuinely leaves the weapon: the grip is measured per frame, never
        assumed rigid.
        """
        return self._blend_trs([(np.linalg.inv(wp) @ Wi[arm["palm"]], w)
                                for (Wi, w, wp) in wworlds])

    # ------------------------------------------------------------------------------------ solve
    def solve(self, poses, p, q, s):
        """Return (p, q, s) with the weapon in the hand and both arms on the blended authored grip.

        `poses` is the active clips as [(pos, quat, scale, weight[, keys_weapon]), ..] -- each the
        clip's OWN pose at its own local time, i.e. exactly what the caller fed into the blend.
        `p`/`q`/`s` are the blended local pose.  `keys_weapon` defaults to True; pass it so a clip
        with no `Weapon_root` track can be repaired (see `_carry_grip`).

        Two independent corrections, in order:
          1. the weapon, whenever any active clip fails to key it -- needed at w=1 as well, because
             an unkeyed clip is wrong for its whole length and not just across a fade;
          2. the arms, only across a fade -- with one active clip the target IS that clip's own palm
             and the solve a no-op, so it is skipped rather than relied on to cancel.
        """
        if not self.enabled:
            return p, q, s
        p, q, s = p.copy(), q.copy(), s.copy()
        W = self._fk(p, q, s)

        # Each clip's own world pose, once: the grip carrier and the arm targets both need it.
        worlds = []
        for entry in poses:
            pi, qi, si, w = entry[:4]
            authored = entry[4] if len(entry) > 4 else True
            worlds.append((self._fk(pi, qi, si), w, authored))

        # ---- 1. the weapon ----
        grip = self._carry_grip(poses, worlds)
        if grip is not None and not all(a for _, _, a in worlds):
            target = W[self.palm_r] @ grip
            self._set_local(p, q, s, self.weapon,
                            np.linalg.inv(W[self.parents[self.weapon]]) @ target)
            W[self.weapon] = target          # keep the FK consistent for the arm targets below
            self.stats["weapon_synthesised"] += 1

        # ---- 2. the arms ----
        if len(poses) < 2:
            return p, q, s
        self.stats["frames"] += 1
        # A clip that never keyed the weapon has no grip of its own, so read its palms against the
        # SAME synthesised socket the body is now using; its authored palms are then reproduced
        # exactly, which is what w=1 on that clip has to give.
        wworlds = []
        for (Wi, w, authored) in worlds:
            wp = Wi[self.ref] if authored else (Wi[self.palm_r] @ grip if grip is not None
                                                else Wi[self.ref])
            wworlds.append((Wi, w, wp))
        targets = {a["side"]: W[self.ref] @ self._blended_grip(wworlds, a) for a in self.arms}

        for arm in self.arms:
            T = targets[arm["side"]]
            up, fo, pa = arm["upper"], arm["fore"], arm["palm"]
            a = W[up][:3, 3].copy()
            b = W[fo][:3, 3].copy()
            e = W[pa][:3, 3].copy()
            l1 = float(np.linalg.norm(b - a))
            l2 = float(np.linalg.norm(e - b))
            goal = T[:3, 3]
            d = goal - a
            dist = float(np.linalg.norm(d))
            if dist < 1e-9 or l1 < 1e-9 or l2 < 1e-9:
                continue
            if dist > (l1 + l2) * MAX_REACH:
                self.stats["clamped"] += 1
            reach = min(dist, (l1 + l2) * MAX_REACH)
            dhat = d / dist

            # Elbow: the law of cosines fixes how far along a->goal it sits, and the pole target
            # fixes which way it swings out of that axis.
            cos1 = float(np.clip((l1 * l1 + reach * reach - l2 * l2) / (2 * l1 * reach), -1.0, 1.0))
            proj = l1 * cos1
            h = math.sqrt(max(0.0, l1 * l1 - proj * proj))
            pole = W[arm["pole"]][:3, 3] - a
            perp = pole - dhat * float(pole @ dhat)
            if float(np.linalg.norm(perp)) < 1e-7:
                # Pole collinear with the arm: keep the bend plane the blend already had.
                perp = (b - a) - dhat * float((b - a) @ dhat)
            if float(np.linalg.norm(perp)) < 1e-7:
                continue
            perp /= np.linalg.norm(perp)
            b_new = a + dhat * proj + perp * h
            e_new = a + dhat * reach

            # Both corrections are pure world rotations about a joint, so the rest of the chain
            # rides along and only the two segment locals need rewriting.
            R1 = _swing(b - a, b_new - a)
            Wup = W[up].copy()
            Wup[:3, :3] = R1 @ Wup[:3, :3]
            R2 = _swing((a + R1 @ (e - a)) - b_new, e_new - b_new)
            Wfo = W[fo].copy()
            Wfo[:3, :3] = R2 @ R1 @ Wfo[:3, :3]
            Wfo[:3, 3] = b_new

            self._set_local(p, q, s, up, np.linalg.inv(W[self.parents[up]]) @ Wup)
            self._set_local(p, q, s, fo, np.linalg.inv(Wup) @ Wfo)

            # Wrist: re-FK the twist stub off the corrected forearm, then set the palm's local to
            # whatever lands it on the authored grip ORIENTATION as well as its position.
            Wcur = Wfo
            for bb in reversed(self.twist[arm["side"]][1:]):
                Wcur = Wcur @ _compose(p[bb], q[bb], s[bb])
            self._set_local(p, q, s, pa, np.linalg.inv(Wcur) @ T)

            self.stats["solved"] += 1
            r = float(np.linalg.norm(e_new - goal)) * 1000.0
            self.stats["max_residual_mm"] = max(self.stats["max_residual_mm"], r)
        return p, q, s

    def report(self):
        st = self.stats
        extra = ""
        if st["weapon_synthesised"]:
            extra += ", weapon placed from the hand on %d frames (clip keys no Weapon_root)" \
                     % st["weapon_synthesised"]
        if st["no_grip_yet"]:
            extra += ", %d frames had NO grip to carry yet (weapon left as blended)" \
                     % st["no_grip_yet"]
        return ("[hold] %d faded frames, %d arm solves, %d reach-clamped, worst palm-to-target "
                "%.3f mm%s" % (st["frames"], st["solved"], st["clamped"],
                               st["max_residual_mm"], extra))
