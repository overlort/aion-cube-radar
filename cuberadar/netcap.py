"""Поиск потока игры и захват (перенесено из aion-economy/econ/netcap.py).

Слушаем весь TCP на всех адаптерах, включая loopback и VPN (GearUP, ExitLag, WireGuard),
находим поток, где сервер шлёт heartbeat ~19 раз в секунду, и дальше слушаем только его.
При тишине 45 с (переподключение) ищем заново. Порт заранее знать не нужно.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import os
import sys
import threading
import time

from .wire import HEARTBEATS, NOISY, HEARTBEAT, TcpStream, op_name, KNOWN_OPCODES
from . import netdiag

from scapy.all import AsyncSniffer, IP, IPv6, TCP, PcapWriter, conf  # noqa: E402

WINDOW = 3.0             # секунд до и после метки
SIG_THRESHOLD = 12       # heartbeat-пакетов ...
SIG_WINDOW = 3.0         # ... за столько секунд = это поток игры
WEB_PORTS = {443, 80, 8080, 8443}
VERIFY_AFTER = 25        # через столько секунд после захвата проверяем, что это правда игра
LOOPBACK_GRACE = 2.5     # сколько ждать, не появится ли тот же поток на loopback/VPN
RELOCK_AFTER = 45.0      # тишина в захваченном потоке -> ищем поток заново (переподключение)

VIRTUAL_HINTS = ("loopback", "wintun", "tap-windows", "tap adapter", "tunnel", "vpn", "gearup",
                 "exitlag", "wireguard", "openvpn", "virtual adapter")


def app_dir() -> str:
    """Папка программы: рядом с exe, а при запуске из исходников - корень проекта."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def tcp_payload(ip, tcp) -> bytes:
    """Данные TCP без Ethernet-заполнителя (scapy иногда приклеивает его к payload)."""
    raw = bytes(tcp)[int(tcp.dataofs) * 4:]
    if ip.version == 4:
        plen = int(ip.len or 0) - int(ip.ihl) * 4 - int(tcp.dataofs) * 4
    else:
        plen = int(ip.plen or 0) - int(tcp.dataofs) * 4
    if 0 <= plen <= len(raw) and (ip.version != 4 or ip.len):
        return raw[:plen]
    return raw  # склеенные сетевой картой пакеты: длина в заголовке бывает неверной


