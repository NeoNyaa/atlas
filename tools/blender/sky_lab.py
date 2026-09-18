"""Render the sky, and only the sky, in seconds instead of the twenty minutes a map costs.

    blender -b --factory-startup --python tools/blender/sky_lab.py -- --out renders/sky/lab.png
    EFT_BACKDROP=pack blender -b --factory-startup --python tools/blender/sky_lab.py -- --out a.png

Judging a backdrop by rendering Interchange is the wrong loop: the pack load dominates, the sky is a
third of the frame, and every iteration costs a coffee.  This builds the EXACT world node tree
example_scene.py builds - it imports `build_sky_world` rather than reimplementing it, so the preview
cannot drift from the render - and puts it behind a horizon that is enough to judge against:

  * a ground plane at the pack's own scale, so the horizon LINE is in the right place and any dark
    band under it is visible rather than hidden behind terrain;
  * a few silhouette blocks at varying distance, because a sky is judged against what it sits
    behind, and because aerial perspective needs something to recede;
  * the same 50 mm, the same AgX, the same resolution as the real shot.

What it deliberately does NOT reproduce: the pack's geometry, grass, characters and the analytic
depth haze.  This is a sky lab.  A frame that looks right here still has to be confirmed on the map
once, which is what `--scene` in the A/B driver is for.
"""

import argparse
import math
import os
import sys

import bpy
import mathutils

HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import example_scene as ex


def _args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=os.path.join(ex.REPO, "renders", "sky", "lab.png"))
    p.add_argument("--backdrop", default=None, help="pack|physical; default follows EFT_BACKDROP")
    p.add_argument("--samples", type=int, default=64)
    p.add_argument("--res", type=int, default=1280)
    p.add_argument("--yaw", type=float, default=None,
                   help="camera yaw in degrees; default looks toward the sun's azimuth")
    p.add_argument("--pitch", type=float, default=12.0,
                   help="camera pitch ABOVE horizontal in degrees; positive looks up")
    p.add_argument("--exr", action="store_true", help="also write linear EXR for measurement")
    p.add_argument("--no-ground", dest="no_ground", action="store_true",
                   help="sky only, so the below-horizon half is visible instead of covered")
    return p.parse_args(argv)


CAM_AT = (0.0, -12.0, 1.7)     # eye height; the horizon then sits where a person sees it


def _clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)


def _ground(sun_dir):
    """A large plane at a plausible ground albedo, so the horizon is a real edge."""
    import bmesh
    me = bpy.data.meshes.new("ground")
    bm = bmesh.new()
    # 20 km: far enough that its edge is well past where haze has swallowed everything, so the
    # horizon in frame is the PLANE'S horizon and not the plane running out.
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=20000.0)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("ground", me)
    bpy.context.scene.collection.objects.link(ob)
    mat = bpy.data.materials.new("ground")
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]
    # The damp, desaturated green-grey a Tarkov raid ground actually is; this is a STAND-IN for the
    # map's terrain and it exists to be a silhouette, not to be accurate.
    b.inputs["Base Color"].default_value = (0.045, 0.050, 0.038, 1.0)
    b.inputs["Roughness"].default_value = 0.92
    me.materials.append(mat)
    return ob


def _blocks(yaw_deg, cam_at):
    """Silhouettes at receding distance: the only way to see whether aerial perspective reads.

    Laid out along the CAMERA'S heading, not along +Y. The default heading follows the sun's
    azimuth, so a fixed +Y layout puts every block off frame - which is exactly what the first lab
    render did.
    """
    import bmesh
    yaw = math.radians(yaw_deg)
    fwd = mathutils.Vector((math.sin(yaw), math.cos(yaw), 0.0))
    right = mathutils.Vector((math.cos(yaw), -math.sin(yaw), 0.0))
    mat = bpy.data.materials.new("block")
    mat.use_nodes = True
    b = mat.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (0.08, 0.08, 0.085, 1.0)
    b.inputs["Roughness"].default_value = 0.85
    # Offsets are a FRACTION of distance, not metres, so every block stays inside the same 50 mm
    # frame no matter how far away it is. Fixed metre offsets put the near ones out of shot.
    for i, (d, h, frac) in enumerate(((34.0, 6.0, -0.28), (75.0, 12.0, 0.22), (155.0, 22.0, -0.20),
                                      (330.0, 40.0, 0.17), (720.0, 70.0, -0.15))):
        off = d * frac
        me = bpy.data.meshes.new("block%d" % i)
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=1.0)
        bm.to_mesh(me)
        bm.free()
        ob = bpy.data.objects.new("block%d" % i, me)
        bpy.context.scene.collection.objects.link(ob)
        ob.scale = (h * 0.8, h * 0.8, h)
        p = cam_at + fwd * d + right * off
        ob.location = (p.x, p.y, h * 0.5)
        me.materials.append(mat)


