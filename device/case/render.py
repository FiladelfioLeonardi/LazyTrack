"""Renders preview images of the case parts (simple z-buffer, numpy only)."""
import math, sys
import numpy as np
from PIL import Image
import egg_case as E

def rot(elev, azim):
    a, e = math.radians(azim), math.radians(elev)
    Rz = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
    Rx = np.array([[1, 0, 0], [0, math.cos(e), -math.sin(e)], [0, math.sin(e), math.cos(e)]])
    return Rx @ Rz

def render(tris, R, size=520, color=(250, 205, 160), bg=(246, 243, 238)):
    v = tris.reshape(-1, 3) @ R.T
    v = v.reshape(-1, 3, 3)
    n = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    keep = n[:, 2] > 0                          # facing the camera (camera looks down -z... we view from +z)
    v, n = v[keep], n[keep]
    lo, hi = v.reshape(-1, 3).min(0), v.reshape(-1, 3).max(0)
    s = (size * 0.9) / max(hi[0] - lo[0], hi[1] - lo[1])
    off = np.array([size / 2, size / 2]) - s * (lo[:2] + hi[:2]) / 2
    P = v[:, :, :2] * s + off
    Z = v[:, :, 2]
    L = np.array([-0.35, 0.45, 0.82]); L /= np.linalg.norm(L)
    shade = 0.42 + 0.58 * np.clip(n @ L, 0, 1)
    zb = np.full((size, size), -1e9); img = np.zeros((size, size, 3)); img[:] = bg
    col = np.array(color, float)
    for t in range(len(P)):
        p = P[t]; x0, y0 = np.floor(p.min(0)).astype(int); x1, y1 = np.ceil(p.max(0)).astype(int)
        x0, y0 = max(x0, 0), max(y0, 0); x1, y1 = min(x1, size - 1), min(y1, size - 1)
        if x1 < x0 or y1 < y0: continue
        xs, ys = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        (ax, ay), (bx, by), (cx, cy) = p
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-12: continue
        w0 = ((by - cy) * (xs - cx) + (cx - bx) * (ys - cy)) / d
        w1 = ((cy - ay) * (xs - cx) + (ax - cx) * (ys - cy)) / d
        w2 = 1 - w0 - w1
        m = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not m.any(): continue
        z = w0 * Z[t, 0] + w1 * Z[t, 1] + w2 * Z[t, 2]
        sub = zb[y0:y1 + 1, x0:x1 + 1]
        upd = m & (z > sub)
        sub[upd] = z[upd]
        img[y0:y1 + 1, x0:x1 + 1][upd] = col * shade[t]
    return Image.fromarray(np.flipud(img.clip(0, 255).astype(np.uint8)))

if __name__ == "__main__":
    f, b, c = E.front_shell(), E.back_shell(), E.caps()
    step = 0.5
    views = [
        (f, np.diag([-1, 1, -1]), "front"),           # the face, as you'll see it
        (f, rot(-35, 20), "front inside"),
        (b, rot(-35, -20), "back inside"),
        (b, np.diag([-1, 1, -1]), "back"),
    ]
    ims = [render(p.triangles(step), R) for p, R, _ in views]
    W = sum(i.width for i in ims); H = ims[0].height
    out = Image.new("RGB", (W, H), (246, 243, 238))
    x = 0
    for i in ims:
        out.paste(i, (x, 0)); x += i.width
    out.save(sys.argv[1] if len(sys.argv) > 1 else "preview.png")
