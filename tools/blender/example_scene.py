"""End-to-end example: a map region, terrain, grass, and a character walking a real patrol.

Run inside a live Blender session (5.x):

    exec(open(r"<repo>/tools/blender/example_scene.py").read())

or from the command line:

    blender --python tools/blender/example_scene.py

Everything it does is documented in docs/extraction/blender-import.md. The point of this file is
to show the ORDER and the handful of decisions that are easy to get wrong, not to be configurable:
edit the constants at the top and re-run.

What it builds:
  * the map around a patrol, at LOD0, with textures
  * terrain rebuilt as the real MicroSplat splat rather than the soft baked slice
  * the grass field, distance-limited the way the viewer culls it, waving on the pack's own wind
  * the scav, its rifle, and a walk along one of the game's own patrol_ways
  * a camera that follows him, the game's sky as the world, and a bounded atmosphere
  * Cycles on the GPU

TWO MODES, ONE BUILDER. `MODE` selects which of the repo's two documented and deliberately
conflicting goals this run is serving:

    MODE = "game"       docs/extraction/game-parity.md. The viewer is the authority. Fitted sun
                        and sky, the pack's own cubemap, every material as the viewer reads it,
                        finished with the game's grade LUT through tools/blender/eft_grade.py.
    MODE = "photoreal"  docs/extraction/photorealism.md. Departs from the viewer wherever a
                        photograph would, and says so at every departure.

Geometry, UVs, nav routing, the camera solve and the decal lift are IDENTICAL in both - there is
nothing photographic about where a wall is. Everything the modes disagree about is collected in
the two tables below so the diff between the images is readable as a diff between two dicts.

WHAT PHOTOREAL MODE DOES *NOT* DO, because it was measured and did not pay:
  * a physical Nishita sky. It delivers 131.4 W/m^2 against the pack's total 10.42, i.e. 12.6x,
    and 59.6% of the frame then clips against the parity path's 0.611%. The measured "12x dynamic
    range" of the earlier photoreal test frame is mostly BRIGHTNESS: normalised by each frame's
    own median it is 12.1 against 7.3, only 1.66x more range, with 60.4% of the frame blown.
  * rebuilding the 8-bit pack sky as an HDR environment with a real solar disc. Energy-matched to
    0.01%, it moved the render by a uniform -3.4 to -4.3% at every percentile, which is exactly
    the disc's solid-angle quantization and not a lighting change. The sun LAMP already supplies
    the specular energy and Cycles shows sun lamps in glossy reflections. It also cost +37% seed
    to seed noise, because a 0.526 degree disc in an importance map samples worse than a lamp.
  * foliage translucency. Cycles already shades a backfacing double-sided leaf with the flipped
    normal, so foliage-pixel mean luminance moved +0.5% and the A/B crop is indistinguishable.
  * more grass. grass.bin's 3,261,251 clumps are 96.6% of Unity's own authored detail grids, so
    multiplying density is invention, not restoration.
  * lens distortion. Blender's Distortion socket is clamped to barrel only, and the ~1% a named
    lens shows is lens-specific, i.e. invented, and costs a full-frame resample.
"""

import json
import math
import os
import sys

import bpy
import mathutils

# ---------------------------------------------------------------------------------------------
# EDIT THESE
# ---------------------------------------------------------------------------------------------
# The repo root, derived from this file's own location. When the script is pasted into Blender's
# text editor rather than exec'd from disk there is no __file__, so set EFT_REPO instead.
REPO = (os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if "__file__" in globals() else os.environ.get("EFT_REPO", os.getcwd()))
PACK = os.path.join(REPO, "packs", "interchange.eftpack")
DATASET = os.path.join(REPO, "eft_assets", "interchange_v2")
CHARACTER = os.path.join(REPO, "out", "characters", "scav")
WEAPON = os.path.join(REPO, "out", "weapons", "weapon_colt_m4a1_556x45")
SKY_EQUIRECT = os.path.join(REPO, "packs", "shared", "sky", "NatureCubemap_equirect.png")
# Cavity maps baked from the pack's normal maps by tools/blender/bake_cavity.py. Photoreal only,
# and an absent or partial directory is not an error - the importer skips what it cannot find.
CAVITY_DIR = os.path.join(REPO, "out", "cavity")

MODE = "game"                      # "game" or "photoreal"; see the module docstring

# THE STAGING. ZoneBearCamp 3..5 is a 24.6 m walk out of the forest past two truck cabs and the
# camp building. It was chosen against the four things that decide whether any of the lighting
# work below is visible at all, all measured on the pack:
#   varied ground   5 terrain layers under the filmed 19.2 m (Gravel_Road_B 29%, Soil_Grass 22%,
#                   Grassy_Ground 20%, Forest_Ground 15%, Grass 9%) and 1.96 m of climb, so the
#                   camera changes height instead of dollying across a plane
#   occlusion       44 LOD groups over 4 m within 14 m of the walk, one of them 39.5 m across, so
#                   the solver's rear-left camera has trunks and a wall to pass behind
#   scale           471 instances within 22 m: Ural-280 and Kamaz-4310 cabs, sandbags, pallets
#   light           the filmed heading is 42 deg off the sun azimuth and cine_camera's PREF_AZ is
#                   +118 deg, so the camera-to-subject bearing puts the backlight factor at +0.94
# It is also a LIGHTER build than the old staging: MAP_RADIUS 170 imports 3,766 instances against
# the previous staging's 150 m / 4,484.
# (The line that used to sit here claimed 170 "clears terrain tile Slice_2_2's 159.0 m centre by
# 11 m at 3,528 instances". Neither number is reproducible and the reasoning behind them was the
# bug: the tile's true centroid is 163.3 m away, the sampled centre the importer actually used
# reported 185.5 m, and MAP_RADIUS 170 clears NEITHER - it culled the tile the camera stands on
# until _select started testing the mesh's world AABB, whose distance to this route is 0.0 m
# because the route is INSIDE the tile. A radius is not a clearance over an object's middle.)
# The previous staging, kept because every measurement in game-parity.md was taken on it:
#     PATROL_ZONE, PATROL_SPAN, MAP_RADIUS, GRASS_RADIUS = "ZonePowerStation", (0, 6), 150.0, 26.0
PATROL_ZONE = "ZoneBearCamp"       # a gamedata.json patrol_ways zone
PATROL_SPAN = (3, 5)               # waypoint slice; the ways are NETWORKS, not ordered paths
CLIP = "walk_aim_slow_0"           # a LOOPING clip with forward root motion
SHOT_SECONDS = 12.0                # how much of the patrol to film (the walk itself stays whole)
MAP_RADIUS = 170.0                 # metres of map to build around the route
GRASS_RADIUS = 40.0                # the viewer culls grass by screen size; a static build needs this
SAMPLES = 96

HORIZ_MIN = 0.5                    # walk_ground.rs: a face is GROUND when its normal.y/|n| > 0.5
STEP_UP = 0.5                      # walk_ground.rs: how far the feet may rise to select a surface

# Sun and sky are the two numbers this scene cannot read out of the pack: it ships NO directional
# light (the game's outdoor lighting lives in the baked SH volume), so there is nothing to copy.
# They were therefore SOLVED against the viewer rather than eyeballed. Cycles is linear in every
# light's power, so for a fixed camera
#     render(sun=a, sky=b) == a * render(sun=1, sky=0) + b * render(sun=0, sky=1)
# exactly. Two basis renders span the whole space, and the pair below is the least-squares fit of
# that combination - pushed through the game's own grade LUT at the viewer's exposure 1.35 - to an
# Atlas frame of the identical camera. Fitting on lit surfaces cut RMS error 16% versus the values
# originally guessed here (6.0 / 1.6), and moved the sun:sky ratio from 3.75 to 2.94: the guess had
# too much direct sun and too little sky, which is what made sunlit ground blow out while shaded
# faces went muddy under an overcast sky it was inconsistent with.
# Re-solve with tools/blender/eft_grade.py + a basis pair whenever the sky or map changes.
SUN_ENERGY = 6.90
SKY_STRENGTH = 2.35

