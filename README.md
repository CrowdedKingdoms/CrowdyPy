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

**v0.40.0: fleet sessions for an organization's game servers.** A game server opens one realtime session with the organization's org token (`client.fleet_sessions`), carries its players on it (`crowdypy.fleet`), and reaches hubs as itself (`client.exec.connect_as_server`, `call_as`). A player who moves between the organization's servers signs in once. Needs Application API v2.89.0 or later, realtime servers v0.50.3, and ck-exec 0.25.0 for the hub connection, and a Crowded Kingdoms operator switches fleet sessions on for an organization. See [Game servers: fleet sessions](#game-servers-fleet-sessions) and [MIGRATION.md](MIGRATION.md).

**v0.39.1: a dropped build-status read no longer ends a Studio deploy.** `CrowdyStudioController`'s build wait and `exec.wait_for_mod_build` read the status again after no answer, an HTTP 5xx or 429 that is not the API's answer, or `PLATFORM_BUSY`, and a CLIENT-only project's mod listing and mod starter get one more try. A refusal still ends the wait. See [MIGRATION.md](MIGRATION.md).

**v0.39.0: the Game API is now the Application API.** Results carry `application_api_url` / `application_api_ws_url` beside the deprecated `game_api_url` / `game_api_ws_url`, which go on January 1, 2027; needs Application API v2.85.0. See [MIGRATION.md](MIGRATION.md).

**v0.38.0: choose whose voice and video each player receives.** From replication server v0.49.0, `client.udp.set_speakers(voice=[...], video=[...])` names the actors a player receives spatial voice and webcam video from (`None` for everyone again); streams a list leaves out are neither sent nor billed. See [MIGRATION.md](MIGRATION.md).

**v0.37.0: a speaker's own voice and video stop coming back.** From replication server v0.48.0 a session is not sent its own spatial voice and video; `client.udp.set_own_media_echo(True)` asks for the copy and `False` stops it, for a game that confirms capture by playing back what came round (`own_media_echo=True` on `connect` is the starting value). The same server counts keepalives as data in. See [MIGRATION.md](MIGRATION.md).

**v0.36.0: an organization's cap of apps.** `app_limit_reached_of(err)` reads Game API v2.80.0's `APP_LIMIT_REACHED` (an organization without a funded wallet or auto-billing past 10 apps) as `CrowdyAppLimitReached(limit, max)`, and the exec status reports the compute ceiling an unfunded organization's apps share. Vendors CrowdyCPP 0.89.0; schema at CrowdyJS 18.35.0.

**v0.35.0: a full channel says so.** `group_limit_reached_of(err)` reads Game API v2.77.0's `GROUP_LIMIT_REACHED` (a join, an add or a creation past a channel or team cap) as `CrowdyGroupLimitReached(limit, max)`, and `payment_method_required` no longer counts wallet funds. Vendors CrowdyCPP 0.88.0 and follows CrowdyJS 18.34.0; works with every Game API on dev, test and prod. See [MIGRATION.md](MIGRATION.md).

**v0.34.0: a mod knows the Studio revision it runs.** `client.exec.mod_build` and `mod_client_build` take the Studio project and saved revision a crate is (`ExecStudioSource`), and the build, the mod version and the CLIENT half deployed from it report them; the Studio controller sends them. `client.exec.mod_client_starter` is the platform's CLIENT half starter, which a new project's CLIENT target now starts from, and `client.crowdy_studio.links()` is this tier's Studio origin and wallet page. Vendors CrowdyCPP 0.87.0 and follows CrowdyJS 18.33.0; needs Game API v2.73.0. See [MIGRATION.md](MIGRATION.md).

**v0.33.0: a voxel's time is its chunk's whole-chunk write.** The chunk store dates a copy's voxels by `voxelsUpdatedAt` (Game API v2.70.0, where an `updateChunk` or `world.set_chunk` supersedes older edits), so a chunk-state or LOD write drops no newer change; `CachedChunk.row_at` orders copies. Vendors CrowdyCPP 0.85.0. Needs ck-api v2.70.0. See [MIGRATION.md](MIGRATION.md).

v0.32.0: CrowdyCPP 0.84.0's chunk store. The store merges by the server's time (`chunks.confirmed_at`): an older change is dropped, a change made while a chunk loads survives the copy, another client's edit goes under your unanswered one, `catch_up(since, boxes=None)` covers every tracked chunk and merges edits by their time, `evict` and `prune_if` prune any view, and `hydrate` returns whether it applied. After a v0.46.0 server acknowledges the bound control frames, legacy reconnects and releases are dropped. Needs ck-api v2.69.0. See [MIGRATION.md](MIGRATION.md).

