"""An FPV drone as a flight model, not a curve.

WHY NOT A CURVE, AND WHY NOT `cine_camera`.  `cine_camera.solve_follow_camera` optimises a SMOOTH,
unoccluded, cinematic boom: it is a Viterbi over candidate standoffs whose whole purpose is to hold
the subject cleanly framed and to move as little as possible.  That is the opposite of this.  An FPV
quad reads as an FPV quad because of the things a cinematic solver removes:

  * it LAGS.  The airframe accelerates toward where the pilot wants it and arrives late and hot, so
    a turn overshoots and settles.  A path that hits its marks exactly reads as a crane.
  * it BANKS.  There is no gimbal on a freestyle quad, so the horizon rolls with the turn - roll is
    not a style choice, it is `atan2(lateral acceleration, g)` and it is the single strongest cue.
  * it points where it is GOING, not where it is looking.  A quad accelerates by tilting, so the
    nose drops as it chases and lifts as it brakes.  Aiming the lens straight at the subject the
    whole time is a gimbal shot, which is a different vehicle.
  * it VIBRATES, slightly, at frame rate.

So this integrates a little dynamics model and reads the camera off the airframe state.  Everything
below is a consequence of three parameters (`accel`, `max_speed`, `drag`) plus the aim blend; there
is no keyframed path to fight with, and retiming a shot cannot break it.

The subject track is supplied per frame, so the drone chases whatever the sequencer produced rather
than a guess about where the actor will be.

Pure numpy - no `bpy` in the solver - so a flight can be checked, plotted and unit-tested outside
Blender.  `key_camera()` is the only part that touches Blender and it is a thin wrapper.

    from fpv_camera import Flight, chase
    fl = chase(subject_xyz_per_frame, fps=30, standoff=6.0, height=2.2, close_to=1.8)
    key_camera(cam_obj, fl, fps=30)
"""

import math

import numpy as np

G = 9.81

#: A 2.7 mm FPV lens on a 1/2.3" sensor is about a 15 mm full-frame equivalent.  Wide is not a taste
#: here: the speed cue in FPV footage is near-field parallax, and a long lens deletes it.
DEFAULT_LENS_MM = 15.0

#: Airframe presets.  `accel` is m/s^2 available to the controller, `max_speed` m/s, `drag` per
#: second.  A 5" freestyle quad really does pull ~2 g and cruise at 20 m/s, which is why the chase
#: reads as fast even when the subject is only doing 5.
AIRFRAMES = {
    # heavy, smooth, for long approaches - closest to a cinelifter
    "cine":      dict(accel=12.0, max_speed=14.0, drag=1.2, aim_lag=0.55, bank=0.55, shake=0.10,
                      uptilt_deg="auto"),
    # the default: quick, banks hard, still readable
    "freestyle": dict(accel=18.0, max_speed=22.0, drag=1.3, aim_lag=0.35, bank=0.85, shake=0.22,
                      uptilt_deg="auto"),
    # violent. overshoots, whips, and will lose the subject if the shot is not staged for it
    "racer":     dict(accel=30.0, max_speed=32.0, drag=1.1, aim_lag=0.22, bank=1.00, shake=0.35,
                      uptilt_deg="auto"),
    # A TERMINAL DIVE, and the numbers are the real envelope rather than a dramatic guess. A 5"
    # freestyle quad hits 100-130 km/h in a full-speed dive (28-36 m/s) and purpose-built race
    # airframes reach 150-170 km/h; 34 m/s sits at the top of the freestyle band, which is what a
    # cheap airframe pointed at the ground actually does. `accel` is what the CONTROLLER commands,
    # not total thrust: a 5" pulls something like 8:1 static, but a diving quad is adding gravity to
    # a partial-throttle vector, so ~2.6 g of commanded acceleration is the honest figure.
    # Low aim_lag because a diving pilot is looking exactly where he is going; high shake because
    # at 30 m/s near the ground the airframe is being thrown around by its own prop wash.
    # `drag` sets the real terminal, not `max_speed`: the integrator bleeds `drag * v` every second,
    # so a quad settles near accel/drag. At accel 26 and drag 1.05 that is 24.8 m/s (89 km/h) and the
    # 34 cap never bound - the airframe simply could not reach the dive speed it was labelled with.
    # accel 34 with drag 0.95 puts the terminal at the cap, i.e. inside the measured 100-130 km/h.
    # `bank` is low on purpose. It is the fraction of commanded acceleration the airframe expresses
    # as attitude, and at 0.90 with 34 m/s^2 of command the thrust axis passes the horizon and the
    # camera rolls through 163 degrees - a real quad does pitch past vertical in a dive, but the
    # footage does not, because the pilot is not commanding all of it as attitude.
    # `bank` must satisfy bank * accel < G or the thrust axis inverts. The airframe points body-up
    # along (acc * bank + G), so at accel 34 anything above 0.29 can drive the vertical term
    # negative on a steep command and the camera rolls through the horizon - measured at 163 deg
    # with bank 0.90 and still 124 deg at 0.40. 0.22 keeps 7.5 m/s^2 of attitude against 9.81 of
    # gravity, which banks visibly and never flips.
    "strike":    dict(accel=34.0, max_speed=34.0, drag=0.95, aim_lag=0.10, bank=0.22, shake=0.25,
                      uptilt_deg="auto"),
}


