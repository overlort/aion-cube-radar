"""Запуск: проверка Npcap, один экземпляр, захват трафика и оверлей радара.

Трафик читается пассивно (как DPS-метры): программа ничего не отправляет и не трогает игру.
Настройки и журнал - в папке radar_data рядом с exe.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

TITLE = "AION 2 · Радар кубов"


def message(text: str, error: bool = True):
    print(text)
    try:
        import tkinter
        from tkinter import messagebox
        r = tkinter.Tk()
        r.withdraw()
        (messagebox.showerror if error else messagebox.showinfo)(TITLE, text)
        r.destroy()
    except Exception:
        pass


def single_instance() -> bool:
    """Второй запуск не нужен: два радара делили бы один трафик и пищали дважды."""
    if os.name != "nt":
        return True
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\Aion2CubeRadar")
    return ctypes.windll.kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS


def main():
    from . import netcap
    from .cubes import CubeWatcher
    from .ui import RadarApp

    if netcap.npcap_missing():
        message("Для работы радару нужен бесплатный драйвер Npcap (его же используют DPS-метры).\n\n"
                "Сейчас откроется страница загрузки: скачайте «Npcap installer», установите и запустите радар снова.")
        try:
            import webbrowser
            webbrowser.open("https://npcap.com/#download")
        except Exception:
            pass
        return
    if not single_instance():
        message("Радар кубов уже запущен.", error=False)
        return

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None, help="порт сервера (обычно не нужен)")
    ap.add_argument("--data", default=os.path.join(netcap.app_dir(), "radar_data"))
    args = ap.parse_args()
    os.makedirs(args.data, exist_ok=True)
    diag = os.path.join(args.data, "diag.txt")
    try:
        os.replace(diag, diag + ".prev")
    except OSError:
        pass
    log = netcap.make_logger(diag)
    from . import __version__
    log(f"Радар кубов {__version__} запущен.")
    ifaces = netcap.usable_ifaces()
    netcap.describe_environment(log, ifaces)

    watcher = CubeWatcher()
    session = netcap.Session(None, False, log, {netcap.dev_id(i): netcap.dev_label(i) for i in ifaces}, record=False)
    session.packet_hooks.append(lambda op, body: watcher.feed(op, body, session.cur_time))
    capture = netcap.Capture(session, ifaces, log, args.port)
    if not capture.start():
        message("Не удалось начать захват трафика.\n\nПроверьте, что установлен Npcap и радар запущен "
                "от администратора. Подробности: radar_data\\diag.txt")
        return

    stopped = threading.Event()

    def stop():
        if stopped.is_set():
            return
        stopped.set()
        capture.stop()
        log("Радар остановлен.")
        log.close()

    try:
        app = RadarApp(watcher, session, stop, os.path.join(args.data, "settings.json"))
    except Exception as e:
        log(f"Оверлей не запустился: {e}")
        message(f"Оверлей не запустился: {e}")
        stop()
        return
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    stop()


if __name__ == "__main__":
    main()
