"""LazyTrack Egg case: builds the STL files for 3D printing.

    python3 egg_case.py            -> writes stl/*.stl
    python3 egg_case.py --preview  -> also writes preview PNGs

No CAD program needed (only numpy). Every part is "2.5D": a stack of flat layers, each layer a 2D
region (egg outline minus holes, plus walls...). A region is described per horizontal line as a list
of x-intervals, so cutting holes is just 1D interval math. The layers are then turned into closed
prisms. Slicers (ElegooSlicer, Orca, Cura) merge touching prisms into one solid.

All sizes are in mm and live in the PARAMETERS block, so the case can be tuned to your parts.
"""
from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------- PARAMETERS
# Egg outline: hull of a big circle (bottom) and a smaller one (top).
R_BOT, Y_BOT = 40.0, 40.0          # bottom circle radius / centre height
R_TOP, Y_TOP = 35.0, 107.0         # top circle  -> egg is 80 wide, 142 tall
WALL = 2.2                         # side wall thickness
PLATE = 2.0                        # front face / back plate thickness
CHAMFER = 1.2                      # rounded-off outer front/back edge
T_TOTAL = 31.0                     # total thickness of the closed egg
H_FRONT = 13.0                     # front shell depth (back shell = T_TOTAL - H_FRONT)
LIP = 2.5                          # how far the back shell's lip slides into the front shell
LIP_T = 1.0                        # lip thickness
FIT = 0.15                         # gap between lip and front wall

# 2.8" screen module (red board, portrait, pin header at the TOP)
SCR_W, SCR_H = 50.0, 86.0          # board size
SCR_T = 6.5                        # front of glass -> back of red board (no pins)
SCR_CLEAR = 0.4                    # extra room around the board
SCR_CY = 83.8                      # board centre height
AA_W, AA_H = 43.2, 57.6            # visible area of a 2.8" 320x240 panel
AA_OFFSET = 3.0                    # window centre minus board centre (+ = towards the header).
                                   # Measured: glass runs 5..75 mm from the header edge -> centre 40 (board centre 43)
WIN_MARGIN = 0.6                   # window a bit bigger than the visible area
WIN_H = 63.6                       # window height: covers the lit area wherever the panel puts its
                                   # thicker black border (top or bottom of the glass)
SCR_HOLE_IN = 3.5                  # board mounting holes, distance from the edges

# 12x12 mm push buttons (12x12x7.3 with round knob)
BTN_Y = 26.0
BTN_X = (-17.0, 0.0, 17.0)
BTN_BODY = 12.0
BTN_BODY_H = 3.6                   # switch body height (without knob)
BTN_KNOB_H = 3.7                   # knob height above the body
BTN_HOLE = 10.6                    # hole in the face for the cap
CAP_D, CAP_FLANGE_D, CAP_FLANGE_T = 10.0, 11.6, 1.2
CAP_STICK_OUT = 1.5                # how far the caps stick out of the face
KNOB_D = 7.2                       # recess under the cap for the switch knob

# ESP32 38-pin dev board (USB-C at the bottom end, components facing the back)
ESP_L, ESP_W = 53.0, 30.0           # measured
ESP_CLEAR = 0.8
ESP_Y0 = 6.5                       # bottom (USB) end of the board
ESP_PAD_H = 1.0                    # raised pad under the ESP32 module (+1 mm foam tape)
ESP_BACK_Z = 7.1                   # board back side height above the back face
USB_W, USB_H = 13.2, 7.2           # opening for the USB-C plug body

# Screws: M3 x 20, self-tapping into the front shell
SCREWS = [(-30.5, 84.0), (30.5, 84.0), (-28.5, 20.0), (28.5, 20.0)]
BOSS_R, PILOT_R, SCREW_R, HEAD_R, HEAD_DEPTH = 3.2, 1.25, 1.7, 3.1, 1.8

# Passive buzzer (10 mm) behind a little grille
BUZ_X, BUZ_Y, BUZ_D = 24.0, 30.0, 10.4          # measured 10 mm buzzer

