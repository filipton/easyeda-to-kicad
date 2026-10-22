"""Geometry helpers.

All functions here work in EasyEDA space: Y axis pointing up, angles in degrees,
positive angle = counter-clockwise.  Conversion to KiCad space (mm, Y down)
happens in the writers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

Pt = tuple  # (x, y)


def rot(p: Pt, deg: float, c: Pt = (0.0, 0.0)) -> Pt:
    if not deg:
        return (p[0], p[1])
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    x, y = p[0] - c[0], p[1] - c[1]
    return (c[0] + x * ca - y * sa, c[1] + x * sa + y * ca)


def add(a: Pt, b: Pt) -> Pt:
    return (a[0] + b[0], a[1] + b[1])


def sub(a: Pt, b: Pt) -> Pt:
    return (a[0] - b[0], a[1] - b[1])


def dist(a: Pt, b: Pt) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def norm_angle(a: float) -> float:
    a = math.fmod(a, 360.0)
    if a < 0:
        a += 360.0
    if abs(a - 360.0) < 1e-9:
        a = 0.0
    return a


def arc_center(p0: Pt, p1: Pt, sweep: float) -> Pt:
    """Center of an arc from p0 to p1 sweeping `sweep` degrees (CCW positive)."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    chord = math.hypot(dx, dy)
    if chord == 0 or abs(sweep) < 1e-9:
        return ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)
    half = math.radians(sweep) / 2
    # distance from chord midpoint to centre (signed)
    d = (chord / 2) / math.tan(half)
    mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    # left normal of the chord direction
    nx, ny = -dy / chord, dx / chord
    return (mx + nx * d, my + ny * d)


def arc_mid(p0: Pt, p1: Pt, sweep: float) -> Pt:
    c = arc_center(p0, p1, sweep)
    return rot(p0, sweep / 2, c)


def arc_points(p0: Pt, p1: Pt, sweep: float, max_err: float = 0.2) -> list[Pt]:
    """Polyline approximation (excluding p0, including p1)."""
    c = arc_center(p0, p1, sweep)
    r = dist(c, p0)
    if r <= 0:
        return [p1]
    # segment count from sagitta error
    if max_err >= r:
        n = 4
    else:
        step = 2 * math.degrees(math.acos(max(-1.0, 1 - max_err / r)))
        n = max(2, int(math.ceil(abs(sweep) / max(step, 1.0))))
    n = min(n, 720)
    pts = [rot(p0, sweep * i / n, c) for i in range(1, n)]
    pts.append(p1)
    return pts


def bezier_points(p0: Pt, c1: Pt, c2: Pt, p1: Pt, n: int = 12) -> list[Pt]:
    out = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        x = mt ** 3 * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * p1[0]
        y = mt ** 3 * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * p1[1]
        out.append((x, y))
    return out


