"""Fit the photoreal BACKDROP to the brightness of the pack sky it replaces, and prove it is only a
backdrop.

    blender -b --factory-startup --python tools/blender/fit_sky_backdrop.py

WHY A SPLIT AND NOT A SWAP.  The pack's sky is `NatureCubemap`, and it is a REFLECTION PROBE: 128
pixels per face, about 0.70 degrees per texel, with a photographic treeline and buildings baked into
its horizon (the viewer refuses to use it as a dome for exactly that reason - see
`viewer/src/main.rs:1639-1644`).  That is ample for what it exists to do, lighting and reflections,
and hopeless as the thing filling the top third of a photograph.

`example_scene.py` records why swapping the environment outright was rejected: a physical sky
delivers 131.4 W/m^2 against the pack's 10.42, i.e. 12.6x, and 59.6% of the frame then clips.  But
that measurement is about the sky LIGHTING THE SCENE.  What a camera ray terminates on is a separate
question, and Cycles answers the two differently through `Light Path > Is Camera Ray`.  Lighting
keeps the pack cubemap EXACTLY, so `SUN_ENERGY`/`SKY_STRENGTH` stay valid; only the pixels the lens
sees change.

WHAT THIS SCRIPT MEASURES.  It builds the REAL graph - it imports `example_scene.build_sky_world`
rather than reimplementing it - so the number it produces describes what actually ships, including
the cloud layer.  That matters: clouds are much brighter than the clear sky they cover, so adding
them moves the backdrop's mean and silently breaks an exposure match fitted without them.  Anything
that changes the backdrop's appearance requires re-running this.

The backdrop is AFFINE in `BACKDROP_RATIO`, not linear: the horizon handover mixes toward
`HAZE_INSCATTER`, a fixed scene-referred colour that does not scale with the ratio. So the fit
samples the hemisphere mean at TWO ratios, solves `mean = a + b*ratio`, and inverts it. That is
exact in one step; a ratio-of-means correction would overshoot and have to be iterated.

The comparison is made over the solid-angle-weighted upper HEMISPHERE with an equirectangular
camera, Raw view transform, so it is a property of the two skies and not of a chosen framing - see
`_hemisphere_mean` for why one rectilinear frame gives an answer that flips sign with the pitch.
"""

import json
import math
import os
import sys

import bpy
import mathutils

HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import example_scene as ex

OUT = os.path.join(ex.REPO, "packs", "shared", "sky", "backdrop_fit.json")


def _clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)


def _probe(name):
    """A mid-grey diffuse sphere: its radiance IS the world's lighting, so it is the control."""
    import bmesh
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=2.0)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (0.18, 0.18, 0.18, 1.0)
    b.inputs["Roughness"].default_value = 1.0
    me.materials.append(mat)
    return ob


def _set_world(sun_dir, backdrop):
    w = bpy.data.worlds.get("fit") or bpy.data.worlds.new("fit")
    bpy.context.scene.world = w
    w.use_nodes = True
    ex.build_sky_world(w.node_tree, sun_dir, backdrop=backdrop)
    return w


def _set_world_ratio(sun_dir, ratio):
    """Rebuild the physical backdrop at a GIVEN ratio, by moving the module constant the builder
    reads. Restored by the caller; nothing else in this process depends on it."""
    keep = ex.BACKDROP_RATIO
    ex.BACKDROP_RATIO = ratio
    try:
        return _set_world(sun_dir, "physical")
    finally:
        ex.BACKDROP_RATIO = keep


def _render_exr(tag, w, h):
    sc = bpy.context.scene
    sc.render.engine = 'CYCLES'
    sc.cycles.samples = 24
    sc.render.resolution_x, sc.render.resolution_y = w, h
    sc.render.resolution_percentage = 100
    sc.view_settings.view_transform = 'Raw'
    sc.render.image_settings.file_format = 'OPEN_EXR'
    sc.render.image_settings.color_mode = 'RGB'
    sc.render.image_settings.color_depth = '32'
    path = os.path.join(bpy.app.tempdir, "skyfit_%s.exr" % tag)
    sc.render.filepath = path[:-4]
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(path, check_existing=False)
    px = list(img.pixels)
    bpy.data.images.remove(img)
    return px


def _mean_radiance(tag):
    """Mean over a flat frame. Only valid for the lighting CONTROL, where framing is fixed."""
    px = _render_exr(tag, 160, 90)
    n = len(px) // 4
    return sum(0.2126 * px[i * 4] + 0.7152 * px[i * 4 + 1] + 0.0722 * px[i * 4 + 2]
               for i in range(n)) / max(1, n)


