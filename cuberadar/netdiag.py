"""Диагностика сети: какие соединения у игры и видит ли Npcap трафик вообще."""

from __future__ import annotations

import collections
import csv
import io
import os
import subprocess
import time

GAME_HINTS = ("aion",)
REDUCER_HINTS = ("exitlag", "gearup", "noping", "wtfast", "lagofast", "mudfish", "haste", "kovpn",
                 "boostnet", "pingzapper", "outfox", "battleping", "ping", "vpn", "proxifier", "netch")


def _run(cmd) -> str:
    try:
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        return subprocess.run(cmd, capture_output=True, text=True, errors="ignore",
                              timeout=15, creationflags=flags).stdout
    except Exception:
        return ""


def processes() -> dict[int, str]:
    names = {}
    for row in csv.reader(io.StringIO(_run(["tasklist", "/fo", "csv", "/nh"]))):
        if len(row) >= 2 and row[1].isdigit():
            names[int(row[1])] = row[0]
    return names


def _split_addr(a: str):
    host, _, port = a.rpartition(":")
    return host.strip("[]"), int(port) if port.isdigit() else 0


def connections():
    """(pid, local_ip, local_port, remote_ip, remote_port) всех TCP-соединений."""
    out = []
    for line in _run(["netstat", "-ano", "-p", "tcp"]).splitlines() + \
            _run(["netstat", "-ano", "-p", "tcpv6"]).splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].upper().startswith("TCP") or not parts[-1].isdigit():
            continue
        lip, lport = _split_addr(parts[1])
        rip, rport = _split_addr(parts[2])
        if rport == 0 or rip in ("0.0.0.0", "*", "::"):
            continue  # слушающий сокет
        out.append((int(parts[-1]), lip, lport, rip, rport))
    return out


def game_connections():
    """Соединения процессов, в имени которых есть 'aion'."""
    names = processes()
    found = []
    for pid, lip, lport, rip, rport in connections():
        name = names.get(pid, "")
        if any(h in name.lower() for h in GAME_HINTS):
            found.append((name, pid, f"{lip}:{lport}", rip, rport))
    return found


def reducers():
    seen = set()
    for name in processes().values():
        low = name.lower()
        if any(h in low for h in REDUCER_HINTS) and "svchost" not in low:
            seen.add(name)
    return sorted(seen)


def probe(ifaces, seconds: float = 5.0):
    """Сколько TCP-пакетов видит Npcap за несколько секунд и с какими адресами."""
    from scapy.all import AsyncSniffer, IP, IPv6, TCP
    counts = collections.Counter()

    def on(pkt):
        ip = pkt.getlayer(IP) or pkt.getlayer(IPv6)
        if ip is None or not pkt.haslayer(TCP):
            return
        t = pkt[TCP]
        counts[f"{ip.src}:{t.sport}"] += 1

    sniffers = []
    for iface in ifaces:
        try:
            s = AsyncSniffer(iface=iface, filter="tcp", prn=on, store=False)
            s.start()
            sniffers.append(s)
        except Exception:
            pass
    time.sleep(seconds)
    for s in sniffers:
        try:
            s.stop()
        except Exception:
            pass
    return counts
