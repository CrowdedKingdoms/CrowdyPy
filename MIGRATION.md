# Migration notes

CrowdyPy is pre-1.0. Within a minor line, patch releases keep source compatibility; each
new minor may change the API. Read the section for every minor you skip.

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