def _hemisphere_mean(tag, w=128, h=64):
    """Solid-angle-weighted mean radiance over the WHOLE upper hemisphere.

    A single rectilinear frame cannot answer "are these two skies equally bright", because the two
    have different angular distributions: measured level, the pack capture's dark treeline drags its
    mean down and the physical sky measures 18.9x brighter; measured 18 degrees up, the pack's bright
    upper sky wins and the same comparison inverts to 2.2x the other way. Matching on one view
    direction therefore fits the framing, not the sky.

    An equirectangular camera sees every direction at once, so this is view-independent. Rows must be
    cosine-weighted: equirect gives every row equal pixels but a row near the zenith covers far less
    solid angle than one at the horizon, and an unweighted mean over-counts the zenith badly.
    """
    sc = bpy.context.scene
    cam = sc.camera
    prev = (cam.data.type, cam.rotation_euler[:])
    cam.data.type = 'PANO'
    # The property moved between versions; set whichever exists.
    for holder in (cam.data, getattr(cam.data, "cycles", None)):
        if holder is not None and hasattr(holder, "panorama_type"):
            try:
                holder.panorama_type = 'EQUIRECTANGULAR'
            except (TypeError, ValueError):
                pass
    # Level, so the frame's vertical span is exactly horizon..zenith..horizon.
    cam.rotation_euler = (math.radians(90.0), 0.0, 0.0)
    for holder in (cam.data, getattr(cam.data, "cycles", None)):
        if holder is None:
            continue
        for attr, val in (("latitude_min", 0.0), ("latitude_max", math.pi / 2),
                          ("longitude_min", -math.pi), ("longitude_max", math.pi)):
            if hasattr(holder, attr):
                try:
                    setattr(holder, attr, val)
                except (TypeError, ValueError):
                    pass
    px = _render_exr(tag, w, h)
    _hemisphere_mean.last_lums = sorted(
        0.2126 * px[k * 4] + 0.7152 * px[k * 4 + 1] + 0.0722 * px[k * 4 + 2]
        for k in range(len(px) // 4))
    num = den = 0.0
    for j in range(h):
        # Blender's pixel rows run bottom-up, and latitude_min (the horizon) is the bottom row.
        lat = (j + 0.5) / h * (math.pi / 2)
        wgt = math.cos(lat)
        for i in range(w):
            k = (j * w + i) * 4
            lum = 0.2126 * px[k] + 0.7152 * px[k + 1] + 0.0722 * px[k + 2]
            num += lum * wgt
            den += wgt
    cam.data.type, cam.rotation_euler = prev[0], prev[1]
    return num / max(1e-9, den)


def main():
    _clear()
    sc = bpy.context.scene
    ex.enable_cycles_gpu()

    sd = [0.449, 0.799, -0.400]
    volj = os.path.join(ex.PACK, "volume.json")
    if os.path.isfile(volj):
        sd = json.load(open(volj, encoding="utf-8")).get("sun_dir", sd)
    sun_dir = mathutils.Vector((sd[0], -sd[2], sd[1])).normalized()

    cam_d = bpy.data.cameras.new("c")
    cam_d.lens = 24.0
    cam_d.sensor_fit = 'HORIZONTAL'
    cam = bpy.data.objects.new("c", cam_d)
    sc.collection.objects.link(cam)
    sc.camera = cam
    # The fit itself uses an equirectangular camera (see _hemisphere_mean) and overrides this; the
    # orientation here only matters for the lighting control at the end.
    yaw = math.atan2(sun_dir.x, -sun_dir.y)
    cam.rotation_euler = (math.radians(90.0), 0.0, yaw)

    if not os.path.isfile(ex.SKY_EQUIRECT):
        print("[fit] no equirect at %s - run make_sky_equirect.py first" % ex.SKY_EQUIRECT)
        return

    def _pct(v):
        L = _hemisphere_mean.last_lums
        return L[min(len(L) - 1, int(v * (len(L) - 1)))]

    _set_world(sun_dir, "pack")
    pack = _hemisphere_mean("pack")
    pack_p = (_pct(0.50), _pct(0.99), _pct(0.999))
    pack_blown = 100.0 * sum(1 for x in _hemisphere_mean.last_lums if x > 1.0)         / len(_hemisphere_mean.last_lums)
    # TWO measurements, because the backdrop is AFFINE in strength and not linear. The horizon
    # handover mixes toward HAZE_INSCATTER, a fixed scene-referred colour that does NOT scale with
    # BACKDROP_RATIO, so the hemisphere mean is a + b*ratio rather than b*ratio. One sample and a
    # ratio-of-means would overshoot every time and only creep toward the answer; two samples solve
    # a and b outright, and the result is exact in one step again.
    r0 = ex.BACKDROP_RATIO
    _set_world_ratio(sun_dir, r0)
    phys = _hemisphere_mean("phys")
    phys_p = (_pct(0.50), _pct(0.99), _pct(0.999))
    phys_blown = 100.0 * sum(1 for x in _hemisphere_mean.last_lums if x > 1.0)         / len(_hemisphere_mean.last_lums)
    r1 = r0 * 0.5
    _set_world_ratio(sun_dir, r1)
    phys_half = _hemisphere_mean("phys_half")
    b = (phys - phys_half) / max(1e-12, (r0 - r1))       # d(mean)/d(ratio)
    a = phys - b * r0                                     # the fixed haze contribution
    _set_world_ratio(sun_dir, r0)                         # leave the world as it ships

    new_ratio = (pack - a) / b if abs(b) > 1e-12 else ex.BACKDROP_RATIO
    corr = new_ratio / max(1e-12, ex.BACKDROP_RATIO)
    clouds = os.environ.get("EFT_CLOUDS", "1") != "0"
    print("[fit] clouds                : %s" % ("on" if clouds else "off"))
    print("[fit] pack backdrop  hemi-mean   : %.6f" % pack)
    print("[fit] physical backdrop hemi-mean: %.6f   (at BACKDROP_RATIO %.6f)"
          % (phys, ex.BACKDROP_RATIO))
    print("[fit] correction            : x%.6f" % corr)
    print("[fit] BACKDROP_RATIO should be %.6f  (currently %.6f)" % (new_ratio, ex.BACKDROP_RATIO))
    # THE DISTRIBUTION, not just the mean. Two skies can carry identical energy and look nothing
    # alike: the pack capture is a low-contrast wash, so matching its MEAN drives a high-contrast
    # physical sky's highlights straight through the top. These are the numbers that say whether the
    # energy match is also a good photographic exposure, or only a safe one.
    print("[fit] pack     p50/p99/p99.9: %.3f / %.3f / %.3f   above 1.0: %.2f%%"
          % (pack_p[0], pack_p[1], pack_p[2], pack_blown))
    print("[fit] physical p50/p99/p99.9: %.3f / %.3f / %.3f   above 1.0: %.2f%%"
          % (phys_p[0], phys_p[1], phys_p[2], phys_blown))
    if abs(corr - 1.0) < 0.02:
        print("[fit]   within 2%: the current value is already matched, no edit needed.")
    else:
        print("[fit]   EDIT example_scene.py BACKDROP_RATIO to the value above and re-run.")

    # THE CONTROL. A diffuse sphere sees only non-camera rays hit the world, so if the split were
    # leaking into the lighting these two would differ. This is what makes "the fitted pair is
    # untouched" a measurement rather than an argument about how the node graph looks.
    ob = _probe("probe")
    ob.location = (0.0, 3.0, 0.0)
    cam_d.lens = 50.0
    cam.rotation_euler = (math.radians(90.0), 0.0, 0.0)
    ob.location = (0.0, 3.0, 0.0)
    _set_world(sun_dir, "pack")
    lit_pack = _mean_radiance("lit_pack")
    _set_world(sun_dir, "physical")
    lit_split = _mean_radiance("lit_split")
    lerr = 100.0 * (lit_split - lit_pack) / lit_pack if lit_pack > 1e-9 else 0.0
    print("[fit] VERIFY lighting: split %.6f vs pack %.6f  (%+.3f%%, want 0.000)"
          % (lit_split, lit_pack, lerr))

    json.dump({"pack_backdrop_mean": pack, "physical_backdrop_mean": phys,
               "current_ratio": ex.BACKDROP_RATIO, "correction": corr,
               "fitted_ratio": new_ratio, "clouds": clouds,
               "verify_lighting_pct": lerr,
               "pack_p50_p99_p999": pack_p, "physical_p50_p99_p999": phys_p,
               "pack_pct_above_1": pack_blown, "physical_pct_above_1": phys_blown,
               "note": "camera-ray backdrop only; lighting keeps the pack cubemap"},
              open(OUT, "w"), indent=1)
    print("[fit] -> %s" % OUT)


main()
