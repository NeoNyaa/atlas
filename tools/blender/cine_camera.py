"""A follow camera solved as one continuous move, not frame by frame.

Standalone. Run inside Blender, then call ``solve_follow_camera(scene, arm, f0, f1)``.

WHY A SOLVER. A follow camera has to satisfy two goals that fight each other: keep the subject
unobstructed, and move smoothly. Deciding each frame independently and smoothing afterwards
satisfies neither - the smoothing drags the camera back through the wall the per-frame choice just
escaped. Because a render is offline we can do what a real camera department does and plan the
WHOLE move before shooting it: score every candidate position at every moment, then choose the
single sequence of positions with the lowest total cost, where cost includes how far the camera
moved between frames. That is a shortest-path problem over time, and Viterbi solves it exactly.

The three things the old fixed-offset rig got wrong, and what replaces them:

  * ONE RAY. Testing the line from the lens to the subject's chest says nothing about a lamp post
    covering his torso, or a chair covering his legs. Visibility here is the fraction of a set of
    BODY POINTS (head, chest, shoulders, hips, knees) with a clear line, so a partial block costs
    partially and the solver routes around it.
  * ONE SIDE. A fixed left offset has nowhere to go when the route hugs a building. Candidates
    cover the full circle, so the camera takes whichever side is open; the transition cost is what
    stops it flip-flopping, and it will only cross behind the subject when the far side stays
    better for long enough to be worth the move.
  * LOCAL SMOOTHING. A rate limit fights the placement instead of informing it. Here movement is
    part of the cost being minimised, so the solved path is already smooth and the filter afterwards
    only polishes it.

Raycasts run against ONE frame's geometry. Everything except the subject is static, and the subject
is on the ignore list, so re-evaluating the depsgraph per frame would cost time and change nothing.

GRASS IS A SOFT OCCLUDER, BUT ONLY AT A DISTANCE, and getting that qualifier wrong is what made
this solver walk into a meadow. Blades are thin and read as natural foreground, so on the body rays
they add cost rather than disqualifying a position - otherwise the solver flees any spot with a
tuft in front of it. The bug was that the same rule fired whether the blade was 6 m from the lens
(foreground, wanted) or 6 cm from it (the frame IS the blade): grass cost
`W_GRASS * min(grass, 4) / 4` = at most 3.0 against a `W_BLOCKED` of 60 wherever along the ray it
sat, and NO term anywhere was a function of distance from the LENS. So a state with the camera
buried in the field scored the same 3.0 as one with a tuft in the mid-ground, and the Viterbi went
there because the smoothness term liked it.

MEASURED on the ZoneBearCamp route this was reported against (240 frames, R=190, photoreal):

  * the shipped solve is usable for 79 of 240 frames, longest unbroken run 40. From frame 51 the
    lens is inside solid geometry and from 140 it is inside the grass field: 100% of the frame
    closer than 0.75 m, five of six lens axes blocked, subject 0.00%.
  * the solver's own verdict on that solve is "chest occluded on 14/240 frames (5.8%)", because
    one ray to the chest can be clear while the lens is underground.
  * and it did NOT run out of places to stand. Sweeping the SHIPPED candidate grid at the worst
    frames (60, 90, 140, 150, 200, 237) finds 52 to 114 of the 216 states fully clean - lens box
    clear, near field clear, all six body points visible. So this is a cost-function bug and not a
    candidate-grid bug, and widening the grid (which was measured too: 190-309 clean of 540) is
    not the fix.

THE NEAR-FIELD TERMS, three parts, all of them cheap.

1. A NEAR-FIELD OCCUPANCY TEST at the lens, which is the term the paragraph above asks for. Two
   measurements, 21 short rays, evaluated BEFORE the body rays so a fatal state costs less to
   reject than it used to cost to score:
      lens box   six axes at LENS_BOX_R. A hit on anything that is not grass means the lens is
                 inside or against built geometry. Fatal.
      near field a 5x3 sample of the view frustum at NEAR_Z. GRASS COUNTS HERE, and that is the
                 whole point: this is the one place where grass is hard, because a blade at 1 m
                 in a 50 mm frame is not foreground, it is the frame.
   Both are fatal rather than costly, but graded (W_FATAL * (1 + n)), so if every state at some
   step is buried the Viterbi still has an ordering and returns the least-bad instead of an
   arbitrary one.

   The thresholds carry a MARGIN over the gate they are checked against (0.70 m box against the
   probe's 0.60, 1.00 m near field against 0.75). Two things need it: the solver raycasts one
   frozen depsgraph, so it does not see the grass wind stage move blades by ~0.15 m; and the solved
   keys are resampled at `step`, so a frame between two keys is interpolated rather than tested.

2. DISTANCE-GRADED GRASS on the body rays. `clear()` now also returns how far the NEAREST grass hit
   is from the lens, and grass inside GRASS_SOFT_M costs on a ramp. This is free - the hits were
   already being walked - and it gives the solver a gradient that pushes it out of the field before
   the fatal term has to fire.

3. THE REPAIR RUNS LAST, AND IT VALIDATES. The old order was smooth -> repair -> smooth, so the
   last thing to touch the path was free to push it back into the wall the repair had just pulled
   it out of; and the repair tested `los.clear(eye, key)`, the same grass-soft rule, so a key deep
   in the field passed it. Now the smoothing and the feasibility test alternate: smooth one pass,
   keep only the keys that still pass, repair what is left by local search, repeat. Repair is per
   FRAME, not per solve key, because the linear resample between two clean keys can cut a corner
   through a trunk - and it is applied to a whole bad RUN through one raised-cosine window, since a
   per-frame repair measured 98.6 m/s peak camera speed, which in a clip is a cut.

AFTER, on the same two routes and with nothing else changed: ZoneBearCamp 79/240 usable frames
becomes 240/240 (longest unbroken run 40 -> 240, lens-in-solid 126 -> 0, subject mean 2.14% ->
7.04%), ZoneTrucks 205/240 -> 240/240. It is also not slower: the fatal test runs FIRST and skips
the body rays of the states it disqualifies, so the whole solve came in at 328 s against the old
build+solve's 425 s on the same scene.

The cost of the move cap is range: the boom holds a narrower offset (bearcamp 2.40..5.37 m becomes
3.42..4.59 m), so on an open route the camera reads as a locked-off follow rather than one that
breathes. Raise MOVE_CAP toward 0.30 m/frame and re-probe if more movement is wanted; the
near-field term does not depend on it, and the blended repair exists to absorb the frames a looser
cap breaks.

Everything else - the Viterbi, the body-point set, the azimuth grid, the preferred framing, the
aim lead - is unchanged.
"""