**v0.31.0: hardening from CrowdyCPP 0.83.0.** MessagePack keys that are containers are refused (a nested key blew up memory), a NaN serialises as `null` in place, unchanged keyframes of many actors are not gameplay input, and a legacy reconnect command is paced (`legacy_reconnect_window_ms`). See [MIGRATION.md](MIGRATION.md).

**v0.30.0: the ChunkStore rework.** `ensure_around` reads only what is missing, as boxes, paged; `set_voxel` is paced and returns an edit id, and an edit the server refuses or never echoes is undone (`on_edit_refused`); a replaced chunk is read again and a reconnect catches up; realtime edits are no longer written back. CrowdyJS 18.30.0 and CrowdyCPP 0.82.0. See [MIGRATION.md](MIGRATION.md).

**v0.29.0: reconnect commands verify, bound control frames, and `chunk_changed`.** A server's `COMMAND_RECONNECT` verifies again; against replication servers v0.45.0 the reconnect and release frames carry a per-assignment nonce under their HMAC, so one captured earlier is refused (`stats()["stale_bound_control"]`); `chunk_changed` reports a chunk a hub's or mod's `world.set_chunk` replaced. CrowdyJS 18.29.0 and CrowdyCPP 0.81.0. See [MIGRATION.md](MIGRATION.md).

**v0.28.0: region reads.** `chunks.by_distance()` and `voxels.list_by_distance()` take a box (`lowChunk`, `highChunk`) as well as a cube, pages are stable and answer the paging applied, `since` filters a chunk read, `voxels.list()` pages, and `game_apps.nearby_grids()` returns your own `permissionKeys`. Needs ck-api v2.67.0. CrowdyJS 18.28.0 and CrowdyCPP 0.80.0. See [MIGRATION.md](MIGRATION.md).

**v0.27.0: a replayed idle release cannot end a playing player's session.** An idle release that arrives within a minute of this client's own gameplay input reconnects instead of going `ConnState.INACTIVE` (`idle_release_guard_ms`, counted in `stats()["idle_releases_after_input"]`), and a reassignment left over from the session that ended no longer reassigns the next one. The native chunk store keeps unechoed local edits over a stored copy. CrowdyJS 18.27.0 and CrowdyCPP 0.79.0. See [MIGRATION.md](MIGRATION.md).

**v0.26.0: a new account confirms its email.** Since ck-api v2.65.0 a password account made since 2026-11-11 gets no gameplay token until it opens the link `auth.register` emailed: minting raises `EMAIL_CONFIRMATION_REQUIRED`, which `crowdypy.is_email_confirmation_required_error` reads. Tell the player to check their inbox, offer `auth.resend_confirmation_email(email)`, then mint again. Accounts made before then and players an organization manages are not asked. CrowdyJS 18.27.0 and CrowdyCPP 0.78.0. See [MIGRATION.md](MIGRATION.md).

**v0.25.0: a session released for idleness is final.** The replication server ends a session that sends no gameplay input for the app's idle timeout (15 minutes unless the app sets its own `idleTimeoutSeconds` with `apps.update()`; `0` turns it off), and the connection moves to `ConnState.INACTIVE` instead of reconnecting: call `client.udp.connect()` on the player's next input. It needs ck-api v2.64.0 on the tier. CrowdyJS 18.26.0 and CrowdyCPP 0.77.0. See [MIGRATION.md](MIGRATION.md).

