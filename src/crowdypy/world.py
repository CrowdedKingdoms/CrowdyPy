"""An app-scoped realtime facade: ``client.world(app_id)``.

``WorldClient.actor()`` gives an :class:`ActorClient` that remembers its chunk after
:meth:`ActorClient.join`, so the sends that follow need only their payload. Its sends use the
``*_and_wait`` path and return the correlated echo, as CrowdyJS's do; text and events are
never echoed, so those surface a server error inline and otherwise time out
(``UDP_SEQUENCE_TIMEOUT``). The lower-level ``client.udp`` stays available.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from crowdypy.utils import generate_crowdy_uuid, validate_chunk_coordinates, validate_crowdy_uuid
from crowdypy.wire import DecayRate

if TYPE_CHECKING:
    from crowdypy.domains.udp import UdpAPI
    from crowdypy.replication import Notification

__all__ = ["ActorClient", "WorldClient"]


class WorldClient:
    def __init__(self, app_id: str | int, udp: UdpAPI) -> None:
        self.app_id = str(app_id)
        self._udp = udp

    def actor(
        self,
        *,
        uuid: str | None = None,
        default_distance: int | None = None,
        default_decay: int | None = None,
    ) -> ActorClient:
        """An actor in this world. Without ``uuid`` a fresh 32-character id is minted; the
        defaults apply to :meth:`ActorClient.send_state` when a call does not override them."""
        return ActorClient(
            self._udp,
            uuid if uuid is not None else generate_crowdy_uuid(),
            default_distance=default_distance,
            default_decay=default_decay,
        )

    def subscribe(
        self, handlers: Mapping[str, Callable[[Notification], Any]]
    ) -> Callable[[], None]:
        """Per-event handlers, as :meth:`crowdypy.domains.udp.UdpAPI.subscribe`."""
        return self._udp.subscribe(handlers)


class ActorClient:
    def __init__(
        self,
        udp: UdpAPI,
        uuid: str,
        *,
        default_distance: int | None = None,
        default_decay: int | None = None,
    ) -> None:
        validate_crowdy_uuid(uuid)
        #: This actor's 32-character wire id.
        self.uuid = uuid
        self._udp = udp
        self._chunk: tuple[int, int, int] | None = None
        self._distance = default_distance
        self._decay = default_decay

    @property
    def chunk(self) -> tuple[int, int, int] | None:
        """The chunk this actor joined or last sent to, if any."""
        return self._chunk

    def _target(self, chunk: Sequence[int | str] | None, what: str) -> tuple[int, int, int]:
        target = chunk if chunk is not None else self._chunk
        if target is None:
            raise ValueError(f"Actor must join a chunk before sending {what}")
        coords = (int(target[0]), int(target[1]), int(target[2]))
        validate_chunk_coordinates({"x": coords[0], "y": coords[1], "z": coords[2]})
        return coords

    async def join(self, chunk: Sequence[int | str], state: Any = b"\x00") -> Notification:
        """Enter ``chunk`` and register presence there; returns your own echo. The first
        message to a brand-new chunk can be dropped while its permissions load: join again
        on a timeout."""
        self._chunk = self._target(chunk, "state")
        return await self.send_state(state)

    async def send_state(
        self,
        state: Any,
        *,
        chunk: Sequence[int | str] | None = None,
        distance: int | None = None,
        decay: int | None = None,
    ) -> Notification:
        target = self._target(chunk, "state")
        return await self._udp.send_actor_update_and_wait(
            target,
            self.uuid,
            state,
            distance=distance
            if distance is not None
            else (self._distance if self._distance is not None else 8),
            decay=decay
            if decay is not None
            else (self._decay if self._decay is not None else DecayRate.EXPONENTIAL),
        )

    async def send_voxel_update(
        self,
        voxel: Sequence[int],
        voxel_type: int,
        voxel_state: Any = b"",
        *,
        chunk: Sequence[int | str] | None = None,
        distance: int = 8,
        decay: int = DecayRate.NONE,
    ) -> Notification:
        target = self._target(chunk, "voxel updates")
        return await self._udp.send_voxel_update_and_wait(
            target, self.uuid, voxel, voxel_type, voxel_state, distance=distance, decay=decay
        )

    async def send_text(
        self,
        text: str | bytes,
        *,
        chunk: Sequence[int | str] | None = None,
        distance: int = 8,
        decay: int = DecayRate.NONE,
    ) -> Notification:
        target = self._target(chunk, "text")
        return await self._udp.send_text_packet_and_wait(
            target, self.uuid, text, distance=distance, decay=decay
        )

    async def send_event(
        self,
        event_type: int,
        state: Any = b"",
        *,
        chunk: Sequence[int | str] | None = None,
        distance: int = 8,
        decay: int = DecayRate.NONE,
    ) -> Notification:
        target = self._target(chunk, "events")
        return await self._udp.send_client_event_and_wait(
            target, self.uuid, event_type, state, distance=distance, decay=decay
        )

    async def send_to_actor(
        self, target_uuid: str, payload: Any, target_chunk: Sequence[int | str]
    ) -> int:
        """A direct message to one actor (fire-and-forget); returns its sequence number."""
        validate_crowdy_uuid(target_uuid)
        coords = (int(target_chunk[0]), int(target_chunk[1]), int(target_chunk[2]))
        validate_chunk_coordinates({"x": coords[0], "y": coords[1], "z": coords[2]})
        return await self._udp.send_single_actor_message(coords, target_uuid, payload)