def _ports_path() -> str:
    base = (os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
            else os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    return os.path.join(base, "data", "game_ports.json") if os.path.isdir(os.path.join(base, "data")) \
        else os.path.join(base, "game_ports.json")


def _known_ports() -> set:
    try:
        with open(_ports_path(), encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError, TypeError):
        return set()


def _forget_port(port: int):
    ports = _known_ports() - {int(port)}
    try:
        with open(_ports_path(), "w", encoding="utf-8") as f:
            json.dump(sorted(ports), f)
    except OSError:
        pass


def _remember_port(port: int):
    ports = _known_ports() | {int(port)}
    try:
        with open(_ports_path(), "w", encoding="utf-8") as f:
            json.dump(sorted(ports), f)
    except OSError:
        pass


def has_signature(payload: bytes) -> bool:
    return any(sig in payload for sig in HEARTBEATS)


def is_virtual(desc: str) -> bool:
    d = desc.lower()
    return any(h in d for h in VIRTUAL_HINTS)


class Session:
    def __init__(self, out_dir, verbose: bool, log, dev_desc: dict, record: bool = True):
        self.verbose = verbose
        self.log = log
        self.dev_desc = dev_desc
        self.dir = out_dir
        self.record = record
        self.packet_hooks = []   # f(opcode, body) для каждого игрового пакета (трекер)
        self.events = self.pcap = None
        if record:
            os.makedirs(out_dir, exist_ok=True)
            self.events = open(os.path.join(out_dir, "events.jsonl"), "a", encoding="utf-8")
            self.pcap = PcapWriter(os.path.join(out_dir, "traffic.pcap"), linktype=101, sync=True)
        self.lock = threading.RLock()
        self.streams: dict[tuple, TcpStream] = {}
        self.op_counts = collections.Counter()
        # порты, которые оказались не игрой: порт -> до какого времени не брать. Не навсегда: 07.11 порт ускорителя
        # 1111 захватили, пока игра ещё грузилась (0% знакомых пакетов), он попал сюда, и до перезапуска приложения
        # игра больше не находилась
        self.bad_ports: dict = {}
        self.ops_at_lock = (0, 0)
        self.verified = False
        self.recent = collections.deque()
        self.raw_packets = 0
        self.markers = 0
        self.started = time.time()
        self.warmup_until = self.started + 20
        self.cur_time = time.time()
        self.last_raw = 0.0
        self.listeners = []  # колбэки для строк-отчётов (оверлей)

        # поиск потока игры
        self.locked = None            # (dev, server_port) или (None, port) при ручном порте
        self.locked_at = 0.0
        self.lock_event = threading.Event()
        self.sig_hits: dict[tuple, list] = {}
        self.first_candidate = 0.0
        self.dev_tcp = collections.Counter()   # все TCP-пакеты по адаптерам (до захвата)
        self.dev_sig = collections.Counter()   # из них с heartbeat-сигнатурой

    def desc(self, dev) -> str:
        return self.dev_desc.get(dev, dev or "любой адаптер")

    # ---------- приём сырых пакетов ----------
    def on_raw(self, pkt, dev):
        ip = pkt.getlayer(IP) or pkt.getlayer(IPv6)
        if ip is None or not pkt.haslayer(TCP):
            return
        tcp = pkt[TCP]
        locked = self.locked
        if locked is None:
            self._detect(ip, tcp, dev)
            return
        ldev, port = locked
        if (ldev is not None and dev != ldev) or port not in (tcp.sport, tcp.dport):
            return
        with self.lock:
            self.raw_packets += 1
            self.last_raw = time.time()
            self.cur_time = float(pkt.time)
            if self.pcap is not None:
                try:
                    self.pcap.write(ip)
                except Exception:
                    pass
            if tcp.sport != port:
                return  # клиент -> сервер зашифрован, только в pcap
            key = (ip.src, tcp.sport, ip.dst, tcp.dport)
            stream = self.streams.get(key)
            syn = bool(int(tcp.flags) & 0x02)
            payload = tcp_payload(ip, tcp)
            if stream is None or syn:
                stream = TcpStream(self.on_game_packet)
                self.streams[key] = stream
                print(f"[поток] {ip.src}:{tcp.sport} -> {ip.dst}:{tcp.dport}" + (" (с начала)" if syn else " (с середины)"))
            stream.segment(int(tcp.seq), payload, syn=syn)

    def _detect(self, ip, tcp, dev):
        payload = tcp_payload(ip, tcp)
        with self.lock:
            self.dev_tcp[dev] += 1
            if not payload or not has_signature(payload) or self.locked is not None:
                return
            # чужие локальные сервисы (веб-интерфейсы на 127.0.0.1:78xx и т.п.) иногда похожи на сигнатуру - игра
            # на loopback только через прокси/ускоритель на порту 1111 (или на порту, где игру уже находили)
            if str(ip.src).startswith("127.") and tcp.sport != 1111 and tcp.sport not in _known_ports():
                return
            # https/http - не игра: 29.10 случайные байты обмена с сайтом цен (13.39.241.58:443) сошли за сигнатуру
            if tcp.sport in WEB_PORTS or self.bad_ports.get(tcp.sport, 0) > time.time():
                return
            self.dev_sig[dev] += 1
            now = time.time()
            # без ускорителя игра держит к серверу ~8 соединений сразу (66.253.35.85:2153, 2026-10-29): сигнатуры по
            # каждому отдельно набирались 45 мин - считаем по серверу (адрес и порт), а не по каждому соединению
            key = (dev, ip.src, tcp.sport)
            hit = self.sig_hits.get(key)
            if hit is None or now - hit[1] > SIG_WINDOW:
                hit = [0, now]
            hit[0] += 1
            hit[1] = now
            self.sig_hits[key] = hit
            if not self.first_candidate:
                self.first_candidate = now
            if hit[0] < SIG_THRESHOLD:
                return
            virtual = is_virtual(self.desc(dev))
            if not virtual and now - self.first_candidate < LOOPBACK_GRACE:
                return  # даём шанс тому же потоку проявиться на loopback/VPN
            self.locked = (dev, tcp.sport)
            self.locked_at = now
            self.ops_at_lock = self._ops_snapshot()
            self.verified = False
            self.log(f"НАШЁЛ ПОТОК ИГРЫ: {ip.src}:{tcp.sport} -> {ip.dst}:{tcp.dport} "
                     f"на адаптере «{self.desc(dev)}»")
            self.lock_event.set()

    def _ops_snapshot(self):
        tot = sum(self.op_counts.values())
        known = sum(self.op_counts[o] for o in KNOWN_OPCODES)
        return known, tot

    def lock_port(self, port: int, why: str):
        """Захват по порту без сигнатуры (порт взят из соединений процесса игры)."""
        with self.lock:
            if self.locked is not None:
                return
            self.locked = (None, port)
            self.locked_at = time.time()
            self.ops_at_lock = self._ops_snapshot()
            self.verified = False
            self.log(f"НАШЁЛ ПОТОК ИГРЫ по соединениям процесса игры: порт {port} ({why})")
            self.lock_event.set()

    def unlock(self):
        with self.lock:
            self.locked = None
            self.sig_hits.clear()
            self.first_candidate = 0.0
            self.dev_tcp.clear()
            self.dev_sig.clear()
            self.streams.clear()

    # ---------- игровые пакеты ----------
    def on_game_packet(self, op: int, body: bytes, depth: int):
        t = self.cur_time
        self.op_counts[op] += 1
        for hook in self.packet_hooks:
            try:
                hook(op, body)
            except Exception as e:
                print(f"ошибка обработчика {op:04x}: {e}")
        if op == HEARTBEAT or self.events is None:
            return
        rec = {"t": round(t, 3), "op": f"{op:04x}", "len": len(body), "d": depth, "hex": body.hex()}
        self.events.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if op not in NOISY:
            self.recent.append((t, op, len(body), body[:24].hex(" ")))
            while self.recent and self.recent[0][0] < t - 60:
                self.recent.popleft()
            first = self.op_counts[op] == 1
            if self.verbose or (first and t > self.warmup_until):
                tag = "НОВЫЙ " if first else ""
                print(f"  {tag}{op_name(op):>12}  len={len(body):<5} {body[:24].hex(' ')}")

    # ---------- метки ----------
    def marker(self, note: str) -> int:
        with self.lock:
            self.markers += 1
            n = self.markers
            t = time.time()
            rec = {"t": round(t, 3), "marker": n, "note": note}
            if self.events is not None:
                self.events.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self.events.flush()
        print(f"\n>>> МЕТКА #{n} {note!r} — жду {WINDOW:.0f} с и покажу, что пришло рядом")
        timer = threading.Timer(WINDOW, self._report, args=(n, t))
        timer.daemon = True
        timer.start()
        return n

    def _report(self, n: int, t: float):
        with self.lock:
            near = [r for r in self.recent if t - WINDOW <= r[0] <= t + WINDOW]
        print(f"--- рядом с меткой #{n}: {len(near)} пакетов (без боевого спама) ---")
        for rt, op, ln, head in near:
            print(f"  {rt - t:+5.1f}s {op_name(op):>12} len={ln:<5} {head}")
        print("---\n")
        ops = collections.Counter(op_name(op) for _, op, _, _ in near)
        short = ", ".join(f"{name}×{c}" if c > 1 else name for name, c in ops.most_common(4)) or "ничего"
        self._notify(f"#{n}: {short}")

    def _notify(self, line: str):
        for cb in self.listeners:
            try:
                cb(line)
            except Exception:
                pass

    def snapshot(self) -> dict:
        with self.lock:
            return {"raw": self.raw_packets, "game": sum(self.op_counts.values()),
                    "markers": self.markers, "last_raw": self.last_raw,
                    "locked": self.locked is not None}

    def status(self):
        with self.lock:
            game = sum(self.op_counts.values())
            top = ", ".join(f"{op_name(o)}:{c}" for o, c in self.op_counts.most_common(6))
            gaps = sum(s.gaps for s in self.streams.values())
            state = "поток найден" if self.locked else "ищу поток игры"
        print(f"[{dt.datetime.now():%H:%M:%S}] {state} | сырых={self.raw_packets} игровых={game} "
              f"опкодов={len(self.op_counts)} потерь={gaps} меток={self.markers} | {top}")

    def search_report(self):
        with self.lock:
            rows = [(self.desc(d), c, self.dev_sig[d]) for d, c in self.dev_tcp.most_common()]
            best = sorted(self.sig_hits.items(), key=lambda kv: -kv[1][0])[:5]
        self.log("Пока не нашёл поток игры. TCP-пакеты по адаптерам (всего / с сигнатурой игры):")
        if not rows:
            self.log("  ни одного TCP-пакета — Npcap ничего не видит (установка Npcap? права администратора?)")
        for d, c, s in rows:
            self.log(f"  {d:<50} {c:>7} / {s}")
        for (dev, src, sp), (cnt, _) in best:
            self.log(f"  кандидат: сервер {src}:{sp} на «{self.desc(dev)}», сигнатур подряд: {cnt}")

    def close(self):
        with self.lock:
            if self.events is None:
                return
            self.events.close()
            self.pcap.close()
            summary = {f"{o:04x}": c for o, c in self.op_counts.most_common()}
            with open(os.path.join(self.dir, "opcodes.json"), "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=1)


def usable_ifaces():
    out = []
    for iface in list(conf.ifaces.values()):
        try:
            if hasattr(iface, "is_valid") and not iface.is_valid():
                continue
        except Exception:
            pass
        out.append(iface)
    return out


def dev_id(iface) -> str:
    return getattr(iface, "network_name", None) or getattr(iface, "name", None) or str(iface)


def dev_label(iface) -> str:
    return getattr(iface, "description", None) or getattr(iface, "name", None) or str(iface)


def start_sniffers(session: Session, ifaces, bpf: str):
    sniffers = []
    for iface in ifaces:
        dev = dev_id(iface)
        try:
            s = AsyncSniffer(iface=iface, filter=bpf, store=False,
                             prn=lambda p, d=dev: session.on_raw(p, d))
            s.start()
            sniffers.append(s)
        except Exception:
            pass
    return sniffers


def stop_sniffers(sniffers):
    for s in sniffers:
        try:
            s.stop()
        except Exception:
            pass


class Capture:
    """Управляет снифферами: ищет поток игры, сужает захват, ищет заново при тишине."""

    def __init__(self, session: Session, ifaces, log, port=None):
        self.session = session
        self.ifaces = ifaces
        self.by_dev = {dev_id(i): i for i in ifaces}
        self.log = log
        self.port = port
        self.sniffers = []
        self.stopped = threading.Event()
        self.swap = threading.Lock()

    def start(self) -> bool:
        if self.port:
            self.session.locked = (None, self.port)
            self.session.locked_at = time.time()
            bpf = f"tcp port {self.port}"
            self.log(f"Порт задан вручную: {self.port}")
        else:
            bpf = "tcp"
            self.log("Ищу поток игры по сигнатуре на всех адаптерах...")
        self.sniffers = start_sniffers(self.session, self.ifaces, bpf)
        self.log(f"Слушаю {len(self.sniffers)} адаптеров.")
        if not self.sniffers:
            self.log("Не удалось открыть ни один адаптер. Установлен ли Npcap? Запущено ли от администратора?")
            return False
        if not self.port:
            threading.Thread(target=self._watcher, daemon=True).start()
        threading.Thread(target=self._monitor, daemon=True).start()
        return True

    def stop(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        with self.swap:
            stop_sniffers(self.sniffers)

    def _narrow(self):
        dev, port = self.session.locked
        target = [self.by_dev[dev]] if dev in self.by_dev else self.ifaces
        with self.swap:
            if self.stopped.is_set():
                return
            old = self.sniffers
            self.sniffers = start_sniffers(self.session, target, f"tcp port {port}")
            stop_sniffers(old)
        self.log(f"Дальше слушаю только порт {port} на «{self.session.desc(dev)}».")

    def _widen(self):
        with self.swap:
            if self.stopped.is_set():
                return
            self.session.unlock()
            self.session.lock_event.clear()
            old = self.sniffers
            self.sniffers = start_sniffers(self.session, self.ifaces, "tcp")
            stop_sniffers(old)

    def _watcher(self):
        while not self.stopped.is_set():
            if self.session.lock_event.wait(1) and not self.stopped.is_set():
                self.session.lock_event.clear()
                self._narrow()

    def _monitor(self):
        reported = time.time()
        while not self.stopped.wait(5):
            s = self.session
            now = time.time()
            if s.locked is None:
                # подсказка от системы: соединения процесса игры (netstat) - без ускорителя игра сразу видна там
                if not self.port and now - getattr(self, "_conn_check", 0) > 10:
                    self._conn_check = now
                    try:
                        # на 127.0.0.1 игра ходит только через ускоритель (1111) - прочие локальные порты не игра
                        ports = collections.Counter(rport for name, pid, local, rip, rport in netdiag.game_connections()
                                                    if not any(x in name.lower() for x in ("kina", "radar", "python"))
                                                    and not (str(rip).startswith("127.") and rport != 1111)
                                                    and rport not in WEB_PORTS and s.bad_ports.get(rport, 0) <= now)
                    except Exception:
                        ports = None
                    if ports:
                        port, n = ports.most_common(1)[0]
                        s.lock_port(port, f"соединений игры: {n}")
                        continue
                if now - reported > 30:
                    reported = now
                    s.search_report()
            elif not self.port and not s.verified and now - s.locked_at > VERIFY_AFTER:
                k0, t0 = s.ops_at_lock
                k1, t1 = s._ops_snapshot()
                if t1 - t0 >= 200:
                    share = (k1 - k0) / (t1 - t0)
                    if share < 0.05:   # в настоящем потоке знакомых опкодов ~20%, в чужом - почти 0
                        port = s.locked[1]
                        s.bad_ports[port] = now + (30 if port == 1111 else 300)
                        _forget_port(port)
                        self.log(f"Порт {port} оказался не игрой (знакомых пакетов {share:.1%}) — ищу заново.")
                        self._widen()
                        continue
                    s.verified = True
                    _remember_port(s.locked[1])
            elif not self.port and now - max(s.last_raw, s.locked_at) > RELOCK_AFTER:
                self.log(f"Поток игры молчит {RELOCK_AFTER:.0f} с (переподключение?) — ищу заново.")
                self._widen()


def describe_environment(log, ifaces):
    log(f"Сетевых адаптеров: {len(ifaces)}")
    for i in ifaces:
        tag = "  (loopback/VPN)" if is_virtual(dev_label(i)) else ""
        log(f"  {dev_label(i)}{tag}")
    red = netdiag.reducers()
    if red:
        log("Запущены пинг-редьюсер/VPN: " + ", ".join(red) + " — ищу поток и на loopback/VPN-адаптерах.")
    conns = netdiag.game_connections()
    if conns:
        log("Соединения процесса игры (для справки):")
        for name, pid, local, rip, rport in conns:
            log(f"  {name} (pid {pid})  {local} -> {rip}:{rport}")
    else:
        log("Процесс игры не найден по имени (ищу 'aion') — это не страшно, поток ищется по содержимому.")


def npcap_missing() -> bool:
    if os.name != "nt":
        return False
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return not os.path.exists(os.path.join(root, "System32", "Npcap", "wpcap.dll"))


def make_logger(path):
    f = open(path, "a", encoding="utf-8")
    lock = threading.Lock()

    def log(msg=""):
        print(msg)
        with lock:
            if not f.closed:
                f.write(f"[{dt.datetime.now():%H:%M:%S}] {msg}\n")
                f.flush()

    def close():
        with lock:
            f.close()
    log.close = close
    return log