**v0.24.0: appointed moderators, regions, and the consent refusal's reason.** ck-api v2.61.0 refuses a player under 18 `SAFETY_MODERATOR_REQUIRED` in an app whose organization has appointed no safety moderator (`crowdypy.errors.safety_moderator_required_of`; `apps.app()` reads `hasSafetyModerator` for the organization's members), checks some countries by region (`region` on an org-managed player's mint and assertion, `regionCode` on `platform.territory_policies()`), and says why parental consent cannot be requested: `parental_consent_unavailable_of` now returns a `CrowdyParentalConsentUnavailable` with its `reason`, or `None`. An age refusal at sign-up stops the same client for 24 hours. It needs ck-api v2.61.0 on the tier. CrowdyJS 18.25.0 and CrowdyCPP 0.76.0. See [MIGRATION.md](MIGRATION.md).

**v0.23.0: teen safety.** ck-api v2.60.0's age, territory and documents gate, and a player's own safety controls. `auth.register` takes the age screen's `birth_date`, `country` and `parental_confirmation`, `auth.record_player_age` gives an account its age once, and `crowdypy.errors` reads the gate's refusals (`age_requirement_not_met_of` is deliberately a boolean: no refusal names an age). `client.safety` holds the voice and video scopes, contacts, blocks, a parent's consent and reports; `client.moderation` is the app's moderators'; `apps.recording` is what the notice and the indicator show. Org-managed players carry `ageYears` and `country`. It also brings CrowdyJS 18.23's Studio game editor (`CrowdyStudioController(draft_store=...)`). It needs ck-api v2.60.0 on the tier. CrowdyJS 18.24.0 and CrowdyCPP 0.75.0. See [MIGRATION.md](MIGRATION.md).

**v0.22.0: a deploy's dropped node types.** `exec.instances` returns `orphaned`, true for an instance whose node type the app's active version no longer deploys: ck-exec 0.19 stops such an instance at once when a deploy or a rollback drops its type, and keeps its saved state. It needs Game API v2.58.0 on the tier. CrowdyJS 18.22.0 and CrowdyCPP 0.74.0. See [MIGRATION.md](MIGRATION.md).

**v0.21.0: a send waiting across a reassignment is resent then.** An actor or voxel update still waiting for its echo when the connection is reassigned is resent as soon as the new session is connected, rather than at its timeout, since its echo went to the session it left. That is its one retry; audio, text and events are never resent. CrowdyJS 18.21.0 and CrowdyCPP 0.73.0. See [MIGRATION.md](MIGRATION.md).

**v0.20.0: a session's first awaited send is not lost.** Every `*_and_wait` retries the first awaited send of a session once, with a fresh sequence, when it is refused `UNAUTHORIZED` or, for the echoed actor and voxel updates, when it times out: how a replication server older than v0.42.0 answers a session's first message (v0.42.0 holds it until the session is ready). CrowdyJS 18.20.0 and CrowdyCPP 0.72.0; no API change. See [MIGRATION.md](MIGRATION.md).

**v0.19.0: a draining instance is left at once.** Game API v2.56.0 expires the balancer's affinity cookie on a draining instance's responses; CrowdyPy forgets the expired cookie, so the next request lands on an instance that is staying. CrowdyJS 18.19.0 and CrowdyCPP 0.71.0; no API change. See [MIGRATION.md](MIGRATION.md).

**v0.18.0: text chat and direct-message keys, and the recording switch.** Game API v2.55.0 adds the app-level `use_text_chat` and `use_direct_messages` keys: without them the realtime servers refuse a player's text, channel text and direct messages with `UNAUTHORIZED`. A tier that grants `access` gets both unless `applyCommunicationDefaults` is false, and `replayLoggingRecordsMedia` leaves voice and video out of an app's recording. Deleting a managed player now erases its content, and a legal hold refuses it with `LEGAL_HOLD`. See [MIGRATION.md](MIGRATION.md).

**v0.17.0: the Org-Managed Players Addendum.** ck-api v2.53.0 serves org-managed players on every tier, once an admin of the organization has accepted the addendum in Studio; until then its sign-ins and refreshes answer `ORG_LEGAL_ACCEPTANCE_REQUIRED`, which `crowdypy.errors.org_legal_acceptance_required_of` reads, and `client.managed_players.settings(org_id)` says where the organization stands. CrowdyJS 18.17.0 and CrowdyCPP 0.69.0. See [MIGRATION.md](MIGRATION.md).

**v0.16.0: org-managed players.** An organization that runs its own accounts signs its players in itself: its server mints a player's app token with an org token, or its sign-in service signs an assertion. A device holds the first with `client.portal.adopt_app_token(response)` (it refuses an org token, which stays on the organization's server; there is no org-token mint wrapper) or trades the second with `client.portal.exchange_managed_player_assertion(app_id, assertion)`, and gives the result to `client.set_app_token`. `client.managed_players` is the organization's administration. CrowdyJS 18.16.0 and CrowdyCPP 0.68.0. See [MIGRATION.md](MIGRATION.md).

**v0.15.0: a voxel edit that waits for the server, and opcode 140 from `client.udp`.** `chunks.set_voxel(..., optimistic=False)` changes nothing locally until the server's echo of the edit arrives; `client.udp.send_generic_spatial` sends an app-defined spatial message. CrowdyJS 18.15.0 and CrowdyCPP 0.67.4. See [MIGRATION.md](MIGRATION.md).

**v0.14.2: CrowdyJS 18.14.0 and CrowdyCPP 0.67.3.** `player_wallet.charges()` is deprecated: read `transactions()`, whose `usage_debit` entries are the usage charges.

**v0.14.1: the exec socket names a paused app and a switched-off app or type.** `AppPaused`, `AppDisabled`, and `TypeDisabled`. Still CrowdyJS 18.13.0 and CrowdyCPP 0.66.0.

**v0.14.0: the economy (CrowdyJS 18.13.0, CrowdyCPP 0.66.0).** `kit.player`, the caller's own
`player` hub, and its views `kit.inventory`, `kit.wallet`, `kit.progression`,
`kit.achievements`, `kit.quests`, `kit.crafting`, `kit.loot` and `kit.production`; markets by
name: `kit.shops.at(key)`, `kit.market.at(key)` and `kit.trades.at(key)`. It lives in hub state
and is not a ledger. A change refused `unavailable` is sent again with its action id. They need
ckx-sdk 0.12.1 in the tier's compute toolchain (dev: v1.15.0).

