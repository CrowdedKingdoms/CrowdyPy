# CrowdyPy agent guidance

CrowdyPy is a standalone public Python SDK for Crowded Kingdoms. Its API surface
follows CrowdyJS (the flagship); its replication hot path is CrowdyCPP's native
core, bound with nanobind.

GitHub default is **`prod`**. Work lands on `dev` through a pull request; `test`
and `prod` need an admin to merge. Nobody can push to the three branches directly,
the admin included (`GH013: Repository rule violations found` is the rule
working).

**TIER ALIGNMENT (hard rule).** A CrowdyPy branch may only pin CrowdyJS and
CrowdyCPP commits that exist on the **same** tier branch of those repos
(`dev`↔`dev`, `test`↔`test`, `prod`↔`prod`).

**Identity surface.** A PR that touches the paths CODEOWNERS lists under
`src/crowdypy/` (auth, portal, pkce, session, auth_state) runs the Cursor
`security-review` subagent first, and the PR body carries its findings.

This file grows with the SDK on `dev`.
