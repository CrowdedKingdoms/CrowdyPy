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
| `CROWDY_E2E_STUDIO_GRID_ID` | no | a grid in the app that the owner controls (Studio suite) |
| `CROWDY_E2E_SLOW=1` | no | long-running suites |
| `CROWDY_E2E_THROWAWAY_OWNER=1` | no | the open-grid suite registers a throwaway owner, org and app (archived afterwards) |

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

## Live runs

| Date (UTC) | Deployment | CrowdyPy | Result |
|---|---|---|---|
| 2026-10-01 | local stack: ck-api at `dev` (`78386df3`), two replication servers, the 3-node Citus lab | 0.4.0 (branch) | 8 passed; Studio skipped (no grid in the app) |
| 2026-10-01 | dev tier, app `96701660793088` (CrowdyPy e2e, in the org admin's org) | 0.4.0 (branch) | 8 passed; Studio skipped (the suite needs a grid the owner controls in that app; a placeholder is refused with `CROWDY_STUDIO_GRID_NOT_FOUND`) |
| 2026-10-01 | dev tier, app `96701660793088` | the published `dev/v0.4.0` wheel (`0.4.0.dev1`, manylinux x86_64, installed into a clean Python 3.12) | 8 passed |
| 2026-10-01 | dev tier, app `96701660793088` | `crowdypy 0.4.1.dev1` from PyPI (`pip install crowdypy` in a clean Python 3.12; the first version published by trusted publishing) | 8 passed; Studio skipped |
| 2026-10-01 | dev tier, app `96701660793088`; the open-grid suite in its own throwaway org and app (`CROWDY_E2E_THROWAWAY_OWNER=1`) | `crowdypy 0.5.0.dev1` from PyPI (`pip install crowdypy` in a clean Python 3.12) | 10 passed; Studio skipped |
| 2026-10-08 | local stack: ck-api at the distance-limited channel branch (off `dev`), two replication servers serving message type 32, the 3-node Citus lab | 0.6.0 (branch) | identity, ranged channel, replication, world and world data suites: 10 passed |

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
