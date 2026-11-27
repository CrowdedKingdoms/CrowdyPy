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

## Re-check, 0.40.0

0.40.0 re-vendored CrowdyCPP 0.94.1, added `verified` to an error's row, and bound the fleet
session. Both builds below ran back to back on the CKS builder on November 26, 2026, under
CPython 3.12.3 and the system's shared OpenSSL 3.0.13, each from a clean build, so compare
them with each other and with the 0.8.0 re-check, and leave the 0.2.0 tables aside. Each row
is three interleaved runs.

| | 0.39.1 (CrowdyCPP 0.93.0) | 0.40.0 (CrowdyCPP 0.94.1) |
|---|---|---|
| 1a. `send_actor_updates`, one batch of 200 | 747.5, 740.5, 735.9 ns/entity | 739.0, 745.8, 741.8 ns/entity |
| 1a. `native.send_spatial` x200 | 930.7, 919.1, 931.5 ns/entity | 917.0, 935.6, 957.1 ns/entity |
| 1a. `send_actor_update` x200 | 1065.8, 1063.2, 1058.3 ns/entity | 1056.7, 1085.4, 1067.9 ns/entity |
| 2. notifications delivered to Python | 1.757, 1.749, 1.735 million/s | 1.465, 1.453, 1.453 million/s |
| 2. mean batch | 31 events | 19 events |
| 4. echo round trip, p50 (one run) | 75.4 µs | 74.8 µs |

The send path is unchanged. **Receiving is about 17% slower in section 2** (1.46 against 1.75 million
notifications a second, with no ring drop and no HMAC failure in either build). The difference
is in the vendored core, and it came with CrowdyCPP 0.94.0, whose replication sources 0.94.1
keeps byte for byte: earlier the same day 0.94.0 measured 1.51 against 1.77 million, and
0.39.1's binding compiled over CrowdyCPP 0.94.0 measured 1.49, 1.50, and 1.52 million. Section 2
sends plain bundles of six notifications, each with its own HMAC, and since 0.94.0 the network
thread no longer outpaces the blaster process. The cause inside CrowdyCPP is not identified
here.

That exposed a second property of CrowdyCPP's connection: its network thread reads until a
batch comes back short, so `disconnect()` does not return while datagrams arrive faster than
the thread verifies them. A script that closes a connection after six seconds of section 2's
flood, six runs for each build: with 0.93.0 `close()` returned every time, after 0.6 to 8.7
seconds; with 0.94.1 it had not returned 23 seconds later in four runs, and took 3.9 and 17.4
seconds in the other two. Section 2 used to close its connection with the blaster still
running. It now stops the blaster first.

### 6. A fleet session's send cost per carried player

`python benchmarks/bench_replication.py fleet` runs section 1a's conditions on a fleet
session: one actor update for each of 200 carried players a frame, an 88-byte payload,
bundling on, manual pump, and a peer that answers the 200 carries and is never read again.

| 6. one actor update for each of 200 carried players | ns/entity | vs a player's connection (1a, same build, mean of three) |
|---|---|---|
| `fleet.send_actor_updates`, one batch of 200 players | 814.7 | 1.10x |
| `native.send_spatial` x200 (the raw binding) | 995.2 | 1.06x |
| `fleet.send_actor_update` x200 (the Python API) | 1160.5 | 1.08x |

Before it signs, a send for a carried player looks the player up in the session's table, and
afterwards it records the sequence number the message left under, each under a lock. Together
they are the 70 ns a batch row costs over a player's connection.
