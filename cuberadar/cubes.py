"""Кубы, ресурсы вокруг и своя позиция по трафику (то же, что в aion-economy/econ/gatherwatch.py, без базы).

Пакеты (сервер -> клиент):
    3436  объект появился: <eid varint> <вид u8> 00 <шаблон u32> <x f32> <y f32> <z f32> ... <u64 время появления, мс>
    3536  состояние: <eid> 04 03 = открыт/исчерпан; 01 01 = ушёл из поля зрения
    3836  <eid объекта> <eid игрока> 01 - игрок начал собирать/открывать
    3336  свой персонаж при входе в мир/телепорте; 2b38 2a38 0238 0338 - умения (eid, позиция)
    1156  весь инвентарь при входе; 1b56 изменения (ключи от куба)
Своя позиция в полёте не приходит - её дорисовывает locate.Locator по видимым объектам.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time

from .locate import Locator

OP_APPEAR, OP_STATE = 0x3436, 0x3536
OP_SELF = 0x3336
OP_GATHER_START = 0x3836
POS_OPS = {0x2B38, 0x2A38, 0x0238, 0x0338, OP_SELF}
OP_INVENTORY, OP_INV_CHANGE = 0x1156, 0x1B56
# вход в инстанс (Голдрин и т.п.) приходит БЕЗ 3336: новый свой eid - в 2036, позиция - в 2136
OP_ENTER, OP_PLACE = 0x2036, 0x2136

CUBE_KEY = 630900003      # Key: Hidden Cube (Bound)
# у Скрытого куба несколько шаблонов (1301200 - запись 2026-10-25 23:53, радар его пропустил).
# Незнакомый шаблон, открытый ключом от куба, добавляется сюда сам (см. _learn_cube).
CUBES = {1301199: "Скрытый куб", 1301200: "Скрытый куб"}


def _learned_path() -> str:
    import sys     # папка программы: рядом с exe или корень проекта (как netcap.app_dir, без scapy)
    base = (os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
            else os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    return os.path.join(base, "cube_tmpls.json")


def _load_learned():
    """Виды куба, выученные раньше (открыли ключом объект с новым шаблоном), - видны сразу после запуска."""
    try:
        with open(_learned_path(), encoding="utf-8") as f:
            for t in json.load(f):
                CUBES.setdefault(int(t), "Скрытый куб")
    except (OSError, ValueError, TypeError):
        pass


def _save_learned():
    try:
        with open(_learned_path(), "w", encoding="utf-8") as f:
            json.dump(sorted(t for t in CUBES if t not in (1301199, 1301200)), f)
    except OSError:
        pass


OP_GATHER_STEP = 0x3A36   # <eid узла> <eid игрока> ...; у куба в конце id ключа
KEY_TAIL = struct.pack("<I", CUBE_KEY)
NODE_NAMES = {1550033: "Календула", 1550012: "Асвата"}
RADAR_M = 100             # сервер присылает объекты примерно в 100 м

CUBE_ALERT_SEC = 1800     # отметка куба живёт 30 мин
CUBE_FAR_M = 500          # улетели дальше 500 м - решили не лететь, отметку убираем
CUBE_CHECK_M = 80         # ближе 80 м (по точной позиции), а куба нет - его забрали
CUBE_VIEW_M = 110         # дальше этого сервер куб не присылает: примерная позиция дальше от видимого куба - ошибка


def varint(b: bytes, j: int) -> tuple[int, int]:
    v = s = 0
    while j < len(b):
        v |= (b[j] & 0x7F) << s
        s += 7
        if b[j] < 0x80:
            return v, j + 1
        j += 1
    raise ValueError("обрезанный varint")


def find_pos(b: bytes, start: int = 1):
    for i in range(start, len(b) - 11):
        x, y, z = struct.unpack_from("<fff", b, i)
        if 100 < abs(x) < 2e6 and 100 < abs(y) < 2e6 and -50000 < z < 200000 and abs(z) > 1:
            return x, y, z
    return None


def server_ts(b: bytes, start: int):
    for i in range(start, len(b) - 7):
        v = struct.unpack_from("<Q", b, i)[0]
        if 1_700_000_000_000 < v < 2_100_000_000_000:
            return v / 1000
    return None


def dist(a, b) -> float:
    return ((a[0] - b["x"]) ** 2 + (a[1] - b["y"]) ** 2) ** 0.5


class CubeWatcher:
    def __init__(self):
        self.lock = threading.RLock()     # пакеты идут из потока сниффера, радар читает окно
        self.objs: dict[int, dict] = {}   # видимые кубы и ресурсы
        self.cubes: dict[int, dict] = {}  # отмеченные кубы (живут, пока не убраны)
        self.keys: int | None = None
        self.me: int | None = None
        self._guess: dict[int, int] = {}
        self.fix = None                   # последняя позиция от сервера (x, y, z, когда)
        self.loc = Locator(skip_tmpl=CUBES)
        self._tmpl13: dict[int, int] = {}  # eid -> шаблон объектов 13xxxxx: узнать новый вид куба при открытии ключом
        self.approx = False
        self.new_cubes = 0                # счётчик новых кубов: окно пищит, когда он растёт
        self.live = True
        self._now = 0.0
        self.on_new_cube = None           # f(куб) - зовётся прямо из потока сниффера, без ожидания перерисовки

    def now(self) -> float:
        return time.time() if self.live else self._now

    @property
    def my_pos(self):
        b = self.loc.best(self.fix, self.now())
        if b is None:
            self.approx = False
            return None
        self.approx = b[4]
        if self.approx or self.now() - b[3] > 30:   # примерная или давняя «точная» (бывала 20+ мин назад)
            # куб виден только в ~100 м: если примерная позиция дальше от видимого куба - она врёт (бывало на 1+ км).
            # Подтягиваем её к кубу на 90 м по той же линии - направление сохраняется.
            vis = [o for e, o in self.cubes.items() if e in self.objs]
            if vis:
                c = min(vis, key=lambda o: dist(b, o))
                d = dist(b, c)
                if d > CUBE_VIEW_M * 100:
                    k = 9000 / d
                    self.approx = True
                    return (c["x"] + (b[0] - c["x"]) * k, c["y"] + (b[1] - c["y"]) * k, b[2], b[3])
        return b[:4]

    def _set_fix(self, v):
        self.fix = v
        self.loc.on_fix(v[3])

    # ---------- отметки ----------
    def live_cubes(self, now: float):
        me = self.my_pos
        exact = self.fix if self.fix and not self.approx else None
        for eid, o in list(self.cubes.items()):
            visible = eid in self.objs
            gone = now - o.get("seen", now) > CUBE_ALERT_SEC
            if (not visible and exact and now - exact[3] < 10 and exact[3] - o.get("lost", 0) > 3
                    and dist(exact, o) < CUBE_CHECK_M * 100):
                gone = True
            # улетели дальше 500 м - только по точной позиции и не раньше чем через минуту: по примерной
            # куб иногда пропадал в ту же секунду, как появился (звук был, а отметки нет)
            if (exact and now - exact[3] < 10 and not visible and now - o.get("seen", now) > 60
                    and dist(exact, o) > CUBE_FAR_M * 100):
                gone = True
            if gone:
                self.cubes.pop(eid, None)
                self.objs.pop(eid, None)

    def dismiss(self):
        with self.lock:
            self.cubes.clear()

    def reset_view(self):
        self.objs.clear()
        now = self.now()
        for o in self.cubes.values():
            o["lost"] = now

    def radar(self) -> dict:
        with self.lock:
            now = self.now()
            self.live_cubes(now)
            me = self.my_pos
            items = dict(self.objs)
            items.update(self.cubes)
            objs = []
            for eid, o in list(items.items()):
                cube = o["tmpl"] in CUBES
                if not cube and now - o.get("seen", now) > 3600:
                    self.objs.pop(eid, None)
                    continue
                if me and not cube and dist(me, o) > RADAR_M * 100:
                    continue
                objs.append({"eid": eid, "tmpl": o["tmpl"], "x": o["x"], "y": o["y"], "z": o.get("z", 0), "cube": cube, "spawn": o.get("spawn"),
                             "away": eid not in self.objs,
                             "name": CUBES.get(o["tmpl"]) or NODE_NAMES.get(o["tmpl"]) or "ресурс"})
            return {"me": {"x": me[0], "y": me[1], "z": me[2], "age": round(now - me[3], 1), "approx": self.approx,
                           "zage": round(now - self.fix[3], 1) if self.fix else None} if me else None,
                    "objs": objs, "range": RADAR_M, "keys": self.keys}

    # ---------- пакеты ----------
    @staticmethod
    def _key_count(b: bytes):
        key = struct.pack("<I", CUBE_KEY)
        i = b.find(key)
        found = None
        while i >= 2:
            if b[i - 2:i] == b"\x00\x00" and i + 12 <= len(b):
                found = struct.unpack_from("<Q", b, i + 4)[0]
            i = b.find(key, i + 1)
        return found

    def feed(self, op: int, body: bytes, ts: float):
        with self.lock:
            self._now = ts
            try:
                self._feed(op, body, ts)
            except (ValueError, struct.error, IndexError):
                pass

    def _feed(self, op: int, body: bytes, ts: float):
        if op in (OP_SELF, OP_INVENTORY, OP_ENTER, OP_PLACE):
            self.loc.reset()
        else:
            e, j0 = varint(body, 0)
            self.loc.feed(op, body, j0, e, ts, self.me)
        if op == OP_APPEAR:
            self._appear(body, ts)
        elif op == OP_STATE:
            self._state(body, ts)
        elif op == OP_GATHER_STEP and body.endswith(KEY_TAIL):
            t = self._tmpl13.get(varint(body, 0)[0])
            if t is not None and t not in CUBES:
                CUBES[t] = "Скрытый куб"   # новый вид куба: дальше радар его видит
                _save_learned()
        elif op == OP_GATHER_START:
            node, j = varint(body, 0)
            player, _ = varint(body, j)
            if self.me is None:
                self.me = player
            if player == self.me and node in self.objs:
                o = self.objs[node]
                self._set_fix((o["x"], o["y"], o["z"], ts))
        elif op == OP_INVENTORY:
            self.reset_view()
            self.keys = self._key_count(body) or 0
        elif op == OP_INV_CHANGE:
            k = self._key_count(body)
            if k is not None:
                self.keys = k
        if op == OP_SELF:
            self.reset_view()
        elif op == OP_ENTER and len(body) > 3:
            self.me = varint(body, 2)[0]
            self.reset_view()
        elif op == OP_PLACE and len(body) >= 28:
            x, y, z = struct.unpack_from("<fff", body, 16)
            if 100 < abs(x) + abs(y) < 4e6:
                self._set_fix((x, y, z, ts))
        if op in POS_OPS:
            eid, j = varint(body, 0)
            if op == OP_SELF:
                self.me = eid
            elif self.me is None and op == 0x2A38:
                # запустили посреди игры: свой персонаж чаще всех применяет умения
                self._guess[eid] = self._guess.get(eid, 0) + 1
                if sum(self._guess.values()) >= 15:
                    self.me = max(self._guess, key=self._guess.get)
            if eid == self.me:
                p = find_pos(body, j)
                if p:
                    self._set_fix((*p, ts))

    def _appear(self, b: bytes, ts: float):
        eid, j = varint(b, 0)
        if len(b) < j + 18:
            return
        tmpl = struct.unpack_from("<I", b, j + 2)[0]
        is_cube = tmpl in CUBES
        if 1300000 <= tmpl < 1400000:
            if len(self._tmpl13) > 5000:
                self._tmpl13.clear()
            self._tmpl13[eid] = tmpl
        if not is_cube and not 1550000 <= tmpl < 1560000:
            return
        if eid in self.objs and self.objs[eid]["tmpl"] == tmpl:
            return
        if eid in self.cubes and self.cubes[eid]["tmpl"] == tmpl:
            self.objs[eid] = self.cubes[eid]   # куб на месте: снова виден
            return
        x, y, z = struct.unpack_from("<fff", b, j + 6)
        o = {"tmpl": tmpl, "x": x, "y": y, "z": z, "spawn": server_ts(b, j + 18), "seen": ts}
        self.objs[eid] = o
        if is_cube:
            self.cubes[eid] = o
            self.new_cubes += 1
            if self.on_new_cube and self.live:
                try:
                    self.on_new_cube(o)
                except Exception:
                    pass

    def _state(self, b: bytes, ts: float):
        eid, j = varint(b, 0)
        if b[j:j + 2] == b"\x01\x01":
            o = self.objs.pop(eid, None)
            if o and eid in self.cubes:
                self.cubes[eid]["lost"] = ts
            return
        if b[j:j + 2] == b"\x04\x03":
            self.objs.pop(eid, None)
            self.cubes.pop(eid, None)   # открыли при нас


_load_learned()