**v0.13.0: the genre starters (CrowdyJS 18.12.0, CrowdyCPP 0.65.0).** Nine more kit modules,
one per ck-exec genre starter: `kit.leaderboards.board(name)`, `kit.tables.table(key)` (the
caller's hand as private state), `kit.tactics.battle(key)`, `kit.encounters.encounter(key)`,
`kit.minigames.duel(key)`, `kit.instances.run(key)`, `kit.arenas.arena(key)`,
`kit.racing.race(key)` / `ball(key)` and `kit.territory.map(key)` / `zone(key)`, in both clients.
They need a compute toolchain with ckx-sdk 0.11.0 on the tier.

**v0.12.0: the Game Kit on ck-exec (CrowdyJS 18.11.0, CrowdyCPP 0.64.0).** `client.kit(app_id)`
gains typed clients for the ck-exec starter hubs, the client half of the game kit whose server
half is `ckx_sdk::kit`: `kit.world` (conditions and resource nodes mirrored, harvests, liveops for
developers), `kit.matchmaking.queue(name)` (parties matched whole), `kit.sessions.open(key)` (lobby
to result), `kit.npcs.area(key)` (mobs, NPCs, players' pets), `kit.topology` (compose, check, build
and deploy the starters), and `kit.spectate`, `kit.emotes` and `kit.shards`. They share one exec
connection (`kit.runtime`) that paces calls under the gateway's limit, resends only what is safe
and mirrors topics with resync on gaps, epochs and reconnects; a hub's refusal is a `KitError`
with its `code`. Both clients have it: the blocking one runs a mirror's resync beside the
connection's reader thread. Exec reconnects wait with jitter. See [MIGRATION.md](MIGRATION.md).

**v0.11.0: access that ends ends for good, and channel and team bans (CrowdyJS 18.10.0, CrowdyCPP
0.63.0).** A host's `4403` close (ck-exec 0.16: the player was suspended, revoked, signed out or
blocked) is final: `ExecConnection.on_access_ended` hears the reason, the call in flight fails
`Denied`, and the connection does not redial. `client.channels.ban` / `unban` and
`client.teams.ban` / `unban` manage bans, and `account_blocked_of` / `group_member_banned_of` read
the two refusals. See [MIGRATION.md](MIGRATION.md).

**v0.10.0: the input log says what it lost (CrowdyJS 18.9.0, CrowdyCPP 0.62.0).** Every session
from `client.input_log.sessions` carries `missingRecords`, the inputs the replication server
accepted that never reached the log, which needs the Game API release after v2.40.2. Its
docstrings name the new end reasons (`paused` among them), the two read refusals worth retrying
with the same cursor, and how to stop a read nobody will see: cancel its task, which closes the
request and frees its server read share. See [MIGRATION.md](MIGRATION.md).

**v0.9.1: the input log's session filter sends `userId` as a string, and its reads document when
to retry.** See the 0.9.1 bullet below; 0.9.0 added the SDK a build compiled against.

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
   `auth.record_player_consents(accept_legal=True, attest_age_of_majority=True)`. With an
   app's own documents (`auth.app_required_documents(app_id)`), pass its `app_id` too.
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

Every mint and refresh passes the player gate (ck-api v2.60.0), and its refusals have readers
too: `territory_not_supported_of` (with the `country`), `age_required_of` (give the age with
`auth.record_player_age`), `age_requirement_not_met_of` (a boolean: no refusal names an age),
`legal_acceptance_required_of` (with the `missing_documents`), `safety_log_required_of` and
`safety_moderator_required_of` (ck-api v2.61.0). `client.safety` answers
`social_limit_reached_of` and `parental_consent_unavailable_of` (with the `reason`). After an
age refusal at sign-up or at `record_player_age`, the same client is refused both for 24 hours
whatever it sends: do not ask again.

## Player safety

`client.safety` is the player's own controls in one app, on the game client (the app token):
voice and video scopes (`comm_settings`, `set_comm_settings`; a player can only lower them),
contacts (`request_contact`, `accept_contact`, `remove_contact`, `contacts`,
`contact_requests`), blocks (`block`, `unblock`, `blocks`), a parent's consent for a player
under 18 (`request_parental_consent`) and reports to the app's moderators (`file_report`). The
platform enforces all of it on the realtime servers. When `apps.recording(app_id)` says voice
or video is recorded, show the player your notice and indicator. An app's moderators
(`moderate_safety`) read and decide reports with `client.moderation`.

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

## Game servers: fleet sessions

A fleet session is one realtime session that a game server opens for itself and uses for
every player on that server. A player signs in once, when they join the game. When the
player moves to another of your servers, that server starts to carry them with one UDP
message, and neither server calls the Application API.

