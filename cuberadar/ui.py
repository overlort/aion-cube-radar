"""Оверлей радара кубов (Tk), логика замка - как в трекере душ петов.

    квадрат  радар: вы в центре, север сверху, круги 50/100 м; куб - звезда, дальше 100 м - стрелка на краю
    🔒/🔓     замок (своё окно под радаром, кликабелен всегда): закрыт - радар и шестерёнка пропускают клики в игру;
             открыт - радар можно перетаскивать мышью
    ⚙        настройки: размер, прозрачность, ориентация, ресурсы, звук (выбор, громкость, тест), выход
    ✕        закрыть радар

Прозрачность и клики сквозь окно - средствами Windows (WS_EX_LAYERED + SetLayeredWindowAttributes, WS_EX_TRANSPARENT).
"""

from __future__ import annotations

import json
import math
import os
import time

from . import sound

TITLE = "AION 2 · Радар кубов"
BG = "#17142a"
PANEL = "#1f1b36"
FIELD = "#1a1630"
LINE = "#3a3356"
CROSS = "#2c2744"
FG = "#ebe6f7"
MUTED = "#a39bc0"
FAINT = "#6f6890"
GOLD = "#e9c46a"
RED = "#ff8f8f"
ME = "#a98bff"
NODE_COLORS = {1550033: "#6fdcae", 1550012: "#c9a06a"}

SIZE_MIN, SIZE_MAX, SIZE_STEP = 140, 700, 20
OPACITY_MIN = 0.25
ORIENT_NAMES = ["0", "90°", "180°", "270°", "зеркало", "зеркало 90°", "зеркало 180°", "зеркало 270°"]

DEFAULTS = {
    "x": 80, "y": 200, "size": 260, "opacity": 0.8, "locked": False, "orient": 6,
    "mode": "cube",                  # cube - кубы и ресурсы; bingo - рейдовое «бинго» (где сейв)
    "show_res": True, "sound_on": True, "sound": sound.DEFAULT, "volume": 70, "repeats": 2, "custom_sound": "",
}


# ---------- Windows ----------
def hwnd_of(win) -> int:
    import ctypes
    return ctypes.windll.user32.GetAncestor(win.winfo_id(), 2) or win.winfo_id()


def keep_on_top(*wins):
    """Игра при запуске/перезапуске встаёт поверх всех окон (в т.ч. «поверх всех») - периодически
    возвращаем наши окна наверх, не забирая фокус и не показывая спрятанные."""
    for win in wins:
        if win is None:
            continue
        try:
            if not win.winfo_viewable():
                continue
            if os.name != "nt":
                win.attributes("-topmost", True)
                continue
            import ctypes
            ctypes.windll.user32.SetWindowPos(hwnd_of(win), -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)  # NOSIZE|NOMOVE|NOACTIVATE
        except Exception:
            pass


def apply_style(win, opacity: float, click_through: bool):
    if os.name != "nt":
        try:
            win.attributes("-alpha", opacity)
        except Exception:
            pass
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = hwnd_of(win)
        GWL_EXSTYLE, LAYERED, TRANSPARENT, TOOLWINDOW, LWA_ALPHA = -20, 0x80000, 0x20, 0x80, 0x2
        st = u.GetWindowLongW(hwnd, GWL_EXSTYLE) | LAYERED | TOOLWINDOW   # без кнопки на панели задач
        st = (st | TRANSPARENT) if click_through else (st & ~TRANSPARENT)
        u.SetWindowLongW(hwnd, GWL_EXSTYLE, st)
        u.SetLayeredWindowAttributes(hwnd, 0, int(max(OPACITY_MIN, min(1.0, opacity)) * 255), LWA_ALPHA)
    except Exception:
        pass


def bring_to_top(win):
    if os.name != "nt":
        win.lift()
        return
    try:
        import ctypes
        ctypes.windll.user32.SetWindowPos(hwnd_of(win), -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10 | 0x40)
    except Exception:
        win.lift()


def cube_age(ob) -> str:
    """Сколько куб уже стоит: «новый» - только что появился рядом с вами, конкуренты его ещё не видели."""
    sp = ob.get("spawn")
    if not sp:
        return ""
    a = time.time() - sp
    return " · новый" if a < 30 else f" · {int(a // 60)} мин" if a >= 60 else f" · {int(a)} с"

