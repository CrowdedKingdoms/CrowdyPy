"""Provision an owner, an organization and an app for the e2e suites on a deployment that
lets accounts register (a local stack), and print the ``CROWDY_E2E_*`` exports.

    python tools/e2e/provision.py --api-url http://127.0.0.1:3000 --email you@example.com

On a deployed tier, sign in as its existing org admin instead and create only the app
(the password comes from the environment and is not printed):

    CROWDY_E2E_OWNER_PASSWORD=... python tools/e2e/provision.py --api-url <origin> \
        --email you@example.com --owner-email admin@example.com --org-id 123

Everything goes through the public API, as the suites do. Dev and test are staff-only: a
fresh owner registers there only with ``CROWDY_E2E_PROVISIONING_TOKEN`` (sent as
``X-CK-Provisioning-Token``, read by ``register`` alone), or sign in as the existing owner.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shlex

import httpx

import crowdypy
from crowdypy.graphql import graphql_endpoint


async def _register_with_token(api_url: str, email: str, password: str, token: str) -> str:
    """``register`` with the provisioning token, which the SDK does not send; returns the session."""
    query = "mutation R($i: RegisterUserInput!) { register(registerUserInput: $i) { token } }"
    variables = {
        "i": {
            "email": email,
            "password": password,
            "acceptLegal": True,
            "attestAgeOfMajority": True,
        }
    }
    async with httpx.AsyncClient(timeout=60.0) as http:
        res = await http.post(
            graphql_endpoint(api_url) or api_url,
            json={"query": query, "variables": variables},
            headers={"x-ck-provisioning-token": token},
        )
    payload = res.json()
    if payload.get("errors"):
        raise SystemExit(f"register {email}: {payload['errors']}")
    return str(payload["data"]["register"]["token"])


async def provision(
    api_url: str, email: str, owner_email: str | None = None, org_id: str | None = None
) -> dict[str, str]:
    suffix = secrets.token_hex(3)
    local, _, domain = email.partition("@")
    existing = owner_email is not None
    owner = owner_email or f"{local}+py-owner-{suffix}@{domain}"
    password = os.environ["CROWDY_E2E_OWNER_PASSWORD"] if existing else "Aa1!e2e-" + owner
    provisioning = os.environ.get("CROWDY_E2E_PROVISIONING_TOKEN", "").strip()
    async with crowdypy.AsyncCrowdyClient(http_url=api_url) as client:
        if existing:
            await client.auth.login(owner, password)
        elif provisioning:
            client.set_token(await _register_with_token(api_url, owner, password, provisioning))
        else:
            await client.auth.register(
                owner, password, accept_legal=True, attest_age_of_majority=True
            )
        org = (
            {"orgId": org_id}
            if org_id
            else await client.admin.organizations.create(
                {"name": f"CrowdyPy e2e {suffix}", "slug": f"crowdypy-e2e-{suffix}"}
            )
        )
        placeable = (await client.admin.apps.placeable_datacenters()).get("datacenters") or []
        serving = [
            dc["code"] for dc in placeable if dc.get("placeable") and dc.get("serving") == "SERVING"
        ]
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
    exports = {
        "CROWDY_E2E_API_URL": api_url,
        "CROWDY_E2E_EMAIL": email,
        "CROWDY_E2E_APP_ID": str(app["appId"]),
        "CROWDY_E2E_OWNER_EMAIL": owner,
    }
    if not existing:
        exports["CROWDY_E2E_OWNER_PASSWORD"] = password
    return exports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--email", required=True, help="base address; accounts are plus-addressed")
    parser.add_argument(
        "--owner-email", help="sign in as this existing owner (password from the environment)"
    )
    parser.add_argument("--org-id", help="create the app in this organization")
    args = parser.parse_args()
    exports = asyncio.run(provision(args.api_url, args.email, args.owner_email, args.org_id))
    for name, value in exports.items():
        print(f"export {name}={shlex.quote(value)}")


if __name__ == "__main__":
    main()
