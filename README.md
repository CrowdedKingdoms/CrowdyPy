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

**v0.1.0: the GraphQL client and the wire codec.** Every portable CrowdyJS 18.0.4 domain
(`AsyncCrowdyClient`, plus the blocking `crowdypy.sync.CrowdyClient` generated from it), the
error hierarchy, the datacenter move, the load-balancer cookie, endpoint re-discovery,
`client.grid()`, and `crowdypy.wire`, CrowdyCPP's codec, byte-identical to CrowdyJS's on the
shared fixtures. The replication connection arrives in 0.2.0, the World Stores, ck-exec
gateway and Game Kit in 0.3.0, and the headless Studio in 0.4.0; until then
[`docs/parity-matrix.md`](docs/parity-matrix.md) lists each of them as a portable gap. See
[MIGRATION.md](MIGRATION.md).

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
| dev | `X.Y.Z.devN` | `crowdypy==0.1.0.dev1` |
| test | `X.Y.ZrcN` | `crowdypy==0.1.0rc1` |
| prod | `X.Y.Z` | `crowdypy==0.1.0` |

pip skips pre-releases unless a requirement names one, so a prod range never picks up a dev
or test build. The same wheels are attached to each GitHub release.

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
        game.set_token(minted.token)
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
   with `client.refresh_gameplay_token()` (concurrent callers share one refresh).
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

## Performance

The replication hot path is built so that Python does no per-datagram work: receive, verify,
decode and dispatch batching run in CrowdyCPP's native thread, which never takes the GIL.
Python sees batched notifications (columnar, with zero-copy numpy views when numpy is
installed), sends in batches with the GIL released, and an asyncio loop is woken through a
socket instead of polling. The connection lands in 0.2.0, with benchmarks against CrowdyCPP's
own numbers.

## Development

```bash
uv sync                       # builds the extension (CMake, a C++20 compiler) and installs dev tools
uv run pytest tests/unit
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

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
