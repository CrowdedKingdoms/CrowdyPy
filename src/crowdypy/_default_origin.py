"""GENERATED -- do not edit. The operator tooling's
`sync-client-origins.mjs --write --tier prod` writes this file, and
`check-sdk-default-origin.mjs` refuses it when it names the wrong tier or a
host that declaration does not carry. Regenerate; never hand-edit.

THE DEFAULT IS LOAD-BEARING DURING A ROLLOUT: while the branches are
mid-migration this is what an unconfigured consumer gets, so a WRONG default on
one branch is worse than no default at all. A published wheel carries this file
and PyPI never lets a version be replaced.

Source: the operator's per-tier public CK API origin declaration, tier 'prod'
"""

from typing import Final

#: The tier this build of the SDK is published for.
CROWDY_DEFAULT_TIER: Final = "prod"

#: The public CK API origin for that tier.
CROWDY_DEFAULT_HTTP_ORIGIN: Final = "https://ck.prod.crowdedkingdoms.com"

#: The same host over WebSocket. A scheme is composed; a hostname is looked up.
CROWDY_DEFAULT_WS_ORIGIN: Final = "wss://ck.prod.crowdedkingdoms.com"

#: The bare hostname, for callers that need to compare rather than dial.
CROWDY_DEFAULT_HOST: Final = "ck.prod.crowdedkingdoms.com"