# ---------------------------------------------------------------------------------------------
# THE ONLY THINGS THE TWO MODES DISAGREE ABOUT
# ---------------------------------------------------------------------------------------------
# Every entry is either derived from the pack, or invented and labelled. Read the two dicts side
# by side: that IS the difference between the two images.
MODES = {
    "game": dict(
        # The traced haze box. Nothing in the pack supports it - gamedata/particles/volume/sky/
        # manifest were grepped for fog|haze|atmos|scatter|mist|density and returned zero hits, and
        # volume.json is the SH irradiance bake ("direct": false), not a medium. But SUN_ENERGY and
        # SKY_STRENGTH were least-squares fitted against an Atlas frame WITH this box in place, so
        # on the parity path it stays exactly as fitted. Removing it here without re-solving that
        # pair would drop the median 16%.
        atmosphere="uniform",
        haze_post=False,           # and TRACED, because that is what the pair was fitted against
        backdrop="pack",           # the cubemap is what the GAME shows; parity means showing it
        glass_mode="trs",          # the bounded legacy reflection, term for term
        cavity=False,              # the game shader has no ambient-occlusion term at all
        comp=False,                # no glare, no chromatic aberration: the viewer has neither
        aperture_blades=0,
        samples=SAMPLES,
    ),
    "photoreal": dict(
        # HEIGHT FALLOFF instead of a uniform slab, and 2.5x thinner at the ground. A constant
        # density veils near and far equally, which is fog; aerial perspective needs the extinction
        # to build with distance, and a uniform box only approximates that when it is much larger
        # than the scene. Measured on the old staging, disabling the box entirely took the frame's
        # p99.9/median range from 7.3 to 9.1 (+25%) and the render from 129.9 s to 73.2 s (-44%):
        # this is the single biggest lever on the whole photoreal path, and it BUYS time.
        # The numbers are invented in the same sense the 0.0016 they replace was - the pack ships
        # no atmosphere - but 4e-4 with a 40 m scale height is the aerosol profile a real 150 m
        # vista shows, where a flat 0.0016 is ~130x sea-level Rayleigh applied uniformly.
        atmosphere="falloff",
        # ...and then do NOT path-trace it. Thinning the box was only half the win: measured on
        # this staging at 1280x720, tracing the thinned box still costs HALF the frame (see
        # HAZE_SIGMA below for the numbers), and every one of those seconds is spent marching
        # single scatter through the whole scene BVH to produce something a depth-driven
        # exponential reproduces to 1.5x the path tracer's own noise. The box is still built, sized
        # and shaped by HAZE_DENSITY/HAZE_SCALE_H, and still visible in the viewport; it is
        # hide_render'd and applied analytically from the Z pass instead. See _depth_haze_nodes.
        haze_post=True,
        # The sky the LENS sees, and only the lens: see BACKDROP_RATIO. Lighting stays on the pack
        # cubemap in both modes, so SUN_ENERGY/SKY_STRENGTH remain the pair that was fitted.
        backdrop="physical",
        glass_mode="physical",     # real transmission on panes the pack already ships as slabs
        cavity=True,               # the normal maps' own self-occlusion, Poisson-derived
        comp=True,                 # veiling glare + lateral CA, calibrated below
        aperture_blades=9,         # a 50 mm at f/2.8 is stopped down; no iris is a perfect circle
        # Spend what that frees on samples rather than on wall clock, which is the whole point of
        # not tracing the box. Noise falls as 1/sqrt(spp) and that law was confirmed to 1% here.
        # The multiplier is not a preference: at 172 spp the frame costs 91.2 s traced and 45.7 s
        # untraced, and the untraced frame's marginal cost is 0.2034 s/spp (172 spp 45.7 s, 345
        # spp 80.9 s), so 4.0x SAMPLES = 384 spp predicts ~88.9 s and measured 87.8 s against the
        # traced frame's 92.1 s. Anything less than this leaves measured time on the table.
        samples=int(SAMPLES * 4.0),
    ),
}

# THE BACKDROP SPLIT (photoreal only). The pack's sky is a 128 px/face REFLECTION PROBE, ~0.70 deg
# per texel, resampled to 2048x1024 by make_sky_equirect.py. It is smooth and it is structureless,
# and no resampling recovers cloud edges that were never in the source. The game's visible horizon
# is SkyboxMountains01..08 GEOMETRY, which this repository has never extracted, so a sharp horizon
# is out of reach here either way.
#
# The docstring above records why a physical sky was rejected as the ENVIRONMENT: 12.6x the pack's
# energy, 59.6% of the frame clipped. That is a statement about the sky LIGHTING THE SCENE. What a
# camera ray terminates on is a separate question, and `Light Path > Is Camera Ray` lets Cycles
# answer the two differently. So: lighting keeps the pack cubemap EXACTLY - every fitted number,
# including the SUN_ENERGY/SKY_STRENGTH pair, is untouched - and only the pixels the lens sees are
# replaced. Reflections stay on the cubemap too, which is right: a puddle in this scene should
# mirror the sky the rest of the lighting came from.
#
# BACKDROP_RATIO is a MEASUREMENT, not a taste knob, on exactly the footing SUN_ENERGY is: it is the
# factor that makes the physical backdrop, CLOUDS INCLUDED, carry the same radiance as the pack sky
# it replaces. Reproduce with tools/blender/fit_sky_backdrop.py, which builds this very graph and
# writes packs/shared/sky/backdrop_fit.json.
#
# It is fitted over the SOLID-ANGLE-WEIGHTED UPPER HEMISPHERE, not over one frame, and that is not
# pedantry. Measured on a level 12 mm frame the physical sky came out 18.9x brighter than the pack
# capture, whose dark baked treeline sits exactly there; measured 18 degrees up, the comparison
# inverted to 2.2x the other way, because the pack's upper sky is the bright part. One view direction
# fits the FRAMING, not the sky. An equirectangular camera sees every direction at once, and rows are
# cosine-weighted because equirect gives the zenith as many pixels as the horizon while it covers far
# less solid angle.
#
# Re-run the fit after ANY change to the backdrop's appearance. Adding the cloud layer alone moved
# this by 2.37x, because cloud is much brighter than the clear sky it covers, and the horizon
# handover moved it a further 1.20x. The fit solves an AFFINE model for that reason: HORIZON_HAZE
# mixes toward a fixed scene-referred colour that does not scale with this constant.
BACKDROP_RATIO = 0.149731

# THE CLOUD LAYER, photoreal backdrop only. See _cloud_nodes for the ray-plane geometry.
#
# EVERY NUMBER BELOW IS INVENTED and that is a deliberate, disclosed choice rather than an oversight.
# The pack ships no cloud data at all: its sky is a 128 px/face capture whose clouds are already an
# unrecoverable blur, and there is no cloud texture, cloud mesh or weather record anywhere in the
# extraction. So there is nothing to derive from, and the repo's rule - derive, do not author - is
# not being broken here so much as it has nothing to bite on. What keeps this honest is that it runs
# ONLY behind Is Camera Ray on the photoreal path: it cannot touch the lighting solve, it cannot
# touch parity mode, and `EFT_CLOUDS=0` removes it entirely.
#
# The values aim at the overcast-but-breaking deck a Tarkov raid reads as, not at a summer postcard:
# high coverage, low contrast, desaturated, with the sun's position agreeing with the sun lamp.
CLOUD_ALTITUDE = 1600.0     # m; stratocumulus base. Lower = faster convergence at the horizon.
CLOUD_SCALE = 2100.0        # m; the size of one cloud cell on the layer
CLOUD_DETAIL = 8.0          # fBm octaves; below ~5 the edges read as smooth blobs
CLOUD_ROUGHNESS = 0.545
CLOUD_DISTORTION = 0.32     # breaks the grid the noise would otherwise betray
CLOUD_COVERAGE = 0.495      # the noise value at which cloud starts; LOWER covers more sky
CLOUD_SOFT = 0.065          # the width of the edge ramp; a hard cut reads as a paper cutout
CLOUD_DENSITY_SPAN = 0.26   # noise range from "just formed" to "full thickness"; drives brightness
CLOUD_RELIEF_WEIGHT = 0.45  # how much of the body tone is directional relief vs sheer thickness
CLOUD_OPACITY = 0.97        # never fully 1.0 - a real deck lets some sky through even when thick
CLOUD_FADE = (0.020, 0.115) # ray.z over which the layer dissolves into the horizon haze
CLOUD_RELIEF_STEP = 0.075   # layer-units the second noise sample is offset toward the sun
CLOUD_RELIEF_GAIN = 0.070   # the relief difference that maps to fully lit
# A cloud top is BRIGHTER than the sky behind it and its base is much darker; that spread is the
# whole reason a deck reads as three-dimensional rather than as fog on glass.
CLOUD_SHADOW = (0.230, 0.248, 0.280)   # the underside; blue-grey, because it is lit by sky
CLOUD_LIT = (0.960, 0.968, 0.985)      # the sun-facing face
CLOUD_SILVER = (1.320, 1.300, 1.240)   # the rim near the disc; above 1.0 on purpose, for the grade
CLOUD_SILVER_ARC = (0.72, 0.995)       # dot(view, sun) over which the silver lining ramps in

# Where the backdrop hands over to the scene's own aerial perspective, as ray.z. At or below the
# first value it is pure HAZE_INSCATTER, above the second it is pure sky.
# The lower bound is EXACTLY the horizon, and it has to be. Blender's sky texture is discontinuous
# there - clear sky above, a dim painted ground below - so any handover that still lets a fraction
# of it through at z=0 draws that discontinuity as a hard line across the frame. Ending the blend
# at the horizon replaces the sky's ground entirely with the colour distant terrain fades into,
# which is both the correct colour and the only one that leaves no seam.
HORIZON_HAZE = (0.000, 0.160)