class Flight(object):
    """The solved result: per-frame position and orientation basis, plus what produced them."""

    def __init__(self, pos, fwd, up, roll, fps, lens_mm=DEFAULT_LENS_MM, miss=None):
        self.pos = pos            # (F, 3) world, Blender Z-up
        self.fwd = fwd            # (F, 3) unit
        self.up = up              # (F, 3) unit
        self.roll = roll          # (F,) radians, for reporting
        self.fps = fps
        self.lens_mm = lens_mm
        self.miss = miss          # worst gap between intent and reality over the final quarter

    def __len__(self):
        return len(self.pos)

    def speed(self):
        d = np.diff(self.pos, axis=0) * self.fps
        return np.concatenate([[0.0], np.linalg.norm(d, axis=1)])

    def framing(self, subject, sensor_mm=36.0, aspect=16.0 / 9.0):
        """Per frame: (horizontal, vertical) angle of the subject off the lens axis, in degrees.

        A flight that flies beautifully and loses the actor is a wasted render, and at these speeds
        and this focal length that happens easily - the airframe pitches DOWN to accelerate, so a
        hard chase points the lens at the ground exactly when the subject matters most.
        """
        s = np.asarray(subject, dtype=np.float64)
        to = s - self.pos
        n = np.linalg.norm(to, axis=1, keepdims=True)
        to = to / np.maximum(n, 1e-9)
        right = np.cross(self.fwd, self.up)
        fz = np.einsum('ij,ij->i', to, self.fwd)
        fx = np.einsum('ij,ij->i', to, right)
        fy = np.einsum('ij,ij->i', to, self.up)
        return (np.degrees(np.arctan2(fx, np.maximum(fz, 1e-9))),
                np.degrees(np.arctan2(fy, np.maximum(fz, 1e-9))))

    def half_fov(self, sensor_mm=36.0, aspect=16.0 / 9.0):
        h = math.degrees(math.atan((sensor_mm * 0.5) / self.lens_mm))
        return h, math.degrees(math.atan((sensor_mm / aspect * 0.5) / self.lens_mm))

    def report(self, subject=None):
        sp = self.speed()
        txt = ("[fpv] %d frames, speed %.1f..%.1f m/s (mean %.1f), bank max %.0f deg, lens %.0f mm"
               % (len(self), sp.min(), sp.max(), sp.mean(),
                  math.degrees(np.abs(self.roll).max()), self.lens_mm))
        if subject is not None:
            hx, hy = self.framing(subject)
            fh, fv = self.half_fov()
            out = (np.abs(hx) > fh * 0.85) | (np.abs(hy) > fv * 0.85)
            txt += ("\n[fpv] subject off-axis %.0f/%.0f deg worst (frame is %.0f/%.0f), "
                    "%d/%d frames near or past the edge"
                    % (np.abs(hx).max(), np.abs(hy).max(), fh, fv, int(out.sum()), len(out)))
            if out.any():
                txt += ("\n[fpv] WARNING: the subject leaves frame. Lower `uptilt_deg` or `aim_lag`,"
                        " widen the lens, or raise the standoff.")
        if self.miss is not None and self.miss > 0.75:
            # An airframe can only close if its acceleration budget exceeds what drag costs just to
            # hold the subject's speed (`drag * v`). Past that it flies a permanent standoff and the
            # shot silently never tightens, which is invisible until the render is done.
            txt += ("\n[fpv] WARNING: never reached the intended standoff, off by %.1f m at the "
                    "worst point. Raise `accel`, lower `drag`, or use a lighter airframe."
                    % self.miss)
        return txt


