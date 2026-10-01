#!/usr/bin/env python3
"""CrowdyPy's replication benchmarks, offline, on 127.0.0.1.

    python benchmarks/bench_replication.py all
    python benchmarks/bench_replication.py send | receive | latency | echo | jitter

Every peer (the draining server, the notification blaster, the echo server) runs in its own
process, so nothing it does competes with the client for the GIL. Compare the send numbers
with CrowdyCPP's ``bench_send`` (section 5, ``Connection::sendActorUpdate``) run on the same
machine; benchmarks/README.md records both.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import multiprocessing as mp
import os
import platform
import socket
import statistics
import struct
import sys
import time
from typing import Any

import numpy as np

from crowdypy import _native, wire
from crowdypy.replication import (
    Assignment,
    AsyncReplicationConnection,
    ReplicationConnection,
    TokenMaterial,
)
from crowdypy.wire import MessageType

TOKEN = "t" * 64
ME = "a" * 32
OTHER = "b" * 32
PAYLOAD = bytes(88)  # bench_send's payload size
QUIET = {"session_ready_wait_ms": 0, "advertise_capabilities": False}


class Provider:
    def __init__(self, port: int) -> None:
        self.port = port

    def assign_server(self) -> Assignment:
        return Assignment("127.0.0.1", "", self.port)

    def refresh_token(self, current: Assignment | None) -> TokenMaterial:
        raise RuntimeError("no refresh in a benchmark")


def percentiles(samples: list[float]) -> str:
    ordered = sorted(samples)

    def at(q: float) -> float:
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]

    return f"p50 {at(0.50):8.1f}  p90 {at(0.90):8.1f}  p99 {at(0.99):8.1f}  max {ordered[-1]:8.1f}"


# ------------------------------------------------------------------ peers (other processes)


def _drain(port_out: Any, stop: Any) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    port_out.put(sock.getsockname()[1])
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            sock.recv(2048)


def _blast(port_out: Any, stop: Any, per_bundle: int, rate: float, timestamped: bool) -> None:
    """Learn the client's address from its first datagram, then send bundles of signed
    actor-update notifications: as fast as possible (rate 0) or `rate` bundles per second."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port_out.put(sock.getsockname()[1])
    _, client = sock.recvfrom(2048)

    def message(stamp: int) -> bytes:
        body = struct.pack("<q", stamp) + bytes(56)
        return wire.encode_long_spatial(
            TOKEN, MessageType.ACTOR_UPDATE_NOTIFICATION, 7, (1, 2, 3), OTHER, body,
            distance=8, game_token_id=1_700_000_000_000,
        )  # fmt: skip

    fixed = wire.bundle([message(0)] * per_bundle)
    interval = 1.0 / rate if rate else 0.0
    next_at = time.perf_counter()
    while not stop.is_set():
        if timestamped:
            sock.sendto(message(time.perf_counter_ns()), client)
        else:
            sock.sendto(fixed, client)
        if interval:
            next_at += interval
            delay = next_at - time.perf_counter()
            if delay > 0:
                time.sleep(delay)


