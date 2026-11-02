"""Звук оповещения: встроенные сигналы, системные звуки Windows или свой .wav, с громкостью.

winsound не умеет громкость, поэтому звук собирается в памяти уже с нужной громкостью
(сэмплы умножаются на коэффициент) и играется в отдельном потоке.
"""

from __future__ import annotations

import array
import glob
import io
import math
import os
import threading
import wave

RATE = 22050

# встроенные сигналы: список (частота Гц, длительность с, пауза после, затухание)
BUILTIN = {
    "Колокольчик": [(1320, 0.45, 0.0, 6)],
    "Двойной сигнал": [(880, 0.12, 0.08, 0), (880, 0.12, 0.0, 0)],
    "Монетка": [(988, 0.08, 0.0, 0), (1319, 0.35, 0.0, 7)],
    "Мягкий аккорд": [(523, 0.14, 0.0, 3), (659, 0.14, 0.0, 3), (784, 0.35, 0.0, 5)],
    "Тревога": [(700, 0.12, 0.03, 0), (1000, 0.12, 0.03, 0)] * 3,
    "Сонар": [(1500, 0.6, 0.0, 9)],
}
DEFAULT = "Колокольчик"
CUSTOM = "Свой файл…"


def _tone(freq, dur, decay) -> array.array:
    n = int(RATE * dur)
    out = array.array("h")
    for i in range(n):
        t = i / RATE
        env = math.exp(-decay * t) if decay else 1.0
        env *= min(1.0, i / (RATE * 0.005), (n - i) / (RATE * 0.01))   # без щелчков на краях
        v = math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(4 * math.pi * freq * t)
        out.append(int(max(-1, min(1, v / 1.3 * env)) * 30000))
    return out


def _wav(samples: array.array, rate=RATE, channels=1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def system_sounds() -> dict[str, str]:
    """Звуки Windows из C:\\Windows\\Media: имя -> путь."""
    root = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Media")
    out = {}
    for p in sorted(glob.glob(os.path.join(root, "*.wav"))):
        out["Windows: " + os.path.splitext(os.path.basename(p))[0]] = p
    return out


def choices() -> list[str]:
    return list(BUILTIN) + list(system_sounds()) + [CUSTOM]


def _scaled_file(path: str, vol: float) -> bytes | None:
    try:
        with wave.open(path, "rb") as w:
            ch, sw, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.readframes(w.getnframes())
    except Exception:
        return None
    if sw == 2:
        a = array.array("h")
        a.frombytes(frames[: len(frames) // 2 * 2])
        for i in range(len(a)):
            a[i] = int(max(-32768, min(32767, a[i] * vol)))
        return _wav(a, rate, ch)
    if sw == 1:   # 8 бит без знака
        a = array.array("h", ((b - 128) * 256 for b in frames))
        for i in range(len(a)):
            a[i] = int(max(-32768, min(32767, a[i] * vol)))
        return _wav(a, rate, ch)
    try:          # другие форматы - как есть, без громкости
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


_cache: dict[tuple, bytes] = {}


def build(name: str, volume: int, custom: str = "") -> bytes | None:
    vol = max(0, min(100, int(volume))) / 100
    key = (name, round(vol, 2), custom)
    if key in _cache:
        return _cache[key]
    data = None
    if name in BUILTIN:
        a = array.array("h")
        for freq, dur, gap, decay in BUILTIN[name]:
            a.extend(_tone(freq, dur, decay))
            a.extend([0] * int(RATE * gap))
        for i in range(len(a)):
            a[i] = int(a[i] * vol)
        data = _wav(a)
    elif name == CUSTOM:
        data = _scaled_file(custom, vol) if custom else None
    else:
        p = system_sounds().get(name)
        data = _scaled_file(p, vol) if p else None
    if data is None and name != DEFAULT:
        return build(DEFAULT, volume)
    _cache[key] = data
    return data


_playing = threading.Lock()


def play(name: str, volume: int, repeats: int = 1, custom: str = ""):
    if os.name != "nt" or volume <= 0:
        return
    data = build(name, volume, custom)
    if not data:
        return

    def run():
        if not _playing.acquire(blocking=False):
            return   # уже играет - не накладываем
        try:
            import winsound
            for i in range(max(1, int(repeats))):
                winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except Exception:
            pass
        finally:
            _playing.release()
    threading.Thread(target=run, daemon=True).start()
