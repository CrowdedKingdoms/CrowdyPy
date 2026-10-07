"""Every GraphQL input object as a msgspec struct (generated from schema.gql).

Attributes are snake_case and encode under their GraphQL names; a field left at ``UNSET``
is omitted, which is how the API tells "not provided" from an explicit ``None``. Methods
also accept a plain mapping keyed by the GraphQL field names.
"""

from crowdypy._generated.inputs import *  # noqa: F403
from crowdypy._generated.inputs import __all__ as __all__