# Ground extinction and scale height for the photoreal atmosphere. See MODES above.
HAZE_DENSITY = 4.0e-4
HAZE_SCALE_H = 40.0
ATM_LIFT = 30.0        # box centre above the route, metres
ATM_HEIGHT = 80.0      # box height, metres; so it spans route-10 m .. route+70 m

# THE ANALYTIC STAND-IN for that box on the photoreal path, evaluated per pixel from the Z pass:
#     out_c = in_c * exp(-sigma_c * z) + L_c * (1 - exp(-sigma_c * z))
# with z in metres, clamped to the box's own half-width because that is the longest crossing a
# camera near its centre can have. sigma and L are NOT authored numbers. They are a least-squares
# fit of that model to THIS SCENE'S OWN traced falloff volume: two renders of the same frame and
# seed, one with the box in the beauty pass and one without, plus the Z pass, then a scan over
# sigma with L solved in closed form at each step. So the replacement is a model of the exact
# thing it replaces, not a second invention on top of the first.
# They are also properties of the VOLUME rather than of this staging, which is the only reason it
# is safe to write them down as constants: refitting on a second build (MAP_RADIUS 260, 7050
# instances instead of 3706, terrain present, clamp 286 m instead of 187 m) moved sigma by 6 to 8%
# and L by under 2%. Refit if HAZE_DENSITY or HAZE_SCALE_H move, not if the shot does.
# Fit residual and what it buys are in _depth_haze_nodes' docstring.
HAZE_SIGMA = (2.549e-4, 2.525e-4, 2.623e-4)     # per-channel extinction, 1/m
HAZE_INSCATTER = (1.2491, 1.3298, 1.3951)       # in-scatter colour, linear, scene-referred
HAZE_ZMAX = MAP_RADIUS * 1.1                    # the box is MAP_RADIUS * 2.2 across; see step 6

# COMPOSITOR, photoreal only, and every number here is calibrated rather than chosen.
# Veiling glare is a whole-image PSF convolution at THRESHOLD 0, because that is what a lens does:
# it scatters a fixed fraction of all light into wide tails, it does not run a threshold. At
# strength 0.015 it adds 1.43% of total frame energy, inside the 0.5-2% a real prime scatters.
# Lateral CA: the calibration is shift_px = 275 * Dispersion at r = 1216 px on a 2560-wide frame,
# linear to about 0.002. A good 50 mm shows 1-1.5 px at 6000 px wide, i.e. ~0.5 px here, so 0.0012.
# (A first guess of 0.004 produced +110/-91 CV swings - four times too strong.)
GLARE_STRENGTH = 0.015
CA_DISPERSION = 0.0012
# NOT included: a threshold Bloom node. It would have to be re-derived from the exposure every
# time exposure moves, which bakes a grading decision into the linear EXR and stops that file
# being scene-referred. Exposure is solved later, in eft_grade.py, where it belongs.


def _load(script):
    p = os.path.join(REPO, "tools", "blender", script)
    g = {"__name__": os.path.splitext(script)[0], "__file__": p}
    exec(compile(open(p, encoding="utf-8").read(), p, "exec"), g)
    return g


def _clear():
    for c in list(bpy.data.collections):
        for o in list(c.objects):
            bpy.data.objects.remove(o, do_unlink=True)
        bpy.data.collections.remove(c)
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)


def _patrol(pack, zone, span):
    """The game's waypoints, ROUTED on the baked nav grid, pack Y-up -> Blender Z-up.

    The waypoints alone are not a path. A patrol_way is a NETWORK: consecutive entries are not
    guaranteed to be mutually visible, and a straight line between two of them walks the character
    through whatever stands in between (on this route, an 11.7 m tanker). Routing each leg on the
    same grid the viewer navigates gives a path a bot could actually walk: around obstacles, up
    stairs, through doors, and never off a ledge it could not drop.
    """
    gd = json.load(open(os.path.join(pack, "gamedata.json"), encoding="utf-8"))
    way = next(w for w in gd["patrol_ways"] if str(w.get("zone")) == zone)
    raw = [tuple(p) for p in way["points"][span[0]:span[1]]]
    if len(raw) < 2:
        raise SystemExit("patrol %r has fewer than 2 usable points" % zone)
    nav = _load("nav_route.py")["NavGrid"](pack)
    routed = nav.route_through(raw)
    if len(routed) < 2:
        raise SystemExit("patrol %r would not route on the nav grid" % zone)
    return [mathutils.Vector((p[0], -p[2], p[1])) for p in routed]


def _ground_height(scene, dg, arm, x, y, feet_z):
    """Atlas's rule: the GREATEST walkable surface at (x, y) that is <= feet_z + STEP_UP.

    Cast DOWN from the cap, never from the sky: a sky-down ray stops on bush canopies and
    container roofs, which is how a character ends up standing in mid-air.
    """
    org = mathutils.Vector((x, y, feet_z + STEP_UP))
    d = mathutils.Vector((0, 0, -1))
    for _ in range(16):
        hit, loc, nrm, _i, obj, _m = scene.ray_cast(dg, org, d, distance=60.0)
        if not hit:
            return None
        foliage = any(ms.material and ms.material.name.endswith(".cutout")
                      for ms in obj.material_slots)
        if (nrm.z > HORIZ_MIN and not obj.name.startswith("grass_kind")
                and obj.parent is not arm and not foliage):
            return loc.z
        org = loc + d * 0.02
    return None


def enable_cycles_gpu():
    """Pick a GPU backend and enable its devices.

    This lives in the ADDON PREFERENCES, not in the .blend. `scene.cycles.device = 'GPU'` is saved
    with the file, but the list of enabled devices is not, so a `--factory-startup` run that opens
    an existing scene silently renders on the CPU at a tenth of the speed. Any script that renders
    without calling build() has to call this itself.
    """
    prefs = bpy.context.preferences.addons.get("cycles")
    if not prefs:
        print("[cycles] no cycles addon; leaving the device alone")
        return None
    cp = prefs.preferences
    chosen = None
    for backend in ("OPTIX", "CUDA", "HIP", "ONEAPI"):
        try:
            cp.compute_device_type = backend
            cp.get_devices()
            if any(d.type == backend for d in cp.devices):
                chosen = backend
                break
        except Exception:
            continue
    on = []
    for d in cp.devices:
        d.use = (d.type != 'CPU')
        if d.use:
            on.append(d.name)
    print("[cycles] backend %s, %d device(s) enabled: %s"
          % (chosen or "NONE (CPU!)", len(on), ", ".join(on) or "none"))
    return chosen


def _atmosphere_nodes(ant, mode, h_lo, h_hi):
    """The bounded haze volume, in whichever of the two shapes the mode asks for.

    "uniform"  the fitted parity slab: one constant density, exactly what SUN_ENERGY and
               SKY_STRENGTH were solved against.
    "falloff"  exponential in height, exp(-(z - ground) / H). Real aerosol is stratified, and a
               constant slab veils a wall 5 m away as hard as a treeline at 150 m, which is what
               made the parity frame milky (p99.9/median 7.3 against 9.1 with no volume at all).
    """
    ao = ant.nodes.new("ShaderNodeOutputMaterial"); ao.location = (300, 0)
    vs = ant.nodes.new("ShaderNodeVolumeScatter"); vs.location = (100, 0)
    vs.inputs["Anisotropy"].default_value = 0.6
    if mode != "falloff":
        vs.inputs["Density"].default_value = 0.0016
    else:
        # HEIGHT ABOVE THE ROUTE, in metres. Object coordinates run [-0.5, 0.5] over a cube built
        # at size 1.0 whatever the object's scale is, so Map Range converts them back to the metres
        # the caller placed the box at, and MAXIMUM floors the profile at the ground: below it
        # exp(-h/H) climbs above 1 and would put MORE haze under the terrain than over it.
        tc = ant.nodes.new("ShaderNodeTexCoord"); tc.location = (-900, 0)
        sep = ant.nodes.new("ShaderNodeSeparateXYZ"); sep.location = (-720, 0)
        ant.links.new(tc.outputs["Object"], sep.inputs["Vector"])
        mr = ant.nodes.new("ShaderNodeMapRange"); mr.location = (-540, 0)
        mr.inputs["From Min"].default_value = -0.5
        mr.inputs["From Max"].default_value = 0.5
        mr.inputs["To Min"].default_value = float(h_lo)
        mr.inputs["To Max"].default_value = float(h_hi)
        ant.links.new(sep.outputs["Z"], mr.inputs["Value"])
        fl = ant.nodes.new("ShaderNodeMath"); fl.location = (-380, 0)
        fl.operation = 'MAXIMUM'; fl.inputs[1].default_value = 0.0
        ant.links.new(mr.outputs["Result"], fl.inputs[0])
        neg = ant.nodes.new("ShaderNodeMath"); neg.location = (-260, 0)
        neg.operation = 'MULTIPLY'; neg.inputs[1].default_value = -1.0 / HAZE_SCALE_H
        ant.links.new(fl.outputs[0], neg.inputs[0])
        ex = ant.nodes.new("ShaderNodeMath"); ex.location = (-100, 0)
        ex.operation = 'MULTIPLY_ADD'
        ex.inputs[1].default_value = HAZE_DENSITY
        ex.inputs[2].default_value = 0.0
        exp = ant.nodes.new("ShaderNodeMath"); exp.location = (-180, -180)
        exp.operation = 'EXPONENT'
        ant.links.new(neg.outputs[0], exp.inputs[0])
        ant.links.new(exp.outputs[0], ex.inputs[0])
        ant.links.new(ex.outputs[0], vs.inputs["Density"])
    ant.links.new(vs.outputs["Volume"], ao.inputs["Volume"])


