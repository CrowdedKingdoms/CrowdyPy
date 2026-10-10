# Compatibility

This page records what this build of CrowdyPy was made against. The authoritative values
are in `pyproject.toml` (`[project].version`, `[tool.crowdypy.crowdyjs]`,
`[tool.crowdypy.crowdycpp]`); `tests/unit/test_release_versions.py` refuses this page when
its version disagrees.

- CrowdyPy `0.8.0`
- API surface: CrowdyJS (the pinned version and commit in `pyproject.toml`); the generated
  `docs/parity-matrix.md` lists every covered method, every native equivalent and every
  browser exclusion with its reason. The gate is strict: there are no portable gaps, and a
  new one fails the build.
- Native core: CrowdyCPP (the pinned tag and commit in `pyproject.toml`), vendored under
  `vendor/CrowdyCPP/` and re-derived from that commit in CI.
- GraphQL schema: CrowdyJS's `schema.gql` at the pinned commit.
- Python: CPython 3.12 or later (one `cp312-abi3` wheel per platform), plus free-threaded
  CPython 3.14 (`cp314t`).
- Platforms with wheels: Linux x86_64 and aarch64 (manylinux_2_28 and musllinux_1_2), macOS
  11+ arm64 and x86_64, Windows x64. Elsewhere, pip builds the sdist (a C++20 compiler,
  CMake 3.22+ and OpenSSL 3 development headers are needed).

## Tiers

Each branch publishes one tier and carries that tier's default API origin:

| Branch | Git tag | PyPI version | Consumers pin |
|---|---|---|---|
| `dev` | `dev/vX.Y.Z` | `X.Y.Z.devN` | `crowdypy==X.Y.Z.devN` |
| `test` | `test/vX.Y.Z` | `X.Y.ZrcN` | `crowdypy==X.Y.ZrcN` |
| `prod` | `prod/vX.Y.Z` | `X.Y.Z` | `crowdypy==X.Y.Z` (or a range) |

A consumer on one tier pins only that tier's versions: pip ignores pre-releases unless a
requirement names one exactly, the same way a caret never matches an npm prerelease.

## Server compatibility

- ck-exec (`client.exec`) runs on dev and test, and on prod from its migration; a Game API that does not serve the
  `exec*` roots answers those calls with a GraphQL validation error.
- Direct sign-in (`auth.login`, `auth.register`) is served to non-browser callers. CrowdyPy
  sends no `Origin` header, so it is exempt from `HOSTED_SIGN_IN_REQUIRED`.
- `auth.record_player_consents` and `auth.player_legal_acceptance` need ck-api v2.35.0; an
  older API answers them with a GraphQL validation error. From v2.35.0 a gameplay token
  waits for both consents (`LEGAL_ACCEPTANCE_REQUIRED`).
- `udp.send_ranged_channel_message` needs a replication server that serves message type 32
  (CHANNEL_MESSAGE_RANGED_REQUEST); one that does not delivers nothing.
- 0.8.0 selects `runtimeGate` on every token mutation (`portal.mint_app_token`,
  `exchange_code`, `refresh`) and on `game_client_bootstrap`, so it needs the ck-api release
  after v2.39.0 that serves it; an older API refuses those calls with a GraphQL validation
  error. The same release serves `playerProfile(s)`, the access suspension and
  `execRestartType`.
- Channel audio (message types 35 and 36) and the echo of your own voxel edits need replication
  server v0.37.0; an older one drops message type 35 and echoes nothing.
- The wheels link no audio codec (CrowdyCPP's optional libopus wrapper is built OFF): the
  voice helpers carry an app's codec frames without reading them.