import math

import bpy
import mathutils

__all__ = ["solve_follow_camera", "cinematic_render_settings"]

# candidate grid: azimuth measured from the subject's forward, positive toward his left
AZIMUTHS = [math.radians(a) for a in range(-180, 180, 20)]
DISTANCES = [3.0, 4.0, 5.0, 6.0]
RISES = [1.35, 1.80, 2.25]

PREF_AZ = math.radians(118.0)   # behind and to the left: a 3/4 rear that still shows his face-side
IDEAL_DIST = 5.0
IDEAL_RISE = 1.80

W_BLOCKED = 60.0                # per unit of lost visibility
W_GRASS = 3.0                   # grass in the MID-ground, still soft, still capped
W_GRASS_NEAR = 45.0             # grass approaching the lens; see GRASS_SOFT_M
W_DIST = 0.8
W_RISE = 0.6
W_AZ = 1.6
W_MOVE = 1.2                    # per square metre of camera travel between solve steps
W_GROUND = 40.0
# A CAMERA CANNOT TELEPORT, and W_MOVE alone does not say so. Its quadratic is 0.4 * |d|^2 at
# step 3, so a 12 m jump across the subject costs 57 - about what one fully blocked state costs,
# and far less than the disqualifying term below. Measured: the shipped solve already peaks at
# 19.4 m/s of camera speed at frames 201-207 and 235-239, where the route enters dense foliage, and
# adding a fatal term without this made the same two places 44.5 m/s. So travel past MOVE_CAP per
# frame gets its own steep ramp: 10 m/s costs about what a fully blocked frame costs, 20 m/s costs
# ten times that, and the whole ramp still stays under W_FATAL so the camera will always choose to
# leave a wall fast rather than stay in it.
MOVE_CAP = 0.18                 # metres per FRAME before the ramp starts (5.4 m/s)
W_MOVE_CAP = 300.0
W_NEAR = 220.0                  # per unit of near-field occupancy, below the fatal threshold
W_FATAL = 1.0e4                 # lens inside geometry, or the near field full: disqualifying

