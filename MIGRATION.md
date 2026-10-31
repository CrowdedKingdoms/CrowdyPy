# Migration notes

CrowdyPy is pre-1.0. Within a minor line, patch releases keep source compatibility; each
new minor may change the API. Read the section for every minor you skip.

## 0.23.0

CrowdyJS 18.24.0 and CrowdyCPP 0.75.0. Additive, but `users.me()`, `apps.app()` and
`apps.update()` now select the teen safety fields, so they need ck-api v2.60.0 on the tier
(an older API refuses the fields with a GraphQL validation error).

**The teen safety program (ck-api v2.60.0).** Every tier admits adults only until the
platform admits players from 13; the calls below work today and carry the rest.

- **Age and country.** `auth.register(..., birth_date=, country=, parental_confirmation=)`
  takes the answers of a neutral age screen (the date of birth and the country, no age shown,
  the parental line only for a player under 18). An account with no age
  (`users.me()["ageBracket"] == "unknown"`) gives it once with
  `auth.record_player_age(birth_date=, country=, parental_confirmation=)`; asking again is
  `CONFLICT`. Only the year and month are kept. `users.me()` reads `ageBracket` and `country`.
- **Documents.** `auth.player_legal_documents()` and `auth.app_required_documents(app_id)` are
  public; `auth.record_player_consents(accept_legal=True, app_id=..., method=...)` records the
  platform's and the app's documents (`attest_age_of_majority` is optional now), and
  `auth.player_legal_acceptance(app_id=...)` counts the app's. The call with both flags and
  nothing else sends the v2.35.0 document, so it still works against an older API. An app's
  admins set its own documents with `apps.set_required_documents(app_id, documents)`; the
  developer set is `auth.developer_legal_documents()`, `developer_legal_acceptance()` and
  `record_developer_consents(accept_developer_terms=True)`.
- **The gate's refusals,** in `crowdypy.errors` and on the package root, each with its
  `*_CODE`: `legal_acceptance_required_of` (its `missing_documents`), `age_required_of`,
  `age_requirement_not_met_of`, `territory_not_supported_of` (its `country`),
  `safety_log_required_of`, `parental_consent_unavailable_of`, `social_limit_reached_of`,
  `minecraft_verification_failed_of` (its `reason`) and `minecraft_verification_unavailable_of`.
  `age_requirement_not_met_of` is a boolean on purpose: it does not surface the `minimumAge` or
  `ageBracket` an older API sent, and a game must not show an age.
- **`client.safety`** (the app token, on the game client): `comm_settings`,
  `set_comm_settings`, `request_parental_consent`, `contacts`, `contact_requests`,
  `request_contact`, `accept_contact`, `remove_contact`, `blocks`, `block`, `unblock` and
  `file_report`. The platform enforces all of it; a player can only lower its scopes.