STEP = 0.25                        # scanline spacing (mm)

# ---------------------------------------------------------------- 1D interval maths
def norm(iv):
    iv = sorted((a, b) for a, b in iv if b - a > 1e-6)
    out = []
    for a, b in iv:
        if out and a <= out[-1][1] + 1e-9:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(x) for x in out]


def diff(a, b):
    out = []
    for x0, x1 in a:
        cur = [(x0, x1)]
        for y0, y1 in b:
            nxt = []
            for c0, c1 in cur:
                if y1 <= c0 or y0 >= c1:
                    nxt.append((c0, c1))
                else:
                    if y0 > c0:
                        nxt.append((c0, y0))
                    if y1 < c1:
                        nxt.append((y1, c1))
            cur = nxt
        out += cur
    return norm(out)


def inter(a, b):
    out = []
    for x0, x1 in a:
        for y0, y1 in b:
            lo, hi = max(x0, y0), min(x1, y1)
            if hi > lo:
                out.append((lo, hi))
    return norm(out)


# ---------------------------------------------------------------- 2D shapes (span at height y)
class Shape:
    def __add__(s, o): return Op("+", s, o)
    def __sub__(s, o): return Op("-", s, o)
    def __and__(s, o): return Op("&", s, o)


class Op(Shape):
    def __init__(s, op, a, b): s.op, s.a, s.b = op, a, b
    def span(s, y):
        A, B = s.a.span(y), s.b.span(y)
        return norm(A + B) if s.op == "+" else diff(A, B) if s.op == "-" else inter(A, B)
    def ys(s): return s.a.ys() + s.b.ys()


class Empty(Shape):
    def span(s, y): return []
    def ys(s): return []


class Circle(Shape):
    def __init__(s, cx, cy, r): s.cx, s.cy, s.r = cx, cy, r
    def span(s, y):
        d = y - s.cy
        if abs(d) >= s.r: return []
        w = math.sqrt(s.r * s.r - d * d)
        return [(s.cx - w, s.cx + w)]
    def ys(s): return [s.cy - s.r, s.cy + s.r]


class RRect(Shape):
    """Rectangle centred at (cx, cy), size w x h, corner radius r."""
    def __init__(s, cx, cy, w, h, r=0.0):
        s.cx, s.cy, s.w, s.h, s.r = cx, cy, w, h, min(r, w / 2, h / 2)
    def span(s, y):
        d = abs(y - s.cy)
        if d >= s.h / 2: return []
        k = s.w / 2 - s.r
        e = d - (s.h / 2 - s.r)
        if e > 0:
            k += math.sqrt(max(0.0, s.r * s.r - e * e))
        else:
            k += s.r
        return [(s.cx - k, s.cx + k)]
    def ys(s): return [s.cy - s.h / 2, s.cy + s.h / 2]