BODY = [(0.30, 0.00), (0.90, 0.00), (1.35, 0.00),
        (1.35, -0.28), (1.35, 0.28), (1.68, 0.00)]   # (height, lateral offset) in metres

MIN_GROUND_CLEAR = 0.45
LEAD = 0.85                     # aim this far ahead of the subject, so he has looking room

# ---- the near-field test's own constants -----------------------------------------------------
LENS_BOX_R = 0.70        # six-axis probe radius; the gate this is checked against uses 0.60
NEAR_Z = 1.00            # frustum sample depth; the gate uses 0.75
NEAR_MAX = 0.0           # ANY hit in the frustum sample is fatal; see below
NEAR_SH = (-1.0, -0.5, 0.0, 0.5, 1.0)     # frustum sample, horizontal, in tan(half-fov) units
NEAR_SV = (-1.0, 0.0, 1.0)                # ... and vertical: 15 rays
GRASS_SOFT_M = 2.20      # grass nearer than this to the LENS stops being foreground
# NEAR_MAX 0 rather than "a bit of grass is allowed", measured: at 0.12 on a 3x3 sample (one hit in
# nine passes) the re-solved move still lost frames 101-107, where the frame probe reports 5.9 to
# 17.4% of the frame closer than 0.75 m with a blade on the lens. A 15-ray sample at zero tolerance
# is affordable because there is somewhere to stand: the same sweep that found 52-114 fully clean
# states of 216 at the worst frames used exactly this test.
SENSOR_W, SENSOR_H = 36.0, 20.25          # 36 mm sensor, 16:9

AXES6 = [mathutils.Vector(t) for t in ((1, 0, 0), (-1, 0, 0), (0, 1, 0),
                                       (0, -1, 0), (0, 0, 1), (0, 0, -1))]


def _fcurves(action):
    """Blender 4.4+ keeps an action's curves in a slot channelbag; `Action.fcurves` is gone in 5.x."""
    try:
        fcs = action.fcurves
        if fcs is not None:
            return list(fcs)
    except (AttributeError, TypeError):
        pass
    out = []
    for layer in getattr(action, "layers", []):
        for strip in getattr(layer, "strips", []):
            for cb in getattr(strip, "channelbags", []):
                out.extend(cb.fcurves)
    return out


def _binomial(seq, passes=2):
    """Smooth a list of Vectors with a [1 2 1] kernel, endpoints held."""
    out = list(seq)
    for _ in range(passes):
        prev = list(out)
        for i in range(1, len(out) - 1):
            out[i] = (prev[i - 1] + prev[i] * 2.0 + prev[i + 1]) * 0.25
    return out