- **`client.moderation`** (also `client.admin.moderation`; `moderate_safety`, an identity
  session at the app's datacenter): `reports`, `resolve_report` and `escalate_report`. A
  moderator reads a reported session with `input_log.messages` on an app token.
- **The safety log.** `apps.recording(app_id)` (the app token works) says what is recorded, for
  the notice and the indicator. `apps.update` takes `replayLoggingRecordsVoice`,
  `replayLoggingRecordsVideo` and `minPlayerAge`; turning the log off in an app that admits
  players under 18 is `SAFETY_LOG_REQUIRED`.
- `platform.territory_policies()` (public) lists the countries the platform does not serve and
  the ones with a higher minimum age.
- **Org-managed players.** The organization's server passes `ageYears` and `country` to
  `mintManagedPlayerToken` (`crowdypy.inputs.MintManagedPlayerTokenInput`) or as the assertion's
  claims (`ManagedPlayerAssertionClaims`); both become required once players under 18 are
  admitted.
- `portal.minecraft_challenge(app_id)` and `portal.sign_in_with_minecraft(...)` serve Crowded
  Kingdoms' own apps only; any other app is `FORBIDDEN`.
- The operator fields (`setTerritoryPolicy`, `exportPlayerData`) are not wrapped.

**The Studio game editor (CrowdyJS 18.23.0).** A GitHub project the API classifies as a
Construct game (`CrowdyStudioGitHubLayout.kind == "CONSTRUCT_GAME"`) opens its repository
draft when the host passes `CrowdyStudioController(draft_store=factory)`, a factory that
returns the project's `crowdypy.studio.DraftStore`: `draft_store`, `when_editor_known`,
`refresh_repository_mode`, `reload_repository`, `open_repository_file`,
`update_repository_file`, `commit_repository`, `discard_repository_files`,
`resolve_repository_conflict`, the `repository_*` state fields and `REPOSITORY` file refs.
`crowdypy.studio.GitHubDraftStore` is the hosted backend, over `client.crowdy_studio_github`:
an overlay over the bound branch, published as one commit, which carries what does not
collide onto a moved branch and lists the rest as conflicts. It keeps the overlay in a
`DraftOverlayStorage` (`MemoryDraftOverlayStorage` by default; CrowdyJS's IndexedDB storage is
the browser's). Without a factory, or for a mod, nothing changes.
`client.crowdy_studio_github.get_files` reads up to 100 files at one commit and `commit_files`
writes up to 100 changes as one commit.

## 0.22.0

CrowdyJS 18.22.0 and CrowdyCPP 0.74.0. No breaking change, but `exec.instances` now selects
`orphaned`, so it needs Game API v2.58.0 on the tier (an older API refuses the field).

`ExecInstance.orphaned` is true when the instance's node type is not in the app's active
version. ck-exec 0.19 stops such an instance at once when a deploy or a rollback drops its
type, keeps its saved state for a version that deploys the type again, and drops the type
from `disabled_types`. It is `None` from a manager older than 0.19. The docstrings of
`deploy`, `activate_version`, `set_enabled` and `restart_type` say so.

## 0.21.0

CrowdyJS 18.21.0 and CrowdyCPP 0.73.0. No API change. An actor or voxel update still waiting
for its echo when the connection was reassigned (a move, `COMMAND_RECONNECT`, the watchdog)
had its echo sent to the session it left, and waited out its whole timeout. It is now resent
once, with a fresh sequence, as soon as the new session is connected. That is the send's one
retry, as the first-send retry is; audio, text and events are never resent.
`wait_for_sequence` is unchanged.

The Game API on the same tier refuses a gamertag or disambiguation with a control,
line-break or bidirectional-formatting character (`BAD_REQUEST`).

## 0.20.0

CrowdyJS 18.20.0 and CrowdyCPP 0.72.0. No API change. A replication server older than
v0.42.0 drops or refuses a session's first message while the session's permissions load, so
the first awaited send after `client.udp.connect()` or a reassignment (a move,
`COMMAND_RECONNECT`, the watchdog) used to fail or time out. Every `*_and_wait` now retries
the first awaited send of a session once, with a fresh sequence, when it is refused
`UNAUTHORIZED` or, for the actor and voxel updates the server echoes, when it times out
(`UDP_SEQUENCE_TIMEOUT`). Audio, text and events have no echo and succeed by timing out, so
their timeout is never retried. From v0.42.0 the server holds that first message until the
session is ready, so the retry does not fire; a warm-up send is no longer needed.

## 0.19.0

CrowdyJS 18.19.0 and CrowdyCPP 0.71.0. No API change. ck-api v2.56.0 makes a draining
instance easy to leave: it answers with the balancer's affinity cookie (`cks_ga`) expired,
so the next request is placed on an instance that is staying. `LbCookieStore` now forgets a
`cks_ga` that a response expires (an empty value, `Max-Age` of zero or less, or an `Expires`
in the past) instead of sending it again, and gains `clear()`, CrowdyJS's way of dropping
the affinity on a move.

## 0.18.0

CrowdyJS 18.18.0 and CrowdyCPP 0.70.0. No API change. ck-api v2.55.0 adds two app-level
permission keys, and the realtime servers enforce them:

- **`use_text_chat`** for proximity text (`client.udp.send_text_packet`, `World.send_text`,
  the replication connection's `send_text_packet`) and for text on a channel
  (`send_channel_message`, `send_ranged_channel_message`).
- **`use_direct_messages`** for `send_single_actor_message` and `World.send_to_actor`.

Without the key the server answers `UNAUTHORIZED` for the send's sequence number. Every
access tier that grants `access` has both: ck-api adds them when a tier is created or updated
(`client.app_access.create_tier` / `update_tier`) unless `applyCommunicationDefaults` is false,
and an operator backfilled the existing tiers. An app that wants some players without chat
removes a key from their tier.

Also from ck-api v2.54.0 and v2.55.0:

- `client.managed_players.delete` now erases the player's content within minutes; a legal hold
  on the player or its organization refuses it with `LEGAL_HOLD`.
- `client.apps.update(app_id, {"replayLoggingRecordsMedia": False})` leaves voice and video out
  of the app's recording (default true).
- The operator fields for account erasure and legal holds are not wrapped, as in CrowdyJS and
  CrowdyCPP: the SDK is for players, developers and org-admins.

## 0.17.0

CrowdyJS 18.17.0 and CrowdyCPP 0.69.0. Additive. ck-api v2.53.0 serves org-managed players on
dev, test and prod, and refuses an organization's managed sign-ins (both ways), refreshes and
provisioning with `ORG_LEGAL_ACCEPTANCE_REQUIRED` until an admin of the organization accepts
the Org-Managed Players Addendum in Studio.

- **`crowdypy.errors.org_legal_acceptance_required_of`** reads that refusal
  (`ORG_LEGAL_ACCEPTANCE_REQUIRED_CODE`). Neither the player nor the organization's server can
  fix it, so do not retry.
- **`client.managed_players.settings(org_id)`** also returns `termsVersion`, `termsUrl`,
  `termsAccepted`, `termsAcceptedVersion`, `termsAcceptedAt`, `termsAcceptedByUserId` and
  `viewerMayAcceptTerms`.
- **Fix:** `crowdypy.domains.portal.is_legal_acceptance_required_error` no longer matches the
  organization's code by its message: a managed player has no clickwrap to show.
- No wrapper for `acceptManagedPlayerTerms`, as in CrowdyJS and CrowdyCPP: it binds the
  organization, so a person accepts it in Studio.

## 0.16.0

CrowdyJS 18.16.0 and CrowdyCPP 0.68.0. Additive, and served on the dev tier only at the time
(every other tier answered `FEATURE_DISABLED` until ck-api v2.53.0): org-managed players, an
organization's own players that it signs in itself.

- **`client.portal.adopt_app_token(response)`** holds the app token the organization's server
  minted with its org token (`mintManagedPlayerToken`) and returns it as an
  `AppTokenResponse`; on a game client pass that to `client.set_app_token` so native UDP signs
  with it. It raises `ValueError` and holds nothing for a response without a token or with an
  org token (`cko_...`). There is no org-token mint wrapper, on purpose.
- **`client.portal.exchange_managed_player_assertion(app_id, assertion)`** trades an assertion
  the organization's sign-in service signed; public, and each assertion works once.
  `crowdypy.errors.assertion_rejected_of` reads the refusal's `reason`.
- **`client.managed_players`** (also `client.admin.managed_players`): `list`, `get`,
  `provision` (up to 500 a call), `update`, `end_sessions`, `delete`, `settings`,
  `signing_keys`, `register_signing_key` (a JWK with private members raises `ValueError` before
  any request; `publicJwk` may be a mapping), `revoke_signing_key`, `app_mods_enabled` and
  `set_app_mods`.
- `client.organizations.revoke_token(id, end_sessions=True)` also ends the sessions a token
  signed in. `crowdypy.errors.managed_player_not_allowed_of` reads `MANAGED_PLAYER_NOT_ALLOWED`.

## 0.15.0

CrowdyJS 18.15.0 and CrowdyCPP 0.67.4. Additive.

- **`chunks.set_voxel(..., optimistic=False)`**, CrowdyJS's `setVoxel({ optimistic: false })`:
  nothing changes locally and nothing is queued for write-back; the edit is applied when the
  server's echo of it arrives, so the cache holds only what the server accepted. The default
  (`True`) is the edit as before. The native core carries the flag since CrowdyCPP 0.67.4
  (`ChunkStore::setVoxel(..., optimistic)`).
- **`client.udp.send_generic_spatial(chunk, uuid, payload)`**: an app-defined spatial message
  (opcode 140) that the actors near `chunk` receive as `generic_spatial`; CrowdyJS 18.15.0's
  `udp.sendGenericSpatial`. The replication connection had `send_generic_spatial` already.

## 0.14.2

CrowdyJS 18.14.0 and CrowdyCPP 0.67.3. No breaking change. `PlayerWalletAPI.charges()` is
deprecated and warns (`DeprecationWarning`), as the `playerUsageCharges` field it wraps is: it
reads the cents-era hourly rows, which the lossless ledger does not write. Read
`transactions()`: each `usage_debit` entry is one usage charge. The schema's billing
descriptions are 18.14.0's. The vendored native core moves from CrowdyCPP 0.66.0 to 0.67.3;
the replication client it binds is unchanged.

## 0.14.1

CrowdyJS 18.13.0 and CrowdyCPP 0.66.0. Additive. A paused app, an app switched off, and a type switched off used to arrive as a bare denial. They are now `AppPaused`, `AppDisabled`, and `TypeDisabled`.

## 0.14.0

CrowdyJS 18.13.0 and CrowdyCPP 0.66.0. Additive: the economy, which lives in hub state and is
not a ledger (HS-52); the `player` and `market` starters need ckx-sdk 0.12.0.

- **`kit.player`** (`PlayerKit`), the caller's own hub, keyed by `player_id` (pass it to
  `client.kit()` or let the kit ask who the player is): `watch(*parts)`, `me`, `rules`,
  `profile_of`, `on_event`, and the stores `profile`, `inventory`, `wallet`, `progress`, filled
  as they are read. Its views: `kit.inventory` (`items`, `count`, `discard`, `take_mail`),
  `kit.wallet` (`balances`, `balance`), `kit.progression` (`on_level`, `profile_of`),
  `kit.achievements` (`list`, `on_unlock`), `kit.quests` (`list`, `accept`, `claim`, `step`),
  `kit.crafting` (`recipes`, `craft(recipe, times)`), `kit.loot` (`tables`, `open`),
  `kit.production` (`view`, `collect`).
- **Markets**: `kit.shops.at(key)` (`vendors`, `buy`, `sell`), `kit.market.at(key)` (`book`,
  `order(item=, side=, price=, count=)`, `cancel`, `orders` mirrored) and `kit.trades.at(key)`
  (`open`, `offer`, `accept`, `decline`, `mine` mirrored).
- **`KIT_CODES["UNAVAILABLE"]`**: a hub refuses a change it could not finish because another hub
  did not answer and keeps no receipt; the kit sends a call with an action id again.

## 0.13.0

CrowdyJS 18.12.0 and CrowdyCPP 0.65.0. Additive: a kit module per ck-exec genre starter
(ckx-sdk 0.11.0, which the tier's compute toolchain must carry; `kit.topology` lists, builds and
deploys them like the others). Against an app that does not run one, its calls are refused with
`NotFound` (the app has no such node type).

- **The modules.** `kit.leaderboards.board(name)` (`view` mirrored, `top(limit=, offset=)`,
  `around(player=, radius=)`, `me`, `season`; `submit`, `configure`, `roll` for developers),
  `kit.tables.table(key)` (`table` mirrored, `hand` private, `join`, `start`, `play(card, suit)`,
  `draw`, `pass_`), `kit.tactics.battle(key)` (`move(x, z)`, `attack`, `end_turn`, `my_units`),
  `kit.encounters.encounter(key)` (`build(x, z)`, `hit`), `kit.minigames.duel(key)` (`throw`,
  `rematch`), `kit.instances.run(key)` (`open(seed=, party=)`, `invite`, `join`, `enter`,
  `descend`, `abandon`), `kit.arenas.arena(key)` (`join(hero_class)`, `start`, `move(x, z)`,
  `cast(ability, x, z)`), `kit.racing.race(key)` (`pos`, `ghost`) and `kit.racing.ball(key)`
  (`claim`, `pass_`, `shoot`), `kit.territory.map(key)` (`join(faction)`, `reset`) and
  `kit.territory.zone(key)`. `pass` is a Python keyword, so it is `pass_`.
- **Node types** a game deployed under other names go in `nodes=`: `leaderboard`, `deck`,
  `tactics`, `director`, `minigame`, `instance`, `arena`, `race`, `ball`, `territory`, `zone`.
- **Positions the hub keeps.** `move` / `pos` answer `[x, z]`, the position the hub's movement
  warden kept, and go without an action id: the next report replaces a lost one.

## 0.12.0

CrowdyJS 18.11.0 and CrowdyCPP 0.64.0. Additive. The kit's typed clients drive the ck-exec
starter hubs, whose server half is `ckx_sdk::kit` (ckx-sdk 0.10.0): the tier's compute toolchain
must carry it (dev's v1.12.0 does) and the app must run the starters (`kit.topology.deploy`).

- **The modules.** `kit.world` (`conditions` and `nodes` mirrored; `state`, `harvest`, and
  `schedule` / `cancel` for developers), `kit.matchmaking.queue(name)` (`join(rating, party=...,
  leader=...)`, `leave`, `accept`, `decline`, `status`, `configure`, `place`, `watch` / `unwatch`,
  `on_matched`), `kit.sessions.open(key)` (`watch`, `join`, `ready`, `start_game`, `act`,
  `configure`, `current_turn`, `is_my_turn`, `state` mirrored), `kit.npcs.area(key)` /
  `area_at(x, z)` (`mobs`, `npcs`, `pets`, `attack`, `talk`, `adopt`, `rename`, `summon`,
  `dismiss`, `release`, `my_pets`), `kit.topology` (`pack`, `types`, `plan(select,
  overrides=...)`, `deploy`; `kit_manifest_problems`), `kit.spectate.observe(uuid, chunk)` (a
  heartbeat to `run()` beside the game), `kit.emotes` (`send`, `parse`: client event 101 with
  `{"emote", "targetUuid"}`) and `kit.shards`. Replies are the hub's msgpack as Python values:
  snake_case keys as the hub sends them, and a hub's integer-keyed maps (a session's `scores`) keep
  integer keys.
- **Refusals.** A hub's `kit:<code>:` refusal raises `KitError` (a `CrowdyExecError` with status
  `AppError`): branch on `code`, show `text`. `KitError.from_error` and `kit_error_code` read one
  out of any exec error.
- **The runtime.** `kit.runtime` (`KitRuntime`) is one exec connection for every module, opened
  on first use: as the player with an app token, or as a developer with `developer=True`. It paces
  what it sends to `call_budget` (100) a `budget_window_ms` (10 s), holds as long as a `Busy` "rate
  limited" refusal says, sends a call again when it did not run (`Busy`, `RateLimited`) or when it
  carries an action id and its outcome is unknown (`DeadlineExceeded`, `Unavailable`, `Moved`), at
  most `retries` (2) times, and refuses a 257th subscription with code `limit`. `call(...,
  action=True)` sends a fresh `action_id` the hub answers once.
- **Mirrors.** `HubState`, `CollectionMirror` (`list`, `member`), `PrivateState` (read with a
  caller-scoped call, again on a `changed` that names the player) and `WritableKitStore`. A mirror
  takes a new snapshot on another epoch, a gap in a collection's `seq`, and a reconnect. In the
  blocking client the resync a push or a reconnect starts runs on a thread of its own
  (`run_detached`), because those handlers run on the connection's reader thread.
- **Realtime.** `decode_kit_actor_state` and `kit_actor_lanes` for the actors hubs show,
  `KIT_EVENT_TYPES`, `parse_kit_server_event`; `crowdypy.kit.wire` adds events 99 (encounter), 100
  (possession) and 101 (emote) with their parsers and `EVENT_APP_FIRST` (1000).
- **Exec reconnects** wait half their backoff plus up to as much again at random, in both
  connections, so the players of a host that went away do not all dial at once.
- `tests/fixtures/kit-protocol.json` is ck-exec's golden record of the starters (by way of
  CrowdyJS); `tests/unit/test_kit_protocol.py` replays it through a gateway that answers only the
  next recorded step, with both clients. The parity gate reads CrowdyJS `src/kit/modules/` too.