**A fleet session runs on a server your organization runs.** Your organization's org token
opens it, and its fleet token signs for every player the session carries. An org token and a
fleet token never go to a player's device or into a browser page. A game on a player's device
uses `client.udp` with that player's own app token.

**CrowdyPy 0.40.0 carries fleet sessions.** They need Application API v2.89.0 or later,
realtime servers v0.50.3, and ck-exec 0.25.0 for the hub connection. All three have been on
dev, test, and prod since November 26, 2026. Fleet sessions stay off for an organization
until a Crowded Kingdoms operator switches them on, and the organization also needs
org-managed players (`client.managed_players`). Until then, the Application API refuses
`fleet_sessions.open` with `FORBIDDEN`.

```python
import os
import crowdypy
from crowdypy.fleet import AsyncFleetSession

# A client for this server, holding the org token (cko_..., scope fleet:session).
# Give it no token_store: the org token stays where your server keeps its secrets.
api = crowdypy.AsyncCrowdyClient(http_url="https://ck.dev.crowdedkingdoms.com")
api.set_token(os.environ["CK_ORG_TOKEN"])

fleet = AsyncFleetSession(
    await api.fleet_sessions.open(app_id),  # one session for each game server
    handlers={
        "actor_update": lambda n, players: world.apply(n),  # state arrives once for the session
        "text": lambda n, players: chat.deliver(players, n),  # only these players
        "released": lambda r: roster.remove(r.player_game_token_id),  # the platform ended a carry
    },
)
await fleet.connect()

# A player joined this server. `minted` is the app token your server minted for them
# (mintManagedPlayerToken): its token and its gameTokenId.
carry = await fleet.carry(minted)
if carry.carried:
    fleet.send_actor_update(carry.player_game_token_id, chunk, uuid, state)

# The player left the game.
fleet.release(carry.player_game_token_id)

# In your loop: keep the session, and follow it to another realtime server.
if fleet.refresh_due():
    fleet.apply_refresh(await api.fleet_sessions.refresh(fleet.refresh_input()))

# The server stops.
await api.fleet_sessions.close({"appId": fleet.app_id, "gameTokenId": fleet.game_token_id})
await fleet.close()
```

`crowdypy.fleet.FleetSession` is the same session for code without an event loop: call
`poll()` every tick, `carry_nowait(player)` to start a carry, and read its result in the
`carry_result` handler. The session is CrowdyCPP's, bound as a player's connection is: its
network thread verifies every frame with the fleet token before Python sees it, and Python
takes events in batches.

### The org token that opens a session keeps it and ends it

`client.fleet_sessions` (also `client.admin.fleet_sessions`) wraps the three Application API
calls, on the async client and on the blocking one. Each takes the org token as the client's
bearer.

| Call | Application API | Answer |
| --- | --- | --- |
| `open(app_id)` | `openFleetSession` | `FleetSessionAnswer`: `token` (64 characters), `game_token_id`, `app_id`, `expires_at` (24 hours away), `max_players`, and the realtime `server` (`ip4`, `ip6`, `client_port`) |
| `refresh({"appId", "gameTokenId", "currentServer"})` | `refreshFleetSession` | The same token and `game_token_id`, with `expires_at` moved to 24 hours from now |
| `close({"appId", "gameTokenId"})` | `closeFleetSession` | `True` when a session was ended |

- **One org token owns a session.** `refresh` and `close` work with the org token that
  opened the session. Another org token of the same organization gets `NOT_FOUND` from
  `refresh` and `False` from `close`.
- **Revoking an org token ends what it opened:** every fleet session and every hub
  connection.
- **A session ends at its `expires_at`** when it is neither refreshed nor closed: 24 hours
  after it was opened or last refreshed.
- **Limits.** An organization holds at most 512 fleet sessions at once, and a session
  carries at most 256 players, unless an operator set other numbers for the organization.
  The answer's `max_players` is the session's own number. Open one session for each game
  server.
- **The fleet token is a signing key.** The Application API does not accept it as a bearer,
  and the repr of a `FleetSessionAnswer` and of a session never shows it.

The session makes no Application API call. A server that calls the API with its own HTTP
client gives `AsyncFleetSession` the same fields, as the API names them.

### Carrying a player takes one message and proof of the player's token

`await fleet.carry(player)` sends `FLEET_CARRY`. `player` is the answer of
`mintManagedPlayerToken` or `refreshAppToken`, a mapping with its `token` and `gameTokenId`,
or a `FleetPlayerToken`. The message names the player's game token id and carries a proof
that your server holds the player's token: an HMAC keyed with that token over the session's
game token id, the player's game token id, and the time. The result says how the carry ended:

| `status` | Meaning |
| --- | --- |
| `carried` | The session carries the player. `user_id` is the player's user id. |
| `no_live_token` | The realtime servers know no live token by that id: it expired, the sign-in ended, or it belongs to another app. The session asked twice before it settled on this answer. |
| `other_organization` | The token belongs to another organization's player. |
| `proof_failed` | The proof or its time failed. A clock more than 30 seconds from the server's fails too. |
| `session_full` | The session carries as many players as it may (`max_players`). |
| `timed_out` | No answer came within 5 seconds of the first send. |
| `refused` | A status this version of the SDK does not know. `wire_status` has the byte. |
| `released`, `closed` | You called `release` for the player, or `close`, while the carry was waiting. |

- **An unanswered carry is sent again** every 250 ms, each time with a new time and a new
  proof (`carry_retry_ms`, `carry_timeout_ms`).
- **A carry answered "no live token" is sent once more**, 250 ms later. The carry settles on
  the answer to that second send.
- **Carries are paced.** After a burst of `max_players` carries (32 at least), the session
  sends at most 50 `FLEET_CARRY` messages a second, first sends and retries together
  (`max_carries_per_second`, at most 63). The realtime servers drop a session's carries past
  64 a second without an answer.
- **A crossing is a carry on the next server.** Stop sending for the player on this server,
  then give the next server the player's token over your own link. Its session carries the
  player, and this session's `released` handler fires with reason 1.
- **`fleet.release(player_game_token_id)`** sends `FLEET_RELEASE` when the player leaves the
  game. The message has no answer. If it is lost, the carry stays on the platform until
  another session carries the player, their sign-in ends, or they idle out.

The `released` handler reports a carry the platform ended, with its reason: 1 another
session carries the player, 2 the player's sign-in ended or their token expired, 5 the
player sent no gameplay input for the app's idle timeout (15 minutes unless the app sets its
own). The platform sends that message once. It can be lost, and it can arrive up to about 6
seconds after the carry ended, so after a crossing the refusal of the previous server's next
send for the player (`UNAUTHORIZED`) is the sure sign.

### A refreshed token replaces the old one with no gap

A player's token expires. Whoever holds the token refreshes it with `refreshAppToken`
before then, and the answer has a new token and a new game token id. Carry the player again
with both, and name the id the player is carried under now:

```python
result = await fleet.carry(
    FleetPlayerToken(refreshed.token, refreshed.game_token_id, replaces=previous_game_token_id)
)
```

- **The player stays carried throughout**, with their actors and their idle clock. No
  `released` event fires.
- **Every send names the new id from the moment the new `FLEET_CARRY` is sent.** A send you
  make with either id goes out under the old id before that moment and under the new id
  after it.
- **Use the new id once the result is `carried`.** From then on the old id names nobody: a
  send with it raises `FleetNotCarriedError`, and `fleet.carries` answers false for it.
- **A late message about the old id changes nothing.** The session ignores it and counts it
  (`replaced_id_frames` in `fleet.stats()`).
- **A refused replacement leaves the player carried under the old id.** A replacement with
  no answer in 5 seconds ends the carry under both ids.

### Every send names the carried player it is for

Each of `client.udp`'s sends has a form that takes the carried player's game token id first:
`send_actor_update`, `send_voxel_update`, `send_client_event`, `send_text_packet`,
`send_audio_packet`, `send_video_packet`, `send_video_frame`, `send_generic_spatial`,
`send_single_actor_message`, `send_channel_message`, `send_channel_audio`,
`send_ranged_channel_message`, `send_heartbeat`, and `set_speakers`. The message is the one
that player's own session would send, signed with the fleet token, and the platform applies
that player's permissions, blocks, age rules, speaker lists, rate budget (500 messages a
second), and idle timeout to it. Each send returns the message's sequence number.

- **A send for a player the session does not carry now raises `FleetNotCarriedError`** and
  sends nothing.
- **A tick of players is one call.** `fleet.send_actor_updates(players, chunks, uuids,
  states)` sends one actor update for each player and releases the GIL once. It returns how
  many left and the rows it skipped because their player is not carried now.
- **The session sends no heartbeat for an actor by itself.** Call `send_heartbeat(player,
  chunk, uuid)` every 2 seconds for an actor that sends nothing else, so that it stays
  present. A heartbeat is not gameplay input and does not hold off the idle timeout.

### State arrives once for the session, and communications name their players

Every notification handler receives `(notification, players)`:

- **`players` is `None` for state**: actor updates and departures, voxel edits, server
  events, and chunk changes. State arrives once for the session, for every one of your
  players in range, and your server decides which of them see it.
- **`players` is a tuple of game token ids for a communication**: spatial voice, text, and
  video, direct messages, channel messages, and channel audio. The notification arrived
  inside a `FLEET_DELIVERY`, which lists the only carried players who may receive it (1 to
  32). Deliver it to those players and no others.
