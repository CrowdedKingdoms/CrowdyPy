#pragma once

#include "crowdy/replication/types.hpp"

/// Internal (uninstalled): the typed dispatch of one spatial notification, shared by
/// Connection::poll and FleetSession::poll so both read a notification the same way.
namespace crowdy::replication::detail {

/// Call `handlers.any` and then the handler for `n.type`, each with `extra` after its own
/// arguments. Connection passes none; a FleetSession passes the carried players who may
/// receive the notification.
template <class HandlerSet, class... Extra>
void dispatchSpatial(const HandlerSet& handlers, const SpatialNotification& n,
                     const Extra&... extra) {
  using wire::MessageType;
  if (handlers.any) handlers.any(n, extra...);
  switch (n.type) {
    case MessageType::ActorUpdateNotification:
      if (handlers.actorUpdate) handlers.actorUpdate(n, extra...);
      break;
    case MessageType::VoxelUpdateNotification: {
      if (handlers.voxelUpdate) {
        auto voxel = wire::parseVoxelPayload(n.payload);
        if (voxel.ok()) handlers.voxelUpdate(n, voxel.value(), extra...);
      }
      break;
    }
    case MessageType::ClientAudioNotification:
      if (handlers.audio) handlers.audio(n, extra...);
      break;
    case MessageType::ClientVideoNotification:
      if (handlers.video) handlers.video(n, extra...);
      break;
    case MessageType::ActorLeftNotification:
      if (handlers.actorLeft)
        handlers.actorLeft(n, wire::actorLeftReason(n.payload.data(), n.payload.size()),
                           extra...);
      break;
    case MessageType::ChunkChangedNotification:
      if (handlers.chunkChanged)
        handlers.chunkChanged(n, wire::chunkChangedUpdatedAtMs(n.payload), extra...);
      break;
    case MessageType::ClientTextNotification:
      if (handlers.text) handlers.text(n, extra...);
      break;
    case MessageType::ClientEventNotification: {
      if (handlers.clientEvent) {
        auto ev = wire::parseEventPayload(n.payload);
        if (ev.ok()) handlers.clientEvent(n, ev.value(), extra...);
      }
      break;
    }
    case MessageType::ServerEventNotification: {
      if (handlers.serverEvent) {
        auto ev = wire::parseEventPayload(n.payload);
        if (ev.ok()) handlers.serverEvent(n, ev.value(), extra...);
      }
      break;
    }
    case MessageType::GenericSpatial1:
      if (handlers.genericSpatial) handlers.genericSpatial(n, extra...);
      break;
    case MessageType::SingleActorMessage:
      if (handlers.singleActorMessage) handlers.singleActorMessage(n, extra...);
      break;
    default:
      break;
  }
}

}  // namespace crowdy::replication::detail