def _unit(v, fallback=(0.0, 1.0, 0.0)):
    n = np.linalg.norm(v)
    if n < 1e-9:
        return np.asarray(fallback, dtype=np.float64)
    return v / n


def _smooth_noise(n, rng, octaves=3, base=1.6, fps=30.0):
    """Band-limited wobble: a few sine octaves with random phase.

    White noise per frame would be denoised away by the renderer's own temporal filtering and reads
    as sensor grain rather than airframe vibration; a quad's frame buzz is a handful of tones.

    THE BAND LIMIT IS THE WHOLE POINT AND IT USED TO BE MISSING. At base 6.0 the octaves landed on
    6, 12 and 24 Hz. Nyquist at 30 fps is 15, so the top octave aliased outright, and 12 Hz is a
    2.5-frame period - which is not vibration on screen, it is the camera changing direction every
    other frame. Measured on a chase, 90.3% of the flight's angular energy sat above 5 Hz: the
    footage was almost entirely buzz with a move hidden underneath it.

    A real quad's frame buzz IS up at 60-200 Hz, but none of that survives a 30 fps sample - a
    camera integrates it into motion blur, it does not step through it. What belongs at this frame
    rate is the slow wander of a pilot holding a line, so the base drops to 1.6 Hz and any octave
    that would land above `fps * 0.28` (a period under ~3.5 frames) is dropped rather than aliased.
    """
    t = np.arange(n) / float(fps)
    out = np.zeros(n)
    amp = 1.0
    used = 0
    for k in range(octaves):
        f = base * (2 ** k)
        if f > fps * 0.28:
            break
        out += amp * np.sin(2 * np.pi * f * t + rng.uniform(0, 2 * np.pi))
        amp *= 0.5
        used += 1
    if used == 0:
        return np.zeros(n)
    return out / max(1e-9, np.abs(out).max())


