"""Composition-root wiring tests: the runtime container is fully populated.

Guards against the Phase 4.2 regression where a service was built but not
passed into AppContext, so every unit test passed while the live server ran
without the service (API 500 "direct-auth service unavailable").
"""

from pathlib import Path

from app.core.config import Settings
from app.core.lifecycle import build_context


def test_build_context_wires_every_runtime_service(tmp_path: Path) -> None:
    context = build_context(Settings(data_dir=tmp_path))
    assert context.workspace is not None
    assert context.transfers is not None
    assert context.distribution is not None
    assert context.sync_planner is not None
    assert context.batches is not None
    # The direct-auth service must be both built AND injected into the
    # transfer service (the native->dedicated-key ladder reads it).
    assert context.direct_auth is not None
