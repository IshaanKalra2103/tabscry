"""The scrying orb: a raymarched glass sphere with two gyroscope rings, rendered as ASCII.

Pipeline mirrors an image ASCII effect: shade a 3D scene per character cell, map luminance to a
glyph ramp, tint by material, then post-process (sweeping scanline, film grain, rim glow).
"""

import math

import numpy as np
from rich.align import Align
from rich.text import Text
from textual.widget import Widget

RAMP = " .'`:-~=+*#%@"


def _mix(a: str, b: str, t: float) -> str:
    ca = [int(a[i : i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def _gradient(*stops: str, n: int = 10) -> list[str]:
    out = []
    for i in range(n):
        t = i / (n - 1) * (len(stops) - 1)
        k = min(int(t), len(stops) - 2)
        out.append(_mix(stops[k], stops[k + 1], t - k))
    return out


_GRADIENT_CACHE: dict[str, dict[int, list[str]]] = {}


def orb_colors(theme_name: str) -> dict[int, list[str]]:
    """material -> dark..bright colour ramp for the given theme"""
    if theme_name not in _GRADIENT_CACHE:
        from .themes import BY_NAME, PALETTES

        stops = BY_NAME.get(theme_name, PALETTES[0]).orb
        _GRADIENT_CACHE[theme_name] = {m: _gradient(*c) for m, c in stops.items()}
    return _GRADIENT_CACHE[theme_name]


def _rot(ax: float, ay: float, az: float) -> np.ndarray:
    cx, sx, cy, sy, cz, sz = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
    rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return rz @ ry @ rx


def _torus(p: np.ndarray, big: float, small: float) -> np.ndarray:
    q = np.sqrt(p[:, 0] ** 2 + p[:, 2] ** 2) - big
    return np.sqrt(q**2 + p[:, 1] ** 2) - small


class OrbRenderer:
    def __init__(self, cols: int, rows: int, seed: int = 7):
        self.cols, self.rows = cols, rows
        ys, xs = np.mgrid[0:rows, 0:cols]
        s = 2.05 / rows
        u = (xs + 0.5 - cols / 2) * s * 0.5  # terminal cells are ~2x taller than wide
        v = -(ys + 0.5 - rows / 2) * s
        d = np.stack([u, v, np.full_like(u, 2.75)], axis=-1).reshape(-1, 3)
        self.rd = d / np.linalg.norm(d, axis=1, keepdims=True)
        self.ro = np.array([0.0, 0.0, -3.4])
        rng = np.random.default_rng(seed)
        self.stars = rng.random(rows * cols) < 0.018
        self.star_phase = rng.random(rows * cols) * math.tau
        self.rng = np.random.default_rng()
        self.light = np.array([-0.55, 0.7, -0.45]) / np.linalg.norm([-0.55, 0.7, -0.45])

    def scene(self, p: np.ndarray, t: float, energy: float):
        ra = _rot(t * 0.9, t * 0.55, 0.35).T
        rb = _rot(math.pi / 2 + t * 0.4, -t * 0.8, t * 0.25).T
        pulse = 0.2 + 0.04 * math.sin(t * 3.1) + 0.06 * energy
        da = _torus(p @ ra, 0.74, 0.17)
        db = _torus(p @ rb, 0.47, 0.14)
        dc = np.linalg.norm(p, axis=1) - pulse
        d = np.minimum(np.minimum(da, db), dc)
        mat = np.where(d == da, 1, np.where(d == db, 2, 3))
        return d, mat

    def render(self, t: float, energy: float = 0.0, theme: str = "tabscry") -> Text:
        """energy 0..1 spins things up (used while searching)."""
        t = t * (1 + 1.5 * energy)
        n = self.rd.shape[0]
        ro, rd = self.ro, self.rd

        # glass shell: analytic ray/sphere intersection, rim-lit by fresnel
        b = rd @ ro
        c = ro @ ro - 1.08**2
        disc = b * b - c
        inside = disc > 0
        t_enter = np.where(inside, -b - np.sqrt(np.maximum(disc, 0)), 0.0)

        # sphere-trace the rings + core, starting at the shell to save steps
        dist = np.where(inside, t_enter, 99.0)
        hit = np.zeros(n, bool)
        mat = np.zeros(n, int)
        active = inside.copy()
        for _ in range(40):
            if not active.any():
                break
            idx = np.nonzero(active)[0]
            p = ro + rd[idx] * dist[idx, None]
            d, m = self.scene(p, t, energy)
            dist[idx] += d
            done = d < 0.02
            hit[idx[done]] = True
            mat[idx[done]] = m[done]
            active[idx[done | (dist[idx] > 5.5)]] = False

        lum = np.zeros(n)
        if hit.any():
            idx = np.nonzero(hit)[0]
            p = ro + rd[idx] * dist[idx, None]
            e = 0.003
            grad = np.stack([
                self.scene(p + [e, 0, 0], t, energy)[0] - self.scene(p - [e, 0, 0], t, energy)[0],
                self.scene(p + [0, e, 0], t, energy)[0] - self.scene(p - [0, e, 0], t, energy)[0],
                self.scene(p + [0, 0, e], t, energy)[0] - self.scene(p - [0, 0, e], t, energy)[0],
            ], axis=1)
            nrm = grad / (np.linalg.norm(grad, axis=1, keepdims=True) + 1e-9)
            diff = np.clip(nrm @ self.light, 0, 1)
            refl = self.light - 2 * (nrm @ self.light)[:, None] * nrm
            spec = np.clip((refl * rd[idx]).sum(axis=1), 0, 1) ** 18
            depth = np.clip(1.2 - (dist[idx] - 2.4) * 0.45, 0.35, 1)
            lum[idx] = np.clip((0.3 + 0.65 * diff + 0.8 * spec) * depth, 0.28, 1)
            core = mat[idx] == 3
            lum[idx[core]] = np.clip(lum[idx[core]] * 0.6 + 0.45 + 0.25 * math.sin(t * 3.1), 0, 1)

        # shell glow where nothing solid was hit
        normal_in = (ro + rd * t_enter[:, None]) / 1.08
        fres = np.clip(1 - np.abs((normal_in * rd).sum(axis=1)), 0, 1) ** 1.6
        shell = inside & ~hit
        mat[shell] = 4
        glow = fres[shell] * (0.9 + 0.12 * math.sin(t * 1.7))
        lum[shell] = np.where(glow > 0.3, glow, 0.0)  # keep the inside of the glass clear

        # stars (twinkle) outside the orb
        sky = ~inside & self.stars
        mat[sky] = 5
        lum[sky] = 0.35 + 0.35 * np.sin(self.star_phase[sky] + t * 2.0)

        # post: sweeping scanline + grain
        lum = lum.reshape(self.rows, self.cols)
        band = (t * 7.0) % (self.rows + 8) - 4
        rows = np.arange(self.rows)[:, None]
        lum = lum * (1 + 0.55 * np.exp(-((rows - band) ** 2) / 1.6))
        lum = np.clip(lum, 0, 1).reshape(-1)
        grain = (self.rng.random(n) < 0.03) & (lum > 0.08)
        lum[grain] = np.clip(lum[grain] + self.rng.normal(0, 0.18, grain.sum()), 0, 1)

        return self._to_text(lum, mat, orb_colors(theme))

    def _to_text(self, lum: np.ndarray, mat: np.ndarray, palettes: dict[int, list[str]]) -> Text:
        glyph_idx = np.minimum((lum * len(RAMP)).astype(int), len(RAMP) - 1)
        text = Text(no_wrap=True, overflow="crop")
        cols = self.cols
        for r in range(self.rows):
            run_chars: list[str] = []
            run_style = None
            for i in range(r * cols, (r + 1) * cols):
                g = glyph_idx[i]
                m = mat[i]
                if g == 0 or m == 0:
                    ch, style = " ", None
                else:
                    ch = RAMP[g]
                    pal = palettes[m]
                    style = pal[min(int(lum[i] * len(pal)), len(pal) - 1)]
                    if m == 5:
                        ch = "·" if lum[i] < 0.5 else "+"
                if style != run_style and run_chars:
                    text.append("".join(run_chars), run_style)
                    run_chars = []
                run_style = style
                run_chars.append(ch)
            if run_chars:
                text.append("".join(run_chars), run_style)
            if r < self.rows - 1:
                text.append("\n")
        return text


class Orb(Widget):
    """Animated orb widget. Set `energy` (0..1) to make it spin up."""

    DEFAULT_CSS = """
    Orb { width: 1fr; height: 22; }
    """

    def __init__(self, cols: int = 56, rows: int = 22, **kwargs):
        super().__init__(**kwargs)
        self.renderer = OrbRenderer(cols, rows)
        self.energy = 0.0
        self.target_energy = 0.0
        self.t = 0.0
        self.frame: Text = Text()

    def on_mount(self):
        self.frame = self.renderer.render(0, theme=self.app.theme)
        self.set_interval(1 / 24, self.advance)

    def advance(self):
        self.energy += (self.target_energy - self.energy) * 0.08
        self.t += 1 / 24
        self.frame = self.renderer.render(self.t, self.energy, self.app.theme)
        self.refresh()

    def render(self) -> Align:
        return Align.center(self.frame)
