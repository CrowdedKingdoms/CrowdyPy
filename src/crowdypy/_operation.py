"""A GraphQL operation document as the transport sends it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["Operation", "OperationKind", "inline_operation"]

OperationKind = Literal["query", "mutation", "subscription"]


@dataclass(frozen=True, slots=True)
class Operation:
    """One named operation, isolated with exactly the fragments it uses.

    Generated operations live in ``crowdypy._generated.operations``; the few documents
    CrowdyJS declares inline are declared the same way next to the method that sends
    them, and a test validates every one against ``schema.gql``.
    """

    name: str
    kind: OperationKind
    document: str
    #: The response key of each root selection, in document order.
    root_fields: tuple[str, ...]


def inline_operation(name: str, kind: OperationKind, root: str, document: str) -> Operation:
    """Declare a document a domain module carries inline, as CrowdyJS does."""
    return Operation(name=name, kind=kind, document=document, root_fields=(root,))
