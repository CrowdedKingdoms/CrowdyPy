"""The blocking client, for code that does not run an event loop.

Generated from the async source by ``scripts/unasync.py``: the same methods, without
``await``. Prefer :class:`crowdypy.AsyncCrowdyClient` where an event loop exists.
"""

from crowdypy._sync.client import CrowdyClient, create_crowdy_client
from crowdypy._sync.graphql import GraphQLClient

__all__ = ["CrowdyClient", "GraphQLClient", "create_crowdy_client"]
