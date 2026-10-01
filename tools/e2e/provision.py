"""Provision an owner, an organization and an app for the e2e suites on a deployment that
lets accounts register (a local stack), and print the ``CROWDY_E2E_*`` exports.

    python tools/e2e/provision.py --api-url http://127.0.0.1:3000 --email you@example.com

Everything goes through the public API, as the suites do. Deployed tiers already have an
owner and an app; take theirs instead (``docs/e2e-coverage.md``).
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import shlex

import crowdypy


async def provision(api_url: str, email: str) -> dict[str, str]:
    suffix = secrets.token_hex(3)
    local, _, domain = email.partition("@")
    owner = f"{local}+py-owner-{suffix}@{domain}"
    password = "Aa1!e2e-" + owner
    async with crowdypy.AsyncCrowdyClient(http_url=api_url) as client:
        await client.auth.register(owner, password)
        org = await client.admin.organizations.create(
            {"name": f"CrowdyPy e2e {suffix}", "slug": f"crowdypy-e2e-{suffix}"}
        )
        placeable = (await client.admin.apps.placeable_datacenters()).get("datacenters") or []
        serving = [dc["code"] for dc in placeable if dc.get("placeable") and dc.get("serving") == "SERVING"]
        open_ = [dc["code"] for dc in placeable if dc.get("placeable")]
        if not open_:
            raise SystemExit("no placeable datacenter on this deployment")
        app = await client.admin.apps.create(
            {
                "orgId": str(org["orgId"]),
                "name": f"CrowdyPy e2e {suffix}",
                "slug": f"crowdypy-e2e-{suffix}",
                "datacenter": (serving or open_)[0],
            }
        )
    return {
        "CROWDY_E2E_API_URL": api_url,
        "CROWDY_E2E_EMAIL": email,
        "CROWDY_E2E_APP_ID": str(app["appId"]),
        "CROWDY_E2E_OWNER_EMAIL": owner,
        "CROWDY_E2E_OWNER_PASSWORD": password,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--email", required=True, help="base address; accounts are plus-addressed")
    args = parser.parse_args()
    for name, value in asyncio.run(provision(args.api_url, args.email)).items():
        print(f"export {name}={shlex.quote(value)}")


if __name__ == "__main__":
    main()