def _depth_haze_nodes(ng, rl, src):
    """The traced atmosphere as six multiply-adds on the Z pass. Returns the new image socket.

        out_c = in_c * exp(-sigma_c * z) + L_c * (1 - exp(-sigma_c * z))

    written as (in_c - L_c) * T_c + L_c so each channel is one MULTIPLY_ADD. It goes BEFORE the
    glare, because the haze is in the scene and the glare is in the lens.

    WHY THIS EXISTS. Path-tracing the box was measured at 45.5 s of a 91.2 s frame (49.9%) on this
    staging at 1280x720, and volume_bounces=0 was already known to save only 5.5%: the cost is the
    single-scatter march through the scene BVH, not the extra bounce. Against the traced
    box on the same frame and seed, deleting it outright is an rms error of 0.01047 and drops the
    frame mean 7.0%; this model brings the error to 0.00515 and the mean to within 2.2%, against a
    two-seed path-trace noise floor of 0.00338. So what is left is 1.5x noise, and it is not a fit
    artifact: scored against a held-out second seed the same constants give 0.00508. The error is
    concentrated exactly where there is the least of it, by depth shell (model vs delete):
    0-25 m, 84% of the frame, 0.00269 vs 0.00307; 25-50 m 0.00638 vs 0.01114; 50-100 m 0.00741 vs
    0.01720; past 100 m, 3% of the frame, 0.02225 vs 0.05216.

    END TO END, the two builds A/B'd at 1280x720 with the full compositor on both, two seeds each,
    interleaved in one session on an idle GPU: traced box at 172 spp is 92.1 s, rel-sigma 0.0634,
    bright quartile 0.0376; this at 384 spp is 87.8 s, rel-sigma 0.0564, bright quartile 0.0339.
    So 4.7% LESS wall clock for 1.12x less noise overall and 1.11x in the highlights, with the
    frame mean moved +2.2% and rms 0.00532 against the image it replaces. The gain is smaller than
    1/sqrt(spp) predicts because OIDN is already doing most of that work; what the extra samples
    buy is what the denoiser was inventing.

    WHAT IT CANNOT DO, since the fit cannot invent what it never saw: the model is isotropic, so
    the box's anisotropy 0.6 forward-scatter halo around the sun and any shafts through the canopy
    are gone. On this shot that lives in the 3% of pixels past 100 m. A vista shot, or the sun in
    frame, wants the traced box back (set haze_post False) rather than this.

    5.1 TRAP, and it fails loudly at node creation rather than silently at render, which is a
    mercy: there is no CompositorNodeMath in this build. `nodes.new("CompositorNodeMath")` raises
    "Node type undefined". The compositor takes the unified ShaderNodeMath now, while Separate and
    Combine Color are still CompositorNode*. The other half of the trap is silent: Render Layers
    grows its "Depth" output only once view_layer.use_pass_z is True, so the pass has to be
    enabled before this runs or `outputs.get("Depth")` is None.
    """
    zs = rl.outputs.get("Depth") or rl.outputs.get("Z")
    if zs is None:
        print("[example] Render Layers has no Depth output; skipping the depth haze")
        return src

    # Sky and any ray that leaves the box come back at 1e10, so clamp to the longest crossing the
    # box actually has. Without this the exponential saturates and the sky is painted flat L.
    zc = ng.nodes.new("ShaderNodeMath"); zc.location = (-300, -320)
    zc.operation = 'MINIMUM'; zc.inputs[1].default_value = float(HAZE_ZMAX)
    ng.links.new(zs, zc.inputs[0])

    # Per channel and explicit rather than one vector op: the fit is per channel, and a Color fed
    # into a Vector socket would go through an implicit conversion that drops alpha.
    sep = ng.nodes.new("CompositorNodeSeparateColor"); sep.location = (-300, 180)
    ng.links.new(src, sep.inputs["Image"])
    comb = ng.nodes.new("CompositorNodeCombineColor"); comb.location = (30, 180)
    ng.links.new(sep.outputs["Alpha"], comb.inputs["Alpha"])
    for i, ch in enumerate(("Red", "Green", "Blue")):
        mz = ng.nodes.new("ShaderNodeMath"); mz.location = (-190, -400 - i * 130)
        mz.operation = 'MULTIPLY'; mz.inputs[1].default_value = -float(HAZE_SIGMA[i])
        ng.links.new(zc.outputs[0], mz.inputs[0])
        tr = ng.nodes.new("ShaderNodeMath"); tr.location = (-60, -400 - i * 130)
        tr.operation = 'EXPONENT'                       # T_c = exp(-sigma_c * z)
        ng.links.new(mz.outputs[0], tr.inputs[0])
        df = ng.nodes.new("ShaderNodeMath"); df.location = (-190, 120 - i * 130)
        df.operation = 'SUBTRACT'; df.inputs[1].default_value = float(HAZE_INSCATTER[i])
        ng.links.new(sep.outputs[ch], df.inputs[0])
        ma = ng.nodes.new("ShaderNodeMath"); ma.location = (-60, 120 - i * 130)
        ma.operation = 'MULTIPLY_ADD'; ma.inputs[2].default_value = float(HAZE_INSCATTER[i])
        ng.links.new(df.outputs[0], ma.inputs[0])
        ng.links.new(tr.outputs[0], ma.inputs[1])
        ng.links.new(ma.outputs[0], comb.inputs[ch])
    return comb.outputs["Image"]


def _compositor(scene, haze=False):
    """Veiling glare and lateral CA, photoreal only. Returns the node group, or None.

    THE 5.1 API MOVED, three times, and every trap is silent. `scene.node_tree` is gone and so is
    CompositorNodeComposite (hasattr is False): the pointer is now `scene.compositing_node_group`
    and it takes a CompositorNodeTree with a Group Input and a Group Output. Second, Glare's and
    Lens Distortion's parameters are INPUT SOCKETS now, not RNA properties - `node.glare_type`
    does not exist, it is `node.inputs["Type"]` on a menu socket that takes TITLE-CASE strings
    ('Bloom', 'Ghosts', 'Streaks', 'Fog Glow', 'Simple Star'). 'FOG_GLOW' raises.

    THIRD, AND IT COSTS A WHOLE FRAME: the group's INPUT SOCKET IS NOT THE RENDER RESULT. The
    beauty pass has to be pulled in by a CompositorNodeRLayers node INSIDE the group. Feed the
    chain from the Group Input instead and the render is not merely uncomposited - it never runs:
    measured at 480x270, `Group Input -> Group Output` returned in 0.03 s with every pixel exactly
    0.0, against 6.26 s and a correct frame for `Render Layers -> Group Output` (identical to the
    no-compositor render to 6 decimal places, mean 0.084657). With the full chain the frame comes
    back at mean 0.08588, i.e. the Fog Glow adds 1.4% of total energy, which is the number the
    veiling-glare calibration predicts. Nothing warns; MODE='photoreal' just writes a black EXR.

    compositor_device stays on CPU deliberately: OptiX already owns both GPUs for the path trace,
    and Fog Glow measured 0.26 s on the CPU against 0.50-0.74 s on the GPU. The whole chain is
    +0.24 s on a 34.8 s frame, i.e. +0.7%.
    """
    if not hasattr(scene, "compositing_node_group"):
        print("[example] no scene.compositing_node_group on this build; skipping comp")
        return None
    ng = bpy.data.node_groups.new("eft_photoreal_comp", "CompositorNodeTree")
    ng.interface.new_socket("Image", in_out='INPUT', socket_type='NodeSocketColor')
    ng.interface.new_socket("Image", in_out='OUTPUT', socket_type='NodeSocketColor')
    ng.nodes.new("NodeGroupInput").location = (-620, -220)   # unused; see the docstring
    go = ng.nodes.new("NodeGroupOutput"); go.location = (400, 0)
    rl = ng.nodes.new("CompositorNodeRLayers"); rl.location = (-400, 0)
    rl.scene = scene                                 # the beauty pass; the group input is NOT it

    src = rl.outputs["Image"]
    if haze:
        src = _depth_haze_nodes(ng, rl, src)      # the atmosphere, before the lens sees it

    gl = ng.nodes.new("CompositorNodeGlare"); gl.location = (-150, 0)
    gl.inputs["Type"].default_value = "Fog Glow"
    gl.inputs["Quality"].default_value = "High"
    gl.inputs["Threshold"].default_value = 0.0       # a lens has no threshold; see the docstring
    gl.inputs["Strength"].default_value = GLARE_STRENGTH
    gl.inputs["Size"].default_value = 1.0
    ng.links.new(src, gl.inputs["Image"])

    ca = ng.nodes.new("CompositorNodeLensdist"); ca.location = (120, 0)
    ca.inputs["Type"].default_value = "Radial"
    ca.inputs["Distortion"].default_value = 0.0      # barrel only in 5.1, and it is invented
    ca.inputs["Dispersion"].default_value = CA_DISPERSION
    ng.links.new(gl.outputs["Image"], ca.inputs["Image"])
    ng.links.new(ca.outputs["Image"], go.inputs[0])

    scene.compositing_node_group = ng
    _try(scene.render, "compositor_device", 'CPU')
    return ng


