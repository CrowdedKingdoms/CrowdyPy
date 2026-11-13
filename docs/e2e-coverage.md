# CrowdyPy e2e coverage

The suites in [`tests/e2e/`](../tests/e2e/) are black-box: they drive a live deployment
through the public API the way an integrator does (owner sign-in, an access tier, `grant`),
and replication runs over CrowdyPy's native UDP client to the real replication servers.
Unconfigured, every suite skips, so `pytest` in CI records skips, not live passes.

## Running

| Variable | Required | Purpose |
|---|---|---|
| `CROWDY_E2E_API_URL` | yes | the API's entry origin (`https://ck.<tier>.crowdedkingdoms.com`) |
| `CROWDY_E2E_EMAIL` | yes | base address; players are plus-addressed per run and register fresh |
| `CROWDY_E2E_APP_ID` | yes | the app under test |
| `CROWDY_E2E_OWNER_EMAIL` | yes | an account with `manage_apps` and `manage_access_tiers` on the app |
| `CROWDY_E2E_OWNER_PASSWORD` | deployed tiers | signs the owner in; unset, a fresh derived owner registers. On a tier that asks an org admin for the developer documents (dev since 2026-11-02, when it moved to the `player` document set and players from 13), the owner must have accepted them (`auth.developer_legal_acceptance()`), or every `grant` the harness makes is refused |
| `CROWDY_E2E_HTTP_URL` | no | the game API origin, when not the minted `game_api_url` |
| `CROWDY_E2E_STUDIO_GRID_ID` | no | a grid in the app that the owner controls (Studio suite). On dev, app `96701660793088`'s is `105350185270272` (chunks 900,0,900 to 901,1,901, made by the org admin with `createGrid` on 2026-10-25) |
| `CROWDY_E2E_SLOW=1` | no | long-running suites |
| `CROWDY_E2E_THROWAWAY_OWNER=1` | no | the open-grid suite registers a throwaway owner, org and app (archived afterwards), at `crowdy-e2e-py-<kind>-<id>@test.invalid` addresses and with the provisioning token when one is set |
| `CROWDY_E2E_ORG_TOKEN`, `CROWDY_E2E_MANAGED_APP_ID` | no | the teen safety suite's org-managed players: an org token with `players:mint` and `players:manage` and one of its organization's apps, whose organization has accepted the Org-Managed Players Addendum. On dev, org `107209212824576` and app `107209219611648` (the 13+ test org); the token is the operator's, kept outside the repository. Unset, those scenarios skip |
| `CROWDY_E2E_PLAYER_EMAIL`, `CROWDY_E2E_PLAYER_PASSWORD` | no | the teen safety suite's existing-account scenario: an existing player outside the app's organization (it reads `hasSafetyModerator` as `None` and lists its own input-log sessions); it also needs `CROWDY_E2E_OWNER_PASSWORD`, and registers nobody. CrowdyCPP's suite reads the same pair. Unset, it skips |
| `CROWDY_E2E_UNDER13_SIGNUP=1` | no | the teen safety suite also tries a sign-up under 13 and expects `AGE_REQUIREMENT_NOT_MET`. Since ck-api v2.61.0 that refusal stops every sign-up and `record_player_age` from the same address and user agent for 24 hours, so never set it on a machine others register from (the builder) |
| `CROWDY_E2E_PROVISIONING_TOKEN` | dev and test | dev and test are staff-only, so a new derived player is refused (`TIER_ACCESS_REQUIRED`) unless `register` carries a provisioning token covering its address. Use the `harness` token (Secrets Manager `infra-cp/<tier>/loadtest/provisioning-token-harness`, `token` key), which covers `crowdy-e2e*@test.invalid`, with `CROWDY_E2E_EMAIL=crowdy-e2e-py@test.invalid` (the `*` needs at least one character, so the bare `crowdy-e2e@test.invalid` is refused for a suite that registers the base address). Sent on `register` only; other tiers ignore it |

```bash
pytest tests/e2e -q
```