- **A client's event or an app-defined spatial message arrives either way.** It has a list
  when blocks, age rules, or scopes exclude some of your players in range, and `None` when
  all of them may receive it.

The session verifies every frame with the fleet token before a handler runs, and no option
turns that off. It drops a frame that fails and counts it in `fleet.stats()`
(`hmac_failures`, `unsigned_notifications`, `malformed`), and it does the same with a
notification that names another app than the session's (`wrong_app`). The largest datagram a
fleet session is sent is 1,242 bytes. A server that receives thousands of notifications a
second reads whole batches: `fleet.poll()` and `fleet.batches()` give a `FleetBatch`, whose
columns are the ones a player's connection has, with `listed`, `recipient_offsets`, and
`recipient_ids` for the players of each row.

### A refused send is signed, and the handler says so

The `generic_error` handler receives a refused send and a second argument, `verified`. A
refusal is 3 bytes: the message type, the sequence number of the send, and a code.

- **`verified` is true for a refusal that arrived inside a bundle the session verified with
  the fleet token.** Realtime servers from v0.50.3 send a fleet session every refusal that
  way. The `send_refused` handler then names the player (`FleetSendRefused`), when no send
  for another player used the same sequence number in the last 3 seconds.
- **`verified` is false for a refusal that arrived alone.** 3 bytes leave no room for a
  signature, so anyone who can reach the session's socket could have sent it. Treat one as
  a hint: stop sending for that player, then carry the player again and act on that answer.
- **The session changes nothing on a refusal.** Your code decides what a refusal means for
  the player. `TOKEN_EXPIRED` is about a carried player's token: refresh it and carry again
  with `replaces`.

### The session refreshes before it expires, and follows a move

`fleet.refresh_due()` is true once 12 of the session's 24 hours have passed. It is also true
as soon as the realtime server tells the session to connect to another server, releases it,
or says nothing that verifies for 45 seconds (`silence_timeout_ms`; the `move_ordered` and
`session_released` handlers). Call `refreshFleetSession` then, with `fleet.refresh_input()`,
and give the answer to `fleet.apply_refresh`.

- **`refresh_input()` names the server the session is on**, so the session stays there
  while that server still serves the app. After a move order it names no server, and the
  answer's `server` is the next one.
- **The same server:** the carries stand, and the session sends its capabilities again.
- **Another server**, or the same one after it released the session or went quiet: the
  session connects, sends its capabilities, and carries every player again with a new time
  and proof. Until a player's answer arrives, sends for that player raise
  `FleetNotCarriedError`. Each answer reaches the `carry_result` handler.
- **A refresh refused `NOT_FOUND`** (`fleet_session_not_found_of`) means the session ended.
  Open a new one and carry your players again.
- **`session_released` carries a reason:** 0 the fleet token was revoked (the session was
  closed through the Application API, or its org token was revoked), 2 the fleet token
  expired, and 1, 3, and 4 the server stopped serving the session. Every reason makes a
  refresh due.

### The server connects to hubs as itself

`client.exec.connect_as_server(app_id)` opens one hub connection for the game server, with
the org token as the client's bearer. Its plain calls are the server's own, and `call_as`
makes a call for one of your players:

```python
hubs = await api.exec.connect_as_server(app_id)

await hubs.call("lobby", "", "report", {"players": len(roster)})  # the server's own call
await hubs.call_as(carry.user_id, "player", player_key, "grant", {"gold": 10})  # for a player
```

- **A hub sees `Caller::Org`**, with the player the server named or none. Crowded Kingdoms
  checks that the connection is your organization's. The player a call names is your
  organization's word, and a hub treats it as that.
- **`player` is the player's user id** (a carry result's `user_id`), as an `int` or a
  decimal string.
- **Limits.** A server connection has a budget of 2,000 calls a second, and an organization
  holds at most 256 server connections to one app on one execution host.
- **Hubs built with ckx-sdk before 0.15** answer such a call `Denied` until they are built
  again.
- **On any other connection `call_as` raises `CrowdyExecError` with `Denied`**, and nothing
  is sent.
- **The connection reconnects** with a fresh server connect token. `on_access_ended` fires
  when the org token is revoked or fleet sessions are switched off.

### Refusals carry a code

The four calls raise `CrowdyGraphQLError`. Branch on its `code` with these readers:

| `code` | Meaning | Reader |
| --- | --- | --- |
| `ORG_TOKEN_REQUIRED` | The bearer is not an org token | `org_token_required_of(err)` |
| `ORG_TOKEN_NOT_ALLOWED` | The request carried an `Origin` header | `err.code` |
| `FLEET_SESSION_LIMIT` | `open`: the organization holds as many sessions as it may | `fleet_session_limit_of(err)` |
| `NOT_FOUND` | `refresh`: the session expired, was closed, or was opened with another org token | `fleet_session_not_found_of(err)` |
| `PLATFORM_BUSY` | The platform could not start the call in time. Wait `retry_after_ms`, then call again | `platform_busy_of(err)` |
| `ORG_LEGAL_ACCEPTANCE_REQUIRED` | An admin has not accepted the current Org-Managed Players Addendum | `org_legal_acceptance_required_of(err)` |
| `FORBIDDEN` | Fleet sessions are not switched on for the organization, or the org token lacks the `fleet:session` scope | `err.code` |
| `SCOPE_MISSING` | The app is not in the org token's app list | `err.code` |

### Keep the tokens out of your logs

- **Never log the `token` of what `fleet_sessions.open`, `fleet_sessions.refresh`, or
  `exec.server_endpoint` answers.** The first two hold the fleet token, and the third holds
  a hub connect token. Their repr shows `token=<redacted>`, and the repr of a session and of
  a `FleetPlayerToken` shows no token.
- **The hub connect token travels in the WebSocket URL's query string**, as a player's does.
  It lasts one minute, and the connection asks for a new one at each reconnect. Keep that
  URL out of your logs too.
- **A replayed `FLEET_RELEASED` makes the session report a player released.** The frame is
  signed, so a copy that someone captured verifies when it is sent again. If the player is
  still on your server, carry them again.

### The session follows CrowdyCPP where CrowdyJS's differs

CrowdyPy's session is CrowdyCPP's `replication::FleetSession`, so five things differ from
`FleetSession` in CrowdyJS:

- **Ids are integers.** Game token ids and user ids are `int` in results, and every method
  that takes one also takes its decimal string.
- **The table has no limit of its own.** CrowdyJS ends a carry `tableFull` once its table
  holds `maxPlayers` and 32 more rows. Here the realtime server answers a carry past
  `max_players` with `session_full`, and a carry that is refused or times out leaves the
  table.
- **Actors get no automatic heartbeat.** CrowdyJS sends one every 2 seconds for each actor
  it has sent an update for. Here your server calls `send_heartbeat`.
- **The state is the connection's.** `fleet.state` is a `ConnState`, as on a player's
  connection, where CrowdyJS has `status`. The `session_released` and `move_ordered`
  handlers report what CrowdyJS's `released` status does.
- **The shortest silence timeout is 4,500 ms.** A shorter `silence_timeout_ms` that is not 0
  is raised to it. CrowdyCPP 0.94.0 holds the capabilities interval to a third of the
  timeout, and it never counts a silence while that interval is 1,000 ms or less.

The wire codecs are in `crowdypy.wire` for a server that builds the messages itself:
`fleet_carry_proof`, `encode_fleet_carry`, `encode_fleet_release`, `verify_fleet_frame`,
`parse_fleet_carry_result`, `parse_fleet_released`, and `parse_fleet_delivery`.

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
applied a second time; with `optimistic=False` the edit is applied only when that echo arrives,
and nothing is queued for write-back. A game with other addressing reads every edit from
`session.on("voxel_update", ...)`, which includes those echoes.

## Game Kit

`client.kit(app_id)` drives the ck-exec starter hubs (the server half is `ckx_sdk::kit`), with
the same modules as CrowdyJS's `client.kit(appId)`:

```python
kit = game.kit(app_id)
await kit.world.conditions.start()  # mirrored: time of day, weather, liveops modifiers
queue = kit.matchmaking.queue("ranked")
queue.on_matched(lambda session: print("matched into", session))
await queue.join(1200)
watching = asyncio.create_task(queue.watch())  # asks while the player waits
try:
    await kit.npcs.area_at(12, -3).attack(4)
except KitError as e:
    if e.code == "cooldown":
        ...
```

`kit.world`, `kit.matchmaking`, `kit.sessions`, `kit.npcs` and `kit.topology` (compose, check,
build and deploy the starters) share one exec connection (`kit.runtime`): it paces calls under the
gateway's 120 per 10 s, sends a call again only when that is safe (it did not run, or it carries
an `action_id` the hub answers once), and its mirrors (`HubState`, `CollectionMirror`,
`PrivateState`) take a new snapshot after a gap, another epoch or a reconnect. `kit.spectate`,
`kit.emotes` and `kit.shards` are the client conventions, and `kit.social` the parties, guilds and
chat rooms. The blocking client's kit is the same, with blocking calls.

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
Web Worker. A bound Construct game opens its repository draft instead of the mod crates when
you pass `draft_store=`, a factory returning the project's `crowdypy.studio.DraftStore`, such
as a `GitHubDraftStore` over `game.crowdy_studio_github`.

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
and the last live runs. The fleet session suite reads `CROWDY_FLEET_*` and has not run
against a deployment yet.

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