def _try(obj, attr, value):
    try:
        setattr(obj, attr, value)
    except Exception:
        pass


def _sock(node, name, idx):
    """A socket by name, falling back to index. Blender renames sockets between versions and the
    Mix node in particular has four same-named pairs, so neither lookup alone is safe."""
    try:
        return node.inputs[name]
    except (KeyError, TypeError):
        return node.inputs[idx]


def _cloud_nodes(nt, sun_dir, sky_col, x0=-1500, y0=-620):
    """A cloud LAYER for the camera-ray backdrop. Returns a Color socket.

    THESE VALUES ARE INVENTED. The pack ships no cloud data - its sky is a 128 px/face capture whose
    clouds are an unrecoverable blur - so nothing here is derived from the game and this function
    runs on the photoreal path only. It is the mode that departs from the game deliberately; this is
    a departure, and this paragraph is the disclosure the repo's own rule asks for.

    WHY A PLANE AND NOT A DOME. Mapping noise onto the sky direction is the standard shortcut and it
    is instantly readable as fake: the cloud cells stay the same angular size all the way to the
    horizon, so the sky looks like a painted ceiling. Real clouds live on a roughly flat layer a
    kilometre or two up, so their apparent size falls off and they CROWD TOGETHER at the horizon.
    That is the single strongest cue, and it costs one ray-plane intersection:

        rd = -Incoming                     the view ray, pointing away from the camera
        t  = CLOUD_ALTITUDE / rd.z         where that ray pierces the cloud layer
        p  = rd.xy * t                     the point on the layer, in metres

    rd.z is clamped away from zero because a ray at the horizon meets the layer at infinity; the
    same clamp is what CLOUD_FADE then hides, so the layer dissolves into haze instead of smearing
    into infinitely stretched streaks.

    Lighting the clouds is faked deliberately and cheaply. A second noise sample, offset along the
    sun's azimuth, differenced against the first gives relief that brightens the sun-facing side of
    every cell - which is what actually reads as "lit cloud" - and a sun-proximity term adds the
    silver lining near the disc. No volume, no extra bounces: this is a background shader and it
    costs texture lookups, not path tracing.
    """
    geo = nt.nodes.new("ShaderNodeNewGeometry"); geo.location = (x0, y0)
    # Incoming points from the shaded point back toward the camera, so the view ray is its negative.
    rd = nt.nodes.new("ShaderNodeVectorMath"); rd.location = (x0 + 180, y0)
    rd.operation = 'SCALE'
    nt.links.new(geo.outputs["Incoming"], rd.inputs[0])
    _sock(rd, "Scale", 3).default_value = -1.0

    sep = nt.nodes.new("ShaderNodeSeparateXYZ"); sep.location = (x0 + 360, y0)
    nt.links.new(rd.outputs["Vector"], sep.inputs[0])

    # Clamp the ray's climb: at the horizon it never reaches the layer at all.
    zc = nt.nodes.new("ShaderNodeMath"); zc.location = (x0 + 540, y0 - 120)
    zc.operation = 'MAXIMUM'
    nt.links.new(sep.outputs["Z"], zc.inputs[0])
    zc.inputs[1].default_value = 0.030

    t = nt.nodes.new("ShaderNodeMath"); t.location = (x0 + 720, y0 - 120)
    t.operation = 'DIVIDE'
    t.inputs[0].default_value = CLOUD_ALTITUDE
    nt.links.new(zc.outputs[0], t.inputs[1])

    flat = nt.nodes.new("ShaderNodeCombineXYZ"); flat.location = (x0 + 540, y0 + 80)
    nt.links.new(sep.outputs["X"], flat.inputs["X"])
    nt.links.new(sep.outputs["Y"], flat.inputs["Y"])
    flat.inputs["Z"].default_value = 0.0

    hit = nt.nodes.new("ShaderNodeVectorMath"); hit.location = (x0 + 900, y0)
    hit.operation = 'SCALE'
    nt.links.new(flat.outputs["Vector"], hit.inputs[0])
    nt.links.new(t.outputs[0], _sock(hit, "Scale", 3))

    # Into layer units, and offset so the camera does not sit under a fixed noise feature.
    uv = nt.nodes.new("ShaderNodeVectorMath"); uv.location = (x0 + 1080, y0)
    uv.operation = 'SCALE'
    nt.links.new(hit.outputs["Vector"], uv.inputs[0])
    _sock(uv, "Scale", 3).default_value = 1.0 / CLOUD_SCALE

    def _noise(loc, vec_socket):
        n = nt.nodes.new("ShaderNodeTexNoise"); n.location = loc
        nt.links.new(vec_socket, n.inputs["Vector"])
        n.inputs["Scale"].default_value = 1.0        # scale lives in CLOUD_SCALE, in metres
        n.inputs["Detail"].default_value = CLOUD_DETAIL
        n.inputs["Roughness"].default_value = CLOUD_ROUGHNESS
        for k, v in (("Lacunarity", 2.0), ("Distortion", CLOUD_DISTORTION)):
            if k in n.inputs:
                n.inputs[k].default_value = v
        return n

    main_n = _noise((x0 + 1260, y0 + 120), uv.outputs["Vector"])

    # The sun-offset sample, for relief.
    sun_xy = mathutils.Vector((sun_dir.x, sun_dir.y, 0.0))
    if sun_xy.length > 1e-6:
        sun_xy.normalize()
    off = nt.nodes.new("ShaderNodeVectorMath"); off.location = (x0 + 1260, y0 - 160)
    off.operation = 'ADD'
    nt.links.new(uv.outputs["Vector"], off.inputs[0])
    off.inputs[1].default_value = (sun_xy.x * CLOUD_RELIEF_STEP,
                                   sun_xy.y * CLOUD_RELIEF_STEP, 0.0)
    lit_n = _noise((x0 + 1440, y0 - 160), off.outputs["Vector"])

    # COVERAGE. A ramp, not a threshold: real cloud edges are soft over tens of metres and a hard
    # cut reads as a paper cutout at any resolution.
    cov = nt.nodes.new("ShaderNodeMapRange"); cov.location = (x0 + 1450, y0 + 120)
    cov.interpolation_type = 'SMOOTHSTEP'
    nt.links.new(main_n.outputs["Fac"], cov.inputs["Value"])
    _sock(cov, "From Min", 1).default_value = CLOUD_COVERAGE
    _sock(cov, "From Max", 2).default_value = CLOUD_COVERAGE + CLOUD_SOFT
    _sock(cov, "To Min", 3).default_value = 0.0
    _sock(cov, "To Max", 4).default_value = CLOUD_OPACITY

    # HORIZON FADE. The layer must dissolve before the clamp above turns it into streaks.
    fade = nt.nodes.new("ShaderNodeMapRange"); fade.location = (x0 + 1450, y0 - 380)
    fade.interpolation_type = 'SMOOTHSTEP'
    nt.links.new(sep.outputs["Z"], fade.inputs["Value"])
    _sock(fade, "From Min", 1).default_value = CLOUD_FADE[0]
    _sock(fade, "From Max", 2).default_value = CLOUD_FADE[1]
    _sock(fade, "To Min", 3).default_value = 0.0
    _sock(fade, "To Max", 4).default_value = 1.0

    fac = nt.nodes.new("ShaderNodeMath"); fac.location = (x0 + 1640, y0 - 120)
    fac.operation = 'MULTIPLY'
    nt.links.new(cov.outputs["Result"], fac.inputs[0])
    nt.links.new(fade.outputs["Result"], fac.inputs[1])

    # RELIEF: where the layer thickens toward the sun, that face is lit.
    rel = nt.nodes.new("ShaderNodeMath"); rel.location = (x0 + 1640, y0 + 260)
    rel.operation = 'SUBTRACT'
    nt.links.new(lit_n.outputs["Fac"], rel.inputs[0])
    nt.links.new(main_n.outputs["Fac"], rel.inputs[1])
    relm = nt.nodes.new("ShaderNodeMapRange"); relm.location = (x0 + 1820, y0 + 260)
    relm.interpolation_type = 'SMOOTHSTEP'
    nt.links.new(rel.outputs[0], relm.inputs["Value"])
    _sock(relm, "From Min", 1).default_value = -CLOUD_RELIEF_GAIN
    _sock(relm, "From Max", 2).default_value = CLOUD_RELIEF_GAIN
    _sock(relm, "To Min", 3).default_value = 0.0
    _sock(relm, "To Max", 4).default_value = 1.0

    # DENSITY, separately from coverage. Coverage decides where cloud IS; density decides how thick
    # it is once it is there, and thickness is most of why a cloud top is white while its edge is
    # grey. Driving brightness from relief alone was the first version's mistake: it gave every cell
    # the same mid-grey body, so thick cloud read as a dirty smudge sitting on a brighter sky.
    dens = nt.nodes.new("ShaderNodeMapRange"); dens.location = (x0 + 1450, y0 + 400)
    dens.interpolation_type = 'SMOOTHSTEP'
    nt.links.new(main_n.outputs["Fac"], dens.inputs["Value"])
    _sock(dens, "From Min", 1).default_value = CLOUD_COVERAGE
    _sock(dens, "From Max", 2).default_value = CLOUD_COVERAGE + CLOUD_DENSITY_SPAN
    # INVERTED, and this is the correction that made the clouds read at all. Thin cloud transmits,
    # so an edge is BRIGHT; a thick base seen from below is starved of light and is DARK. Mapping
    # brightness up with density did the opposite, and because alpha ramps from the same threshold
    # every edge that was thin enough to actually see was also the darkest thing in frame - which is
    # why sparse cover looked like soot smudges and heavy cover looked like a flat white sheet.
    _sock(dens, "To Min", 3).default_value = 1.0
    _sock(dens, "To Max", 4).default_value = 0.0

    w_rel = nt.nodes.new("ShaderNodeMath"); w_rel.location = (x0 + 1820, y0 + 460)
    w_rel.operation = 'MULTIPLY'
    nt.links.new(relm.outputs["Result"], w_rel.inputs[0])
    w_rel.inputs[1].default_value = CLOUD_RELIEF_WEIGHT
    w_den = nt.nodes.new("ShaderNodeMath"); w_den.location = (x0 + 1820, y0 + 400)
    w_den.operation = 'MULTIPLY'
    nt.links.new(dens.outputs["Result"], w_den.inputs[0])
    w_den.inputs[1].default_value = 1.0 - CLOUD_RELIEF_WEIGHT
    body_fac = nt.nodes.new("ShaderNodeMath"); body_fac.location = (x0 + 2000, y0 + 430)
    body_fac.operation = 'ADD'
    body_fac.use_clamp = True
    nt.links.new(w_rel.outputs[0], body_fac.inputs[0])
    nt.links.new(w_den.outputs[0], body_fac.inputs[1])

    body = nt.nodes.new("ShaderNodeMix"); body.location = (x0 + 2180, y0 + 260)
    body.data_type = 'RGBA'
    nt.links.new(body_fac.outputs[0], _sock(body, "Factor", 0))
    _sock(body, "A", 6).default_value = tuple(CLOUD_SHADOW) + (1.0,)
    _sock(body, "B", 7).default_value = tuple(CLOUD_LIT) + (1.0,)

    # SILVER LINING near the sun, which is where a real cloud deck is brightest and thinnest.
    dot = nt.nodes.new("ShaderNodeVectorMath"); dot.location = (x0 + 1260, y0 - 420)
    dot.operation = 'DOT_PRODUCT'
    nt.links.new(rd.outputs["Vector"], dot.inputs[0])
    dot.inputs[1].default_value = (sun_dir.x, sun_dir.y, sun_dir.z)
    silver = nt.nodes.new("ShaderNodeMapRange"); silver.location = (x0 + 1450, y0 - 620)
    silver.interpolation_type = 'SMOOTHSTEP'
    nt.links.new(dot.outputs["Value"], silver.inputs["Value"])
    _sock(silver, "From Min", 1).default_value = CLOUD_SILVER_ARC[0]
    _sock(silver, "From Max", 2).default_value = CLOUD_SILVER_ARC[1]
    _sock(silver, "To Min", 3).default_value = 0.0
    _sock(silver, "To Max", 4).default_value = 1.0

    hot = nt.nodes.new("ShaderNodeMix"); hot.location = (x0 + 2180, y0 + 120)
    hot.data_type = 'RGBA'
    nt.links.new(silver.outputs["Result"], _sock(hot, "Factor", 0))
    nt.links.new(body.outputs[2], _sock(hot, "A", 6))
    _sock(hot, "B", 7).default_value = tuple(CLOUD_SILVER) + (1.0,)

    # Composite over the sky. Clouds are OPAQUE where they are thick, so this is a plain mix.
    over = nt.nodes.new("ShaderNodeMix"); over.location = (x0 + 2360, y0)
    over.data_type = 'RGBA'
    nt.links.new(fac.outputs[0], _sock(over, "Factor", 0))
    nt.links.new(sky_col, _sock(over, "A", 6))
    nt.links.new(hot.outputs[2], _sock(over, "B", 7))
    return over.outputs[2]