HEIGHT_M = 3          # выше/ниже на 3 м и больше - показываем ▲/▼
HEIGHT_FRESH = 90     # своя высота - из точной позиции; старше 90 с уже не верим (в полёте меняется)


def cube_height(ob, me) -> str:
    """« ▲12 м» / « ▼5 м» - куб выше или ниже вас (по последней точной позиции)."""
    if not me or me.get("z") is None or me.get("zage") is None or me["zage"] > HEIGHT_FRESH or ob.get("z") is None:
        return ""
    dz = (ob["z"] - me["z"]) / 100
    if abs(dz) < HEIGHT_M:
        return ""
    return f" {'▲' if dz > 0 else '▼'}{abs(dz):.0f} м"



def to_screen(dx: float, dy: float, o: int):
    x, y = dx, -dy
    if o >= 4:
        x = -x
    for _ in range(o % 4):
        x, y = -y, x
    return x, y


class RadarApp:
    def __init__(self, watcher, session, on_close, state_path: str):
        import tkinter as tk
        self.tk = tk
        self.w = watcher
        self.s = session
        self.on_close = on_close
        self.state_path = state_path
        self.st = dict(DEFAULTS)
        self.st.update(self._load())
        self.st["size"] = max(SIZE_MIN, min(SIZE_MAX, int(self.st["size"])))
        self.settings_win = None
        self.seen_cubes = watcher.new_cubes
        self.sound_done = watcher.new_cubes
        watcher.on_new_cube = self._instant_sound   # звук сразу из потока сниффера, не ждём перерисовки
        self.phase = 0
        self.flash_until = 0.0

        root = self.root = tk.Tk()
        root.title(TITLE)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=BG)
        self.cv = tk.Canvas(root, bg=BG, highlightthickness=2, highlightbackground=LINE, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<ButtonPress-1>", self._drag_start)
        self.cv.bind("<B1-Motion>", self._drag_move)
        self.cv.bind("<ButtonRelease-1>", lambda e: self.save())
        self._geometry()

        # под радаром - замок (кликабелен всегда) и шестерёнка (как радар: при закрытом замке пропускает клики)
        self.lock_win = self._float_window()
        self.tools_win = self._float_window()
        self.btn_lock = self._icon(self.lock_win, "", self.toggle_lock)
        self.btn_lock.pack(side="left", padx=1)
        self.btn_gear = self._icon(self.tools_win, "⚙", self.open_settings)
        self.btn_gear.pack(side="left", padx=1)
        self.btn_close = self._icon(self.tools_win, "✕", self.close)
        self.btn_close.configure(font=("Segoe UI", 11, "bold"))
        self.btn_close.bind("<Enter>", lambda e: self.btn_close.configure(fg=RED))
        self.btn_close.bind("<Leave>", lambda e: self.btn_close.configure(fg=MUTED))
        self.btn_close.pack(side="left", padx=1)
        self._icons()

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.update_idletasks()
        self._place_icons(force=True)
        root.after(150, self._restyle)
        root.after(1000, self._restyle)
        self._tick()

    # ---------- окна и кнопки ----------
    def _float_window(self):
        w = self.tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=BG, highlightthickness=1, highlightbackground=LINE)
        return w

    def _icon(self, parent, text, command):
        b = self.tk.Label(parent, text=text, bg=BG, fg=MUTED, font=("Segoe UI Emoji", 11), cursor="hand2", width=2)
        b.bind("<Button-1>", lambda e: command())
        return b

    def _icons(self):
        locked = self.st["locked"]
        self.btn_lock.configure(text="🔒" if locked else "🔓", fg=GOLD if locked else MUTED)

    def _geometry(self):
        n = self.st["size"]
        self.root.geometry(f"{n}x{n}+{int(self.st['x'])}+{int(self.st['y'])}")

    def _place_icons(self, force=False):
        try:
            # значки - в ряд под радаром: замок, за ним шестерёнка и крестик
            self.lock_win.update_idletasks()
            self.tools_win.update_idletasks()
            lw = max(self.lock_win.winfo_width(), 26)
            ih = max(self.lock_win.winfo_height(), self.tools_win.winfo_height(), 26)
            x = self.root.winfo_x()
            y = self.root.winfo_y() + self.root.winfo_height() + 3
            if y + ih > self.root.winfo_screenheight():        # у нижнего края экрана - над радаром
                y = self.root.winfo_y() - ih - 3
            tx = x + lw + 3
            if force or (x, y, tx) != getattr(self, "_placed", None):
                self.lock_win.geometry(f"+{x}+{y}")
                self.tools_win.geometry(f"+{tx}+{y}")
                self._placed = (x, y, tx)
            bring_to_top(self.tools_win)
            bring_to_top(self.lock_win)
            if self.settings_win is not None:   # клик по радару не должен прятать настройки
                try:
                    if self.settings_win.winfo_exists() and self.settings_win.state() != "withdrawn":
                        bring_to_top(self.settings_win)
                except Exception:
                    self.settings_win = None
        except Exception:
            pass

    def _restyle(self):
        locked = self.st["locked"]
        apply_style(self.root, self.st["opacity"], locked)
        apply_style(self.tools_win, max(self.st["opacity"], 0.6), locked)
        apply_style(self.lock_win, max(self.st["opacity"], 0.6), False)

    def toggle_lock(self):
        self.st["locked"] = not self.st["locked"]
        self._restyle()
        self._icons()
        self.save()

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        if self.st["locked"]:
            return
        self.st["x"], self.st["y"] = e.x_root - self._dx, e.y_root - self._dy
        self.root.geometry(f"+{self.st['x']}+{self.st['y']}")
        self._place_icons()

    def set_size(self, n: int):
        self.st["size"] = max(SIZE_MIN, min(SIZE_MAX, int(n)))
        self._geometry()
        self.root.update_idletasks()
        self._place_icons(force=True)
        self.save()

    # ---------- настройки ----------
    def open_settings(self):
        tk = self.tk
        if self.settings_win is not None:
            try:
                if not self.settings_win.winfo_exists():
                    raise RuntimeError
                self.settings_win.deiconify()
                bring_to_top(self.settings_win)
                self.settings_win.focus_force()
                return
            except Exception:
                self.settings_win = None
        w = self.settings_win = tk.Toplevel(self.root)
        from . import __version__
        w.title(f"{TITLE} {__version__} — настройки")
        w.configure(bg=BG)
        w.resizable(False, False)
        w.attributes("-topmost", True)
        w.geometry(f"+{self.root.winfo_x() + self.root.winfo_width() + 12}+{self.root.winfo_y()}")
        lbl = dict(bg=BG, fg=FG, font=("Segoe UI", 9))
        mut = dict(bg=BG, fg=MUTED, font=("Segoe UI", 9))
        btn = dict(bg=PANEL, fg=FG, activebackground=LINE, activeforeground=FG, relief="flat", bd=0,
                   font=("Segoe UI", 9), cursor="hand2", padx=10, pady=2)
        scale_kw = dict(orient="horizontal", showvalue=False, length=190, bg=GOLD, fg=FG, troughcolor=PANEL,
                        activebackground="#f0d48a", highlightthickness=0, bd=0, sliderrelief="flat",
                        sliderlength=16, width=12)

        def section(text):
            tk.Label(w, text=text, bg=BG, fg=GOLD, font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(12, 2))

        def row():
            f = tk.Frame(w, bg=BG)
            f.pack(fill="x", padx=14, pady=2)
            return f

        # --- режим ---
        section("Режим")
        r = row()
        v_mode = tk.StringVar(value=self.st.get("mode", "cube"))

        def on_mode():
            self.st["mode"] = v_mode.get()
            self.save()
            self._draw(self.w.radar())
        for val, text in (("cube", "Кубы"), ("bingo", "Бинго (рейд)")):
            tk.Radiobutton(r, text=text, value=val, variable=v_mode, command=on_mode, bg=BG, fg=FG, selectcolor=PANEL,
                           activebackground=BG, activeforeground=FG, font=("Segoe UI", 9, "bold"), indicatoron=False,
                           padx=12, pady=3, relief="flat", bd=0, highlightthickness=0, cursor="hand2"
                           ).pack(side="left", padx=(0, 6))
        tk.Label(w, text="Бинго: за ~19 с до взрыва показывает, в какой клетке поля 6×6 белый квадрат-сейв.",
                 wraplength=330, justify="left", bg=BG, fg=FAINT, font=("Segoe UI", 8)).pack(anchor="w", padx=14)

        # --- радар ---
        section("Радар")
        r = row()
        tk.Label(r, text="Размер", width=14, anchor="w", **lbl).pack(side="left")
        size_lbl = tk.Label(r, text="", width=7, **mut)

        def size_step(d):
            self.set_size(self.st["size"] + d)
            size_lbl.configure(text=f"{self.st['size']} px")
        tk.Button(r, text="−", width=2, command=lambda: size_step(-SIZE_STEP), **btn).pack(side="left")
        size_lbl.pack(side="left", padx=4)
        tk.Button(r, text="+", width=2, command=lambda: size_step(SIZE_STEP), **btn).pack(side="left")
        size_lbl.configure(text=f"{self.st['size']} px")

        r = row()
        tk.Label(r, text="Непрозрачность", width=14, anchor="w", **lbl).pack(side="left")
        op_lbl = tk.Label(r, text="", width=5, **mut)

        def on_opacity(v):
            self.st["opacity"] = int(float(v)) / 100
            op_lbl.configure(text=f"{int(float(v))}%")
            self._restyle()
        sc = tk.Scale(r, from_=int(OPACITY_MIN * 100), to=100, resolution=5, command=on_opacity, **scale_kw)
        sc.set(int(round(self.st["opacity"] * 100)))
        sc.pack(side="left")
        op_lbl.pack(side="left")

        r = row()
        tk.Label(r, text="Ориентация", width=14, anchor="w", **lbl).pack(side="left")
        or_lbl = tk.Label(r, text="", width=12, anchor="w", **mut)

        def orient(rot):
            o = self.st["orient"]
            o = (o // 4) * 4 + (o % 4 + 1) % 4 if rot else (o + 4) % 8
            self.st["orient"] = o
            or_lbl.configure(text=ORIENT_NAMES[o])
            self.save()
        tk.Button(r, text="Повернуть", command=lambda: orient(True), **btn).pack(side="left")
        tk.Button(r, text="Отразить", command=lambda: orient(False), **btn).pack(side="left", padx=4)
        or_lbl.pack(side="left", padx=4)
        or_lbl.configure(text=ORIENT_NAMES[self.st["orient"]])
        tk.Label(w, text="Если север на радаре не совпадает с картой игры — поверните/отразите.",
                 wraplength=330, justify="left", bg=BG, fg=FAINT, font=("Segoe UI", 8)).pack(anchor="w", padx=14)

        chk = dict(bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                   font=("Segoe UI", 9), anchor="w", highlightthickness=0, bd=0)
        v_res = tk.BooleanVar(value=bool(self.st["show_res"]))

        def on_res():
            self.st["show_res"] = bool(v_res.get())
            self.save()
        tk.Checkbutton(w, text="Показывать ресурсы вокруг (100 м)", variable=v_res, command=on_res, **chk
                       ).pack(fill="x", padx=12, pady=(4, 0))
        r = row()
        tk.Button(r, text="Убрать отмеченные кубы", command=self.w.dismiss, **btn).pack(side="left")

        # --- звук ---
        section("Звук при новом кубе")
        v_on = tk.BooleanVar(value=bool(self.st["sound_on"]))

        def on_sound():
            self.st["sound_on"] = bool(v_on.get())
            self.save()
        tk.Checkbutton(w, text="Оповещать звуком", variable=v_on, command=on_sound, **chk).pack(fill="x", padx=12)

        r = row()
        tk.Label(r, text="Звук", width=14, anchor="w", **lbl).pack(side="left")
        names = sound.choices()
        v_snd = tk.StringVar(value=self.st["sound"] if self.st["sound"] in names else sound.DEFAULT)
        om = tk.OptionMenu(r, v_snd, *names)
        om.configure(bg=PANEL, fg=FG, activebackground=LINE, activeforeground=FG, relief="flat", bd=0,
                     highlightthickness=0, font=("Segoe UI", 9), width=22, anchor="w")
        om["menu"].configure(bg=PANEL, fg=FG, activebackground=LINE, activeforeground=FG, font=("Segoe UI", 9))
        om.pack(side="left")
        file_lbl = tk.Label(w, text="", wraplength=330, justify="left", bg=BG, fg=FAINT, font=("Segoe UI", 8))

        def show_file():
            if v_snd.get() == sound.CUSTOM:
                p = self.st.get("custom_sound") or ""
                file_lbl.configure(text=("Файл: " + os.path.basename(p)) if p else "Выберите .wav файл")
                file_lbl.pack(anchor="w", padx=14, after=r)
            else:
                file_lbl.pack_forget()

        def on_choice(*_):
            name = v_snd.get()
            if name == sound.CUSTOM:
                from tkinter import filedialog
                p = filedialog.askopenfilename(parent=w, title="Звук оповещения (.wav)",
                                               filetypes=[("Звук WAV", "*.wav"), ("Все файлы", "*.*")])
                if p:
                    self.st["custom_sound"] = p
                elif not self.st.get("custom_sound"):
                    v_snd.set(self.st["sound"] if self.st["sound"] != sound.CUSTOM else sound.DEFAULT)
                    return
            self.st["sound"] = v_snd.get()
            show_file()
            self.save()
            self.test_sound()
        v_snd.trace_add("write", on_choice)
        show_file()

        r = row()
        tk.Label(r, text="Громкость", width=14, anchor="w", **lbl).pack(side="left")
        vol_lbl = tk.Label(r, text="", width=5, **mut)

        def on_vol(v):
            self.st["volume"] = int(float(v))
            vol_lbl.configure(text=f"{int(float(v))}%")
        sv = tk.Scale(r, from_=0, to=100, resolution=5, command=on_vol, **scale_kw)
        sv.set(int(self.st["volume"]))
        sv.bind("<ButtonRelease-1>", lambda e: (self.save(), self.test_sound()))
        sv.pack(side="left")
        vol_lbl.pack(side="left")

        r = row()
        tk.Label(r, text="Повторов", width=14, anchor="w", **lbl).pack(side="left")
        rep_lbl = tk.Label(r, text=str(self.st["repeats"]), width=3, **mut)

        def rep(d):
            self.st["repeats"] = max(1, min(5, int(self.st["repeats"]) + d))
            rep_lbl.configure(text=str(self.st["repeats"]))
            self.save()
        tk.Button(r, text="−", width=2, command=lambda: rep(-1), **btn).pack(side="left")
        rep_lbl.pack(side="left", padx=4)
        tk.Button(r, text="+", width=2, command=lambda: rep(1), **btn).pack(side="left")
        tk.Button(r, text="▶ Тест звука", command=self.test_sound, **btn).pack(side="left", padx=(16, 0))

        # --- низ ---
        self.status_lbl = tk.Label(w, text="", wraplength=330, justify="left", **mut)
        self.status_lbl.pack(anchor="w", padx=14, pady=(14, 0))
        r = row()
        r.pack_configure(pady=(10, 12))

        def done():
            self.save()
            w.destroy()
            self.settings_win = None
        tk.Button(r, text="Закрыть радар", command=self.close, bg="#3a1f2a", fg=RED, activebackground="#4a2534",
                  activeforeground=RED, relief="flat", bd=0, font=("Segoe UI", 9), cursor="hand2", padx=10,
                  pady=2).pack(side="left")
        tk.Button(r, text="Готово", command=done, **btn).pack(side="right")
        w.protocol("WM_DELETE_WINDOW", done)

    def _instant_sound(self, cube):
        self.sound_done = self.w.new_cubes
        if self.st["sound_on"]:
            sound.play(self.st["sound"], self.st["volume"], self.st["repeats"], self.st.get("custom_sound", ""))

    def test_sound(self):
        sound.play(self.st["sound"], self.st["volume"], 1, self.st.get("custom_sound", ""))

    def _status_text(self, r) -> str:
        if not self.s.locked:
            return "Ищу поток игры… (вы в мире? программа запущена от администратора?)"
        parts = ["Игра найдена."]
        me = r.get("me")
        if not me:
            parts.append("Своя позиция появится после умения, сбора или телепорта.")
        elif me["approx"]:
            parts.append("Позиция примерная (±15 м, по объектам вокруг) — в полёте сервер её не присылает.")
        else:
            parts.append(f"Позиция обновлена {me['age']:.0f} с назад.")
        if r.get("keys") is not None:
            parts.append(f"Ключей от куба: {r['keys']}.")
        return " ".join(parts)

    # ---------- отрисовка ----------
    def _tick(self):
        try:
            r = self.w.radar()
            if self.w.new_cubes != self.seen_cubes:
                self.seen_cubes = self.w.new_cubes
                self.flash_until = time.time() + 6
                if self.st["sound_on"] and self.sound_done != self.w.new_cubes:
                    self.sound_done = self.w.new_cubes
                    sound.play(self.st["sound"], self.st["volume"], self.st["repeats"], self.st.get("custom_sound", ""))
            self.phase ^= 1
            self._draw(r)
            if self.settings_win is not None:
                try:
                    self.status_lbl.configure(text=self._status_text(r))
                except Exception:
                    pass
            self._place_icons()
        except Exception as e:
            print("ошибка отрисовки:", e)
        if time.time() - getattr(self, "_top_at", 0) > 1.5:
            self._top_at = time.time()
            keep_on_top(self.root, self.tools_win, self.lock_win, self.settings_win)
        self.root.after(300, self._tick)

    def _text(self, x, y, text, fill, size, bold=False, outline=True, anchor="center"):
        font = ("Segoe UI", size, "bold" if bold else "normal")
        if outline:
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                self.cv.create_text(x + dx, y + dy, text=text, fill="#000", font=font, anchor=anchor)
        self.cv.create_text(x, y, text=text, fill=fill, font=font, anchor=anchor)

    def _star(self, x, y, R, fill=GOLD):
        pts = []
        for i in range(10):
            rr = R if i % 2 == 0 else R * 0.42
            pts += [x + rr * math.sin(i * math.pi / 5), y - rr * math.cos(i * math.pi / 5)]
        self.cv.create_polygon(pts, fill=fill, outline="#000")

    def _draw(self, r):
        cv = self.cv
        cv.delete("all")
        n = self.st["size"]
        f = n / 300
        c = n / 2
        R = c - 16 * max(f, 0.8)
        cubes_now = [o for o in r["objs"] if o["cube"]]
        cv.configure(highlightbackground=GOLD if cubes_now and (time.time() < self.flash_until or any(not o["away"] for o in cubes_now)) else LINE)
        cv.create_oval(c - R, c - R, c + R, c + R, fill=FIELD, outline=LINE)
        fs = max(7, int(9 * f))
        for side, x, y in (("С", c, 9 * max(f, .8)), ("Ю", c, n - 9 * max(f, .8)), ("З", 8 * max(f, .8), c), ("В", n - 8 * max(f, .8), c)):
            cv.create_text(x, y, text=side, fill=RED if side == "С" else MUTED, font=("Segoe UI", fs + 1, "bold"))
        if not self.s.locked:
            self._text(c, c, "ищу игру…", MUTED, fs + 1)
            return
        if self.st.get("mode") == "bingo":
            self._draw_bingo(c, n, f, fs)
            return
        me = r.get("me")
        if not me:
            self._text(c, c - 8, "позиция появится", MUTED, fs)
            self._text(c, c + 8, "после умения или сбора", MUTED, fs)
            return
        k = R / (r["range"] * 100)
        for m in (50, 100):
            rr = m * 100 * k
            cv.create_oval(c - rr, c - rr, c + rr, c + rr, outline=LINE, dash=(3, 4))
            cv.create_text(c + 3, c - rr + 8, text=f"{m} м", fill=FAINT, font=("Segoe UI", max(6, int(8 * f))), anchor="w")
        cv.create_line(c, c - R, c, c + R, fill=CROSS)
        cv.create_line(c - R, c, c + R, c, fill=CROSS)
        o = self.st["orient"]
        pts = []
        for ob in r["objs"]:
            x, y = to_screen(ob["x"] - me["x"], ob["y"] - me["y"], o)
            d = math.hypot(x, y) * k
            sc = R / d if d > R else 1
            pts.append((ob, c + x * k * sc, c + y * k * sc, d > R, math.hypot(x, y) / 100))
        if self.st["show_res"]:
            for ob, sx, sy, out, dist in pts:
                if ob["cube"]:
                    continue
                rad = 2.5 if out else 4.5 * max(f, .7)
                cv.create_oval(sx - rad, sy - rad, sx + rad, sy + rad,
                               fill=FAINT if out else NODE_COLORS.get(ob["tmpl"], "#8f86b5"), outline="")
        for ob, sx, sy, out, dist in pts:
            if not ob["cube"]:
                continue
            near = "≈" if me["approx"] else ""
            if out:
                ang = math.atan2(sy - c, sx - c)
                ax, ay = c + math.cos(ang) * (R - 14 * f), c + math.sin(ang) * (R - 14 * f)
                cv.create_line(c, c, ax, ay, fill=GOLD, dash=(4, 4), width=1)
                s = 14 * max(f, .8) * (1.15 if self.phase else 1)
                ca, sa = math.cos(ang), math.sin(ang)
                poly = [(s, 0), (-0.6 * s, -0.7 * s), (-0.25 * s, 0), (-0.6 * s, 0.7 * s)]
                cv.create_polygon([v for px, py in poly for v in (ax + px * ca - py * sa, ay + px * sa + py * ca)],
                                  fill=GOLD, outline="#000")
                tx, ty = c + ca * (R - 52 * f), c + sa * (R - 52 * f)
                self._star(tx - 22 * max(f, .8), ty, 6 * max(f, .8))
                self._text(tx + 4 * max(f, .8), ty, f"{near}{dist:.0f} м{cube_height(ob, me)}{cube_age(ob)}", GOLD, fs + 1, bold=True)
            else:
                rad = (13 if ob["away"] else (16 if self.phase else 19)) * max(f, .8)
                cv.create_oval(sx - rad, sy - rad, sx + rad, sy + rad, outline=GOLD, width=2,
                               dash=(3, 3) if ob["away"] else None)
                self._star(sx, sy, 8 * max(f, .8))
                ly = sy + rad + 8 * max(f, .8)
                if ly > n - 14:
                    ly = sy - rad - 8 * max(f, .8)
                self._text(sx, ly, f"{near}{dist:.0f} м{cube_height(ob, me)}{cube_age(ob)}", GOLD, fs, bold=True)
        if me["approx"]:
            rr = max(8, 15 * 100 * k)
            cv.create_oval(c - rr, c - rr, c + rr, c + rr, outline=ME, dash=(3, 3))
            cv.create_oval(c - 5, c - 5, c + 5, c + 5, outline=ME, width=2)
        else:
            cv.create_oval(c - 6 * max(f, .8), c - 6 * max(f, .8), c + 6 * max(f, .8), c + 6 * max(f, .8), fill=ME, outline="#fff", width=1.5)
        # углы: давность позиции и ключи
        self._text(5, n - 6, "≈ позиция" if me["approx"] else f"{me['age']:.0f} с", FAINT, max(6, int(8 * f)), anchor="sw", outline=False)
        if r.get("keys") is not None:
            no_key = r["keys"] == 0
            self._text(n - 5, n - 6, "нет ключа" if no_key else f"ключей {r['keys']}", RED if no_key and cubes_now else FAINT,
                       max(6, int(8 * f)), anchor="se", outline=False)

    def _draw_bingo(self, c, n, f, fs):
        """Режим «бинго»: крупно номер клетки сейва и поле, где сейв белый. Своя позиция не нужна."""
        cv = self.cv
        b = self.w.bingo_state(self.st["orient"], to_screen)
        if not b:
            cv.configure(highlightbackground=LINE)
            self._text(c, c - 12 * max(f, .8), "БИНГО", MUTED, fs + 6, bold=True)
            self._text(c, c + 10 * max(f, .8), "жду поле…", FAINT, fs + 1)
            return
        cv.configure(highlightbackground="#ffffff")
        big = fs + 5
        self._text(c, 34 * max(f, .8), f"{b['row']}-й сверху", "#ffffff", big, bold=True)
        self._text(c, 34 * max(f, .8) + big * 1.6, f"{b['col']}-й слева", "#ffffff", big, bold=True)
        cell = min(n * 0.55 / max(b["ncol"], 1), n * 0.42 / max(b["nrow"], 1))
        gx0 = c - b["ncol"] * cell / 2
        gy0 = 34 * max(f, .8) + big * 2.6
        for rr in range(b["nrow"]):
            for k in range(b["ncol"]):
                safe = (rr + 1, k + 1) == (b["row"], b["col"])
                cv.create_rectangle(gx0 + k * cell + 1, gy0 + rr * cell + 1, gx0 + (k + 1) * cell - 1, gy0 + (rr + 1) * cell - 1,
                                    fill="#ffffff" if safe else "#5a1f1a", outline="#000")
        tail = [f"взрыв через ~{b['left']} с"] + ([f"до сейва ~{b['dist']:.0f} м"] if b.get("dist") is not None else [])
        self._text(c, gy0 + b["nrow"] * cell + fs + 2, " · ".join(tail), FG, fs, bold=True)

    # ---------- состояние ----------
    def _load(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def save(self):
        try:
            self.st.update({"x": self.root.winfo_x(), "y": self.root.winfo_y()})
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump(self.st, fh, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def close(self):
        self.save()
        try:
            self.root.destroy()
        except Exception:
            pass
        self.on_close()

    def run(self):
        self.root.mainloop()