def circle_points(c: Pt, r: float, max_err: float = 0.2) -> list[Pt]:
    if r <= 0:
        return [c]
    step = 2 * math.degrees(math.acos(max(-1.0, 1 - min(max_err, r) / r))) if max_err < r else 90
    n = max(16, int(math.ceil(360 / max(step, 1.0))))
    n = min(n, 360)
    return [(c[0] + r * math.cos(2 * math.pi * i / n), c[1] + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


# ---------------------------------------------------------------------------
# Path model
# ---------------------------------------------------------------------------


@dataclass
class Seg:
    kind: str  # 'line' | 'arc' | 'bezier'
    a: Pt
    b: Pt
    sweep: float = 0.0  # arcs
    c1: Pt | None = None  # beziers
    c2: Pt | None = None


@dataclass
class Shape:
    """One parsed EasyEDA contour."""

    kind: str  # 'path' | 'rect' | 'circle'
    segs: list[Seg] = field(default_factory=list)
    # rect
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    angle: float = 0.0
    radius: float = 0.0
    # circle
    center: Pt = (0.0, 0.0)
    r: float = 0.0

    def closed(self) -> bool:
        if self.kind != "path":
            return True
        if not self.segs:
            return False
        return dist(self.segs[0].a, self.segs[-1].b) < 1e-6

    def rect_corners(self) -> list[Pt]:
        """Corners for a rect (EasyEDA: x,y is the top-left corner, rotation about it)."""
        x, y, w, h = self.x, self.y, self.w, self.h
        pts = [(x, y), (x + w, y), (x + w, y - h), (x, y - h)]
        return [rot(p, self.angle, (x, y)) for p in pts]

    def rect_center(self) -> Pt:
        return rot((self.x + self.w / 2, self.y - self.h / 2), self.angle, (self.x, self.y))

    def to_segments(self) -> list[Seg]:
        """Express rects/circles as segments (circles become two arcs)."""
        if self.kind == "path":
            return self.segs
        if self.kind == "circle":
            c, r = self.center, self.r
            p0 = (c[0] + r, c[1])
            p1 = (c[0] - r, c[1])
            return [Seg("arc", p0, p1, 180.0), Seg("arc", p1, p0, 180.0)]
        x, y, w, h, cr = self.x, self.y, self.w, self.h, self.radius
        cr = max(0.0, min(cr, abs(w) / 2, abs(h) / 2))
        o = (x, y)
        R = lambda p: rot(p, self.angle, o)  # noqa: E731
        if cr <= 1e-9:
            c = self.rect_corners()
            return [Seg("line", c[i], c[(i + 1) % 4]) for i in range(4)]
        # clockwise around the rect starting at top edge (Y-up: top is y)
        tl, tr, br, bl = (x, y), (x + w, y), (x + w, y - h), (x, y - h)
        segs = [
            Seg("line", R((tl[0] + cr, tl[1])), R((tr[0] - cr, tr[1]))),
            Seg("arc", R((tr[0] - cr, tr[1])), R((tr[0], tr[1] - cr)), -90.0),
            Seg("line", R((tr[0], tr[1] - cr)), R((br[0], br[1] + cr))),
            Seg("arc", R((br[0], br[1] + cr)), R((br[0] - cr, br[1])), -90.0),
            Seg("line", R((br[0] - cr, br[1])), R((bl[0] + cr, bl[1]))),
            Seg("arc", R((bl[0] + cr, bl[1])), R((bl[0], bl[1] + cr)), -90.0),
            Seg("line", R((bl[0], bl[1] + cr)), R((tl[0], tl[1] - cr))),
            Seg("arc", R((tl[0], tl[1] - cr)), R((tl[0] + cr, tl[1])), -90.0),
        ]
        return segs

    def polygon(self, max_err: float = 0.2) -> list[Pt]:
        """Closed polygon approximation (no repeated closing point)."""
        if self.kind == "circle":
            return circle_points(self.center, self.r, max_err)
        segs = self.to_segments()
        if not segs:
            return []
        pts = [segs[0].a]
        for s in segs:
            if s.kind == "line":
                pts.append(s.b)
            elif s.kind == "arc":
                pts.extend(arc_points(s.a, s.b, s.sweep, max_err))
            else:
                pts.extend(bezier_points(s.a, s.c1, s.c2, s.b))
        if len(pts) > 1 and dist(pts[0], pts[-1]) < 1e-6:
            pts.pop()
        return dedupe(pts)


def dedupe(pts: list[Pt], eps: float = 1e-6) -> list[Pt]:
    out: list[Pt] = []
    for p in pts:
        if not out or dist(out[-1], p) > eps:
            out.append(p)
    if len(out) > 1 and dist(out[0], out[-1]) <= eps:
        out.pop()
    return out


def _num(v) -> float:
    return float(v)


def parse_contour(data, arc_scale: float = 1.0) -> Shape | None:
    """Parse one EasyEDA contour description.

    Formats: ``[x, y, "L", x, y, ..., "ARC", sweep, x, y, "C", ...]``,
    ``["R", x, y, w, h, angle, radius]``, ``["CIRCLE", cx, cy, r]``.
    """
    if not data:
        return None
    if isinstance(data[0], str):
        tag = data[0]
        if tag == "R":
            vals = [_num(v) for v in data[1:] if isinstance(v, (int, float))]
            vals += [0.0] * (6 - len(vals))
            return Shape("rect", x=vals[0], y=vals[1], w=vals[2], h=vals[3], angle=vals[4], radius=vals[5])
        if tag == "CIRCLE":
            return Shape("circle", center=(_num(data[1]), _num(data[2])), r=abs(_num(data[3])))
    segs: list[Seg] = []
    cur: Pt | None = None
    mode = None
    i = 0
    n = len(data)
    while i < n:
        v = data[i]
        if isinstance(v, str):
            mode = v
            i += 1
            if mode in ("ARC", "CARC"):
                sweep = _num(data[i]) * arc_scale
                if abs(sweep) > 360:
                    sweep /= 10.0  # some fills store tenths of a degree
                end = (_num(data[i + 1]), _num(data[i + 2]))
                if cur is not None:
                    segs.append(Seg("arc", cur, end, sweep))
                cur = end
                i += 3
                mode = None
            elif mode == "C":
                while i + 5 < n + 0 and all(isinstance(data[i + k], (int, float)) for k in range(6)):
                    c1 = (_num(data[i]), _num(data[i + 1]))
                    c2 = (_num(data[i + 2]), _num(data[i + 3]))
                    end = (_num(data[i + 4]), _num(data[i + 5]))
                    if cur is not None:
                        segs.append(Seg("bezier", cur, end, c1=c1, c2=c2))
                    cur = end
                    i += 6
                mode = None
            continue
        # numeric pair
        if i + 1 >= n or not isinstance(data[i + 1], (int, float)):
            i += 1
            continue
        p = (_num(v), _num(data[i + 1]))
        i += 2
        if cur is None or mode is None:
            if mode == "L" and cur is not None:
                segs.append(Seg("line", cur, p))
            cur = p
        else:
            segs.append(Seg("line", cur, p))
            cur = p
    sh = Shape("path", segs=segs)
    if not segs and cur is not None:
        sh.segs = []
    return sh


def parse_contours(data, arc_scale: float = 1.0) -> list[Shape]:
    """Parse a list of contours (POUR/FILL/REGION style) or a single contour."""
    if not data:
        return []
    if isinstance(data[0], list):
        out = []
        for c in data:
            s = parse_contour(c, arc_scale)
            if s is not None:
                out.append(s)
        return out
    s = parse_contour(data, arc_scale)
    return [s] if s else []


# ---------------------------------------------------------------------------
# Polygon helpers
# ---------------------------------------------------------------------------


def signed_area(pts: list[Pt]) -> float:
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2


def point_in_poly(p: Pt, poly: list[Pt]) -> bool:
    x, y = p
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xint = (xj - xi) * (y - yi) / ((yj - yi) or 1e-30) + xi
            if x < xint:
                inside = not inside
        j = i
    return inside


def bbox(pts: Iterable[Pt]):
    xs, ys = [], []
    for x, y in pts:
        xs.append(x)
        ys.append(y)
    if not xs:
        return (0, 0, 0, 0)
    return (min(xs), min(ys), max(xs), max(ys))


def nest_contours(polys: list[list[Pt]]) -> list[tuple[list[Pt], list[list[Pt]]]]:
    """Group contours into (outline, holes) using even-odd nesting depth."""
    polys = [p for p in polys if len(p) >= 3 and abs(signed_area(p)) > 1e-9]
    area = [abs(signed_area(p)) for p in polys]
    box = [bbox(p) for p in polys]
    probes = [_interior_probe(p) for p in polys]

    def inside(i, j):  # probe of i inside contour j (bbox test first: most pairs are far apart)
        x, y = probes[i]
        b = box[j]
        return b[0] <= x <= b[2] and b[1] <= y <= b[3] and point_in_poly(probes[i], polys[j])
    order = sorted(range(len(polys)), key=lambda i: -area[i])  # only bigger contours can enclose
    rank = {i: k for k, i in enumerate(order)}
    depth = [sum(1 for j in order[:rank[i]] if area[j] > area[i] and inside(i, j)) for i in range(len(polys))]
    result: list[tuple[list[Pt], list[list[Pt]]]] = []
    outer_idx = [i for i in range(len(polys)) if depth[i] % 2 == 0]
    for i in outer_idx:
        result.append((polys[i], []))
    for i in range(len(polys)):
        if depth[i] % 2 == 1:
            # attach to the smallest enclosing outer with depth-1
            best = None
            for k, oi in enumerate(outer_idx):
                if depth[oi] == depth[i] - 1 and inside(i, oi):
                    if best is None or area[oi] < area[outer_idx[best]]:
                        best = k
            if best is not None:
                result[best][1].append(polys[i])
    return result


def _interior_probe(p: list[Pt]) -> Pt:
    """A point very slightly inside polygon p (near its first vertex)."""
    n = len(p)
    for k in range(n):
        a, b, c = p[k - 1], p[k], p[(k + 1) % n]
        mx, my = (a[0] + b[0] + c[0]) / 3, (a[1] + b[1] + c[1]) / 3
        if point_in_poly((mx, my), p):
            return (mx, my)
    return p[0]


def fracture(outline: list[Pt], holes: list[list[Pt]]) -> list[Pt]:
    """Merge holes into the outline with zero-width bridges (keyhole polygon).

    KiCad graphic polygons cannot store holes, KiCad itself writes such shapes
    in this "fractured" form.  Each hole is bridged horizontally from its right-most vertex to the nearest edge on
    its right (of the outline or of a hole further right, which can't cross a hole not bridged yet), then the
    rings are stitched together in one walk.  Edges are bucketed by Y, so a pour with thousands of holes stays fast.
    """
    out = list(outline)
    if signed_area(out) < 0:
        out.reverse()
    rings = [out]
    for h in holes:
        h = list(h)
        if signed_area(h) > 0:
            h.reverse()  # holes clockwise
        if len(h) >= 3:
            rings.append(h)
    if len(rings) == 1:
        return out
    ys = [p[1] for r in rings for p in r]
    y0, y1 = min(ys), max(ys)
    nb = max(1, min(4096, int(len(ys) ** 0.5)))
    step = (y1 - y0) / nb or 1.0
    buckets: list[list] = [[] for _ in range(nb)]
    for ri, r in enumerate(rings):
        n = len(r)
        for k in range(n):
            c, d = r[k], r[(k + 1) % n]
            if c[1] == d[1]:
                continue  # horizontal edges are never hit by a horizontal ray
            lo, hi = sorted((c[1], d[1]))
            for b in range(int((lo - y0) / step), min(nb - 1, int((hi - y0) / step)) + 1):
                buckets[b].append((ri, k, c, d))
    attach: dict = {}  # (ring, edge) -> [(t along the edge, hit point, hole ring, its right-most vertex)]
    left = []
    for ri in range(1, len(rings)):
        h = rings[ri]
        hi = max(range(len(h)), key=lambda k: (h[k][0], h[k][1]))
        hx, hy = h[hi]
        best = None
        for (rj, k, c, d) in buckets[min(nb - 1, max(0, int((hy - y0) / step)))]:
            if rj == ri or not (min(c[1], d[1]) <= hy < max(c[1], d[1])):
                continue
            x = c[0] + (hy - c[1]) * (d[0] - c[0]) / (d[1] - c[1])
            if x > hx and (best is None or x < best[0]):
                best = (x, rj, k, c, d)
        if best is None:
            left.append(h)
            continue
        x, rj, k, c, d = best
        span = (d[0] - c[0], d[1] - c[1])
        t = ((x - c[0]) * span[0] + (hy - c[1]) * span[1]) / (span[0] ** 2 + span[1] ** 2)
        attach.setdefault((rj, k), []).append((t, (x, hy), ri, hi))
    for v in attach.values():
        v.sort(key=lambda a: a[0])
    # holes hanging (via other holes) off one whose ray missed are not reached by the walk: bridge them the slow way
    reached, todo = {0}, [0]
    children: dict = {}
    for (rj, _), v in attach.items():
        children.setdefault(rj, []).extend(a[2] for a in v)
    while todo:
        for c in children.get(todo.pop(), []):
            if c not in reached:
                reached.add(c)
                todo.append(c)
    left = [rings[ri] for ri in range(1, len(rings)) if ri not in reached]
    pts = _keyhole_walk(rings, attach)
    return fracture_slow(pts, left) if left else pts


def _keyhole_walk(rings: list, attach: dict) -> list[Pt]:
    """Outline ring 0 with every hole spliced in at its bridge: v_k, hit, hole loop (back to its start), hit, v_k+1."""
    pts: list[Pt] = []
    # stack items: ("ring", ring index, start vertex, step, close) or ("pt", point)
    stack = [("ring", 0, 0, 0, False)]
    while stack:
        item = stack.pop()
        if item[0] == "pt":
            pts.append(item[1])
            continue
        _, ri, start, i, close = item
        r = rings[ri]
        n = len(r)
        if i == n:
            if close:
                pts.append(r[start])
            continue
        k = (start + i) % n
        pts.append(r[k])
        stack.append(("ring", ri, start, i + 1, close))
        for _, ip, hj, hk in reversed(attach.get((ri, k), [])):
            stack.append(("pt", ip))
            stack.append(("ring", hj, hk, 0, True))
            stack.append(("pt", ip))
    return pts


def fracture_slow(outline: list[Pt], holes: list[list[Pt]]) -> list[Pt]:
    """Bridge each hole to the nearest outline vertex the bridge can reach without crossing (for the rare hole the
    ray in fracture() misses)."""
    out = list(outline)
    if signed_area(out) < 0:
        out.reverse()
    hs = []
    for h in holes:
        h = list(h)
        if signed_area(h) > 0:
            h.reverse()  # holes clockwise
        hs.append(h)
    hs.sort(key=lambda h: -max(p[0] for p in h))
    for h in hs:
        # right-most vertex of hole
        hi = max(range(len(h)), key=lambda k: (h[k][0], h[k][1]))
        hp = h[hi]
        best, bestd = 0, float("inf")
        for k, p in enumerate(out):
            d = (p[0] - hp[0]) ** 2 + (p[1] - hp[1]) ** 2
            if p[0] >= hp[0] - 1e-9:
                d *= 0.25
            if d < bestd and not _segment_crosses(hp, p, out, h):
                best, bestd = k, d
        bp = out[best]
        ring = h[hi:] + h[:hi] + [hp]
        out = out[: best + 1] + ring + [bp] + out[best + 1:]
    return out


def _segment_crosses(a: Pt, b: Pt, poly: list[Pt], hole: list[Pt]) -> bool:
    for ring in (poly, hole):
        n = len(ring)
        for i in range(n):
            c, d = ring[i], ring[(i + 1) % n]
            if c == a or c == b or d == a or d == b:
                continue
            if _seg_intersect(a, b, c, d):
                return True
    return False


def _seg_intersect(p1: Pt, p2: Pt, p3: Pt, p4: Pt) -> bool:
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1 = orient(p3, p4, p1)
    d2 = orient(p3, p4, p2)
    d3 = orient(p1, p2, p3)
    d4 = orient(p1, p2, p4)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def simplify_collinear(pts: list[Pt], eps: float = 1e-7) -> list[Pt]:
    if len(pts) < 4:
        return pts
    out = []
    n = len(pts)
    for i in range(n):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) > eps * max(1.0, dist(a, c)):
            out.append(b)
    return out if len(out) >= 3 else pts