def Rect(x0, y0, x1, y1):
    return RRect((x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0)


class Egg(Shape):
    """The egg outline shrunk inwards by d."""
    def __init__(s, d=0.0):
        s.r1, s.a, s.r2, s.b = R_BOT - d, Y_BOT, R_TOP - d, Y_TOP
        phi = math.asin((s.r1 - s.r2) / (s.b - s.a))
        s.p1 = (s.r1 * math.cos(phi), s.a + s.r1 * math.sin(phi))
        s.p2 = (s.r2 * math.cos(phi), s.b + s.r2 * math.sin(phi))
    def hw(s, y):
        if y <= s.a - s.r1 or y >= s.b + s.r2: return None
        if y <= s.p1[1]:
            return math.sqrt(max(0.0, s.r1 ** 2 - (y - s.a) ** 2))
        if y >= s.p2[1]:
            return math.sqrt(max(0.0, s.r2 ** 2 - (y - s.b) ** 2))
        t = (y - s.p1[1]) / (s.p2[1] - s.p1[1])
        return s.p1[0] + t * (s.p2[0] - s.p1[0])
    def span(s, y):
        w = s.hw(y)
        return [] if w is None else [(-w, w)]
    def ys(s): return [s.a - s.r1, s.b + s.r2]


# ---------------------------------------------------------------- 2.5D solid -> triangles
class Part:
    def __init__(s, name):
        s.name, s.slabs = name, []

    def add(s, shape, z0, z1):
        if z1 > z0:
            s.slabs.append((shape, z0, z1))

    def triangles(s, step=STEP):
        tris = []
        for shape, z0, z1 in s.slabs:
            ys = shape.ys()
            lo, hi = min(ys), max(ys)
            n = max(1, int(math.ceil((hi - lo) / step)))
            grid = [lo + (hi - lo) * i / n for i in range(n + 1)]
            spans = [shape.span(y) for y in grid]
            for k in range(n):
                ya, yb = grid[k], grid[k + 1]
                A, B = spans[k], spans[k + 1]
                quads = []
                if len(A) == len(B) and len(A) and all(a[0] < b[1] and b[0] < a[1] for a, b in zip(A, B)):
                    quads = [((a[0], ya), (a[1], ya), (b[1], yb), (b[0], yb)) for a, b in zip(A, B)]
                else:
                    for m0, m1 in shape.span((ya + yb) / 2):
                        quads.append(((m0, ya), (m1, ya), (m1, yb), (m0, yb)))
                for q in quads:
                    tris += prism(q, z0, z1)
        return np.array(tris, dtype=np.float32).reshape(-1, 3, 3)


def prism(q, z0, z1):
    """Closed prism over a convex quad (counter-clockwise), outward-facing triangles."""
    b = [(x, y, z0) for x, y in q]
    t = [(x, y, z1) for x, y in q]
    out = [(b[0], b[2], b[1]), (b[0], b[3], b[2]), (t[0], t[1], t[2]), (t[0], t[2], t[3])]
    for i in range(4):
        j = (i + 1) % 4
        out += [(b[i], b[j], t[j]), (b[i], t[j], t[i])]
    return out


def write_stl(path, tris):
    tris = np.asarray(tris, dtype=np.float32)
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    keep = ln[:, 0] > 1e-9
    tris, n = tris[keep], (n[keep] / ln[keep]).astype(np.float32)
    rec = np.zeros(len(tris), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"], rec["v"] = n, tris
    with open(path, "wb") as f:
        f.write(b"LazyTrack Egg case".ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        f.write(rec.tobytes())


# ---------------------------------------------------------------- the parts
def stepped(part, outer_d_at, z0, z1, extra_holes=Empty(), steps=4):
    """Plate z0..z1 whose outline is inset more near z0 (a stepped chamfer)."""
    zc = z0 + CHAMFER
    for i in range(steps):
        a, b = z0 + CHAMFER * i / steps, z0 + CHAMFER * (i + 1) / steps
        part.add(Egg(CHAMFER * (1 - (i + 0.5) / steps)) - extra_holes(i, steps), a, b)
    part.add(Egg(0) - extra_holes(steps, steps), zc, z1)


def window(grow=0.0):
    cy = SCR_CY + AA_OFFSET
    return RRect(0, cy, AA_W + WIN_MARGIN * 2 + grow * 2, WIN_H + grow * 2, 1.0 + grow)


def front_shell():
    p = Part("front_shell")
    btn_holes = Empty()
    for x in BTN_X:
        btn_holes = btn_holes + Circle(x, BTN_Y, BTN_HOLE / 2)
    pilots = Empty()
    for x, y in SCREWS:
        pilots = pilots + Circle(x, y, PILOT_R)

    def holes(i, steps):
        grow = CHAMFER * (1 - (i + 0.5) / steps) if i < steps else 0.0
        return window(grow) + btn_holes
    stepped(p, None, 0, PLATE, holes)

    inner = Egg(WALL)
    p.add(Egg(0) - inner, PLATE, H_FRONT)                    # side wall

    # screen pocket rim
    pw, ph = SCR_W + SCR_CLEAR, SCR_H + SCR_CLEAR
    rim = (RRect(0, SCR_CY, pw + 2.4, ph + 2.4, 1.2) - RRect(0, SCR_CY, pw, ph, 0.3)) & Egg(1.0)
    p.add(rim, PLATE, PLATE + SCR_T - 1.0)

    # button pockets: walls above/below each switch, small snap lip at the top
    for x in BTN_X:
        h = BTN_BODY + 0.3
        top_z = PLATE + CAP_FLANGE_T + 0.8 - 0.8 + BTN_KNOB_H + BTN_BODY_H + 0.0
        # knob top sits 0.8 inside the cap recess: z = PLATE + CAP_FLANGE_T - 0.8
        knob_top = PLATE + CAP_FLANGE_T - 0.8
        body_bottom = knob_top + BTN_KNOB_H + BTN_BODY_H
        for sgn in (-1, 1):
            wall = Rect(x - h / 2, BTN_Y + sgn * h / 2 if sgn > 0 else BTN_Y - h / 2 - 1.2,
                        x + h / 2, BTN_Y + h / 2 + 1.2 if sgn > 0 else BTN_Y - h / 2)
            p.add(wall, PLATE, body_bottom + 0.6)
            lip = Rect(x - 3, BTN_Y + h / 2 - 0.6 if sgn > 0 else BTN_Y - h / 2 - 1.2,
                       x + 3, BTN_Y + h / 2 + 1.2 if sgn > 0 else BTN_Y - h / 2 + 0.6)
            p.add(lip, body_bottom, body_bottom + 0.6)

    # screw bosses
    for x, y in SCREWS:
        p.add((Circle(x, y, BOSS_R) & Egg(0.5)) - Circle(x, y, PILOT_R), PLATE, H_FRONT)
    return p


def back_shell():
    p = Part("back_shell")
    hb = T_TOTAL - H_FRONT
    screw = Empty()
    head = Empty()
    for x, y in SCREWS:
        screw = screw + Circle(x, y, SCREW_R)
        head = head + Circle(x, y, HEAD_R)
    grille = Empty()
    for i in range(-2, 3):
        for j in range(-2, 3):
            if i * i + j * j <= 5:
                grille = grille + Circle(BUZ_X + i * 2.4, BUZ_Y + j * 2.4, 0.75)

    def holes(i, steps):
        z = CHAMFER * (i + 0.5) / steps if i < steps else CHAMFER + 0.1
        return (head if z < HEAD_DEPTH else screw) + grille
    stepped(p, None, 0, CHAMFER, holes)
    p.add(Egg(0) - head - grille, CHAMFER, HEAD_DEPTH)
    p.add(Egg(0) - screw - grille, HEAD_DEPTH, PLATE)

    # side wall, with the USB-C opening at the bottom
    usb_z = ESP_BACK_Z - 1.6
    usb = Rect(-USB_W / 2, -1, USB_W / 2, ESP_Y0 + 1)
    wall = Egg(0) - Egg(WALL)
    z_cut0, z_cut1 = max(PLATE, usb_z - USB_H / 2), usb_z + USB_H / 2
    p.add(wall, PLATE, z_cut0)
    p.add(wall - usb, z_cut0, z_cut1)
    p.add(wall, z_cut1, hb)
    # USB opening also notches the plate edge if the plug sits that low
    if usb_z - USB_H / 2 < PLATE:
        pass

    # lip that slides into the front shell (notched around the screw bosses)
    notch = Empty()
    for x, y in SCREWS:
        notch = notch + Circle(x, y, BOSS_R + 0.5)
    p.add(Egg(WALL + FIT) - Egg(WALL + FIT + LIP_T) - notch, hb, hb + LIP)

    # screw columns
    for x, y in SCREWS:
        p.add((Circle(x, y, BOSS_R) & Egg(0.5)) - Circle(x, y, SCREW_R), PLATE, hb)

    # ESP32 cradle: pad under the module + fence on three sides
    L, W = ESP_L + ESP_CLEAR, ESP_W + ESP_CLEAR
    y1 = ESP_Y0 + L
    p.add(Rect(-9, y1 - 20, 9, y1 - 2), PLATE, PLATE + ESP_PAD_H)
    fence = (Rect(-W / 2 - 1.2, ESP_Y0 + 8, W / 2 + 1.2, y1 + 1.2) - Rect(-W / 2, ESP_Y0 - 1, W / 2, y1)) & Egg(1.0)
    p.add(fence, PLATE, ESP_BACK_Z + 2.0)

    # buzzer ring
    p.add(Circle(BUZ_X, BUZ_Y, BUZ_D / 2 + 1.2) - Circle(BUZ_X, BUZ_Y, BUZ_D / 2), PLATE, PLATE + 3.0)

    # posts that hold the screen against the window
    scr_back = T_TOTAL - (PLATE + SCR_T)            # in back-shell height
    bx, by = SCR_W / 2 - SCR_HOLE_IN, SCR_H / 2 - SCR_HOLE_IN
    for sx in (-1, 1):
        for sy in (-1, 1):
            p.add(Circle(sx * bx, SCR_CY + sy * by, 2.0), PLATE, scr_back - 0.6)   # + a foam-tape dot on top
    return p


def caps():
    p = Part("button_caps")
    stem = PLATE + CAP_STICK_OUT
    for i, x in enumerate((-15.0, 0.0, 15.0)):
        # printed upside down: flange on the bed, knob recess in its underside
        p.add(Circle(x, 0, CAP_FLANGE_D / 2) - Circle(x, 0, KNOB_D / 2), 0, 0.8)
        p.add(Circle(x, 0, CAP_FLANGE_D / 2), 0.8, CAP_FLANGE_T)
        p.add(Circle(x, 0, CAP_D / 2), CAP_FLANGE_T, CAP_FLANGE_T + stem)
    return p


def test_plate():
    """Just the face (and button pockets) - a 30-minute print to check the window and buttons."""
    f = front_shell()
    t = Part("test_face")
    for shape, z0, z1 in f.slabs:
        if z0 < PLATE + 0.01:
            t.add(shape, z0, min(z1, PLATE + 1.0) if z0 >= PLATE else z1)
    return t


# ---------------------------------------------------------------- preview
def preview(parts, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    fig = plt.figure(figsize=(15, 7), facecolor="white")
    for k, (part, title, elev, azim) in enumerate(parts):
        ax = fig.add_subplot(1, len(parts), k + 1, projection="3d")
        tris = part.triangles(step=0.7)
        n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
        light = np.array([0.4, -0.5, 0.75]); light /= np.linalg.norm(light)
        sh = 0.35 + 0.65 * np.clip(n @ light, 0, 1)
        base = np.array([0.98, 0.80, 0.62])
        col = np.clip(base[None, :] * sh[:, None], 0, 1)
        pc = Poly3DCollection(tris, facecolors=col, edgecolors="none", linewidths=0)
        ax.add_collection3d(pc)
        ax.set_xlim(-45, 45); ax.set_ylim(-2, 144); ax.set_zlim(-30, 40)
        ax.set_box_aspect((90, 146, 70))
        ax.view_init(elev=elev, azim=azim)
        ax.set_axis_off(); ax.set_title(title)
    plt.tight_layout()
    plt.savefig(path, dpi=110)


def main():
    out = Path(__file__).with_name("stl")
    out.mkdir(exist_ok=True)
    parts = [front_shell(), back_shell(), caps(), test_plate()]
    for p in parts:
        tris = p.triangles()
        write_stl(out / f"{p.name}.stl", tris)
        print(f"{p.name}.stl  {len(tris)} triangles")
    if "--preview" in sys.argv:
        f, b = parts[0], parts[1]
        preview([(f, "Front shell (inside)", 60, -90), (b, "Back shell (inside)", 60, -90)],
                Path(__file__).with_name("preview.png"))


if __name__ == "__main__":
    main()