def fly(subject, want, fps=30, airframe="freestyle", start=None, seed=0, lens_mm=DEFAULT_LENS_MM,
        aim_at=None, floor=None, tune=None):
    """Integrate the drone toward a per-frame DESIRED position and read the camera off the airframe.

    subject   (F, 3) what the drone is filming, world space, Blender Z-up
    want      (F, 3) where the drone would like to be that frame.  The controller chases it with
              bounded acceleration, so this is an intent, not a path - the drone will lag it, and
              that lag IS the shot.
    aim_at    (F, 3) what the lens should favour; defaults to `subject`
    floor     optional (F,) minimum Z, so a low pass cannot sink through the ground
    """
    cfg = dict(AIRFRAMES[airframe] if isinstance(airframe, str) else airframe)
    # Per-shot overrides. The airframe says what the vehicle CAN do; the shot says how it is being
    # flown. `aim_lag` in particular is a shot decision - an orbit is flown looking inward and a
    # flyby is flown looking ahead, on the same quad.
    if tune:
        cfg.update(tune)
    F = len(want)
    rng = np.random.RandomState(seed)
    subject = np.asarray(subject, dtype=np.float64)
    want = np.asarray(want, dtype=np.float64)
    aim = subject if aim_at is None else np.asarray(aim_at, dtype=np.float64)
    dt = 1.0 / fps

    p = np.asarray(start if start is not None else want[0], dtype=np.float64).copy()
    v = np.zeros(3)
    pos = np.empty((F, 3))
    acc = np.empty((F, 3))
    vel = np.zeros((F, 3))

    # Critically damped pursuit: the gains come from a chosen settle time rather than being dialled
    # by eye, so changing `accel` does not silently change the character of the motion.
    #
    # DAMP ON VELOCITY ERROR, NOT ON VELOCITY, and feed the target's own velocity forward through
    # the drag term.  Damping on absolute velocity leaves a standing lag whenever the target moves:
    # holding a speed against `drag` needs a continuous `drag * v` of acceleration, which a pure
    # position term can only produce by sitting permanently behind.  Measured on a 5 m/s subject,
    # that cost 3 m of standoff - a chase asked to close to 3.1 m settled at 6.0 and the shot never
    # tightened.  With the feed-forward the steady state is right and the lag survives only where it
    # is wanted: on the transients, as overshoot into and out of a turn.
    settle = 0.45
    kp = 2.0 / (settle * settle)
    kd = 2.0 / settle
    wv = np.gradient(want, axis=0) * fps if F > 1 else np.zeros_like(want)
    for f in range(F):
        a = kp * (want[f] - p) + kd * (wv[f] - v) + cfg["drag"] * wv[f]
        n = np.linalg.norm(a)
        if n > cfg["accel"]:
            a *= cfg["accel"] / n
        v += a * dt
        v *= max(0.0, 1.0 - cfg["drag"] * dt)
        sp = np.linalg.norm(v)
        if sp > cfg["max_speed"]:
            v *= cfg["max_speed"] / sp
        p = p + v * dt
        if floor is not None:
            p[2] = max(p[2], float(floor[f]))
        pos[f] = p
        acc[f] = a
        # KEEP THE PER-FRAME VELOCITY. The orientation pass below needs frame 0's velocity, and
        # reading the loop variable `v` after the loop has finished hands it the LAST frame's
        # velocity instead - so frame 0 was oriented from the end of the flight and snapped to the
        # correct heading on frame 1. That one-frame kink is visible in any moving shot.
        vel[f] = v

    # ---- orientation: the airframe TILTS TO ACCELERATE ----
    # A quadrotor has exactly one force it can steer: thrust, along its own body-up.  So to produce
    # a commanded acceleration `a` it must point body-up along `a - g`, i.e. (a_x, a_y, a_z + G).
    # Everything an FPV shot reads by - banking into a turn, the nose dropping as it chases, the
    # nose lifting as it brakes - falls out of that ONE line.  An earlier revision here computed
    # roll as `atan2(lateral accel, G)` and pitch not at all; that is the small-angle shadow of
    # this, right for roll on the level and silently missing the pitch entirely.
    #
    # Yaw is the one degree of freedom the thrust axis does NOT constrain, so yaw is the pilot's:
    # it blends the direction of travel with the direction to the subject.  aim_lag = 0 is a gimbal
    # locked on the actor, 1 is a pilot staring down the flight path, and between is a chase.
    fwd = np.empty((F, 3))
    up = np.empty((F, 3))
    roll = np.zeros(F)
    lag = float(cfg["aim_lag"])
    ut = cfg.get("uptilt_deg", "auto")
    auto_tilt = isinstance(ut, str) and ut == "auto"
    tilt = 0.0 if auto_tilt else math.radians(float(ut))
    # A mount can only travel so far: past this the shot should be restaged, not craned.
    tilt_lo = math.radians(float(cfg.get("tilt_min_deg", -55.0)))
    tilt_hi = math.radians(float(cfg.get("tilt_max_deg", 45.0)))
    bank_gain = float(cfg["bank"])
    yaw_shake = _smooth_noise(F, rng, fps=fps) * math.radians(1.6) * cfg["shake"]
    pitch_shake = _smooth_noise(F, rng, fps=fps) * math.radians(1.2) * cfg["shake"]
    world_up = np.array([0.0, 0.0, 1.0])
    # Yaw authority. A 5" quad will spin far faster than this, but a CAMERA that does is unwatchable
    # and it is not what the footage shows; this is the rate a pilot actually yaws while tracking.
    max_yaw_step = math.radians(float(cfg.get("yaw_rate_deg", 240.0))) / float(fps)
    prev_fwd = None
    prev_tilt = None
    max_tilt_step = math.radians(float(cfg.get("tilt_rate_deg", 150.0))) / float(fps)
    for f in range(F):
        to_sub = _unit(aim[f] - pos[f])
        travel = _unit(vel[0] if f == 0 else pos[f] - pos[max(0, f - 1)], fallback=to_sub)
        yaw_dir = _unit(travel * lag + to_sub * (1.0 - lag), fallback=to_sub)

        # The thrust axis.  `bank` scales how much of the commanded acceleration the airframe is
        # allowed to express as attitude: 1.0 is the honest quadrotor, less is a heavier frame that
        # wallows through a turn instead of snapping over.
        body_up = _unit(acc[f] * bank_gain + world_up * G, fallback=world_up)

        # Body forward is the yaw intent projected into the plane the thrust axis defines.
        # YAW HAS A RATE LIMIT, and without one this line mirrors. `b_fwd` is the yaw intent
        # projected into the plane the thrust axis defines, and in a dive - nose down, nearly over
        # the target - that horizontal component collapses toward zero. Its DIRECTION is then
        # numerically unstable, so consecutive frames can land on opposite sides and the camera
        # snaps from +45 to -45 degrees in a single frame. A quad cannot do that: yaw is a real
        # axis with real authority, a few hundred degrees per second, and 90 degrees in 1/30 s
        # would be 2700.
        raw = yaw_dir - body_up * float(yaw_dir @ body_up)
        if np.linalg.norm(raw) < 1e-3 and prev_fwd is not None:
            # Degenerate: hold the heading rather than inventing one from a fallback constant.
            raw = prev_fwd - body_up * float(prev_fwd @ body_up)
        b_fwd = _unit(raw, fallback=_unit(np.cross(body_up, np.cross(to_sub, body_up)),
                                          fallback=(0.0, 1.0, 0.0)))
        if prev_fwd is not None:
            # Clamp the frame-to-frame turn about the thrust axis to what the airframe can yaw.
            p_flat = _unit(prev_fwd - body_up * float(prev_fwd @ body_up), fallback=b_fwd)
            c = float(np.clip(p_flat @ b_fwd, -1.0, 1.0))
            ang = math.acos(c)
            if ang > max_yaw_step:
                axis = np.cross(p_flat, b_fwd)
                n = np.linalg.norm(axis)
                if n > 1e-9:
                    axis = axis / n
                    k, th = axis, max_yaw_step
                    b_fwd = _unit(p_flat * math.cos(th)
                                  + np.cross(k, p_flat) * math.sin(th)
                                  + k * float(k @ p_flat) * (1.0 - math.cos(th)))
        prev_fwd = b_fwd.copy()
        b_right = _unit(np.cross(b_fwd, body_up), fallback=(1.0, 0.0, 0.0))

        # CAMERA TILT.  A bolted-on FPV cam carries 20-40 deg of uptilt because the airframe has to
        # pitch down to go fast; without it a quad at speed films the tarmac.  But a CHASE flies
        # ABOVE its subject, and then a fixed uptilt aims at the sky while the actor sits 25 deg
        # below the nose - measured on this rig, 40-58 deg off-axis against a 34 deg half-frame, so
        # the subject was out of shot on most frames of every chase.  A quad cannot pitch
        # independently of thrust, so no pilot input fixes that.
        #
        # `uptilt_deg="auto"` therefore solves the tilt per frame to sit the subject on the lens
        # axis, clamped to a range a real mount could hold.  That is a PITCH-ONLY gimbal, which is
        # what a cinelifter actually carries - and roll is deliberately left on the airframe,
        # because the rolling horizon is the whole FPV cue and stabilising it would buy a shot that
        # reads as a crane again.
        if auto_tilt:
            want_dir = _unit(aim[f] - pos[f], fallback=b_fwd)
            t_ang = math.atan2(float(want_dir @ body_up), float(want_dir @ b_fwd))
            t_ang = max(tilt_lo, min(tilt_hi, t_ang))
            # The gimbal has a slew rate too. Solving the tilt per frame with nothing limiting it
            # lets the lens pitch as fast as the geometry demands, which in a dive is most of the
            # residual single-frame swing once yaw is clamped.
            if prev_tilt is not None:
                t_ang = prev_tilt + max(-max_tilt_step, min(max_tilt_step, t_ang - prev_tilt))
            prev_tilt = t_ang
        else:
            t_ang = tilt
        ct, st = math.cos(t_ang), math.sin(t_ang)
        d = _unit(b_fwd * ct + body_up * st)
        u = _unit(body_up * ct - b_fwd * st)

        # Frame buzz, about the camera's own axes so it survives any attitude.
        d = _unit(d + b_right * yaw_shake[f] + u * pitch_shake[f])
        u = _unit(u - d * float(u @ d))

        # Horizon angle in the camera's own frame - i.e. the roll a viewer actually sees.
        flat_right = _unit(np.cross(d, world_up), fallback=b_right)
        cam_right = np.cross(d, u)
        roll[f] = math.atan2(float(np.cross(flat_right, cam_right) @ d),
                             float(cam_right @ flat_right))
        fwd[f] = d
        up[f] = u
    # Judge the miss on the LAST QUARTER only. The opening transient is deliberate - the drone is
    # meant to arrive hot - so measuring the whole flight would flag every well-formed shot.
    tail = max(1, F // 4)
    return Flight(pos, fwd, up, roll, fps, lens_mm,
                  miss=float(np.linalg.norm(pos[-tail:] - want[-tail:], axis=1).max()))


# ----------------------------------------------------------------------------------------------
# shot generators - each returns the per-frame DESIRED position for `fly`
# ----------------------------------------------------------------------------------------------

def _heading(subject):
    """Per-frame unit direction of travel, level.  Falls back to +Y where the subject is still."""
    s = np.asarray(subject, dtype=np.float64)
    if len(s) < 2:
        return np.tile(np.array([0.0, 1.0, 0.0]), (len(s), 1))
    d = np.gradient(s[:, :2], axis=0)
    h = np.concatenate([d, np.zeros((len(s), 1))], axis=1)
    n = np.linalg.norm(h, axis=1)
    # A stationary subject has no heading of its own; carry the last real one rather than
    # snapping the rig to an arbitrary axis mid-shot.
    last = np.array([0.0, 1.0, 0.0])
    for f in range(len(s)):
        if n[f] < 1e-9:
            h[f] = last
        else:
            h[f] = h[f] / n[f]
            last = h[f]
    return h


def _behind(subject, back, height, side=0.0):
    """Points `back` metres behind the subject along its own travel, `height` above, `side` across.

    `back` / `height` / `side` are scalars or per-frame arrays.
    """
    s = np.asarray(subject, dtype=np.float64)
    F = len(s)
    h = _heading(s)
    up = np.array([0.0, 0.0, 1.0])
    left = np.cross(np.tile(up, (F, 1)), h)
    back = np.broadcast_to(np.asarray(back, dtype=np.float64), (F,))
    height = np.broadcast_to(np.asarray(height, dtype=np.float64), (F,))
    side = np.broadcast_to(np.asarray(side, dtype=np.float64), (F,))
    return s - h * back[:, None] + left * side[:, None] + up[None, :] * height[:, None]


def chase(subject, fps=30, standoff=7.0, height=2.4, close_to=2.0, side=0.0, **kw):
    """The core shot: the drone runs the subject down from behind, closing the whole way.

    The standoff ramps from `standoff` to `close_to` over the shot, so the frame tightens without a
    zoom - which is the FPV cue, because a quad has a fixed lens and gets closer instead.
    """
    F = len(subject)
    ramp = np.linspace(1.0, 0.0, F)
    want = _behind(subject, close_to + (standoff - close_to) * ramp, height, side)
    return fly(subject, want, fps=fps, **kw)


def flyby(subject, fps=30, approach=14.0, past=9.0, height=1.8, side=2.5, **kw):
    """Come at the subject head-on and blow past it.  The whip as it passes is the shot."""
    F = len(subject)
    t = np.linspace(-1.0, 1.0, F)
    # NEGATIVE `back` puts the drone in FRONT of the subject, which is the whole shot: it starts
    # ahead, closes head-on, and is behind by the end.
    along = np.where(t < 0, approach * -t, past * t)
    want = _behind(subject, -along, height, side)
    return fly(subject, want, fps=fps, **kw)


def dive(subject, fps=30, from_height=22.0, to_height=2.0, back=10.0, **kw):
    """Drop out of the sky onto the subject.  Opens a montage; the ground rush does the work."""
    F = len(subject)
    ease = 0.5 - 0.5 * np.cos(np.linspace(0.0, math.pi, F))     # raised cosine, no step in velocity
    want = _behind(subject,
                   back * (1.0 - ease) + 2.0 * ease,
                   from_height + (to_height - from_height) * ease)
    return fly(subject, want, fps=fps, **kw)


def orbit(subject, fps=30, radius=5.0, height=2.0, turns=0.75, phase=0.0, **kw):
    """Circle the subject.  Use it on a held beat, not on a run.

    Flown looking INWARD: on an orbit the direction of travel is tangential, so an airframe's stock
    `aim_lag` points the lens off into the scenery - measured, 48 of 150 frames lost the subject.
    """
    kw.setdefault("tune", {}).setdefault("aim_lag", 0.10)
    F = len(subject)
    ang = phase + np.linspace(0.0, 2.0 * math.pi * turns, F)
    off = np.stack([np.cos(ang) * radius, np.sin(ang) * radius, np.full(F, height)], axis=1)
    return fly(subject, np.asarray(subject, dtype=np.float64) + off, fps=fps, **kw)


# ----------------------------------------------------------------------------------------------
# Blender
# ----------------------------------------------------------------------------------------------

def key_camera(cam_obj, flight, frame_start=1, fps=None, subject=None):
    """Bake a Flight onto a Blender camera.  Needs bpy; everything above does not.

    A Blender camera looks down its own -Z with +Y up, so the basis is (right, up, -forward).
    """
    import bpy                                             # noqa: F401
    from mathutils import Matrix

    cam_obj.rotation_mode = 'QUATERNION'
    if getattr(cam_obj.data, "lens", None) is not None:
        cam_obj.data.lens = flight.lens_mm
    for f in range(len(flight)):
        d = flight.fwd[f]
        u = flight.up[f]
        r = np.cross(d, u)
        m = Matrix(((r[0], u[0], -d[0], flight.pos[f][0]),
                    (r[1], u[1], -d[1], flight.pos[f][1]),
                    (r[2], u[2], -d[2], flight.pos[f][2]),
                    (0.0, 0.0, 0.0, 1.0)))
        cam_obj.matrix_world = m
        fr = frame_start + f
        cam_obj.keyframe_insert("location", frame=fr)
        cam_obj.keyframe_insert("rotation_quaternion", frame=fr)
    # LINEAR keys: the flight is already integrated at frame rate, so Bezier handles would add an
    # ease the dynamics never had and soften exactly the whips the shot is for.
    ad = cam_obj.animation_data
    if ad and ad.action:
        for layer in getattr(ad.action, "layers", []) or []:
            for strip in layer.strips:
                for cb in getattr(strip, "channelbags", []) or []:
                    for fc in cb.fcurves:
                        for kp in fc.keyframe_points:
                            kp.interpolation = 'LINEAR'
        for fc in getattr(ad.action, "fcurves", []) or []:
            for kp in fc.keyframe_points:
                kp.interpolation = 'LINEAR'
    print(flight.report(subject), flush=True)
    return cam_obj