## 0.11.0

CrowdyJS 18.10.0 and CrowdyCPP 0.63.0. Additive.

- **Access that ends ends for good.** ck-exec 0.16 closes a session with code `4403` (now
  `EXEC_CLOSE_ACCESS_ENDED`) and the reason when what it rests on ends: the player's access
  (suspended, revoked, an expired grant), their session (signed out, everywhere too, or the app's
  authorization withdrawn), or the account (blocked). `ExecConnection` and `AsyncExecConnection`
  treat it as final: `on_access_ended(listener)` is called once with the reason, the call in flight
  fails `Denied`, and the connection does not reconnect. Until 0.10.0 it redialed, and the redial
  was refused (HTTP 401 "access ended") after one retry. Tell the player; connect again only once
  they hold access again.
- **Channel and team bans.** `client.channels.ban(group_id, user_id, reason=None)` /
  `unban(group_id, user_id)` and the same on `client.teams`, with the group's `manage_members`
  permission (never the group's owner, never yourself). A banned user is removed, stops receiving
  the channel, and joining, requesting to join and being added are refused with
  `GROUP_MEMBER_BANNED` until the ban is lifted; leaving or being removed does not lift it.
- **Two refusal readers.** `account_blocked_of(error)` returns `CrowdyAccountBlocked(blocked_until)`
  for `ACCOUNT_BLOCKED` (a platform operator blocked the account: every request and sign-in is
  refused; `blocked_until` is set for a timed block), and `group_member_banned_of(error)` is `True`
  for `GROUP_MEMBER_BANNED`. Neither is fixed by retrying. `ACCOUNT_BLOCKED_CODE` and
  `GROUP_MEMBER_BANNED_CODE` name the codes.
- `blockAccount`, `unblockAccount` and `accountBlock` are super-admin operations and are not
  wrapped (`docs/parity-matrix.md`).

## 0.10.0

CrowdyJS 18.9.0 and CrowdyCPP 0.62.0. Additive, with one floor: every sessions read selects
`InputLogSession.missingRecords`, so `client.input_log.sessions` needs the Game API release after
v2.40.2 (an older one refuses the selection).

- **`missingRecords`** on every recorded session: inputs the replication server accepted that
  never reached the log (an overloaded server or a broker outage). 0 when the recording is
  complete as far as it is known; `None` on one recorded before the count existed. A lost end of
  the recording cannot be counted: that session closes as `unrecorded`.
- **End reasons.** `endReason` may also be `logging_off`, `shutdown`, `paused` (recording paused
  while the app's logging setting could not be confirmed; a later record reopens the session) or
  `unrecorded`; the `sessions` docstring says what each means.
- **Retrying a read.** `INPUT_LOG_TEMPORARILY_UNAVAILABLE` and `INPUT_LOG_RATE_LIMITED` carry
  `extensions["retryable"]`: retry with the same cursor after a short back-off, a bounded number
  of times. A malformed cursor, or one from another session, is `BAD_USER_INPUT`.
- **Stopping a read** (CrowdyJS's `signal`): cancel its task, or wrap it in `asyncio.timeout`. The
  request is closed, which frees its share of the server's reads (one per user, two per app and
  per org at a time); the client's own timeout applies either way. CrowdyCPP 0.62.0's
  `CancelToken` is the C++ form; CrowdyPy's GraphQL client is Python's, so it needs none.
- **`INPUT_LOG_FUNDS_NEEDED`** on `client.apps.update(app_id, {"replayLoggingEnabled": True})` carries
  `extensions["requiredMicrousd"]` and `["spendableMicrousd"]`: the balance turning logging on
  needs and what the org wallet can spend.
- The vendored CrowdyCPP adds `Errc.Cancelled` beside the codes the replication layer reports;
  nothing in the native client returns it.

## 0.9.1

A patch release with the same API as 0.9.0, the same CrowdyJS (18.8.0) and CrowdyCPP (0.61.0) pins.

- **`client.input_log` sends a session filter's `userId` as a string**, the decimal string every
  id is on the wire. An `int` used to go out as a JSON number.
- **Its docstrings say when a read is retryable.** From the Game API release after v2.40.2,
  `InputLogAPI.messages` answers `INPUT_LOG_TEMPORARILY_UNAVAILABLE` when the log cannot be read
  right now and `INPUT_LOG_RATE_LIMITED` while another read of yours is running; both carry
  `extensions.retryable`, so retry with the same cursor after a short back-off.

## 0.9.0

CrowdyJS 18.8.0 and CrowdyCPP 0.61.0. Additive, with one floor: the build documents select
`ExecBuild.sdkVersion`, so `exec.build`, `build_status`, `wait_for_build`, `mod_build`,
`mod_client_build`, `mod_build_status` and `wait_for_mod_build` need ck-api v2.40.2 (an older
one refuses the selection).

- **`ExecBuild.sdk_version`**: the version of `ckx-sdk` the platform compiled the build against
  (`crowdy-client-sdk` for a CLIENT half), from the toolchain of the API instance that ran it. The
  platform points a crate at its own copy of the SDK whatever version the crate names, so this,
  not your manifest, says what your code was checked against. `None` until the build starts.

## 0.8.0

CrowdyJS 18.7.0 and CrowdyCPP 0.60.0. Additive, with reads that change for values they used to
get wrong (wide voxels) and one new floor: every token mutation (`portal.mint_app_token`,
`exchange_code`, `refresh`) selects `runtimeGate`, so 0.8.0 needs the ck-api release after
v2.39.0 that serves it; an older one refuses the selection. Channel audio and the echo of your
own voxel edits need replication server v0.37.0.

- **Voice helpers** (`crowdypy.media`), an optional convention for what an audio payload
  carries: a 10-byte little-endian header in front of each codec frame (version 1, codec 0 raw,
  1 Opus 48 kHz mono or 2 G.711 µ-law 8 kHz, `u16` seq, `u32` timestamp in codec samples,
  the frame's duration in ms, and the talk-spurt flags `VoiceFlag.SPURT_START` and
  `SPURT_END`).
  - `encode_voice_header(VoiceHeader(...))` and `encode_voice_packet(header, frame)` write it;
    `decode_voice_packet(packet)` returns `None` for a packet shorter than 10 bytes or of
    another version, and never raises on bytes from the network.
  - `VoicePacketizer(codec, frame_ms, *, seq=0, timestamp=0)` numbers one sender's frames
    across the seq and timestamp wraps (`packetize(frame, last=)`, `skip(frames)` for
    silence).
  - `VoiceJitterBuffer(target_delay_ms=60, max_frames=64, reset_after_ms=200)`:
    `push(key, packet, now_ms)` answers `"buffered"`, `"late"`, `"duplicate"` or
    `"malformed"`, and `pull(key, now_ms)` / `poll(now_ms)` return the due `VoicePlayout`s in
    seq order, a gap (`frame` `None`) for each frame that never came.
  - CrowdyCPP's implementation runs natively. Arguments are checked as CrowdyJS checks them, so
    an option or field out of range raises `ValueError` where CrowdyCPP would clamp it.
  - The helpers replay CrowdyJS's `voice-frames.json` byte for byte.
  - No codec ships, and the wheel links none: which codec an app uses, and the library that
    encodes it, is the app's choice.
- **Channel audio** (party and guild voice).
  - `client.udp.send_channel_audio(channel_id, uuid, payload)` sends message type 35, the
    channel message's layout and signing with its own type byte, at most 1,024 bytes. It needs
    the channel's `send_voice` and the player's `use_voice_chat`; without them the server
    answers `UNAUTHORIZED` for the returned sequence. The sender gets no echo.
  - Members receive message type 36, standalone or bundled, on the new `channel_audio` handler
    of `udp.subscribe` (`notification.channel_id`, the sender's `uuid`, `payload`).
  - In the World Stores it is `session.on("channel_audio", ...)`; the channel inbox does not
    get it. Key a jitter buffer by channel and sender, so one player in two channels is two
    streams.
  - `channels.create`, `grids.create_channel` and `GridScope.channels.create` take
    `members_can_speak` (default false), which gives the member role `send_voice`.
    `GridScope.channels.send_audio(channel_id, uuid, payload)` sends on the grid's channels.
  - Underneath are `ReplicationConnection.send_channel_audio`, and
    `crowdypy.wire.encode_channel_audio` and `ChannelAudioNotification` (from `parse_datagram`)
    for a custom transport.
- **App-defined spatial messages** (`GENERIC_SPATIAL_1`, 140) reach
  `session.on("generic_spatial", ...)` while a World Stores session is attached
  (CrowdyCPP's `onGenericSpatial`). Without a session, `udp.subscribe`'s `generic_spatial`
  already received them.
- **`session.on("voxel_update", ...)`** fires for every inbound voxel edit after
  `session.chunks` merged it, with its sender, voxel and state: CrowdyCPP's `onVoxel`, for a
  game that keeps its own world. It includes the echo of your own edits; compare the uuid and
  sequence with your send's.
- **`ChunkStore` keeps wide voxel types and other addresses.** Positions and types are the app's
  signed 16-bit values, which the platform does not check.
  - An edit the 16x16x16 one-byte grid cannot hold (a type outside 0-255, a position outside
    0-15) is kept whole in the chunk's overlay. That covers a realtime update, a hydrated
    `voxelStates` entry and a local `set_voxel`.
  - `ChunkStore.overlay(chunk)` returns the overlay as `ChunkOverlayVoxel(x, y, z,
    voxel_type, state)` values keyed by `voxel_key(x, y, z)` (`"x:y:z"`), and `voxel_type_at`
    and `voxel_state_at` read it first.
  - What changes: hydration no longer stores a type outside 0-255 truncated (300 used to read
    as 44), and keeps an entry outside 0-15 instead of skipping it. `voxels()` holds 0 under
    an in-grid voxel whose type is in the overlay.
  - `set_voxel` keeps a position outside 0-15 in the overlay and sends it, where it used to be
    refused. It raises `ValueError`, before anything changes or is sent, for a position or type
    outside 16 bits or a state over 1,024 bytes (`crowdypy.wire.assert_voxel_edit`,
    `VOXEL_STATE_MAX_BYTES`).
  - `udp.send_voxel_update` and `ReplicationConnection.send_voxel_update` refuse a state over
    1,024 bytes (`CrowdyReplicationError`, `InvalidArgument`); the server refuses one with
    `INVALID_REQUEST`.
- **The echo of your own voxel edit.** Replication server v0.37.0 delivers every accepted edit
  back to its sender. `ChunkStore` matches the echo to the `set_voxel` that sent it (sender,
  sequence, voxel) and does not apply it again, so a local edit fires `on_chunk_changed` once.
  The echo is applied only when another client's edit of the voxel arrived in between (the
  server ordered yours last).
- **Hydration goes through the native store.** `ChunkStore.hydrate()` hands each loaded chunk
  to CrowdyCPP's `ChunkStore`, which puts the entries the grid cannot hold in the overlay. An
  entry without a state now clears the voxel's state, also one a local `set_voxel` gave it (0.5.1
  kept that until the next realtime update). `voxel_state_at` reads the native cache only, and
  `prune_beyond` keeps a dirty chunk's states and overlay as well as its grid.
- **UDP error 33, `APP_PAUSED`** (`crowdypy.wire.ErrorCode.APP_PAUSED`): the replication
  server refuses a paused app's sends.
- **Pause and access refusals.**
  - `crowdypy.is_app_paused(gate)` reads `AppTokenResponse.runtime_gate` (`AppRuntimeGate`,
    `status` and `reason`) and the `runtimeGate` of `server_status.game_client_bootstrap`.
    Anything but `ACTIVE` is paused. A paused app still mints, so check before entering the
    world.
  - `crowdypy.app_paused_of(error)` reads `APP_PAUSED` (`reason`).
  - `crowdypy.access_refusal_of(error)` reads `ACCESS_REVOKED`, `ACCESS_SUSPENDED`
    (`suspended_until`) or `ACCESS_NOT_GRANTED`.
  - `crowdypy.actor_exists_of(error)` reads `ACTOR_EXISTS` (`owned_by_caller`).
  - Each reader takes a raised `CrowdyGraphQLError` or one raw error entry. The `*_CODE`
    constants name the codes.
- **New API wraps.**
  - `users.player_profile(user_id)` and `users.player_profiles(user_ids)` return the public
    `userId`, `gamertag` and `disambiguation`. `player_profiles` takes at most
    `PLAYER_PROFILES_MAX` (100) ids, raises `ValueError` above that, and sends nothing for
    none. `users.get` documents that another user's private fields come back `None`.
  - `app_access.suspend(app_id, user_id, until, idempotency_key=None)` takes a timezone-aware
    `datetime` or an ISO-8601 string. With it come `unsuspend` and
    `resync_tier_grid_permissions` (`manage_access_tiers`), and `suspendedUntil` on every
    access record.
  - `exec.restart_type(app_id, node_type)` returns `ExecRestartResult(node_type, stopped)`
    (`manage_compute`). `ExecAppStatus` gains `budget_pause_reason`, `max_instances`,
    `max_reserved_mb`, `instance_limit`, `instances` and `reserved_mb`.
  - `apps.get` and `apps.update` carry `claimOwnerKeys`, chunk reads `voxelStatesTruncated`,
    and `game_client_bootstrap` `runtimeGate` and `wildernessWritesOpen`.
- The keyframe and heartbeat defaults stay. Idle players on different servers meet at the first
  full update after joining (replication server v0.37.0).

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
  Crowdy Games was retired on 2026-10-29. Its shell bridge and bundle hosting were
  browser-only, no Python wrapper is planned, and developers now host their games on their
  own origins.

Not in 0.1.0, and listed as portable gaps in `docs/parity-matrix.md` until they landed:
the native replication connection, video frames and traffic metrics (0.2.0); the World
Stores, the ck-exec gateway connection, the Game Kit and the subscriptions client (0.3.0);
the headless Studio, and the roots CrowdyCPP covers beyond CrowdyJS (0.4.0).
