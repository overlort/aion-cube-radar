"""Примерная своя позиция, когда сервер её не присылает.

Свою позицию сервер шлёт только при умениях, сборе, входе в мир. В полёте и на бегу - ничего
(движение клиента уходит на сервер зашифрованным). Зато сервер присылает всё, что попадает в поле
зрения вокруг персонажа: ресурсы и объекты (3436), мобов (4136), чужих игроков (1a37-1c37), и
сообщает, когда они уходят из вида (3536 01 01, 4236). Центр видимого - это и есть мы:
по записям ошибка ~15 м (медиана), против 70-600 м у «последней известной точки» после полёта.
"""

from __future__ import annotations

import math
import struct

FRESH_SEC = 3        # серверной позиции не старше 3 с верим как есть
MOVED_M = 40         # центр видимого уехал дальше 40 м от последней точки - значит, мы улетели
OUT_EVENTS = 2       # или из вида ушло 2+ объекта - тоже улетели
OUTLIER_CM = 15000   # дальше 150 м от медианы - выброс (например, маяки события по всей карте)
MIN_POINTS = 3


def _ok(x: float, y: float) -> bool:
    return math.isfinite(x) and math.isfinite(y) and abs(x) < 1e7 and abs(y) < 1e7


class Locator:
    def __init__(self, skip_tmpl=()):
        self.vis: dict[int, tuple[float, float]] = {}
        self.skip = set(skip_tmpl)
        self.changed = 0.0     # когда последний раз менялся набор видимого
        self.out_since = 0     # сколько объектов ушло из вида после последней серверной точки
        self.fix_ts = 0.0
        self.smooth = None     # сглаженная оценка (x, y)
        self._sm_at = 0.0

    def reset(self):
        self.vis.clear()
        self.smooth = None
        self.out_since = 0

    def on_fix(self, ts: float):
        self.fix_ts = ts
        self.out_since = 0
        self.smooth = None

    def feed(self, op: int, b: bytes, j: int, eid: int, ts: float, me_eid=None):
        try:
            if op == 0x3436 and len(b) >= j + 14:
                if struct.unpack_from("<I", b, j + 2)[0] in self.skip:
                    return
                x, y = struct.unpack_from("<ff", b, j + 6)
            elif op == 0x4136 and len(b) >= j + 17 and b[j + 8] == 2:
                x, y = struct.unpack_from("<ff", b, j + 9)
            elif op in (0x1A37, 0x1B37, 0x1C37) and len(b) >= j + 10 and eid != me_eid:
                x, y = struct.unpack_from("<ff", b, j + 2)
            elif op == 0x4236:
                if self.vis.pop(eid, None):
                    self.out_since += 1
                    self.changed = ts
                return
            elif op == 0x3536 and b[j:j + 2] in (b"\x01\x01", b"\x01\x03", b"\x04\x03"):
                if self.vis.pop(eid, None):
                    if b[j:j + 2] == b"\x01\x01":
                        self.out_since += 1
                    self.changed = ts
                return
            else:
                return
        except struct.error:
            return
        if _ok(x, y):
            if eid not in self.vis:
                self.changed = ts
            self.vis[eid] = (x, y)

    def center(self):
        pts = list(self.vis.values())
        if len(pts) < MIN_POINTS:
            return None
        xs = sorted(p[0] for p in pts)
        ys = sorted(p[1] for p in pts)
        mx, my = xs[len(xs) // 2], ys[len(ys) // 2]
        pts = [p for p in pts if math.hypot(p[0] - mx, p[1] - my) < OUTLIER_CM]
        if len(pts) < MIN_POINTS:
            return None
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)

    def best(self, fix, now: float):
        """fix = (x, y, z, ts) от сервера или None. Вернуть (x, y, z, ts, approx)."""
        if fix and now - fix[3] <= FRESH_SEC:
            return (*fix[:4], False)
        c = self.center()
        if c is None:
            return (*fix[:4], False) if fix else None
        if fix:
            moved = self.out_since >= OUT_EVENTS or math.hypot(c[0] - fix[0], c[1] - fix[1]) > MOVED_M * 100
            if not moved:
                return (*fix[:4], False)
        if self.smooth is None:
            self.smooth = c
        elif self._sm_at != self.changed:  # сглаживаем по изменениям видимого, а не по частоте опроса
            self.smooth = (self.smooth[0] * 0.5 + c[0] * 0.5, self.smooth[1] * 0.5 + c[1] * 0.5)
        self._sm_at = self.changed
        z = fix[2] if fix else 0.0
        return (self.smooth[0], self.smooth[1], z, max(self.changed, fix[3] if fix else 0), True)