def _horizon_blend(nt, col, x0=520, y0=-560):
    """Dissolve the backdrop into the scene's own aerial perspective at the horizon.

    Two problems, one fix. Blender's sky renders a dim GROUND below the horizon, so anywhere the
    map's terrain does not reach - and on a 150 m vista it often does not - the frame ends in a hard
    dark blue band that reads as the world running out. And even above the horizon, a sky that stays
    saturated all the way down disagrees with the haze everything else is fading into.

    HAZE_INSCATTER is what this scene's fitted volume converges to at infinite distance, so it is
    also what the sky must converge to at the horizon: it is the SAME colour a distant ridge becomes.
    That makes this derived rather than invented - the number comes from the volume fit, not from
    taste - and it is the one place the backdrop and the depth haze are made to agree.

    `col` must already carry the backdrop's strength (see build_sky_world), because HAZE_INSCATTER is
    scene-referred and mixing it in ahead of a strength multiply would scale the fitted colour.
    """
    geo = nt.nodes.new("ShaderNodeNewGeometry"); geo.location = (x0, y0)
    rd = nt.nodes.new("ShaderNodeVectorMath"); rd.location = (x0 + 170, y0)
    rd.operation = 'SCALE'
    nt.links.new(geo.outputs["Incoming"], rd.inputs[0])
    _sock(rd, "Scale", 3).default_value = -1.0
    sep = nt.nodes.new("ShaderNodeSeparateXYZ"); sep.location = (x0 + 340, y0)
    nt.links.new(rd.outputs["Vector"], sep.inputs[0])

    f = nt.nodes.new("ShaderNodeMapRange"); f.location = (x0 + 510, y0)
    f.interpolation_type = 'SMOOTHSTEP'
    nt.links.new(sep.outputs["Z"], f.inputs["Value"])
    _sock(f, "From Min", 1).default_value = HORIZON_HAZE[0]
    _sock(f, "From Max", 2).default_value = HORIZON_HAZE[1]
    _sock(f, "To Min", 3).default_value = 0.0
    _sock(f, "To Max", 4).default_value = 1.0

    mix = nt.nodes.new("ShaderNodeMix"); mix.location = (x0 + 690, y0)
    mix.data_type = 'RGBA'
    nt.links.new(f.outputs["Result"], _sock(mix, "Factor", 0))
    _sock(mix, "A", 6).default_value = tuple(HAZE_INSCATTER) + (1.0,)   # at and below the horizon
    nt.links.new(col, _sock(mix, "B", 7))                              # the sky proper, above it
    return mix.outputs[2]


