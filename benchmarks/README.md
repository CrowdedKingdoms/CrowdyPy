# Benchmarks

```bash
python benchmarks/bench_replication.py all     # or: send | receive | latency | echo | jitter
```

Everything runs offline on 127.0.0.1. Each peer (the notification blaster, the echo server,
the draining receiver) is a separate process, so it never competes with the client for the
GIL. Compare against CrowdyCPP's own `bench_send` and `bench_codec`, built in Release and run
on the same machine: absolute numbers move with hardware (CrowdyCPP's README explains why),
and the ratio between the two SDKs is the number that carries over to other machines.

## Results, 0.2.0

Recorded on the CKS builder: Intel Xeon 6975P-C, 8 vCPUs, SHA extensions present, CPython
3.14.4, CrowdyCPP 0.54.0, OpenSSL 3.5.9 (static, as in the wheels).

### 1. Sending: cost per entity

CrowdyCPP's `bench_send` section 5 measures the public send path:
`Connection::sendActorUpdate` 200 times a frame, an 88-byte payload, bundling on, manual
pump (no network thread), and a loopback peer that is bound but never read. 1a mirrors those
conditions exactly.

| 1a. bench_send §5 conditions | ns/entity | vs CrowdyCPP |
|---|---|---|
| CrowdyCPP `sendActorUpdate` x200 (C++) | 567 | 1.00x |
| CrowdyPy `send_actor_updates`, one batch of 200 | 602 | 1.06x |
| CrowdyPy `native.send_spatial` x200 (the raw binding) | 769 | 1.36x |
| CrowdyPy `send_actor_update` x200 (the Python API) | 894 | 1.58x |

A batch releases the GIL once and then runs the same C++ loop, so 200 entities cost about
35 ns each over CrowdyCPP. A send per call pays the interpreter's call and argument
conversion, plus a GIL release, every time: send many entities as a batch.

1b runs the same frames with the network thread running, a peer process reading every
datagram, and a flush at the end of every frame. That is closer to a game; the extra cost is
the kernel waking the reader on every datagram, which CrowdyCPP's README prices separately
(`send()` to a draining peer against one that discards).

| 1b. network thread, draining peer, flush per frame | ns/entity |
|---|---|
| `send_actor_updates`, one batch of 200 | 1038 |
| `native.send_spatial` x200 | 1306 |
| `send_actor_update` x200 | 1515 |

The MAC under all of these is CrowdyCPP's pre-keyed HMAC: 267 ns to encode and sign, 287 ns
to verify (`bench_codec` / `bench_send` section 1, same machine).

### 2. Receiving: throughput into Python

A blaster process sends datagrams of six signed actor-update notifications as fast as it
can. The client verifies every HMAC on its network thread and Python drains batches with
`poll()`.

| | |
|---|---|
| notifications delivered to Python | 2.0 million/s |
| datagrams received | 334,000/s |
| mean batch | 32 events |
| ring drops, HMAC failures | 0, 0 |

The blaster, a Python process, is the limit here, not the client: nothing was dropped, and
at 287 ns a verification the network thread spends just over half of one core on HMACs.
Python sees one batch object per poll, never an object per notification, unless it iterates
the batch or subscribes handlers.

### 3. Delay to Python

One notification every millisecond carries its send time; an asyncio handler records the
delay when the reader task delivers it. Microseconds:

| p50 | p90 | p99 | max |
|---|---|---|---|
| 47 | 50 | 73 | 461 |

This covers the kernel, verification, the wake socket, the event loop and the handler call.

### 4. Echo round trip

`send_actor_update`, then `flush_sends`, then the echo server process answers with the
notification, then `wait_for_sequence` resolves. Microseconds, 2000 round trips:

| p50 | p90 | p99 | max |
|---|---|---|---|
| 67 | 73 | 83 | 295 |

The echo server is a Python process, and parsing and re-signing is part of each sample.

### 5. Game-loop jitter

A 60 Hz asyncio loop sends 200 entities a frame (one batch) while about 60,000
notifications a second arrive and are dispatched to a handler. Lateness of each frame
against its schedule, ten seconds: p50 0.1 ms, p99 0.1 ms, max 0.2 ms. Receiving never
holds the loop: it is woken once per batch, not per datagram.

## Re-check, 0.8.0

0.8.0 added handlers to the binding (channel audio, and the session's forwarding of
app-defined spatial messages, channel audio and watched voxel updates) and re-vendored
CrowdyCPP 0.60.0. Both builds below ran back to back on the CKS builder (the machine above),
under CPython 3.12.3 and the system's shared OpenSSL 3.0.13 rather than the wheels' static
3.5.9, so compare them with each other, not with the 0.2.0 tables.

| | 0.7.0 (CrowdyCPP 0.59.0) | 0.8.0 (CrowdyCPP 0.60.0) |
|---|---|---|
| 1a. `send_actor_updates`, one batch of 200 | 647.1 ns/entity | 647.2 ns/entity |
| 1a. `native.send_spatial` x200 | 838.1 ns/entity | 840.1 ns/entity |
| 2. notifications delivered to Python, three interleaved runs | 1.774, 1.763, 1.765 million/s | 1.774, 1.762, 1.772 million/s |
| 4. echo round trip, p50 | 74.8 µs | 74.8 µs |
| 5. game-loop lateness, p99 | 0.3 ms | 0.2 ms |

CrowdyCPP 0.60.0's own `bench_send` section 5 (`sendActorUpdate` x200, Release, same machine
and OpenSSL) measured 623.9 ns/entity, so one batch costs 1.04x CrowdyCPP's send path per
entity (1.06x at 0.2.0).
