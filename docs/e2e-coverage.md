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
| `CROWDY_E2E_OWNER_PASSWORD` | deployed tiers | signs the owner in; unset, a fresh derived owner registers |
| `CROWDY_E2E_HTTP_URL` | no | the game API origin, when not the minted `game_api_url` |
| `CROWDY_E2E_STUDIO_GRID_ID` | no | a grid in the app that the owner controls (Studio suite). On dev, app `96701660793088`'s is `105350185270272` (chunks 900,0,900 to 901,1,901, made by the org admin with `createGrid` on 2026-10-25) |
| `CROWDY_E2E_SLOW=1` | no | long-running suites |
| `CROWDY_E2E_THROWAWAY_OWNER=1` | no | the open-grid suite registers a throwaway owner, org and app (archived afterwards), at `crowdy-e2e-py-<kind>-<id>@test.invalid` addresses and with the provisioning token when one is set |
| `CROWDY_E2E_PROVISIONING_TOKEN` | dev and test | dev and test are staff-only, so a new derived player is refused (`TIER_ACCESS_REQUIRED`) unless `register` carries a provisioning token covering its address. Use the `harness` token (Secrets Manager `infra-cp/<tier>/loadtest/provisioning-token-harness`, `token` key), which covers `crowdy-e2e*@test.invalid`, with `CROWDY_E2E_EMAIL=crowdy-e2e-py@test.invalid` (the `*` needs at least one character, so the bare `crowdy-e2e@test.invalid` is refused for a suite that registers the base address). Sent on `register` only; other tiers ignore it |

```bash
pytest tests/e2e -q
```

`tools/e2e/provision.py` creates what the suites need through the public API: on a stack
that lets accounts register (a local one), an owner, an org and an app; on a deployed tier,
an app in the existing org, signed in as its org admin (`--owner-email`, `--org-id`, the
password in `CROWDY_E2E_OWNER_PASSWORD`). The deployed tiers' org admins are in AWS Secrets
Manager (`us-east-1`, `infra-cp/<tier>/org-admin/...`).

Registration is rate-limited (5 an hour per address, 30 per IP), so a run registers each
player once and signs it in for the later suites.

## Suites

| Suite | Scenarios | CrowdyCPP counterpart |
|---|---|---|
| `test_e2e_identity.py` | register and sign in; mint an app token (64 octets) and refresh it; `serverWithLeastClients` with the app token; a player registered without the legal consents is refused a gameplay token (`LEGAL_ACCEPTANCE_REQUIRED`) until `record_player_consents`, and `player_legal_acceptance` follows; a wrong password is refused; the identity session token is refused for gameplay | `e2e_auth_identities`, `e2e_negative_auth`, `e2e_gameplay_token_refresh` |
| `test_e2e_replication.py` | two players over native UDP: an actor update reaches the other player and echoes back to the sender; a 16-entity batched frame (`send_actor_updates`) reaches the other player; voxel updates and client events reach the other player | `e2e_two_client_actor`, `e2e_self_echo`, `e2e_spatial_distance` |
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

0.8.0's additions (channel audio, the echo of your own voxel edit, `APP_PAUSED`, access
suspension, player profiles, `exec.restart_type`) have no live suite yet, in CrowdyPy or in
CrowdyCPP. The unit suites cover them against a fake replication server and a mocked API. From
0.8.0 every suite mints with `runtimeGate` selected, so run them only against a tier whose
ck-api is the release after v2.39.0 or later; an older one refuses the mint as a GraphQL
validation error. Channel audio and the voxel echo need replication server v0.37.0 as well.
