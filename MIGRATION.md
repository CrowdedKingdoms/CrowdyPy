# Migration notes

CrowdyPy is pre-1.0. Within a minor line, patch releases keep source compatibility; each
new minor may change the API. Read the section for every minor you skip.

## 0.7.0

CrowdyJS 18.6.0 and CrowdyCPP 0.59.0. Additive.

- **The input log.** An app with replay logging on has every client input the realtime
  servers accept recorded. `client.input_log.sessions(app_id, *, first=, after=, filter=)`
  lists the app's recorded sessions, newest first (`edges`, `pageInfo`, `totalCount`; a
  session is one game token's inputs), and `client.input_log.messages(app_id, game_token_id,
  *, first=, after=, filter=)` reads one session's inputs, oldest first. `body` is the client
  message in base64 without its authentication tail (`decode_base64` gives the bytes), and
  `sizeBytes` is what stored input logs are billed on. Keep paging while
  `pageInfo.hasNextPage` is true: a page can be short, or empty, when it reached the server's
  time or scan limit. Both are game plane (the app-scoped client). A player reads their own
  sessions, and a holder of `manage_apps` reads every one; without it, listing another
  user's sessions is `FORBIDDEN` and reading another user's session is `NOT_FOUND`. Both
  answer `INPUT_LOG_UNAVAILABLE` on a deployment without input logging, and `messages` also
  answers it, retryable with the same cursor, when the log cannot be read right now.
- **`replayLoggingEnabled`** is selected on every app read. Turning it on with
  `client.apps.update` is refused with `INPUT_LOG_FUNDS_NEEDED` unless the org's wallet has a
  spendable balance or the org is exempt from billing.

## 0.6.0

CrowdyJS 18.5.0 and CrowdyCPP 0.58.0. Additive.

- **Distance-limited channel messages.**
  `client.udp.send_ranged_channel_message(channel_id, uuid, payload, *, chunk, max_distance)`
  publishes to a channel but reaches only the members whose own actor is within
  `max_distance` chunks of `chunk`. Distance is the straight line between chunk coordinates,
  inclusive: 0 reaches only the origin chunk, and 5 reaches a member 3 chunks east and 4
  north but not one 4 east and 4 north (about 5.66). It takes the same right as
  `send_channel_message`, and members receive the ordinary `channel_message`, so receivers
  change nothing. A member with no actor of its own is not reached, nor is the sender.
  `max_distance` is 0 to `crowdypy.wire.CHANNEL_RANGED_MAX_DISTANCE` (2^31 - 1); the origin's
  app is the connection's. Underneath it is
  `ReplicationConnection.send_ranged_channel_message(channel_id, uuid, payload, chunk,
  max_distance)`, and `crowdypy.wire.encode_ranged_channel_message` encodes the
  CHANNEL_MESSAGE_RANGED_REQUEST (32) for a custom transport. It needs a replication server
  that serves message type 32; one that does not delivers nothing.
- **The terms and age gate** (CrowdyJS 18.4.0). Since ck-api v2.35.0 no gameplay token
  (`portal.mint_app_token`, `portal.create_authorization_code`, `portal.refresh`) is issued
  until the player has agreed to the current required legal documents and attested that they
  are at least 18, or the age of majority where they live if higher; the refusal is
  `LEGAL_ACCEPTANCE_REQUIRED`, which `crowdypy.is_legal_acceptance_required_error` reads.
  Show your own two checkboxes, linking each document, then call
  `client.auth.record_player_consents(accept_legal=True, attest_age_of_majority=True)` before
  minting; `client.auth.player_legal_acceptance()` says whether that is still needed.
  `client.auth.register` takes `accept_legal` and `attest_age_of_majority`, and an account
  registered with both `True` starts accepted. Record them only for a player who ticked both
  boxes: they are the player's agreement. Against an older API the two new calls fail as
  GraphQL validation errors.

## 0.5.1

CrowdyJS 18.2.0 and CrowdyCPP 0.56.0. No API change; one behaviour fix.

- **Chunk loads keep recorded voxel edits** (OI-2026-10-02-006). Since ck-api v2.33.0 every
  voxel edit recorded for a chunk (a hub's or mod's `world.set_voxels`, `updateVoxel`, a
  realtime voxel update) arrives only in the chunk's `voxelStates`, never in its `voxels`.
  `ChunkStore.hydrate()` now applies each entry's voxel type at its voxel, also on a chunk
  stored with `voxels: null`, and an entry without a state clears the hydrated state there.
  `ensure_around()` no longer reloads a chunk it already holds when a bulk load around a new
  center returns it again; prune the chunk to load it afresh. Turn on
  `hydrate_voxel_states` to see hub and mod edits after a reload.
- A state a local `set_voxel` gave a voxel is not cleared by a later hydration that says the
  voxel has none: the native core keeps it until the next realtime update there.

## 0.5.0

CrowdyJS 18.1.0 and CrowdyCPP 0.55.0. Additive, plus one refusal that is a security fix.

- **The ck-exec connect token goes only to a gateway on the estate.** `client.exec.connect`
  and `connect_as_developer` dial a gateway only when `exec_gateway_refusal` (in
  `crowdypy.domains.exec`) passes it. That means `ws` or `wss` without credentials (only
  `wss` under an `https` game API), on the estate of the game API or of this release's default
  origin (two IP addresses only when equal), or a loopback gateway for a loopback game API.
  Any other gateway the game API names is refused before the token leaves the process, with
  `CrowdyExecError("Unavailable", "refusing the gateway …")`. `AsyncExecConnection.open` and
  `ExecConnection.open` still dial what they are given. The rule answers the 19 cases CrowdyJS
  and CrowdyCPP check (`exec-gateway-cases.json`).
- **A refused upgrade carries the gateway's answer.** HTTP `401` (a token it will not take) is
  `Denied` and anything else `Unavailable`, each with the gateway's reason, as in "the
  gateway refused the connection (HTTP 401: bad signature)". Both used to be `Unavailable`
  "connecting … failed".
- **Open grids:** `GameAppsAPI.open_permissions(app_id, grid_id)` and
  `set_open_permissions(input)` (`manage_apps`) cover the keys a grid grants every player with
  access to the app. An empty list closes it.
- CrowdyCPP 0.55.0 also builds on Windows when `<windows.h>` comes first (its `far` and
  `near` macros broke a session header).
- New live suite `tests/e2e/test_e2e_open_grid_exec_gateway.py`, which runs as a throwaway
  org admin with `CROWDY_E2E_THROWAWAY_OWNER=1`.

## 0.4.1

Documentation only; nothing the package does changed. Still CrowdyJS 18.0.4 and CrowdyCPP
0.54.0.

- The README, which PyPI shows, carries the current tier pins and an example of installing a
  wheel straight from a GitHub release.
- The roots CrowdyPy wraps beyond CrowdyJS are described precisely: CrowdyJS's portable API
  sends none of them, CrowdyCPP covers them, and CrowdyJS's in-browser agent does send the
  two provider-consent roots and the model-usage root.
- 0.4.1 is the first version PyPI receives from the release workflow, by trusted publishing.

## 0.4.0

Headless Crowdy Studio and full CrowdyJS parity. Additive; still CrowdyJS 18.0.4 and
CrowdyCPP 0.54.0.

- **`crowdypy.studio`.**
  - `CrowdyStudioController` is CrowdyJS's controller on asyncio, and
    `CrowdyStudioController.for_client(client, app_id=, grid_id=)` builds one over a
    client.
  - Its state is an immutable `CrowdyStudioState`, replaced on every change; `subscribe`
    to follow it. Autosave, retries and the polled surfaces are timers on the running
    loop.
  - A CLIENT target runs in the `broker_factory` you pass: a host for the attached CLIENT
    half. Without one, a CLIENT run fails with a message saying so.
  - A cancelled agent operation raises `CrowdyStudioError`, the module's error class.
- **Studio helpers.**
  - `StudioLayoutController` persists CrowdyJS's layout JSON.
  - `parse_rustc_diagnostics` parses build logs.
  - `create_crowdy_studio_starter_project`, `github_new_repository_url` and
    `parse_client_tick_interval_ms` port their CrowdyJS namesakes.
  - `canonical_json`, `digest_canonical_json` and `project_content_hash` compute the
    approval digests CrowdyJS computes.
- **The roots CrowdyCPP covers and CrowdyJS's portable API leaves out:**
  - `AuthAPI.confirm_email` and `resend_confirmation_email`;
  - `UsageAPI.org_summary`, `app_projection` and `org_projection`;
  - `AppsAPI.compute_budget`, `set_compute_budget` and `clear_compute_budget`;
  - `MarketplaceAPI.app_listing_versions`;
  - CrowdyCPP's narrow `CrowdyStudioAPI` mutations: `save_project_metadata` (fields
    left `UNCHANGED` keep their value), `save_project_files`, `set_project_archived`,
    `set_personal_library_file_archived` and `publish_common_file`;
  - the new `client.crowdy_studio_agent` (`CrowdyStudioAgentAPI`): provider consent,
    model usage and the agent policy.
- **`crowdypy.player_host`.** The typed observation contract a game implements so tooling
  can read the player it controls:
  - `PlayerHostAdapterV1` and the observation, capability and command types;
  - CrowdyJS's schemas for them, generated verbatim from CrowdyJS;
  - `validate_json_schema_value` and `assert_bounded_json_schema`, which give CrowdyJS's
    verdict on every value;
  - the agent error vocabulary (`CrowdyAgentError`, with CrowdyJS's message redaction).
- **The parity gate is strict.** A portable gap fails the build.

## 0.3.0

The World Stores, the Game Kit, the ck-exec gateway and GraphQL subscriptions. Additive,
except for one fix to the blocking client (the last item). Still CrowdyJS 18.0.4 and
CrowdyCPP 0.54.0.

- **World Stores** (`crowdypy.stores`, `create_world_session(game, app_id)` over a
  connected `game.udp`) bind CrowdyCPP's `WorldSession`. `session.tick()` applies every
  notification since the last tick to the stores natively, runs your actor's send loop and
  reaping, and then fires your callbacks. The stores are:
  - `self` (`LocalActorStore`: join, set and patch state, send-on-change with keyframes and
    heartbeats, `last_ack` / `last_error` / `status`);
  - `actors` (`RemoteActorStore`: staleness, sample history, `on_join` / `on_leave` /
    `on_update`, native lanes filtered by `state[offset] & mask == value`, and
    `snapshot()` columns);
  - `chunks` (`ChunkStore`: real-time voxel merge, optimistic `set_voxel`, `ensure_around`
    / `hydrate` from the Game API, write-back with CrowdyJS's refuse-or-retry rules, and an
    `on_missing` worldgen hook);
  - `errors`, `direct_inbox`, `channel_inbox` and `events`.

  `HostTracker`, `SaveStateStore` and `AvatarStateStore` make their GraphQL calls from the
  session's timers. Nothing a tick does waits on the network.
- **Codecs** (`crowdypy.codecs`): `json_codec`, `raw_codec`, `text_codec`, and
  `struct_codec`, which declares a fixed layout and decodes a whole lane's states at once
  into a numpy structured array (`codec.decode_many(snapshot.state_offsets,
  snapshot.state_data)`).
- **`client.world(app_id).actor()`** (`WorldClient` / `ActorClient`): an actor that
  remembers its chunk after `join`, over `client.udp`.
- **The Game Kit** (`crowdypy.kit`, `client.kit(app_id)`):
  - parties, guilds and chat rooms (`kit.social`);
  - the 48-byte engine pose (`encode_engine_pose`, `engine_pose_codec`), and
    `engine_lanes()` as native lane filters;
  - the engine event parsers;
  - `run_optimistic_action`.
- **The ck-exec gateway** (`crowdypy.exec_gateway`): `client.exec.connect(app_id)` and
  `connect_as_developer` return a connection with `call` (msgpack), `call_raw`,
  `subscribe`, `ping` and `on_reconnect`.
  - It redials with backoff and renews its subscriptions, and follows a `Moved` reply to
    the new host. The frames match the platform's shared fixture.
  - `AsyncExecConnection` runs in asyncio; `ExecConnection` is the blocking form.
- **`GraphQLSubscriptions.for_client(client)`**: `graphql-transport-ws` subscriptions
  authenticated as CrowdyJS's are.
- **Fix: the blocking client raises and returns the async client's classes.** Before,
  every class in a generated module was a copy, so `except
  crowdypy.PortalConsentRequiredError` did not catch what `crowdypy.sync.CrowdyClient`
  raised. A class with no async code (errors, result structs, records) is now one class in
  both clients.

## 0.2.0

Native UDP replication: CrowdyCPP's connection, now at CrowdyCPP 0.54.0, bound for Python.
Additive; still CrowdyJS 18.0.4.

- **`client.udp`** has CrowdyJS's UdpAPI methods (`connect`, `disconnect`,
  `connection_status`, every `send_*` and `send_*_and_wait`, `send_video_frame`,
  `flush_sends`, `subscribe`), sent as signed datagrams straight to the replication
  server the Game API assigns rather than through the GraphQL UDP proxy. Defaults per
  message type are CrowdyJS's (actor updates: 8 chunks, exponential decay; audio and video:
  1 chunk). Payloads are `bytes`, not base64, and sends return the sequence number.
- **`connect(app_token)`** takes the `AppTokenResponse` from `portal.mint_app_token`; the
  native client needs its token id and expiry as well as the token. `client.set_app_token(
  minted)` installs one on a game client (the bearer and `client.app_token`), so
  `connect()` can then be called without it.
- **`*_and_wait`** resolves on the send's echo (only actor and voxel updates echo) and, as in
  CrowdyJS, raises `CrowdyRealtimeError` with `code` set to the server's error name
  (`UNAUTHORIZED`, ...) or to `UDP_SEQUENCE_TIMEOUT`.
- **The connection lifecycle runs natively.** Assignment and token refresh are GraphQL
  calls the native side asks Python to make. A proactive refresh names the current server,
  so the socket is kept when the Game API installs the new token there. A failed
  assignment tries endpoint re-discovery once. `move_to_datacenter` re-assigns the UDP
  session, and `refresh_gameplay_token()` with a live connection quiesces it and resumes it
  under the new token. `aclose()` also closes the connection.
- **`crowdypy.replication`** is the layer underneath, for games that drive it themselves:
  - `AsyncReplicationConnection`, and `ReplicationConnection` for code without an event
    loop (`poll()` / `wait()`), both with a `SessionProvider` of your own.
  - `NotificationBatch`: the events one poll drained, as zero-copy columns (numpy arrays
    when numpy is installed, `memoryview` otherwise), or `Notification` objects one at a
    time.
  - `send_actor_updates` / `send_spatial_batch`: many entities in one call, with the GIL
    released once.
  - `stats()`, and `manual_pump=True` with `pump()`.
- **`crowdypy.media`**: `fragment_frame`, `VideoFrameAssembler` (including `ingest_batch`
  for a whole batch at once), `parse_video_fragment_header`, `is_newer_frame_id`.
- **`GridScope.send.*` and `GridScope.channels.send`** check the grid box and send on
  `client.udp`.
- `benchmarks/` measures it against CrowdyCPP on the same machine. A batch of 200 entity
  updates costs 1.06x CrowdyCPP's `sendActorUpdate` per entity, and Python receives two
  million verified notifications a second in batches. See `benchmarks/README.md`.

## 0.1.0

The first release. CrowdyPy follows CrowdyJS 18.0.4 (the pinned commit in
`pyproject.toml`) the way CrowdyCPP does, and runs its native core on CrowdyCPP 0.53.0.

- **The client.** `AsyncCrowdyClient` (asyncio) and its generated blocking twin
  `CrowdyClient`, one origin, the two-token model, the full CrowdyJS error hierarchy
  (`CrowdyGraphQLError.code`, `CrowdyAppUnavailableError`, `CrowdyUserCodeFaultError`
  with `player_fault_of`), the `WRONG_DATACENTER` move with one retry, the sticky
  load-balancer cookie and endpoint re-discovery (`discovery_url`).
- **Names.** Every CrowdyJS method is the snake_case of its name (`mintAppToken` ->
  `mint_app_token`, `listConnection` -> `list_connection`); domains likewise
  (`client.appAccess` -> `client.app_access`). `register` and `delete` keep their names.
- **Big integers** are decimal strings on the wire, as everywhere on the platform; methods
  accept `int` or `str` for a `BigInt` argument and send the string.
- **Typed results where they carry credentials.** `auth.login` returns `AuthResponse` and
  `portal.mint_app_token` / `refresh` / `exchange_code` return `AppTokenResponse`; their
  reprs never show the token. Other results are plain `dict`s, decoded with msgspec.
- **Inputs** are generated msgspec structs (`crowdypy.inputs`); a plain mapping keyed by the
  GraphQL field names works too.
- **The wire codec** (`crowdypy.wire`) is CrowdyCPP's, byte-identical to CrowdyJS's
  serializer on the shared fixtures.
- **Grid scope.** `client.grid(app_id, grid_id)` returns a `GridScope`: `mint_token()` (which
  learns the grid box), `contains()` / `assert_contains()` (raising `GridScopeError`) and the
  grid's channels. Its replication sends (`send.*`, `channels.send`) arrive with the
  connection in 0.2.0.
- **A response without its root field** raises `CrowdyProtocolError`, where CrowdyJS hands
  back `undefined`.
- **Moves stay in the estate.** A `WRONG_DATACENTER` move or a re-discovery answer is
  followed only to a host in the current endpoint's estate (`crowdypy.estate`), since the
  bearer token follows the move; anything else is refused and the error surfaces. CrowdyCPP
  bounds moves the same way; CrowdyJS bounds only its relay reconnects.
- **Licenses.** The wheels link OpenSSL statically, so the distribution is
  `MIT AND Apache-2.0`; every license ships in the wheel's metadata.
- **Not in a non-browser SDK** (as in CrowdyCPP): hosted browser sign-in navigation
  (`portal.signIn`, `handleSignInCallback`, `handleAuthorizeRequest`), the Crowdy Games
  shell bridge and bundle hosting, the Studio browser UI, the in-browser agent pane and the
  Web Worker player runtime. `docs/parity-matrix.md` lists every exclusion with its reason.

Not in 0.1.0, and listed as portable gaps in `docs/parity-matrix.md` until they landed:
the native replication connection, video frames and traffic metrics (0.2.0); the World
Stores, the ck-exec gateway connection, the Game Kit and the subscriptions client (0.3.0);
the headless Studio, and the roots CrowdyCPP covers beyond CrowdyJS (0.4.0).
