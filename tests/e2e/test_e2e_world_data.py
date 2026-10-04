"""World data over GraphQL with an entitled player: a voxel edit is recorded and read back."""

from __future__ import annotations

from e2e.conftest import CONFIG, chunk_band, provision_player


async def test_a_voxel_write_is_recorded() -> None:
    player = await provision_player("py-voxels")
    try:
        x, y, z = chunk_band(5)
        coordinates = {"x": str(x), "y": str(y), "z": str(z)}
        await player.game.voxels.update(
            {
                "appId": CONFIG.app_id,
                "coordinates": coordinates,
                "location": {"x": 4, "y": 5, "z": 6},
                "voxelType": 3,
            }
        )
        edits = await player.game.voxels.list({"appId": CONFIG.app_id, "coordinates": coordinates})
        assert any(
            (edit.get("location") or {}).get("x") == 4 and edit.get("voxelType") == 3
            for edit in edits
        ), edits
    finally:
        await player.aclose()