def build_sky_world(nt, sun_dir, backdrop="pack"):
    """Wire a world node tree's sky. `sun_dir` is BLENDER-space and already normalised.

    Two roles, and the whole point of this function is that they are separable:

      LIGHTING (`backdrop` anything) - the pack cubemap at the fitted SKY_STRENGTH. Every ray that
      carries light terminates here: diffuse, glossy, transmission, shadow. This is what SUN_ENERGY
      and SKY_STRENGTH were least-squares fitted against, so it does not change between modes and
      reflections keep mirroring the sky the rest of the lighting came from.

      BACKDROP (`backdrop="physical"`) - what the LENS sees, split off with Light Path > Is Camera
      Ray. The pack sky is a 128 px/face reflection capture with a photographic treeline baked into
      its horizon; it is the right thing to light with and the wrong thing to look at. Because this
      branch sits behind Is Camera Ray it cannot move the lighting solve BY CONSTRUCTION - verified
      bit-identical by tools/blender/fit_sky_backdrop.py, not merely argued from the graph shape.

    Shared by example_scene.py and the sky lab so that what is previewed is what renders.
    """
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputWorld"); out.location = (400, 0)

    bg = nt.nodes.new("ShaderNodeBackground"); bg.location = (150, 100)
    bg.inputs["Strength"].default_value = SKY_STRENGTH
    if os.path.isfile(SKY_EQUIRECT):
        env = nt.nodes.new("ShaderNodeTexEnvironment"); env.location = (-200, 100)
        env.image = bpy.data.images.load(SKY_EQUIRECT, check_existing=True)
        nt.links.new(env.outputs["Color"], bg.inputs["Color"])
    else:
        bg.inputs["Color"].default_value = (0.36, 0.40, 0.47, 1.0)
        print("[example] no sky equirect; run tools/blender/make_sky_equirect.py first")

    if backdrop != "physical":
        nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
        return out

    cam_bg = nt.nodes.new("ShaderNodeBackground"); cam_bg.location = (150, -160)
    # Left at 1.0 deliberately. The backdrop's strength is folded into its COLOUR further down, so
    # that the horizon handover can mix in a scene-referred colour after it; see the `gain` node.
    cam_bg.inputs["Strength"].default_value = 1.0
    sky = nt.nodes.new("ShaderNodeTexSky"); sky.location = (-200, -160)
    # 5.x renamed Nishita to MULTIPLE_SCATTERING. Take the first spelling this build accepts and
    # STOP - trying both in sequence would leave whichever came last, not whichever worked.
    for val in ('MULTIPLE_SCATTERING', 'NISHITA'):
        try:
            sky.sky_type = val
            break
        except (TypeError, ValueError):
            continue
    # NO SOLAR DISC by default, and the reason here is NOT the one in the docstring. That one - a
    # disc costs +37% seed-to-seed noise for a uniform -3.4% in the image - is about a disc in the
    # ENVIRONMENT, where Cycles importance-samples it as a light; behind Is Camera Ray no light path
    # terminates here at all, so that objection genuinely does not apply and the disc is free.
    #
    # It was tried, and it was rejected on a NEW measurement. A 0.526 degree disc carries enormous
    # radiance, so although it covers well under one pixel of the fit's 128x64 hemisphere it moved
    # the hemisphere MEAN by 15% (1.3217 -> 1.5216) on its own. Energy-matching then pays for that
    # single pixel by darkening the entire visible sky 14% (BACKDROP_RATIO 0.1497 -> 0.1287). One
    # outlier hijacking the statistic that sets everything else is a bad trade for a disc that the
    # cloud deck covers most of the time anyway. EFT_SUN_DISC=1 draws it; re-fit if you do.
    _try(sky, "sun_disc", os.environ.get("EFT_SUN_DISC", "0") != "0")
    # Point it at the PACK'S OWN sun, not Blender's default, or the bright quarter of the sky sits
    # somewhere the shadows disagree with.
    _try(sky, "sun_elevation", math.asin(max(-1.0, min(1.0, sun_dir.z))))
    _try(sky, "sun_rotation", math.atan2(sun_dir.x, -sun_dir.y))

    sky_col = sky.outputs["Color"]
    if os.environ.get("EFT_CLOUDS", "1") != "0":
        sky_col = _cloud_nodes(nt, sun_dir, sky_col)

    # THE STRENGTH IS BAKED INTO THE COLOUR, and the Background node is left at 1.0. It has to be:
    # the horizon blend below mixes toward HAZE_INSCATTER, which is a scene-referred linear value,
    # and anything mixed in BEFORE a strength multiply would come out scaled by 0.294 instead of
    # landing on the fitted colour. Doing it in this order also decouples the two, so re-fitting
    # BACKDROP_RATIO does not silently move the horizon colour.
    s = SKY_STRENGTH * BACKDROP_RATIO
    gain = nt.nodes.new("ShaderNodeMix"); gain.location = (-40, -300)
    gain.data_type = 'RGBA'
    gain.blend_type = 'MULTIPLY'
    _sock(gain, "Factor", 0).default_value = 1.0
    nt.links.new(sky_col, _sock(gain, "A", 6))
    _sock(gain, "B", 7).default_value = (s, s, s, 1.0)
    sky_col = gain.outputs[2]

    sky_col = _horizon_blend(nt, sky_col)
    cam_bg.inputs["Strength"].default_value = 1.0
    nt.links.new(sky_col, cam_bg.inputs["Color"])

    mix = nt.nodes.new("ShaderNodeMixShader"); mix.location = (280, 0)
    lp = nt.nodes.new("ShaderNodeLightPath"); lp.location = (-200, 300)
    # Fac 0 -> lighting sky, Fac 1 -> backdrop. Is Camera Ray is 1 only on the primary ray.
    nt.links.new(lp.outputs["Is Camera Ray"], mix.inputs["Fac"])
    nt.links.new(bg.outputs["Background"], mix.inputs[1])
    nt.links.new(cam_bg.outputs["Background"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    return out


def build(mode=None):
    mode = (mode or MODE).lower()
    if mode not in MODES:
        raise SystemExit("MODE must be one of %s, got %r" % (sorted(MODES), mode))
    cfg = MODES[mode]
    print("[example] MODE = %s  %s" % (mode, cfg))

    scene = bpy.context.scene
    _clear()

    pts = _patrol(PACK, PATROL_ZONE, PATROL_SPAN)
    centre_bl = sum(pts, mathutils.Vector()) / len(pts)
    centre_pack = (centre_bl.x, centre_bl.z, -centre_bl.y)          # back to pack Y-up
    route_pack = [(p.x, p.z, -p.y) for p in pts]                    # the routed walk, pack Y-up

    # 1. map -------------------------------------------------------------------------------
    gm = _load("import_eftpack.py")
    for c in list(bpy.data.collections):                            # its demo import
        if c.name.startswith("eftpack_"):
            for o in list(c.objects):
                bpy.data.objects.remove(o, do_unlink=True)
            bpy.data.collections.remove(c)
    mapres = gm["import_eftpack"](PACK, center=centre_pack, radius=MAP_RADIUS,
                                  with_textures=True, collection_name="map",
                                  glass_mode=cfg["glass_mode"],
                                  cavity_dir=(CAVITY_DIR if cfg["cavity"]
                                              and os.path.isdir(CAVITY_DIR) else None))

    # 2. terrain: the real splat, not the 5.9 texel/m baked slice --------------------------
    _load("terrain_splat.py")["apply_to_scene"](DATASET)

    # 3. grass, distance-limited along the ROUTE ---------------------------------------------
    # A capsule about the routed polyline, not a disc about its centroid: on a 24.6 m walk the
    # actor spends most of the shot outside a centroid disc, so a disc puts the grass where the
    # camera is not. The viewer culls grass by projected screen size, so any static radius is an
    # approximation already and following the route is strictly the closer one.
    # `wind` is not a departure - it is the game's own WavingGrass stage with the sidecar's own
    # constants (strength 1.0, amount 0.157, speed 1.0), which this path used to discard. Without
    # it the field is frozen for all 360 frames while the scav walks through it, and the sway
    # moves half the vertices by a mean of 0.15 m on a 0.9 m blade.
    grass = _load("import_eftgrass.py")["import_eftgrass"](
        PACK, points=route_pack, radius=GRASS_RADIUS, max_clumps=120000, wind=True)
    for o in grass:
        o.visible_shadow = False        # the viewer keeps grass out of the shadow pass

    # 3b. practical lights ------------------------------------------------------------------
    # The sun lights the outdoors and nothing else. Without these every interior is black, and
    # a lit room seen through a window collapses to a flat dark rectangle that looks like a
    # broken glass material. Parented to the map root, so positions stay in pack space.
    _load("import_eftlights.py")["import_eftlights"](
        PACK, parent=mapres["empty"], center=centre_pack, radius=MAP_RADIUS)

    # 4. character + weapon -----------------------------------------------------------------
    coll = bpy.data.collections.new("actor"); scene.collection.children.link(coll)
    man = json.load(open(os.path.join(CHARACTER, "manifest.json"), encoding="utf-8"))
    clip = next(c for c in man["clips"] if c["name"] == CLIP)
    speed = abs(clip["averageSpeed"][2])
    fps = int(round(clip["sampleRate"])); scene.render.fps = fps
    seg = [(pts[i + 1] - pts[i]).length for i in range(len(pts) - 1)]
    dur = sum(seg) / speed
    res = _load("import_eftchar.py")["import_eftchar"](
        CHARACTER, clip_name=CLIP, loops=int(math.ceil(dur / clip["duration"])) + 1,
        apply_root_motion=False, collection=coll)
    arm = res[0] if isinstance(res, (tuple, list)) else res
    _load("import_eftweap.py")["import_eftweap"](WEAPON, armature=arm, bone="Weapon_root",
                                                 collection=coll)

    # 5. walk the patrol, resolving the ground the way the viewer does ----------------------
    f0, f1 = 1, 1 + int(round(dur * fps))
    dg = bpy.context.evaluated_depsgraph_get()
    arm.rotation_mode = 'XYZ'
    prev_z = pts[0].z
    def _at(s):
        """Point at arc length `s` along the routed polyline."""
        acc = 0.0
        for i, L in enumerate(seg):
            if s <= acc + L:
                return pts[i].lerp(pts[i + 1], (s - acc) / L)
            acc += L
        return pts[-1]

    for fr in range(f0, f1 + 1, 3):
        s = (fr - f0) / fps * speed
        P = _at(s)
        # Heading from a LOOKAHEAD, not from the current segment. A grid route is a chain of 0.5 m
        # steps locked to 8 directions, so a per-segment heading makes the character snap between
        # 45-degree facings every few frames. Aiming at a point ~1.5 m ahead averages the staircase
        # out without moving him off the route.
        D = _at(s + 1.5) - P
        if D.length < 1e-4:
            D = pts[-1] - pts[-2]
        D = D.normalized()
        z = _ground_height(scene, dg, arm, P.x, P.y, prev_z)
        z = P.z if z is None else z
        prev_z = z
        arm.location = (P.x, P.y, z)
        # (pi/2, 0, yaw): the X term is the Y-up -> Z-up stand-up the importer applied, and it
        # must be COMPOSED with the yaw, not replaced by it, or the character lies on its back.
        arm.rotation_euler = (math.pi / 2, 0.0, math.atan2(D.x, -D.y))
        arm.keyframe_insert("location", frame=fr)
        arm.keyframe_insert("rotation_euler", frame=fr)
    scene.frame_start, scene.frame_end = f0, f1

    # 6. world, sun, bounded atmosphere ------------------------------------------------------
    w = scene.world or bpy.data.worlds.new("World"); scene.world = w
    w.use_nodes = True; nt = w.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    volj = os.path.join(PACK, "volume.json")
    sd = [0.449, 0.799, -0.400]
    if os.path.isfile(volj):
        sd = json.load(open(volj, encoding="utf-8")).get("sun_dir", sd)
    sun_dir = mathutils.Vector((sd[0], -sd[2], sd[1])).normalized()
    # EFT_BACKDROP=pack|physical overrides the mode's choice, so the A/B is one env var and not an
    # edit. This is the only sky knob; there is deliberately no strength override, because the
    # strength is a measurement and a hand-set one would silently invalidate the exposure match.
    build_sky_world(nt, sun_dir,
                    backdrop=(os.environ.get("EFT_BACKDROP") or cfg.get("backdrop", "pack")))

    ld = bpy.data.lights.new("eft_sun", 'SUN'); ld.energy = SUN_ENERGY
    ld.angle = math.radians(0.526)              # the sun's real angular diameter
    sun = bpy.data.objects.new("eft_sun", ld); scene.collection.objects.link(sun)
    sun.rotation_euler = (-sun_dir).to_track_quat('-Z', 'Y').to_euler()

    # A Cycles WORLD volume is unbounded and renders the frame black. Bound it.
    import bmesh
    me = bpy.data.meshes.new("eft_atmosphere")
    bm = bmesh.new(); bmesh.ops.create_cube(bm, size=1.0); bm.to_mesh(me); bm.free()
    atm = bpy.data.objects.new("eft_atmosphere", me); scene.collection.objects.link(atm)
    atm.location = centre_bl + mathutils.Vector((0, 0, ATM_LIFT))
    atm.scale = mathutils.Vector((MAP_RADIUS * 2.2, MAP_RADIUS * 2.2, ATM_HEIGHT))
    atm.display_type = 'WIRE'
    am = bpy.data.materials.new("eft_atmosphere"); am.use_nodes = True
    ant = am.node_tree
    for n in list(ant.nodes):
        ant.nodes.remove(n)
    _atmosphere_nodes(ant, cfg["atmosphere"],
                      ATM_LIFT - ATM_HEIGHT * 0.5, ATM_LIFT + ATM_HEIGHT * 0.5)
    me.materials.append(am)

    # PHOTOREAL: built, sized and shaded exactly as above, and then kept out of the beauty pass.
    # It stays in the .blend and in the viewport, so the shape it describes is still readable and
    # still the thing HAZE_SIGMA was fitted to; it is just not worth 49.9% of a frame to trace.
    # use_pass_z must be set HERE, not in step 8: Render Layers only grows the Depth output once
    # the pass is on, and _compositor builds that node.
    # The `and cfg["comp"]` is not belt and braces: the haze lives in the compositor group, so
    # hiding the box with the compositor off would delete the atmosphere outright and silently,
    # at a measured -7.0% on the frame mean. Hidden and unapplied is the one combination that is
    # worse than either end of the choice.
    if cfg.get("haze_post") and cfg["comp"]:
        atm.hide_render = True
        for vl in scene.view_layers:
            vl.use_pass_z = True

    # 7. following camera --------------------------------------------------------------------
    # Solved as one continuous move (see cine_camera.py): visibility of the whole body, both sides
    # of the subject, and smoothness are minimised together rather than patched frame by frame.
    # Solve the camera only over the frames actually being shot. The walk keys stay full length,
    # so the shot can be moved or extended by re-solving, but solving all of it is the single
    # slowest thing in this script and it cannot use the GPU: it is one Python thread doing
    # frame_set + ray_cast + keyframe_insert. On a 113 s patrol that is ~1.1 M visibility rays and
    # 10 k key inserts to produce a 12 s shot - about 20 minutes of work, 90% of it discarded.
    shot_end = min(f1, f0 + int(round(SHOT_SECONDS * fps)) - 1)
    cam = _load("cine_camera.py")["solve_follow_camera"](
        scene, arm, f0, shot_end, blades=cfg["aperture_blades"])
    scene.frame_end = shot_end          # keep the scene self-consistent with the solved camera

    # 8. Cycles, and the optics that live outside the shader ----------------------------------
    scene.render.engine = 'CYCLES'
    enable_cycles_gpu()
    scene.cycles.device = 'GPU'
    scene.cycles.samples = cfg["samples"]
    scene.cycles.use_denoising = True
    scene.cycles.volume_bounces = 1
    # TRANSPARENT BOUNCES, and this is not a quality knob - at the default it DELETES pixels.
    # `transparent_bounce` is a WHOLE-PATH counter, not a per-segment one: the camera segment, every
    # diffuse bounce that follows it and the shadow ray toward the sun all draw from the same
    # budget. Every leaf, blade and chain-link in this scene is alpha-tested, so a ray that grazes
    # the grass field or crosses a bush spends the budget on transparency alone - and Cycles fails
    # CLOSED. A camera ray that runs out is TERMINATED and returns black; a shadow ray that runs out
    # is reported FULLY OCCLUDED. Neither warns.
    # MEASURED on this scene's grass at the Cycles default of 8: 14.78% of the frame is EXACTLY
    # 0.0 luma and the mean is 12.2% low. 32 still leaves 0.48% black. 256 is BIT-IDENTICAL to the
    # 1024 maximum, so it is the cheapest value that is indistinguishable from unlimited, and it
    # costs 2.0 -> 2.3 s per frame. A transparent bounce does no shading; this is the least
    # expensive fidelity in the whole file.
    scene.cycles.transparent_max_bounces = 256
    scene.render.resolution_x, scene.render.resolution_y = 1280, 720
    # Both modes shoot FLAT linear EXR; the look is applied afterwards by eft_grade.py, which is
    # also where the two modes' display chains part company (game LUT vs filmic + centre-weighted
    # metering + cos^4 + shot noise). Keeping the render scene-referred is what lets one set of
    # frames answer both questions.
    _try(scene, "compositing_node_group", None)
    if cfg["comp"]:
        _compositor(scene, haze=cfg.get("haze_post", False))   # step 6 hid the box for this
    scene.frame_set((f0 + f1) // 2)

    print("[example] MODE %s built: %d objects, frames %d..%d (%.1f s of walk), %d samples"
          % (mode, len(scene.objects), f0, f1, dur, cfg["samples"]))
    if mode == "game":
        print("[example] grade with: python tools/blender/eft_grade.py frames/ out/ --auto")
    else:
        print("[example] grade with: python tools/blender/eft_grade.py frames/ out/ --look agx "
              "--auto --meter grey --no-vignette --lens 50 --grain 15000")
    return arm, cam


if __name__ == "__main__":
    build(os.environ.get("EFT_MODE") or MODE)