class _Los(object):
    """Line-of-sight and near-field occupancy against the static scene, with an ignore set."""

    def __init__(self, scene, arm, lens=50.0):
        self.scene = scene
        self.dg = bpy.context.evaluated_depsgraph_get()
        skip = set()
        for o in bpy.data.objects:
            n = o.name
            if n.startswith(("grass_kind", "eft_atmosphere", "track", "focus", "shot")):
                continue                       # grass is handled separately, the rest are ignored
            a = o
            while a is not None:
                if a is arm:
                    skip.add(n)
                    break
                a = a.parent
        self.skip = skip
        self.tan_h = math.tan(math.atan(SENSOR_W * 0.5 / lens))
        self.tan_v = math.tan(math.atan(SENSOR_H * 0.5 / lens))
        self.rays = 0

    def _ignorable(self, obj):
        n = obj.name
        return n in self.skip or n.startswith(("eft_atmosphere", "track", "focus", "shot"))

    def clear(self, a, b):
        """(visible, grass_hits, nearest grass hit's distance from `b`) for the segment a->b."""
        d = b - a
        remaining = d.length
        if remaining < 1e-5:
            return True, 0, 99.0
        d = d / remaining
        org, grass, gnear = a.copy(), 0, 99.0
        for _ in range(16):
            self.rays += 1
            hit, loc, _n, _i, obj, _m = self.scene.ray_cast(self.dg, org, d, distance=remaining)
            if not hit:
                return True, grass, gnear
            name = obj.name
            if name.startswith("grass_kind"):
                grass += 1
                # distance from the LENS end, which is the thing the cost has to be a function of
                gnear = min(gnear, (b - loc).length)
            elif not self._ignorable(obj):
                return False, grass, gnear
            remaining -= (loc - org).length + 1e-3
            if remaining <= 1e-3:
                return True, grass, gnear
            org = loc + d * 1e-3
        return True, grass, gnear

    def lens_box(self, p, r=LENS_BOX_R):
        """How many of six axes have NON-GRASS geometry within `r`. Non-zero means buried."""
        n = 0
        for d in AXES6:
            self.rays += 1
            hit, _l, _n, _i, obj, _m = self.scene.ray_cast(self.dg, p, d, distance=r)
            if hit and not obj.name.startswith("grass_kind") and not self._ignorable(obj):
                n += 1
        return n

    def near_field(self, p, look, z=NEAR_Z):
        """Fraction of a 5x3 frustum sample with ANY surface within `z`. GRASS COUNTS.

        This is the one place in the file where grass is not soft. The measurement is the same one
        the frame probe reports as `near_pct`, at 15 rays instead of 3,600. It returns early: with
        NEAR_MAX at 0 the first hit already decides the state.
        """
        up = mathutils.Vector((0, 0, 1))
        right = look.cross(up)
        right = right.normalized() if right.length > 1e-6 else mathutils.Vector((1, 0, 0))
        up = right.cross(look).normalized()
        n = len(NEAR_SH) * len(NEAR_SV)
        hits = 0
        for sv in NEAR_SV:
            for sh in NEAR_SH:
                d = (look + right * (self.tan_h * sh) + up * (self.tan_v * sv)).normalized()
                self.rays += 1
                hit, _l, _nn, _i, obj, _m = self.scene.ray_cast(self.dg, p, d, distance=z)
                if hit and not self._ignorable(obj):
                    hits += 1
                    if hits / float(n) > NEAR_MAX:
                        return hits / float(n)
        return hits / float(n)

    def buried(self, p, look):
        """(fatal cost, lens_box, near_frac). Zero cost means the lens is in open air.

        The box goes first and short-circuits: it is 6 rays against the frustum's 15, and a lens
        inside a wall does not need its frustum sampled to be rejected.
        """
        b = self.lens_box(p)
        if b:
            return W_FATAL * (1.0 + b), b, 1.0
        nf = self.near_field(p, look)
        if nf > NEAR_MAX:
            return W_FATAL * (0.5 + nf), 0, nf
        return 0.0, 0, nf

    def ground_clear(self, p):
        """Height of `p` above whatever is under it (large if it is over a void).

        Skips the ignore set explicitly. The atmosphere box's FLOOR sits about 10 m under the route
        (ATM_LIFT 30 - ATM_HEIGHT/2 40), well inside this 12 m ray, and the shipped version does
        not classify what it hits - so on a photoreal build it can be measuring the box.
        """
        d = mathutils.Vector((0, 0, -1))
        org = p.copy()
        for _ in range(6):
            self.rays += 1
            hit, loc, _n, _i, obj, _m = self.scene.ray_cast(self.dg, org, d,
                                                            distance=12.0 - (p.z - org.z))
            if not hit:
                return 99.0
            if not self._ignorable(obj):
                return p.z - loc.z
            org = loc + d * 1e-3
        return 99.0


