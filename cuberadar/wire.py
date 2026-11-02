"""Декодер сетевого протокола AION 2 (сервер -> клиент).

Формат взят из открытых находок сообщества (chen-zilei/aion2-meter, docs/PROTOCOL.md):

    frame   = varint len, [необязательный байт 0xF0..0xFE], payload
              размер кадра на проводе = len + длина_varint - 4
    payload = opcode (2 байта) body
            | FF FF, u32 raw_size, LZ4-блок   -> внутри ещё кадры
    0x00 между кадрами - выравнивание.
    Heartbeat: 0E 00 36 + 8 байт, ~20 раз в секунду.

Это неофициальный формат, после патчей игры может поменяться.
"""

from __future__ import annotations

try:
    import lz4.block as _lz4
except ImportError:  # без библиотеки работает встроенный распаковщик
    _lz4 = None


def lz4_block_decompress(src: bytes, max_size: int) -> bytes:
    """Распаковка LZ4-блока (без заголовка кадра) на чистом Python."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break  # последняя последовательность - только литералы
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(out):
            raise ValueError("bad lz4 offset")
        mlen = token & 0x0F
        if mlen == 15:
            while True:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - offset
        if offset >= mlen:
            out += out[start:start + mlen]
        else:
            for k in range(mlen):
                out.append(out[start + k])
        if len(out) > max_size:
            raise ValueError("lz4 output too large")
    return bytes(out)


def _decompress(block: bytes, raw: int) -> bytes:
    if _lz4 is not None:
        return _lz4.decompress(block, uncompressed_size=raw)
    return lz4_block_decompress(block, raw)

MAX_FRAME = 65_535
MAX_UNBUNDLED_WAIT = 16_384
MAX_BUNDLE = 8 * 1024 * 1024
MAX_HUNT_BYTES = 256 * 1024
MAX_DEPTH = 4
HEARTBEATS = (b"\x0e\x00\x36", b"\x06\x00\x36")

HEARTBEAT = 0x0036

KNOWN_OPCODES = {
    0x0036: "Heartbeat",
    0x0336: "Pong",
    0x3336: "SelfInfo",
    0x4536: "PlayerInfo",
    0x4136: "Spawn",
    0x4236: "Death",
    0x2136: "MapLoad",
    0x3538: "Despawn",
    0x0238: "Cast",
    0x0438: "Damage",
    0x0538: "DoT",
    0x008D: "RemainHp",
    0x048D: "LootOwner",
    0x0297: "PartyRoster",
}

# Боевой спам: в консоли не показываем, в лог пишем.
NOISY = {0x0036, 0x0238, 0x0438, 0x0538, 0x008D, 0x0336}


def op_name(op: int) -> str:
    return KNOWN_OPCODES.get(op, f"{op >> 8:02x} {op & 0xFF:02x}")


def read_varint(b: bytes, o: int):
    """LEB128, до 5 байт. Возвращает (значение, новый_offset) или None."""
    value = 0
    for i in range(5):
        if o + i >= len(b):
            return None
        byte = b[o + i]
        value |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return value, o + i + 1
    return None


def encode_varint(v: int) -> bytes:
    out = bytearray()
    while True:
        byte = v & 0x7F
        v >>= 7
        if v == 0:
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def encode_frame(opcode: int, body: bytes) -> bytes:
    """Кадр так, как его шлёт сервер (для тестов)."""
    return encode_varint(2 + len(body) + 4) + opcode.to_bytes(2, "big") + body


def _find_heartbeat(span: bytes):
    best = None
    for sig in HEARTBEATS:
        i = span.find(sig)
        if i >= 0 and (best is None or i < best):
            best = i
    return best


class FrameDecoder:
    """Режет поток байт от сервера на пакеты: on_packet(opcode, body, depth)."""

    def __init__(self, starts_aligned: bool):
        self.buf = bytearray()
        self.hunting = not starts_aligned
        self.hunted = 0
        self.stats = dict(frames=0, bundles=0, bad_bundles=0, resyncs=0, dropped_bytes=0)

    def reset(self):
        """TCP пропустил байты: следующий байт не начало кадра."""
        self.buf.clear()
        self.hunting = True
        self.hunted = 0

    def _resync(self) -> int:
        self.stats["resyncs"] += 1
        self.stats["dropped_bytes"] += 1
        if self.hunted <= MAX_HUNT_BYTES:
            self.hunting = True
            self.hunted = 0
        return 1

    def feed(self, data: bytes, on_packet):
        self.buf += data
        buf = bytes(self.buf)
        pos = 0
        n = len(buf)
        while pos < n:
            if self.hunting:
                i = _find_heartbeat(buf[pos:])
                if i is not None:
                    pos += i
                    self.stats["dropped_bytes"] += i
                    self.hunting = False
                    continue
                skip = max(0, n - pos - 2)
                pos += skip
                self.stats["dropped_bytes"] += skip
                self.hunted += skip
                if self.hunted > MAX_HUNT_BYTES:
                    self.hunting = False
                break

            if buf[pos] == 0:
                pos += 1
                continue

            r = read_varint(buf, pos)
            if r is None:
                if n - pos < 5:
                    break
                pos += self._resync()
                continue
            length, after = r
            vlen = after - pos
            size = length + vlen - 4
            if length < 6 or size > MAX_FRAME:
                pos += self._resync()
                continue
            if size > n - pos:
                if size > MAX_UNBUNDLED_WAIT and not _is_bundle(buf, pos, vlen):
                    pos += self._resync()
                    continue
                break
            self._handle_frame(buf[pos:pos + size], vlen, 0, on_packet)
            pos += size
        self.buf = bytearray(buf[pos:])

    def _handle_frame(self, frame: bytes, vlen: int, depth: int, on_packet):
        self.stats["frames"] += 1
        o = vlen
        if o < len(frame) and 0xF0 <= frame[o] <= 0xFE:
            o += 1
        if o + 2 > len(frame):
            return
        if frame[o] == 0xFF and frame[o + 1] == 0xFF:
            self.stats["bundles"] += 1
            o += 2
            if o + 4 > len(frame):
                self.stats["bad_bundles"] += 1
                return
            raw = int.from_bytes(frame[o:o + 4], "little")
            o += 4
            if not (0 < raw <= MAX_BUNDLE) or depth >= MAX_DEPTH:
                self.stats["bad_bundles"] += 1
                return
            try:
                out = _decompress(frame[o:], raw)
            except Exception:
                self.stats["bad_bundles"] += 1
                return
            self._walk_inner(out, depth + 1, on_packet)
            return
        opcode = (frame[o] << 8) | frame[o + 1]
        on_packet(opcode, frame[o + 2:], depth)

    def _walk_inner(self, data: bytes, depth: int, on_packet):
        pos = 0
        n = len(data)
        while pos < n:
            if data[pos] == 0:
                pos += 1
                continue
            r = read_varint(data, pos)
            if r is None:
                return
            length, after = r
            vlen = after - pos
            size = length + vlen - 4
            if length < 6 or size > n - pos:
                return
            self._handle_frame(data[pos:pos + size], vlen, depth, on_packet)
            pos += size


def _is_bundle(buf: bytes, pos: int, vlen: int) -> bool:
    o = pos + vlen
    if o < len(buf) and 0xF0 <= buf[o] <= 0xFE:
        o += 1
    return buf[o:o + 2] == b"\xff\xff"


class TcpStream:
    """Сборка одного направления TCP по seq. Отдаёт байты по порядку в FrameDecoder."""

    MAX_PENDING = 2 * 1024 * 1024

    def __init__(self, on_packet):
        self.on_packet = on_packet
        self.next_seq = None
        self.pending: dict[int, bytes] = {}
        self.pending_bytes = 0
        self.decoder = None
        self.gaps = 0

    def segment(self, seq: int, payload: bytes, syn: bool = False):
        if syn:
            self.next_seq = (seq + 1) & 0xFFFFFFFF
            self.decoder = FrameDecoder(starts_aligned=True)
            self.pending.clear()
            self.pending_bytes = 0
            return
        if not payload:
            return
        if self.next_seq is None:
            # подключились посреди потока
            self.next_seq = seq
            self.decoder = FrameDecoder(starts_aligned=False)

        diff = (seq - self.next_seq) & 0xFFFFFFFF
        if diff >= 0x80000000:
            # старые данные (ретрансмит) - возможно частично новые
            overlap = (self.next_seq - seq) & 0xFFFFFFFF
            if overlap >= len(payload):
                return
            payload = payload[overlap:]
            seq = self.next_seq
            diff = 0
        if diff > 0:
            if seq not in self.pending:
                self.pending[seq] = payload
                self.pending_bytes += len(payload)
            if self.pending_bytes > self.MAX_PENDING:
                self._skip_gap()
            return
        self._deliver(payload)
        self._drain()

    def _deliver(self, payload: bytes):
        self.next_seq = (self.next_seq + len(payload)) & 0xFFFFFFFF
        self.decoder.feed(payload, self.on_packet)

    def _drain(self):
        while self.pending:
            progressed = False
            for seq in list(self.pending):
                diff = (seq - self.next_seq) & 0xFFFFFFFF
                if diff == 0 or diff >= 0x80000000:
                    data = self.pending.pop(seq)
                    self.pending_bytes -= len(data)
                    if diff:
                        overlap = (self.next_seq - seq) & 0xFFFFFFFF
                        if overlap >= len(data):
                            progressed = True
                            continue
                        data = data[overlap:]
                    self._deliver(data)
                    progressed = True
            if not progressed:
                return

    def _skip_gap(self):
        """Потеряли пакет навсегда: прыгаем на ближайший сегмент и ищем heartbeat."""
        self.gaps += 1
        nearest = min(self.pending, key=lambda s: (s - self.next_seq) & 0xFFFFFFFF)
        self.next_seq = nearest
        self.decoder.reset()
        self._drain()
