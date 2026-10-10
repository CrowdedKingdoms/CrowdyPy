# CrowdyPy

The official Python SDK for **Crowded Kingdoms**: typed clients for the platform's one
GraphQL API (identity, studio administration, world data, ck-exec) and a native UDP
replication core.

CrowdyPy follows the [CrowdyJS](https://github.com/CrowdedKingdoms/CrowdyJS) API surface
(the same domains, the same two-token model, the same error codes) the way
[CrowdyCPP](https://github.com/CrowdedKingdoms/CrowdyCPP) does, with Python names
(`mintAppToken` is `mint_app_token`). Its realtime path is CrowdyCPP's native replication
client, bound with [nanobind](https://github.com/wjakob/nanobind) and shipped inside the
wheel. Python never touches a datagram.

**v0.9.0: builds report the SDK they compiled against (CrowdyJS 18.8.0, CrowdyCPP 0.61.0).** See the 0.9.0
bullet below.

**v0.8.0: voice, channel audio, wide voxels, and pause and access refusals (CrowdyJS 18.7.0, CrowdyCPP 0.60.0).**
- Voice: `crowdypy.media`'s voice helpers (an optional 10-byte header per codec frame,
  `VoicePacketizer`, `VoiceJitterBuffer`), and `client.udp.send_channel_audio()` with the
  `channel_audio` handler, which reaches every member of a channel wherever they are (party
  and guild voice).
- World Stores: `ChunkStore` keeps voxel edits the 16x16x16 one-byte grid cannot hold in an
  overlay, and applies the echo of your own edit once. `session.on("voxel_update")` and
  `session.on("generic_spatial")` reach a game that keeps its own world.
- Refusals: `crowdypy.is_app_paused()` reads the runtime gate that tokens and the bootstrap
  carry, and `app_paused_of`, `access_refusal_of` and `actor_exists_of` read the new refusals.
- New calls: `users.player_profile(s)`, `app_access.suspend` and `unsuspend`, and
  `exec.restart_type`.
- 0.9.0: every build (`exec.build`, `build_status`, `wait_for_build` and the mod builds) carries
  `sdk_version`, the `ckx-sdk` (CLIENT half: `crowdy-client-sdk`) version the platform compiled it
  against, whatever the crate names. It needs ck-api v2.40.2, since the build documents select it.
- 0.8.0 needs the ck-api release after v2.39.0, since the token mutations select
  `runtimeGate`. Channel audio and the voxel echo need replication server v0.37.0.
- 0.7.0 brought the input log (`client.input_log.sessions()` and `messages()`, and
  `replayLoggingEnabled`). 0.6.0 brought distance-limited channel messages
  (`udp.send_ranged_channel_message()`) and the terms gate (`auth.record_player_consents()`).
- 0.5.1 made `ChunkStore.hydrate()` keep every recorded voxel edit (`voxelStates`) and
  `ensure_around()` keep a chunk it already loaded.
- 0.5.0 brought the ck-exec gateway check (`client.exec.connect()` sends the connect token
  only to a gateway on the game API's estate, and reports a refusal as `Denied`) and open
  grids (`game_apps.open_permissions()` and `set_open_permissions()`).
- 0.4 brought the headless Crowdy Studio (`crowdypy.studio`), player-host observation
  (`crowdypy.player_host`), the roots CrowdyCPP covers beyond CrowdyJS, and the strict parity
  gate ([`docs/parity-matrix.md`](docs/parity-matrix.md) has no gaps); 0.3.0 the World Stores,
  the Game Kit, ck-exec and subscriptions; 0.2.0 native UDP replication (`client.udp`); 0.1.0
  the GraphQL client.

See [MIGRATION.md](MIGRATION.md).

## Install

```bash
pip install crowdypy
```

Wheels: CPython 3.12+ (one `abi3` wheel per platform) and free-threaded CPython 3.14, on
Linux x86_64 / aarch64, macOS arm64 / x86_64 and Windows x64. OpenSSL is linked statically;
nothing else native is needed. [`docs/compatibility.md`](docs/compatibility.md) has the
details, including the tier versions below.

Each tier of the platform has its own release line, and a build defaults to its own tier's
API origin (`crowdypy.CROWDY_DEFAULT_TIER` says which):

| Tier | PyPI version | Pin |
|---|---|---|
| dev | `X.Y.Z.devN` | `crowdypy==0.4.0.dev1` |
| test | `X.Y.ZrcN` | `crowdypy==0.4.0rc1` |
| prod | `X.Y.Z` | `crowdypy==0.4.0` |

pip skips pre-releases unless a requirement names one, so a prod range never picks up a dev
or test build. The same wheels and the sdist are attached to each
[GitHub release](https://github.com/CrowdedKingdoms/CrowdyPy/releases), and pip installs one
from its URL (`pip install https://github.com/CrowdedKingdoms/CrowdyPy/releases/download/<tag>/<wheel>`).

## Quick start

```python
import asyncio

import crowdypy

API = "https://ck.dev.crowdedkingdoms.com"


async def main() -> None:
    # The identity client: sign-in, account and studio calls, minting.
    async with crowdypy.AsyncCrowdyClient(http_url=API) as identity:
        await identity.auth.login("player@example.com", "correct-horse-battery")
        minted = await identity.portal.mint_app_token("42")

    # The game client: one per app, at the app's own datacenter.
    async with crowdypy.AsyncCrowdyClient(
        http_url=minted.game_api_url or API, discovery_url=minted.discovery_url
    ) as game:
        game.set_app_token(minted)
        print(await game.server_status.server_with_least_clients())


asyncio.run(main())
```

Without an event loop, `crowdypy.sync.CrowdyClient` has the same methods, without `await`:

```python
from crowdypy.sync import CrowdyClient

with CrowdyClient(http_url=API) as identity:
    identity.auth.login("player@example.com", "correct-horse-battery")
    print(identity.users.me())
```

## Two tokens, two clients

There is one API origin but two tokens, as in every Crowded Kingdoms SDK:

1. Sign-in (`auth.login`, a magic link, or social/OIDC) yields an **identity session
   token**: account, studio administration and minting. It is not accepted for gameplay.
2. Gameplay needs a short-lived **app-scoped token** per app
   (`portal.mint_app_token(app_id)`), which is also the HMAC key for native UDP. Refresh it
   with `client.refresh_gameplay_token()` (concurrent callers share one refresh). It is
   refused with `LEGAL_ACCEPTANCE_REQUIRED` until the player's agreement to the legal
   documents and age attestation are stored: show both checkboxes, then call
   `auth.record_player_consents(accept_legal=True, attest_age_of_majority=True)`.
3. Build one identity client and one client per game. The game client points at the app's
   datacenter (`game_api_url`) with `discovery_url` set, so it can find the app again if
   that instance stops answering.

Persist them the same way: `FileTokenStore.session_path(directory, api_origin)` names the
session file (one per origin, shared by every game) and `FileTokenStore.app_path(directory,
app_id)` the gameplay one (one per app):

```python
from crowdypy import FileTokenStore

identity = crowdypy.AsyncCrowdyClient(
    http_url=API, token_store=FileTokenStore(FileTokenStore.session_path(state_dir, API))
)
```

CrowdyPy sends no `Origin` header, so direct sign-in is served to it; hosted browser
sign-in (`portal.signIn` in CrowdyJS) is a browser flow and is not part of this SDK.

## Errors

Every error is a `crowdypy.CrowdyError`. A GraphQL refusal is a `CrowdyGraphQLError` with the
server's stable `code` (`error.code == "FORBIDDEN"`) and `extensions`; HTTP, network and
timeout failures have their own classes. `CrowdyAppUnavailableError` (the app's datacenter
is not serving) and `CrowdyUserCodeFaultError` (a player module faulted;
`player_fault_of(error)` reads the fault) mirror CrowdyJS. A `WRONG_DATACENTER` refusal
moves the client to the app's datacenter and retries once, by itself.

A paused app (its organization ran out of funds, hit a spend cap, or let a subscription
lapse) still mints a token, so check `crowdypy.is_app_paused(minted.runtime_gate)` before
entering the world, and tell the player it is paused. Replication then refuses sends with UDP
error 33 (`APP_PAUSED`). `crowdypy.app_paused_of(error)`, `access_refusal_of(error)`
(`ACCESS_REVOKED`, `ACCESS_SUSPENDED` with `suspended_until`, `ACCESS_NOT_GRANTED`) and
`actor_exists_of(error)` read those refusals. None of them clears by retrying.

## Replication

```python
minted = await identity.portal.mint_app_token("42")
async with crowdypy.AsyncCrowdyClient(
    http_url=minted.game_api_url or API, discovery_url=minted.discovery_url
) as game:
    await game.udp.connect(minted)  # assign a server, open the socket
    game.udp.subscribe({"actor_update": lambda n: print(n.uuid, n.chunk, n.payload)})
    await game.udp.send_actor_update((0, 0, 0), my_uuid, pose_bytes)
    echo = await game.udp.send_actor_update_and_wait((0, 0, 0), my_uuid, pose_bytes)
```

For a game loop, use the connection underneath: send a frame's entities in one call, and
read whole batches.

```python
conn = game.udp.connection
conn.send_actor_updates(chunks, uuids, poses, stride=88)  # numpy (n, 3) int64, n x 32 octets
async for batch in conn.batches():
    actors = batch.types == crowdypy.wire.MessageType.ACTOR_UPDATE_NOTIFICATION
    positions = batch.chunks[actors]  # a view, not a copy
```

`crowdypy.replication.ReplicationConnection` is the same for code without an event loop:
`wait()` and then `poll()` from your own loop.

Voxel positions and types are the app's signed 16-bit values, and a voxel state is at most
1,024 bytes (`InvalidArgument` otherwise, nothing sent). Replication server v0.37.0 echoes
every accepted voxel edit back to its sender as a `voxel_update`.

## Voice

`send_audio_packet` reaches players near a chunk and carries whatever bytes it is given; a game
with a voice format of its own keeps it. The voice helpers in `crowdypy.media` are an optional
format that every Crowded Kingdoms SDK shares, byte for byte. Each packet is a 10-byte header
(version 1, the codec, a `u16` seq, a `u32` timestamp in codec samples, the frame's duration
and two talk-spurt flags) followed by one codec frame. `VoicePacketizer` numbers one sender's
frames. `VoiceJitterBuffer` puts each sender's packets back in order and plays them out 60 ms
after the first packet of a talk spurt arrived. It reports a frame that never came as a gap,
drops one that arrives after its playout time, and holds at most 64 frames a sender.

`send_channel_audio` reaches every member of a channel wherever they are (party or guild
voice), at most 1,024 bytes a packet. The sender needs the channel's `send_voice`
(`members_can_speak=True` on creation gives it to the member role) and the player's
`use_voice_chat`. There is no echo, and members receive `channel_audio`:

```python
from crowdypy.media import VoiceCodec, VoiceJitterBuffer, VoicePacketizer

party = VoicePacketizer(VoiceCodec.OPUS, 20)
# Every 20 ms while the player talks (`opus_frame` from your own encoder):
await game.udp.send_channel_audio(channel_id, my_uuid, party.packetize(opus_frame, last=released))
# ...and party.skip() for every 20 ms of silence that is not sent.

voices = VoiceJitterBuffer()
game.udp.subscribe({"channel_audio": lambda n: voices.push(f"{n.channel_id}:{n.uuid}", n.payload)})
for slot in voices.poll():  # at least once a frame, from your audio clock
    if slot.gap:
        conceal(slot.key, slot.frame_ms)  # the frame never came
    else:
        play(slot.key, slot.codec, slot.frame)
```

No codec ships with CrowdyPy, and the wheel links none: which codec an app uses (Opus, or
G.711 µ-law where it has no Opus), and the library that encodes and decodes it, is the app's
choice. Where a voice sits in the world (panning, distance attenuation) is the game's too.

## World Stores

```python
from crowdypy.codecs import f32, struct_codec, u16
from crowdypy.stores import create_world_session

pose = struct_codec({"x": f32(), "y": f32(), "z": f32(), "yaw": u16()})
session = create_world_session(game, app_id, actor_codec=pose, host=True, save=True)
session.self.join((0, 0, 0), {"x": 0.0, "y": 64.0, "z": 0.0, "yaw": 0})
session.actors.on_join(lambda actor: print("joined", actor.uuid, actor.value))
asyncio.create_task(session.run())  # tick 60 times a second

snapshot = session.actors.snapshot()  # once a frame: every actor at once
states = pose.decode_many(snapshot.state_offsets, snapshot.state_data)  # numpy, no loop
```

`session.chunks` is a helper for 16x16x16 chunks with one byte per voxel. An edit that grid
cannot hold (a type outside 0-255, a position outside 0-15) is kept in the chunk's
`overlay()`, and `voxel_type_at` reads it first. The echo of your own `set_voxel` is not
applied a second time. A game with other addressing reads every edit from
`session.on("voxel_update", ...)`, which includes those echoes.

## Crowdy Studio

```python
from crowdypy.studio import CrowdyStudioController

studio = CrowdyStudioController.for_client(game, app_id=app_id, grid_id=grid_id)
await studio.initialize()  # opens the first project
studio.update_file("SERVER", "src/lib.rs", source)  # autosaves
result = await studio.test_draft()  # build, deploy and switch on the mod
print(result.status, studio.get_state().build_output)
```

A CLIENT target runs in a host you pass as `broker_factory`; the browser Studio runs it in a
Web Worker.

## Performance

Python does no work per datagram. Receive, HMAC verification and decoding happen on
CrowdyCPP's network thread, which never takes the GIL; Python sees one batch object per
poll. Sends release the GIL, and a batch releases it once for the whole batch. An asyncio
loop is woken through a socket, not by polling. Measured against CrowdyCPP on the same
machine ([`benchmarks/README.md`](benchmarks/README.md)): a 200-entity batch costs 1.06x
CrowdyCPP's own send path per entity, Python takes in two million verified notifications a
second, a notification reaches an asyncio handler 47 µs after it hits the wire (p50), and a
60 Hz loop stays within 0.2 ms of its schedule while receiving 60,000 a second.

## Development

```bash
uv sync                       # builds the extension (CMake, a C++20 compiler) and installs dev tools
uv run pytest tests/unit
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

The live suites in `tests/e2e/` run against a deployment when the `CROWDY_E2E_*` variables
are set; [`docs/e2e-coverage.md`](docs/e2e-coverage.md) lists them, how to configure them
and the last live runs.

The gates CI runs, all from a clean checkout:

| Gate | Command |
|---|---|
| Vendored CrowdyCPP is the pinned commit's tree | `python scripts/vendor_crowdycpp.py --check` |
| schema.gql and operations are CrowdyJS's at the pin | `python scripts/schema_sync.py --check` |
| Generated code is current | `python scripts/codegen.py --check` |
| The blocking client is generated from the async one | `python scripts/unasync.py --check` |
| Parity with CrowdyJS | `python tools/parity/parity.py --crowdyjs ../CrowdyJS --check docs/parity-matrix.md` |
| No internal names in the tree or the artifacts | `python scripts/ci/check_content_policy.py` |

[AGENTS.md](AGENTS.md) covers maintenance: re-pinning CrowdyJS and CrowdyCPP, the tier
branches and the release path.

## License

MIT. Wheels also contain CrowdyCPP (MIT), yyjson (MIT) and OpenSSL (Apache-2.0).