def solve_follow_camera(scene, arm, f0, f1, step=3, lens=50.0, fstop=2.8, blades=0,
                        verbose=True):
    """Create and key a follow camera for [f0, f1]. Returns the camera object.

    `blades` is the iris blade count. 0 is Blender's default and means a mathematically perfect
    circular aperture, which no real lens has: an out-of-focus highlight from a stopped-down 50 mm
    is a polygon with the blade count's number of sides. Free (measured 10.56 s at 0 against
    10.72/10.45 s at 9 on the real scene, i.e. inside run-to-run noise), and a departure from the
    viewer only in the sense that DOF already is - the viewer has no defocus at all.
    """
    import numpy as np

    # ---- 1. record the subject's motion, then stop touching the timeline --------------------
    frames = list(range(f0, f1 + 1))
    pos, fwd = [], []
    for fr in frames:
        scene.frame_set(fr)
        bpy.context.view_layer.update()
        pos.append(arm.matrix_world.translation.copy())
        f = (arm.matrix_world.to_3x3() @ mathutils.Vector((0, 0, 1)))
        f.z = 0.0
        fwd.append(f.normalized() if f.length > 1e-5 else mathutils.Vector((1, 0, 0)))
    # the walk cycle yaws the root slightly every step; the camera should not answer that
    fwd = [v.normalized() for v in _binomial(fwd, passes=6)]
    pos_s = _binomial(pos, passes=2)

    los = _Los(scene, arm, lens=lens)
    states = [(a, d, r) for a in AZIMUTHS for d in DISTANCES for r in RISES]
    ns = len(states)

    def offset(st, i):
        a, d, r = st
        left = mathutils.Vector((-fwd[i].y, fwd[i].x, 0.0))
        return fwd[i] * (math.cos(a) * d) + left * (math.sin(a) * d) + mathutils.Vector((0, 0, r))

    def aim_at(i):
        return pos_s[i] + mathutils.Vector((0, 0, 1.30)) + fwd[i] * LEAD

    def look_from(p, i):
        v = aim_at(i) - p
        return v.normalized() if v.length > 1e-5 else fwd[i].copy()

    # static per-state framing cost
    base = np.empty(ns, np.float32)
    for j, (a, d, r) in enumerate(states):
        da = abs((a - PREF_AZ + math.pi) % (2 * math.pi) - math.pi)
        base[j] = (W_AZ * (da / math.radians(30.0))
                   + W_DIST * abs(d - IDEAL_DIST) + W_RISE * abs(r - IDEAL_RISE))

    # ---- 2. emission cost at every solve step -----------------------------------------------
    solve_idx = list(range(0, len(frames), step))
    if solve_idx[-1] != len(frames) - 1:
        solve_idx.append(len(frames) - 1)
    emis = np.empty((len(solve_idx), ns), np.float32)
    n_fatal = 0
    for t, i in enumerate(solve_idx):
        p, f = pos_s[i], fwd[i]
        left = mathutils.Vector((-f.y, f.x, 0.0))
        pts = [p + mathutils.Vector((0, 0, h)) + left * lat for h, lat in BODY]
        for j, st in enumerate(states):
            cam = p + offset(st, i)
            # THE NEAR-FIELD TEST, first, because it is cheap and it disqualifies.
            fatal, _b, nf = los.buried(cam, look_from(cam, i))
            if fatal:
                emis[t, j] = base[j] + fatal
                n_fatal += 1
                continue
            vis, grass, gnear = 0, 0, 99.0
            for k, bp in enumerate(pts):
                ok, g, gn = los.clear(bp, cam)
                vis += 1 if ok else 0
                grass += g
                gnear = min(gnear, gn)
                if k == 1 and vis == 0:
                    break                      # chest blocked: cheap reject, do not ray the rest
            frac = vis / float(len(pts))
            c = (base[j] + W_BLOCKED * (1.0 - frac) + W_GRASS * min(grass, 4) / 4.0
                 + W_NEAR * nf
                 + W_GRASS_NEAR * max(0.0, 1.0 - gnear / GRASS_SOFT_M))
            gc = los.ground_clear(cam)
            if gc < MIN_GROUND_CLEAR:
                c += W_GROUND * (MIN_GROUND_CLEAR - gc + 0.1)
            emis[t, j] = c

    # ---- 3. transition cost, and the Viterbi pass -------------------------------------------
    # Movement is measured in the subject's frame, which is what keeps the camera steady relative
    # to him rather than steady in the world (a world-steady camera falls behind a walking subject).
    ox = np.array([[math.cos(a) * d, math.sin(a) * d, r] for a, d, r in states], np.float32)
    diff = ox[:, None, :] - ox[None, :, :]
    sq = np.einsum("ijk,ijk->ij", diff, diff)
    over = np.maximum(0.0, np.sqrt(sq) - MOVE_CAP * max(step, 1))
    trans = W_MOVE * sq / max(step, 1) + W_MOVE_CAP * over * over

    dp = emis[0].copy()
    back = np.zeros((len(solve_idx), ns), np.int32)
    for t in range(1, len(solve_idx)):
        tot = dp[:, None] + trans
        back[t] = np.argmin(tot, axis=0)
        dp = tot[back[t], np.arange(ns)] + emis[t]
    chain = [int(np.argmin(dp))]
    for t in range(len(solve_idx) - 1, 0, -1):
        chain.append(int(back[t, chain[-1]]))
    chain.reverse()
    chosen_fatal = sum(1 for t in range(len(solve_idx))
                       if emis[t, chain[t]] >= W_FATAL * 0.5)

    # ---- 4. to world space, smooth WITHOUT breaking feasibility, resample, blend-repair ------
    key_pos = [pos_s[i] + offset(states[chain[t]], i) for t, i in enumerate(solve_idx)]

    def ok_at(p, i):
        """The feasibility predicate: not buried, and the chest or head reachable."""
        if los.buried(p, look_from(p, i))[0]:
            return False
        pl, fl = pos_s[i], fwd[i]
        lf = mathutils.Vector((-fl.y, fl.x, 0.0))
        seen = 0
        for h, lat in ((0.90, 0.0), (1.35, 0.0), (1.68, 0.0)):
            if los.clear(pl + mathutils.Vector((0, 0, h)) + lf * lat, p)[0]:
                seen += 1
        return seen >= 2

    def repair(p, i):
        """Nearest position to `p` that passes. Lift first, then pull in, then push out.

        Ordered by how far it moves the camera, so the smallest correction that works is the one
        taken and the move keeps the shape the solve gave it.
        """
        anchor = pos_s[i] + mathutils.Vector((0, 0, 1.30))
        v = p - anchor
        L = v.length
        if L < 1e-4:
            return None
        flat = mathutils.Vector((v.x, v.y, 0.0))
        fl = flat.length
        u = (flat / fl) if fl > 1e-4 else fwd[i].copy()
        cands = []
        for dz in (0.0, 0.30, 0.60, 0.95, 1.40):
            for sc in (1.0, 0.88, 0.76, 1.15, 1.32, 1.55):
                for yaw in (0.0, 0.25, -0.25, 0.55, -0.55, 0.90, -0.90):
                    q = mathutils.Matrix.Rotation(yaw, 3, 'Z') @ (u * (fl * sc))
                    c = anchor + mathutils.Vector((q.x, q.y, v.z + dz))
                    if 1.8 <= (c - anchor).length <= 14.0:
                        cands.append(((c - p).length, c))
        cands.sort(key=lambda t: t[0])
        for _d, c in cands:
            if ok_at(c, i):
                return c
        return None

    # SMOOTHING WITH REJECTION, at the keys. The Viterbi chain is feasible by construction (the
    # near-field term is in the emission cost), and the shipped file's unconditional
    # `_binomial(passes=3)` is free to cut the corner between two clean keys straight into a trunk.
    # Keep each smoothed key only if it survives its own feasibility test.
    for _ in range(3):
        sm = _binomial(key_pos, passes=1)
        for t, i in enumerate(solve_idx):
            if ok_at(sm[t], i):
                key_pos[t] = sm[t]

    cam_at = []
    for n in range(len(frames)):
        t = min(n / float(step), len(key_pos) - 1.0)
        lo = int(math.floor(t)); hi = min(lo + 1, len(key_pos) - 1)
        cam_at.append(key_pos[lo].lerp(key_pos[hi], t - lo))

    # BLENDED REPAIR. A per-frame repair is discontinuous by construction, and it measures as one:
    # repairing each bad frame independently took the solved move to 98.6 m/s peak camera speed and
    # 2,953 m/s^2 peak acceleration - a teleport, which in a clip is a cut. So a bad RUN of frames
    # is corrected by ONE offset, applied through a raised-cosine window whose half-width grows with
    # the size of the correction, and the whole thing is iterated until nothing is left. The camera
    # leaves the wall over a third of a second instead of between two frames.
    bad0 = [n for n in range(len(frames)) if not ok_at(cam_at[n], n)]
    nbump = 0
    for _it in range(8):
        bad = [n for n in range(len(frames)) if not ok_at(cam_at[n], n)]
        if not bad:
            break
        runs, s, prev = [], bad[0], bad[0]
        for n in bad[1:]:
            if n == prev + 1:
                prev = n
                continue
            runs.append((s, prev)); s = prev = n
        runs.append((s, prev))
        for (n0, n1) in runs:
            m = (n0 + n1) // 2
            c = repair(cam_at[m], m)
            if c is None:
                continue
            delta = c - cam_at[m]
            w = max(8, min(36, int(delta.length * 14.0)))
            lo, hi = max(0, n0 - w), min(len(frames) - 1, n1 + w)
            for n in range(lo, hi + 1):
                if n < n0:
                    a = 0.5 * (1.0 - math.cos(math.pi * (n - lo + 1) / float(n0 - lo + 1)))
                elif n > n1:
                    a = 0.5 * (1.0 - math.cos(math.pi * (hi - n + 1) / float(hi - n1 + 1)))
                else:
                    a = 1.0
                cam_at[n] = cam_at[n] + delta * a
            nbump += 1
    # a last smoothing pass, again only where it stays legal
    for _ in range(6):
        sm = _binomial(cam_at, passes=1)
        for n in range(len(frames)):
            if ok_at(sm[n], n):
                cam_at[n] = sm[n]
    failed = len([n for n in range(len(frames)) if not ok_at(cam_at[n], n)])

    # ---- 5. build the camera ----------------------------------------------------------------
    for n in ("shot", "track", "focus"):
        o = bpy.data.objects.get(n)
        if o:
            bpy.data.objects.remove(o, do_unlink=True)
    cd = bpy.data.cameras.new("shot")
    cd.lens = lens; cd.clip_start = 0.05; cd.clip_end = 4000.0
    cd.dof.use_dof = True; cd.dof.aperture_fstop = fstop
    cd.dof.aperture_blades = int(blades)
    cam = bpy.data.objects.new("shot", cd); scene.collection.objects.link(cam)
    cam.rotation_mode = 'QUATERNION'
    focus = bpy.data.objects.new("focus", None); scene.collection.objects.link(focus)
    focus.empty_display_size = 0.2
    cd.dof.focus_object = focus

    # aim ahead of him so he sits on the trailing side of frame with room to walk into
    aim = _binomial([aim_at(i) for i in range(len(frames))], passes=4)
    blocked = 0
    for n, fr in enumerate(frames):
        cam.location = cam_at[n]
        cam.rotation_quaternion = (aim[n] - cam_at[n]).to_track_quat('-Z', 'Y')
        cam.keyframe_insert("location", frame=fr)
        cam.keyframe_insert("rotation_quaternion", frame=fr)
        focus.location = pos_s[n] + mathutils.Vector((0, 0, 1.30))
        focus.keyframe_insert("location", frame=fr)
        if not los.clear(pos_s[n] + mathutils.Vector((0, 0, 1.35)), cam_at[n])[0]:
            blocked += 1
    # Every frame is keyed, so the curve should pass straight through the solved points. Auto-Bezier
    # handles overshoot on a direction change, and an overshoot here pokes the lens into a wall.
    for fc in _fcurves(cam.animation_data.action):
        for kp in fc.keyframe_points:
            kp.interpolation = 'LINEAR'
    scene.camera = cam

    if verbose:
        dists = [(cam_at[n] - pos_s[n]).length for n in range(len(frames))]
        print("[cine] %d frames, %d solve steps, %d state(s) per step, %d ray(s)"
              % (len(frames), len(solve_idx), ns, los.rays))
        print("[cine] %d/%d state-scores were fatal (lens buried); %d/%d CHOSEN steps were"
              % (n_fatal, len(solve_idx) * ns, chosen_fatal, len(solve_idx)))
        print("[cine] boom %.2f..%.2f m (ideal %.1f)" % (min(dists), max(dists), IDEAL_DIST))
        print("[cine] after resample %d/%d frames failed the near-field test; %d blended "
              "correction(s), %d still failing" % (len(bad0), len(frames), nbump, failed))
        fps = float(getattr(scene.render, "fps", 30)) or 30.0
        sp = [(cam_at[n + 1] - cam_at[n]).length * fps for n in range(len(frames) - 1)]
        ac = [abs(sp[n + 1] - sp[n]) * fps for n in range(len(sp) - 1)]
        print("[cine] camera speed mean %.2f max %.2f m/s; |d speed| max %.1f m/s^2"
              % (sum(sp) / len(sp), max(sp), max(ac) if ac else 0.0))
        print("[cine] chest occluded on %d/%d frames (%.1f%%)"
              % (blocked, len(frames), 100.0 * blocked / len(frames)))
    return cam