def _echo(port_out: Any, stop: Any) -> None:
    """Answer each actor update with its notification (same uuid and sequence)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    port_out.put(sock.getsockname()[1])
    while not stop.is_set():
        try:
            data, client = sock.recvfrom(2048)
        except TimeoutError:
            continue
        for raw in wire.split_datagram(data):
            msg = wire.parse_long_spatial(raw)
            if msg.type != MessageType.ACTOR_UPDATE_REQUEST:
                continue
            sock.sendto(
                wire.encode_long_spatial(
                    TOKEN, MessageType.ACTOR_UPDATE_NOTIFICATION, msg.app_id, msg.chunk, msg.uuid,
                    msg.payload, distance=msg.distance, game_token_id=1_700_000_000_000,
                    sequence=msg.sequence,
                ),
                client,
            )  # fmt: skip


class Peer:
    def __init__(self, target: Any, *args: Any) -> None:
        ctx = mp.get_context("spawn")
        self.stop = ctx.Event()
        ports = ctx.Queue()
        self.process = ctx.Process(target=target, args=(ports, self.stop, *args), daemon=True)
        self.process.start()
        self.port: int = ports.get(timeout=30)

    def close(self) -> None:
        self.stop.set()
        self.process.join(5)
        if self.process.is_alive():
            self.process.kill()


# ------------------------------------------------------------------ benchmarks


def _send_frames(conn: Any, frames: int, entities: int, flush: bool) -> dict[str, float]:
    chunks = [(10, 20, 30)] * entities
    uuids = ["0123456789abcdef0123456789abcdef"] * entities
    chunk_rows = np.array(chunks, dtype=np.int64)
    uuid_rows = np.frombuffer("".join(uuids).encode(), dtype=np.uint8)
    payload_rows = PAYLOAD * entities
    native = conn.native
    kind = int(MessageType.ACTOR_UPDATE_REQUEST)

    def frame_api() -> None:
        for i in range(entities):
            conn.send_actor_update(chunks[i], uuids[i], PAYLOAD)
        if flush:
            conn.flush_sends()

    def frame_native() -> None:
        for i in range(entities):
            native.send_spatial(kind, 10, 20, 30, uuids[i], PAYLOAD, 8, 1)
        if flush:
            native.flush_sends()

    def frame_batch() -> None:
        conn.send_actor_updates(
            chunk_rows, uuid_rows, payload_rows, stride=len(PAYLOAD), flush=flush
        )

    results: dict[str, float] = {}
    for name, frame in (
        ("send_actor_update x200 (Python API)", frame_api),
        ("native.send_spatial x200 (raw binding)", frame_native),
        ("send_actor_updates, one batch of 200", frame_batch),
    ):
        for _ in range(50):
            frame()
        start = time.perf_counter_ns()
        for _ in range(frames):
            frame()
        results[name] = (time.perf_counter_ns() - start) / (frames * entities)
    return results


def bench_send(frames: int = 2000, entities: int = 200) -> dict[str, float]:
    """CrowdyCPP's bench_send section 5, mirrored: manual pump (no network thread), a peer
    that is bound and never read, no flush per frame."""
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    peer.bind(("127.0.0.1", 0))
    try:
        with ReplicationConnection(
            Provider(peer.getsockname()[1]), TokenMaterial(TOKEN, 42), app_id=7,
            manual_pump=True, **QUIET,
        ) as conn:  # fmt: skip
            return _send_frames(conn, frames, entities, flush=False)
    finally:
        peer.close()


def bench_send_draining(frames: int = 500, entities: int = 200) -> dict[str, float]:
    """The same frames with the network thread running, a peer process draining every
    datagram, and a flush at the end of each frame: closer to a game, and what it costs."""
    peer = Peer(_drain)
    try:
        with ReplicationConnection(
            Provider(peer.port), TokenMaterial(TOKEN, 42), app_id=7,
            socket_send_buffer_bytes=8 << 20, **QUIET,
        ) as conn:  # fmt: skip
            return _send_frames(conn, frames, entities, flush=True)
    finally:
        peer.close()


def bench_receive(seconds: float = 5.0, per_bundle: int = 6) -> dict[str, float]:
    peer = Peer(_blast, per_bundle, 0.0, False)
    try:
        with ReplicationConnection(
            Provider(peer.port), TokenMaterial(TOKEN, 42), app_id=7,
            socket_recv_buffer_bytes=16 << 20, ring_capacity=1 << 16, **QUIET,
        ) as conn:  # fmt: skip
            conn.send_heartbeat((0, 0, 0), ME)
            conn.flush_sends()
            received = 0
            batches = 0
            deadline = time.perf_counter() + 1.0  # warm-up
            while time.perf_counter() < deadline:
                conn.wait(0.05)
                conn.poll()
            start_stats = conn.stats()
            start = time.perf_counter()
            deadline = start + seconds
            while time.perf_counter() < deadline:
                conn.wait(0.05)
                batch = conn.poll()
                if batch:
                    received += len(batch)
                    batches += 1
            elapsed = time.perf_counter() - start
            stats = conn.stats()
    finally:
        peer.close()
    return {
        "notifications delivered to Python /s": received / elapsed,
        "mean batch (events)": received / max(batches, 1),
        "datagrams received /s": (stats["datagrams_received"] - start_stats["datagrams_received"])
        / elapsed,
        "ring dropped": stats["ring_dropped"] - start_stats["ring_dropped"],
        "hmac failures": stats["hmac_failures"],
    }


async def _latency(seconds: float, rate: float) -> list[float]:
    peer = Peer(_blast, 1, rate, True)
    delays: list[float] = []
    try:
        conn = AsyncReplicationConnection(
            Provider(peer.port), TokenMaterial(TOKEN, 42), app_id=7, **QUIET
        )
        await conn.connect()
        conn.send_heartbeat((0, 0, 0), ME)
        conn.flush_sends()

        def record(event: Any) -> None:
            sent = struct.unpack_from("<q", event.payload)[0]
            delays.append((time.perf_counter_ns() - sent) / 1000)

        conn.subscribe({"actor_update": record})
        await asyncio.sleep(0.5)
        delays.clear()
        await asyncio.sleep(seconds)
        await conn.close()
    finally:
        peer.close()
    return delays


async def _echo_rtt(count: int) -> list[float]:
    peer = Peer(_echo)
    samples: list[float] = []
    try:
        conn = AsyncReplicationConnection(
            Provider(peer.port), TokenMaterial(TOKEN, 42), app_id=7, **QUIET
        )
        await conn.connect()
        for i in range(count + 100):
            start = time.perf_counter_ns()
            sequence = conn.send_actor_update((1, 2, 3), ME, PAYLOAD)
            conn.flush_sends()
            await conn.wait_for_sequence(sequence, ME, 2.0)
            if i >= 100:
                samples.append((time.perf_counter_ns() - start) / 1000)
        await conn.close()
    finally:
        peer.close()
    return samples


async def _jitter(seconds: float, inbound_bundles_per_s: float) -> tuple[list[float], int]:
    peer = Peer(_blast, 6, inbound_bundles_per_s, False)
    lateness: list[float] = []
    try:
        conn = AsyncReplicationConnection(
            Provider(peer.port), TokenMaterial(TOKEN, 42), app_id=7, **QUIET
        )
        await conn.connect()
        conn.send_heartbeat((0, 0, 0), ME)
        conn.flush_sends()
        received = 0

        def count(event: Any) -> None:
            nonlocal received
            received += 1

        conn.subscribe({"actor_update": count})
        chunks = np.zeros((200, 3), dtype=np.int64)
        uuids = np.frombuffer(ME.encode() * 200, dtype=np.uint8)
        period = 1 / 60
        loop = asyncio.get_running_loop()
        next_at = loop.time() + period
        end = loop.time() + seconds
        while next_at < end:
            await asyncio.sleep(max(0.0, next_at - loop.time()))
            lateness.append((loop.time() - next_at) * 1000)
            conn.send_actor_updates(chunks, uuids, PAYLOAD * 200, stride=len(PAYLOAD))
            next_at += period
        await conn.close()
    finally:
        peer.close()
    return lateness, received


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("which", choices=["all", "send", "receive", "latency", "echo", "jitter"])
    args = parser.parse_args(argv)
    which = (
        {"send", "receive", "latency", "echo", "jitter"} if args.which == "all" else {args.which}
    )

    print(f"CrowdyPy replication benchmarks: Python {platform.python_version()}"
          f"{' (free-threaded)' if not getattr(sys, '_is_gil_enabled', lambda: True)() else ''}, "
          f"CrowdyCPP {_native.CROWDYCPP_VERSION}, {_native.OPENSSL_VERSION}, "
          f"{platform.processor() or platform.machine()}, {os.cpu_count()} CPUs")  # fmt: skip
    if "send" in which:
        print("\n1a. Send cost per entity, CrowdyCPP bench_send section 5 mirrored")
        print("    (88-byte payload, 200 per frame, bundled, manual pump, undrained peer)")
        for name, value in bench_send().items():
            print(f"  {name:44s} {value:8.1f} ns/entity")
        print("\n1b. The same with the network thread, a draining peer, a flush per frame")
        for name, value in bench_send_draining().items():
            print(f"  {name:44s} {value:8.1f} ns/entity")
    if "receive" in which:
        print("\n2. Receive throughput (signed notifications, 6 per datagram, HMAC verified)")
        for name, value in bench_receive().items():
            print(f"  {name:44s} {value:12.0f}")
    if "latency" in which:
        delays = asyncio.run(_latency(5.0, 1000))
        print(
            f"\n3. Delay to Python: wire to asyncio handler, 1000 notifications/s ({len(delays)} samples, us)"
        )
        print(f"  {percentiles(delays)}")
    if "echo" in which:
        rtts = asyncio.run(_echo_rtt(2000))
        print(
            f"\n4. Echo round trip: send, server echo, wait_for_sequence resolves ({len(rtts)} samples, us)"
        )
        print(f"  {percentiles(rtts)}  mean {statistics.fmean(rtts):.1f}")
    if "jitter" in which:
        lateness, received = asyncio.run(_jitter(10.0, 10_000))
        print("\n5. Game-loop jitter: 60 Hz asyncio loop sending 200 entities a frame while")
        print(f"   receiving ~60k notifications/s ({received} received, frame lateness in ms)")
        print(f"  {percentiles(lateness)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
