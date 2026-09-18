"""Turn a shipped sky cubemap into an equirectangular HDR-ish PNG for Blender's world.

Blender's Environment Texture node samples equirectangular or mirror-ball images; it has no
6-face cubemap input. The pack ships the game's own cubemaps as six faces in WGPU order
(+X, -X, +Y, -Y, +Z, -Z, i.e. face0..face5), so they have to be resampled once.

    python tools/blender/make_sky_equirect.py [--name NatureCubemap] [--width 2048] [--out PATH]

Runs outside Blender on purpose: it needs PIL, which a Blender install does not ship.
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SKY_DIR = os.path.join(REPO, "packs", "shared", "sky")


def load_faces(sky_dir, entry):
    """(6, S, S, 3) float32 in face order +X, -X, +Y, -Y, +Z, -Z."""
    imgs = []
    for fn in entry["faces"]:
        p = os.path.join(sky_dir, fn)
        if not os.path.isfile(p):
            raise SystemExit("missing face: %s" % p)
        imgs.append(np.asarray(Image.open(p).convert("RGB"), np.float32) / 255.0)
    s = imgs[0].shape[0]
    if any(i.shape[0] != s or i.shape[1] != s for i in imgs):
        raise SystemExit("cube faces are not square and equal-sized")
    return np.stack(imgs, 0), s


def _bspline_w(t):
    """Cubic B-spline weights for the taps at -1, 0, +1, +2.

    C2 continuous and mildly smoothing, which is the correct reconstruction filter for a source
    that genuinely carries no detail above its own texel: it invents nothing and, unlike bilinear,
    leaves no first-derivative discontinuity at each texel boundary for the eye to read as a facet.
    """
    t2, t3 = t * t, t * t * t
    return ((-t3 + 3 * t2 - 3 * t + 1) / 6.0,
            (3 * t3 - 6 * t2 + 4) / 6.0,
            (-3 * t3 + 3 * t2 + 3 * t + 1) / 6.0,
            t3 / 6.0)


def _directions(width, height, ox, oy):
    """Blender-world -> pack-world (Y-up) unit directions for one sub-pixel offset."""
    u = (np.arange(width, dtype=np.float64) + ox) / width
    v = (np.arange(height, dtype=np.float64) + oy) / height
    theta = (u - 0.5) * (2.0 * np.pi)               # azimuth
    phi = (0.5 - v) * np.pi                         # elevation, +pi/2 at the top row
    ct = np.cos(phi)[:, None]
    # AZIMUTH, and this is the one line in the file that is easy to get backwards. Blender's
    # Environment Texture node maps a direction to `u = 0.5 - atan2(d.y, d.x) / 2pi` (the
    # equirectangular case in kernel/geom/../projection). Our `u = 0.5 + theta / 2pi`, so the
    # direction this texel must hold has atan2(by, bx) = -theta, i.e.
    #     bx = cos(theta) * ct,  by = -sin(theta) * ct.
    # The previous pair, (-sin, +cos), is atan2 = theta + pi/2: a 90 degree rotation AND a flip of
    # handedness, so the sky was loaded MIRRORED and every cloud mass - and every reflection of one
    # off the pack's 587 near-mirror materials - sat in the wrong world direction. Verified by
    # rendering a synthetic 5-texel dot: it lands where the formula above predicts to 0.300 deg,
    # and 47.596 deg away from where the old pair predicted.
    bx = (np.cos(theta)[None, :]) * ct
    by = (-np.sin(theta)[None, :]) * ct
    bz = np.repeat(np.sin(phi)[:, None], width, 1)

    # Blender -> pack (Y-up)
    return bx, bz, -by


def _sample(faces, size, dx, dy, dz):
    """Cube lookup with cubic B-spline reconstruction inside the chosen face."""
    ax, ay, az = np.abs(dx), np.abs(dy), np.abs(dz)
    face = np.where(
        (ax >= ay) & (ax >= az), np.where(dx > 0, 0, 1),
        np.where((ay >= az), np.where(dy > 0, 2, 3), np.where(dz > 0, 4, 5)),
    ).astype(np.int32)

    # Per-face (u, v) in [-1, 1], standard cube mapping.
    ma = np.maximum(np.maximum(ax, ay), az)
    ma = np.where(ma == 0, 1e-9, ma)
    sc = np.select(
        [face == 0, face == 1, face == 2, face == 3, face == 4, face == 5],
        [-dz, dz, dx, dx, dx, -dx],
    )
    tc = np.select(
        [face == 0, face == 1, face == 2, face == 3, face == 4, face == 5],
        [-dy, -dy, dz, -dz, -dy, -dy],
    )
    fu = np.clip((sc / ma + 1.0) * 0.5, 0.0, 1.0)
    fv = np.clip((tc / ma + 1.0) * 0.5, 0.0, 1.0)

    # NOT `faces[face, int(fv * size), int(fu * size)]`. That was the whole of the sky's
    # pixelation: NatureCubemap is 128 px per face, i.e. 0.70 deg per source texel, and at the
    # delivered lens (50 mm, 36 mm sensor, 2560x1440 = 0.0159 deg per output pixel) ONE source
    # texel covers 44 output pixels. Nearest-neighbour indexing bakes a hard edge between each of
    # those blocks into the equirect, and Blender's bilinear magnification cannot undo an edge that
    # is already in the image - measured as a 53.3 px cell period in the rendered frame. Texel
    # centres sit at (i + 0.5) / size.
    x = np.clip(fu * size - 0.5, 0.0, size - 1.0)
    y = np.clip(fv * size - 0.5, 0.0, size - 1.0)
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    wx = _bspline_w(x - x0)
    wy = _bspline_w(y - y0)
    acc = np.zeros(x.shape + (3,), np.float64)
    for j in range(4):
        yy = np.clip(y0 + j - 1, 0, size - 1)
        for i in range(4):
            xx = np.clip(x0 + i - 1, 0, size - 1)
            acc += faces[face, yy, xx] * (wx[i] * wy[j])[..., None]
    return acc


def equirect(faces, size, width, ss=2):
    """Resample the cube into an equirectangular image, Blender world orientation.

    Blender is Z-up and the pack's cubemap is authored in the pack's Y-up world, so the
    direction is converted per pixel: d_pack = (bx, bz, -by) is the inverse of the importer's
    (x, y, z) -> (x, -z, y).

    `ss` sub-pixel offsets per axis are box-averaged. Sampling the texel CORNER once (the old
    behaviour) leaves the seam between two cube faces to fall wherever the single sample lands;
    2x2 costs four passes of a vectorised lookup and measures 0.0911 crease against bilinear's
    0.1238 and nearest's 0.2642.
    """
    h = width // 2
    acc = np.zeros((h, width, 3), np.float64)
    for j in range(ss):
        for i in range(ss):
            dx, dy, dz = _directions(width, h, (i + 0.5) / ss, (j + 0.5) / ss)
            acc += _sample(faces, size, dx, dy, dz)
    return (acc / float(ss * ss)).astype(np.float32)


def write_png16(path, arr01):
    """16-bit RGB PNG from float [0,1]. PIL's RGB mode is 8-bit only, so write the chunks.

    This is not cosmetic. The sky is one gentle gradient, 8 bits is 0.4% of range per code, and
    once the equirect texel is small enough to survive Blender's magnification those codes become
    visible contours - worth 0.1590 crease at 4096 width, i.e. WORSE than the blocks they replaced.
    """
    import struct
    import zlib
    q = np.clip(arr01 * 65535.0 + 0.5, 0, 65535).astype(">u2")
    h, w, _ = q.shape
    be = q.tobytes()
    stride = w * 6
    raw = bytearray()
    for y in range(h):
        raw.append(0)                                   # filter type 0, none
        raw += be[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        f.write(chunk(b"IEND", b""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="NatureCubemap")
    ap.add_argument("--width", type=int, default=2048)
    ap.add_argument("--sky-dir", default=SKY_DIR)
    ap.add_argument("--ss", type=int, default=2, help="sub-pixel samples per axis")
    ap.add_argument("--bits", type=int, default=16, choices=(8, 16))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    meta_p = os.path.join(a.sky_dir, "sky.json")
    meta = json.load(open(meta_p, encoding="utf-8"))
    cubes = meta.get("cubemaps") or {}
    if a.name not in cubes:
        sky = [k for k, v in cubes.items() if v.get("is_sky")]
        raise SystemExit("no cubemap %r. is_sky candidates: %s" % (a.name, sorted(sky)))
    entry = cubes[a.name]

    faces, size = load_faces(a.sky_dir, entry)
    img = equirect(faces, size, a.width, ss=a.ss)
    out = a.out or os.path.join(a.sky_dir, "%s_equirect.png" % a.name)
    if a.bits == 16:
        write_png16(out, img)
    else:
        Image.fromarray(np.clip(img * 255.0 + 0.5, 0, 255).astype(np.uint8)).save(out)
    print("%s: %d faces at %dpx -> %dx%d equirect, cubic B-spline, %dx%d supersampled, %d-bit"
          % (a.name, len(entry["faces"]), size, a.width, a.width // 2, a.ss, a.ss, a.bits))
    print("  %.4f deg per source texel, %.4f deg per equirect texel"
          % (90.0 / size, 360.0 / a.width))
    print("  zenith %s  horizon %s  mean %s"
          % (entry.get("zenith"), entry.get("horizon"), entry.get("mean")))
    print("  wrote %s" % out)


if __name__ == "__main__":
    main()