def cinematic_render_settings(scene, samples=256, res=(2560, 1440), threshold=0.005):
    """Everything that buys realism per unit of render time, in roughly that order.

    MOTION BLUR at a 180-degree shutter (`shutter = 0.5`) is the single biggest one: it is what a
    real camera does, and without it a walk cycle reads as stop-motion no matter how many samples
    the frame gets.

    TRANSPARENT BOUNCES matter far more here than the headline `max_bounces`, and the failure is
    not "a bit dark" - it is missing pixels. The counter is per PATH, not per segment: the camera
    ray, every diffuse bounce after it and the shadow ray toward the sun all spend from one budget.
    Every leaf, blade and chain-link is alpha-tested, so a ray crossing foliage exhausts it on
    transparency alone, and Cycles then fails CLOSED - a camera ray that runs out is terminated
    black, a shadow ray that runs out is reported fully occluded. Measured on example_scene's grass
    field: the default 8 leaves 14.78% of the frame at EXACTLY 0.0 luma with the mean 12.2% low;
    32 (what this function used to set, and what the line below used to claim was enough) still
    leaves 0.48% black; 256 is bit-identical to the 1024 maximum for +0.3 s on a 2.0 s frame. So
    256 it is - a transparent bounce does no shading, and there is nothing to buy by shaving it.

    NOTE: nothing in the repo calls this function (it is exported in __all__ and has no caller),
    so these settings do NOT reach the shipped scene. example_scene.py sets its own render config
    in step 8, and that is where the fix above has to live to have any effect.

    A physically sized SUN (0.526 degrees, the real angular diameter) fixes shadow penumbra width,
    which the eye reads as "outdoors" without being able to say why. The stock 1-2 degrees quietly
    softens every contact shadow.

    ADAPTIVE SAMPLING makes the sample count much cheaper than it looks: lowering the noise
    threshold concentrates samples on the noisy regions instead of re-rendering clean sky.
    """
    scene.render.resolution_x, scene.render.resolution_y = res
    scene.render.resolution_percentage = 100
    scene.render.use_motion_blur = True
    scene.render.motion_blur_shutter = 0.5
    scene.render.filter_size = 1.20              # stock 1.5 is soft at high resolution
    # Only the character moves, so re-exporting the map every frame is wasted work. Measured on
    # this scene: per-frame sync 2.4-2.9 s without, 0.6-1.2 s with. Worth about 5% of a 1440p
    # frame, and much more on lighter shots where path tracing is not the bottleneck.
    scene.render.use_persistent_data = True

    c = scene.cycles
    c.samples = samples
    c.use_adaptive_sampling = True
    c.adaptive_threshold = threshold
    c.use_denoising = True
    c.max_bounces = 16
    c.diffuse_bounces = 8
    c.glossy_bounces = 8
    c.transmission_bounces = 16
    c.transparent_max_bounces = 256              # alpha-tested foliage, see above
    c.volume_bounces = 2
    for attr, val in (("denoiser", 'OPENIMAGEDENOISE'), ("denoising_prefilter", 'ACCURATE'),
                      ("denoising_quality", 'HIGH'), ("denoising_use_gpu", True),
                      ("use_light_tree", True)):
        try:
            setattr(c, attr, val)
        except Exception:
            pass

    for lt in bpy.data.lights:
        if lt.type == 'SUN':
            lt.angle = math.radians(0.526)       # the sun's real angular diameter

    print("[cine] %dx%d, %d samples (threshold %.3f), motion blur 180 deg, "
          "transparent bounces %d, sun 0.526 deg"
          % (res[0], res[1], samples, threshold, c.transparent_max_bounces))
