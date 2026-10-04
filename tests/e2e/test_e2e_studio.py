"""The headless Studio controller against the live project API: create a project from the
mod starter, edit it with autosave, and archive it. Needs CROWDY_E2E_STUDIO_GRID_ID."""

from __future__ import annotations

import asyncio

import pytest

import crowdypy
from crowdypy.studio import CrowdyStudioController
from e2e.conftest import CONFIG, RUN, owner_client


@pytest.mark.skipif(not CONFIG.studio_grid_id, reason="set CROWDY_E2E_STUDIO_GRID_ID")
async def test_a_project_is_created_edited_saved_and_archived() -> None:
    owner = await owner_client()
    minted = await owner.portal.mint_app_token(CONFIG.app_id)
    game = crowdypy.AsyncCrowdyClient(
        http_url=CONFIG.http_url or minted.game_api_url or CONFIG.api_url,
        discovery_url=minted.discovery_url or CONFIG.api_url,
    )
    game.set_app_token(minted)
    studio = CrowdyStudioController.for_client(
        game, app_id=CONFIG.app_id, grid_id=CONFIG.studio_grid_id, autosave_ms=200
    )
    try:
        await studio.initialize()
        project = await studio.create_project(name=f"crowdypy e2e {RUN}", kind="SERVER")
        assert project.metadata.server_module_name
        studio.update_file(
            "SERVER", "src/lib.rs", project.files[0].content + "\n// edited by CrowdyPy e2e\n"
        )
        for _ in range(50):
            await asyncio.sleep(0.1)
            if studio.get_state().save_state == "SAVED":
                break
        state = studio.get_state()
        assert state.save_state == "SAVED"
        assert state.project is not None
        assert state.project.revision.id != project.revision.id
        archived = await game.crowdy_studio.set_project_archived(
            CONFIG.app_id, state.project.project_id, state.project.revision.id
        )
        assert archived.project_id == state.project.project_id
    finally:
        studio.destroy()
        await game.aclose()
        await owner.aclose()