def main():
    a = _args()
    backdrop = a.backdrop or os.environ.get("EFT_BACKDROP") or "physical"
    _clear()
    sc = bpy.context.scene
    ex.enable_cycles_gpu()

    # The pack's own sun, read exactly the way example_scene reads it.
    import json
    sd = [0.449, 0.799, -0.400]
    volj = os.path.join(ex.PACK, "volume.json")
    if os.path.isfile(volj):
        sd = json.load(open(volj, encoding="utf-8")).get("sun_dir", sd)
    sun_dir = mathutils.Vector((sd[0], -sd[2], sd[1])).normalized()

    w = bpy.data.worlds.new("sky_lab")
    sc.world = w
    w.use_nodes = True
    ex.build_sky_world(w.node_tree, sun_dir, backdrop=backdrop)

    ld = bpy.data.lights.new("eft_sun", 'SUN')
    ld.energy = ex.SUN_ENERGY
    ld.angle = math.radians(0.526)
    sun = bpy.data.objects.new("eft_sun", ld)
    sc.collection.objects.link(sun)
    sun.rotation_euler = (-sun_dir).to_track_quat('-Z', 'Y').to_euler()

    yaw = a.yaw if a.yaw is not None else math.degrees(math.atan2(sun_dir.x, -sun_dir.y))
    if not a.no_ground:
        _ground(sun_dir)
        _blocks(yaw, mathutils.Vector(CAM_AT))

    cam_d = bpy.data.cameras.new("cam")
    cam_d.lens = 50.0
    cam_d.sensor_fit = 'HORIZONTAL'      # AUTO maps 36 mm to the LARGER axis; see kit_sheet
    cam = bpy.data.objects.new("cam", cam_d)
    sc.collection.objects.link(cam)
    sc.camera = cam
    # `yaw` was solved above, before the blocks were laid out along it. The default looks along the
    # sun's azimuth, the hardest direction for a fake sky: glow, gradient and shadows must agree.
    cam.location = CAM_AT      # eye height, so the horizon sits where a person sees it
    # rot_x = 90 deg looks at the horizon; ADDING pitch looks UP. Subtracting tilts down, which
    # buries the sky under a half-frame of ground and shows only the deck's underside edge-on.
    cam.rotation_euler = (math.radians(90.0 + a.pitch), 0.0, math.radians(yaw))

    sc.render.engine = 'CYCLES'
    sc.cycles.samples = a.samples
    sc.cycles.transparent_max_bounces = 256
    sc.render.resolution_x = a.res
    sc.render.resolution_y = int(a.res * 9 / 16)
    sc.render.resolution_percentage = 100
    sc.view_settings.view_transform = 'AgX'
    # ABSOLUTE, always. Blender resolves a relative render path against the .blend, not the cwd, and
    # with --factory-startup there is no .blend - so a relative --out silently writes nowhere useful
    # and still prints success.
    a.out = os.path.abspath(a.out)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)

    if a.exr:
        sc.render.image_settings.file_format = 'OPEN_EXR'
        sc.render.image_settings.color_depth = '32'
        sc.view_settings.view_transform = 'Raw'
        sc.render.filepath = os.path.splitext(a.out)[0] + "_linear"
        bpy.ops.render.render(write_still=True)
        sc.view_settings.view_transform = 'AgX'

    sc.render.image_settings.file_format = 'PNG'
    sc.render.image_settings.color_depth = '8'
    sc.render.filepath = os.path.splitext(a.out)[0]
    bpy.ops.render.render(write_still=True)
    print("[skylab] backdrop=%s samples=%d -> %s" % (backdrop, a.samples, a.out))


main()