`tools/e2e/provision.py` creates what the suites need through the public API: on a stack
that lets accounts register (a local one), an owner, an org and an app; on a deployed tier,
an app in the existing org, signed in as its org admin (`--owner-email`, `--org-id`, the
password in `CROWDY_E2E_OWNER_PASSWORD`). The deployed tiers' org admins are in AWS Secrets
Manager (`us-east-1`, `infra-cp/<tier>/org-admin/...`).

Registration is rate-limited to 5 an hour per email address (there has been no per-IP limit
since ck-api v2.63.0), so a run registers each player once and signs it in for the later
suites.

## Suites

| Suite | Scenarios | CrowdyCPP counterpart |
|---|---|---|
| `test_e2e_identity.py` | register and sign in; mint an app token (64 octets) and refresh it; `serverWithLeastClients` with the app token; a player registered without the legal consents is refused a gameplay token (`LEGAL_ACCEPTANCE_REQUIRED`, read by `legal_acceptance_required_of`) until `record_player_consents`, gives its age once with `record_player_age` (again is `CONFLICT`), and `player_legal_acceptance` follows; a wrong password is refused; the identity session token is refused for gameplay | `e2e_auth_identities`, `e2e_negative_auth`, `e2e_gameplay_token_refresh` |
| `test_e2e_teen_safety.py` | ck-api v2.60.0 and v2.61.0: sign-up with an age and a country (`me` reads `ageBracket` and `country`), the player and app documents, `record_player_consents` with an `app_id`, the territory policies (CU blocked, KR open; one registration); a sign-up from a blocked country (CU; DE until 2026-11-07, when only the OFAC-sanctioned places stayed blocked) makes no account (`territory_not_supported_of`; one under 13 only with `CROWDY_E2E_UNDER13_SIGNUP=1`); two players' scopes, contacts and blocks; `apps.recording`, an adult's parental consent (`NOT_REQUIRED`) and a report (reason `OTHER`) the org admin reads and resolves with `client.moderation` (two registrations, shared); the Minecraft challenge refused to an app that is not Crowded Kingdoms' own; existing accounts: `hasSafetyModerator` as the org admin (a boolean) and as an outsider (`None`), the outsider's own input-log sessions, every territory row's `regionCode`; an org-managed adult with `ageYears` and `country` (refreshed, deleted), and, refused with no player made: a managed 12-year-old, a blocked country (CU), a blocked region and a region-checked country without a region, while another region of it (`UA-30`) is admitted | `e2e_teen_safety` |
| `test_e2e_replication.py` | two players over native UDP: an actor update reaches the other player and echoes back to the sender; a 16-entity batched frame (`send_actor_updates`) reaches the other player; voxel updates and client events reach the other player | `e2e_two_client_actor`, `e2e_self_echo`, `e2e_spatial_distance` |
| `test_e2e_idle_release.py` (slow) | the owner lowers the app's idle timeout to 60 s (`apps.update`, read back as `effectiveIdleTimeoutSeconds`), a player connects and sends one actor update, and within 105 s the connection is `ConnState.INACTIVE` (`connection_status()` says not connected); 20 s later it still is and nothing reassigned it. The app's own value is put back | none (CrowdyCPP's `replication_test` covers the release against a fake server) |
| `test_e2e_world.py` | two World Stores sessions: each sees the other's actor natively; a voxel edit lands in the other session's chunk store | `e2e_stores_live`, `e2e_world_session` |
| `test_e2e_world_data.py` | a GraphQL voxel write by an entitled player is recorded and listed back | `e2e_chunks` |
| `test_e2e_ranged_channel.py` | the owner's invite channel with four members, each keeping an actor at its own chunk: a distance-limited channel message from A reaches B at 5 chunks with `max_distance` 5 but not C (about 5.66) or D (7); 6 adds C, 7 adds D; A never receives its own; no send is refused | `e2e_ranged_channel` |
| `test_e2e_open_grid_exec_gateway.py` | as a throwaway org admin (`CROWDY_E2E_THROWAWAY_OWNER=1`): open a nested grid, read it back, a player holds its keys, a player-code key is refused `BAD_REQUEST`, close it; the tier's ck-exec gateway passes `exec_gateway_refusal`, a connection pings, and a tampered connect token is `Denied` with the gateway's HTTP 401 reason | `e2e_open_grid_exec_gateway` |
| `test_e2e_studio.py` | the headless Studio controller creates a project from the mod starter, edits it with autosave, and archives it (optional: needs `CROWDY_E2E_STUDIO_GRID_ID`) | `e2e_crowdy_studio` |
| `test_e2e_input_log.py` | read-only, as the owner: the app's sessions list with `missingRecords` on every node (the Game API release after v2.40.2 accepts the selection), one session's inputs page when there is one, and a cancelled read stops with the client still usable | `e2e_input_log` |

## Live runs

| Date (UTC) | Deployment | CrowdyPy | Result |
|---|---|---|---|
| 2026-10-01 | local stack: ck-api at `dev` (`78386df3`), two replication servers, the 3-node Citus lab | 0.4.0 (branch) | 8 passed; Studio skipped (no grid in the app) |
| 2026-10-01 | dev tier, app `96701660793088` (CrowdyPy e2e, in the org admin's org) | 0.4.0 (branch) | 8 passed; Studio skipped (the suite needs a grid the owner controls in that app; a placeholder is refused with `CROWDY_STUDIO_GRID_NOT_FOUND`) |
| 2026-10-01 | dev tier, app `96701660793088` | the published `dev/v0.4.0` wheel (`0.4.0.dev1`, manylinux x86_64, installed into a clean Python 3.12) | 8 passed |
| 2026-10-01 | dev tier, app `96701660793088` | `crowdypy 0.4.1.dev1` from PyPI (`pip install crowdypy` in a clean Python 3.12; the first version published by trusted publishing) | 8 passed; Studio skipped |
| 2026-10-01 | dev tier, app `96701660793088`; the open-grid suite in its own throwaway org and app (`CROWDY_E2E_THROWAWAY_OWNER=1`) | `crowdypy 0.5.0.dev1` from PyPI (`pip install crowdypy` in a clean Python 3.12) | 10 passed; Studio skipped |
| 2026-10-08 | local stack: ck-api at the distance-limited channel branch (off `dev`), two replication servers serving message type 32, the 3-node Citus lab | 0.6.0 (branch) | identity, ranged channel, replication, world and world data suites: 10 passed |
| 2026-10-09 | dev tier, app `96701660793088`, after replication server v0.35.0 and ck-api v2.38.0 reached dev | `crowdypy 0.6.0.dev1` from PyPI (installed into a clean Python 3.12 with uv) | identity (incl. the terms gate), ranged channel, replication, world and world data suites: 10 passed |
| 2026-10-13 | dev tier (ck-api v2.42.0), app `96701660793088`, the `harness` provisioning token with `CROWDY_E2E_EMAIL=crowdy-e2e-py@test.invalid` | 0.10.0 (branch, vendoring CrowdyCPP `dev/v0.62.0`) | identity (the terms-gate case now registers with the token and no consents), input log, ranged channel, replication, world and world data suites: 12 passed; open grid (needs `CROWDY_E2E_THROWAWAY_OWNER=1`) and Studio skipped |
| 2026-10-14 | dev tier (ck-api v2.44.1, replication server v0.39.0), app `96701660793088`, the `harness` provisioning token | 0.11.0 (`dev` at `586fc40`, vendoring CrowdyCPP 0.63.0) | identity, input log, ranged channel, replication, world and world data suites: 12 passed; open grid (needs `CROWDY_E2E_THROWAWAY_OWNER=1`) and Studio skipped |
| 2026-10-16 | test tier (ck-api v2.49.1, compute toolchain v1.15.0), app `101970621262848` (CrowdyPy e2e, in the test org admin's org), the `harness` provisioning token; the open-grid suite in its own throwaway org and app | `crowdypy 0.14.0rc1` from PyPI (the `test/v0.14.0` release, installed into a clean Python 3.12 with uv, numpy beside it for the replication suite) | identity, input log, ranged channel, replication, world and world data suites: 12 passed; open grid: refused `TIER_ACCESS_REQUIRED` at first (its throwaway accounts registered without the token, at addresses it does not cover), then 2 passed once they register through `identity_client` at `crowdy-e2e-py-*@test.invalid`; Studio skipped |
| 2026-10-24 | dev tier (ck-api v2.56.0; both game balancers on HAProxy), app `96701660793088`, the `harness` provisioning token with `CROWDY_E2E_EMAIL=crowdy-e2e-py@test.invalid` | 0.19.0 (`dev` at `c5236f6`, vendoring CrowdyCPP `dev/v0.71.0`) | 12 passed, 3 skipped (the open-grid suite's throwaway owner and Studio, not configured) |
| 2026-10-25 | dev tier (ck-api v2.56.x; the Studio agent re-enabled on the app with `enable-studio-agent.sh --skip-deploy`), app `96701660793088` with Studio grid `105350185270272`, the `harness` provisioning token with `CROWDY_E2E_EMAIL=crowdy-e2e-py@test.invalid` | 0.20.0 plus the resend across a reassignment (branch `fix/retry-on-move`, vendoring CrowdyCPP `dev/v0.72.0`) | 13 passed, 2 skipped (the open-grid suite's throwaway owner); the Studio suite passes for the first time |
| 2026-10-25 | test tier (ck-api v2.57.0), app `101970621262848`, the `harness` provisioning token with `CROWDY_E2E_EMAIL=crowdy-e2e-py@test.invalid` | `crowdypy 0.21.0rc1` from PyPI (the `test/v0.21.0` release, installed into a clean Python 3.12 with uv beside the `test` dependency group) | 12 passed, 3 skipped (the open-grid suite's throwaway owner; the test app has no Studio grid) |
| 2026-10-31 | dev tier (ck-api v2.60.0), app `96701660793088` with Studio grid `105350185270272`, the `harness` provisioning token with `CROWDY_E2E_EMAIL=crowdy-e2e-py@test.invalid`; the teen safety suite's managed players in app `107209219611648` with its organization's org token | 0.23.0 (branch `feat/13plus-teen-safety`: CrowdyJS `dev/v18.24.0`, vendoring CrowdyCPP `dev/v0.75.0`) | 20 passed, 2 skipped (the open-grid suite's throwaway owner), over two runs: registration's limit of 30 an hour per IP is shared by every run from the same host, and one attempt was refused `RATE_LIMITED` at `register` |
| 2026-10-31 | dev tier (ck-api v2.60.0), app `96701660793088`; managed players in app `107209219611648` | `crowdypy 0.23.0.dev1` from PyPI (installed into a clean Python 3.12 with uv) | the teen safety scenarios that register no one (the Minecraft challenge refused; the managed adult; the managed 12-year-old and the blocked country refused): 3 passed |
| 2026-11-03 | dev tier (ck-api v2.61.0, players from 13), app `96701660793088`, the `harness` provisioning token with `CROWDY_E2E_EMAIL=crowdy-e2e-py@test.invalid`; the existing player and the managed players of the 13+ test org (app `107209219611648`) | 0.24.0 (branch `feat/13plus-v2.61`: CrowdyJS `dev/v18.25.0`, vendoring CrowdyCPP `dev/v0.76.0`) | the teen safety suite only, two registrations in all and none under 13: the sign-up with an age (its account deleted), the DE sign-up refused, the Minecraft challenge, the existing accounts (`hasSafetyModerator` as the org admin and `None` as the outsider, its own sessions, every row's `regionCode`) and the managed players (UA-09 and UA without a region refused, UA-30 admitted): 5 passed. The two-player scenarios failed at their setup: dev now asks an org admin for the developer documents, and the CK org admin behind `CROWDY_E2E_OWNER_EMAIL` has not accepted them, so its `grant` is refused ("Accept the current developer terms (recordDeveloperConsents) before creating or administering an organization"); they were not rerun with another owner, which would have registered two more players |
| 2026-11-11 | dev tier (ck-api v2.65.0, the email gate), app `96701660793088`, the `harness` provisioning token with `CROWDY_E2E_EMAIL=crowdy-e2e-py@test.invalid` | 0.26.0 (branch `feat/email-confirmation-required`: CrowdyJS `dev/v18.27.0`, vendoring CrowdyCPP `dev/v0.78.0`) | identity, replication and teen safety suites: 12 passed, 2 skipped (the teen safety suite's existing player and managed players, not configured). Accounts the provisioning token makes are not asked to confirm their email, so every mint went through |
| 2026-11-12 | dev tier (ck-api v2.68.1, replication servers v0.45.0), app `96701660793088`, the `harness` provisioning token with `CROWDY_E2E_EMAIL=crowdy-e2e-py@test.invalid` | 0.30.0 (`dev` at `60de17f`, vendoring CrowdyCPP `dev/v0.82.0`: the ChunkStore rework) | world and replication suites: 4 passed |
| 2026-11-13 | dev tier (ck-api v2.69.0, replication servers v0.46.0), app `96701660793088`, and test tier (the same releases), app `101970621262848`, the `harness` provisioning token with `CROWDY_E2E_EMAIL=crowdy-e2e-py@test.invalid` | 0.32.0 (`dev` at `83a7fc9`, vendoring CrowdyCPP 0.84.0) | dev: `test_e2e_world`, `test_e2e_replication` and the new `test_e2e_chunk_store` (a chunk written with `updateChunk` loads and hydrates with the server's times, an edit settles with its echo, a catch-up over every tracked chunk and a hydrate keep it, an evicted chunk is forgotten) passed; test: `test_e2e_chunk_store` and `test_e2e_world` passed |

## Not covered here

These CrowdyCPP suites have no CrowdyPy counterpart yet; the reason is in each row.

| CrowdyCPP suite | Why |
|---|---|
| `e2e_billing_quotas`, `e2e_payments` | billing and payment mutations on a shared tier; CrowdyPy wraps the same documents CrowdyJS sends, covered by the unit suites |
| `e2e_marketplace_claims` | owns a real chunk; needs a reserved coordinate and a `SELF_CLAIM` app |
| `e2e_exec_client_halves`, `e2e_exec_gateway` | need a ck-exec mod grid, or a gateway running the ck-exec demo app, for calls and subscription pushes; the open-grid suite connects to the tier's gateway and pings, and the gateway client's frames are checked against the shared fixture and a local server |
| `e2e_cross_server` | needs a deployment with two or more replication servers reachable from the runner |
| `e2e_agentic_studio`, `e2e_native_studio_integration` | the agent session is CrowdyJS's in-browser agent; CrowdyPy carries the policy and usage roots only |
| `e2e_soak_two_clients`, `e2e_permission_refresh` | long-running; slated for `CROWDY_E2E_SLOW=1` suites |
| `e2e_host_election`, `e2e_durable_stores`, `e2e_chunk_store_live` | the World Stores' host, save and chunk persistence run over GraphQL timers covered by unit suites against fakes; live coverage needs a dedicated app (chunk write-back closes the wilderness) |
| `e2e_cross_app`, `e2e_cross_tenant`, `e2e_malicious_input` | platform isolation properties the CrowdyCPP suites already exercise against the same API |
| `e2e_bans_blocks` | 0.11.0's `channels.ban` / `teams.ban`, `account_blocked_of` and `group_member_banned_of` send the documents CrowdyCPP's suite exercises live, and the unit suites check them against a mocked API; an account block needs the tier operator's credentials. The tier round trip and the nightly identity probe run the same bans and blocks on each tier |

The teen safety refusals that need a minor's account (`SAFETY_LOG_REQUIRED`,
`SAFETY_MODERATOR_REQUIRED` and `PARENTAL_CONSENT_UNAVAILABLE` need a minor's app token, and dev
has admitted players from 13 only since ck-api v2.61.0), `AGE_REQUIRED`,
`SOCIAL_LIMIT_REACHED` (100 waiting requests), the Minecraft verification refusals (they need
Crowded Kingdoms' own app) and a minor's defaults are covered by the unit suites. The suite
registers no minor: an account it makes is one more of the tier's shared sign-ups, and an age
refusal on the way locks the machine out of signing up for a day.

0.8.0's additions (channel audio, the echo of your own voxel edit, `APP_PAUSED`, access
suspension, player profiles, `exec.restart_type`) have no live suite yet, in CrowdyPy or in
CrowdyCPP. The unit suites cover them against a fake replication server and a mocked API. From
0.8.0 every suite mints with `runtimeGate` selected, so run them only against a tier whose
ck-api is the release after v2.39.0 or later; an older one refuses the mint as a GraphQL
validation error. Channel audio and the voxel echo need replication server v0.37.0 as well.
