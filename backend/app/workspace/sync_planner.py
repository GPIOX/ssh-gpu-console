"""Project Sync planning (Phase 4C): per-artifact decisions toward one target.

SyncPlanner answers "what would it take to get this project's artifacts onto
server X": each artifact becomes SKIP / TRANSFER / UNRESOLVED with a reason.
All SSH is explicit and bounded — one short, fixed, quoted command per checked
placement or source candidate, strictly sequential, no background tasks.
Strategy selection is never reimplemented here: the runtime's
``TransferService.plan`` (rsync preflight, space checks) is injected and
reused, so a plan item carries exactly the strategy a real transfer would get.
"""

from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.collectors.base import ExecutorLike
from app.core.errors import AppError, ConflictError, NotFoundError
from app.models.distribution import (
    ArtifactServerDistribution,
    ArtifactSyncItem,
    DistributionState,
    ProjectSyncPlan,
    SyncAction,
    SyncPlanRequest,
)
from app.models.transfer import TransferPlan, TransferRequest, TransferStrategy
from app.models.workspace import (
    ArtifactKind,
    ArtifactRecord,
    InspectionState,
    PlacementRecord,
    ServerRoots,
)
from app.workspace.paths import normalize_remote_path, paths_overlap, safe_artifact_leaf
from app.workspace.repository import utc_now
from app.workspace.service import WorkspaceService

if TYPE_CHECKING:
    from app.workspace.distribution import DistributionService

__all__ = ["SyncPlanner"]

ExecutorFactory = Callable[[str], ExecutorLike]  # server_id -> executor
PlanTransfer = Callable[[TransferRequest], Awaitable[TransferPlan]]

_PREFLIGHT_TIMEOUT_S = 15.0
# One fixed, quoted read-only command distinguishing a symlink root (unsafe —
# automatic sync never selects a root symlink source) from a missing or
# usable source in a single bounded round trip.
_PREFLIGHT_COMMAND = (
    "p={path};"
    'if [ -L "$p" ]; then echo SYMLINK;'
    ' elif [ -e "$p" ]; then echo OK;'
    " else echo MISSING; fi"
)

_INSPECTION_TO_STATE: dict[InspectionState, DistributionState] = {
    InspectionState.VERIFIED: DistributionState.VERIFIED,
    InspectionState.MISSING: DistributionState.MISSING,
    InspectionState.UNAVAILABLE: DistributionState.UNAVAILABLE,
    InspectionState.DECLARED: DistributionState.DECLARED,
}


class SyncPlanner:
    """Builds project sync plans; SSH only for explicit, bounded checks."""

    def __init__(
        self,
        workspace: WorkspaceService,
        distribution: DistributionService,
        executor_for: ExecutorFactory,
        plan_transfer: PlanTransfer,
    ) -> None:
        self._workspace = workspace
        self._distribution = distribution
        self._executor_for = executor_for
        self._plan_transfer = plan_transfer

    async def build_plan(self, project_id: str, request: SyncPlanRequest) -> ProjectSyncPlan:
        project = self._workspace.project(project_id)  # NotFoundError -> 404
        if request.artifact_ids is None:
            artifact_ids = list(dict.fromkeys(project.artifact_ids))
        else:
            artifact_ids = list(dict.fromkeys(request.artifact_ids))
            project_ids = set(project.artifact_ids)
            foreign = next((aid for aid in artifact_ids if aid not in project_ids), None)
            if foreign is not None:
                raise ConflictError(f"artifact {foreign!r} is not part of project {project_id!r}")

        # Zero-SSH read-model snapshot: target statuses and the source priority
        # order (VERIFIED first) both come from this single call.
        snapshot = self._distribution.distribution(project_id)
        snapshot_by_placement = {
            item.placement_id: item for item in snapshot.items if item.placement_id is not None
        }
        # Plan-scoped suggestion registry (target path -> owning artifact id):
        # every automatic suggestion is claimed here so later items in the same
        # plan (and, via recorded placements, later plans) never collide with it.
        suggested: dict[str, str] = {}
        target_placements = [
            placement
            for placement in self._workspace.placements()
            if placement.server_id == request.target_server_id
        ]
        items: list[ArtifactSyncItem] = []
        for artifact_id in artifact_ids:
            artifact = self._workspace.artifact(artifact_id)  # NotFoundError -> 404
            items.append(
                await self._plan_item(
                    artifact, request, snapshot_by_placement, target_placements, suggested
                )
            )

        valid, error = _overlap_error(items)
        return ProjectSyncPlan(
            project_id=project_id,
            target_server_id=request.target_server_id,
            generated_at=utc_now(),
            refresh_code=request.refresh_code,
            items=items,
            valid=valid,
            error=error,
        )

    # ---- per-artifact decision ---------------------------------------------------

    async def _plan_item(
        self,
        artifact: ArtifactRecord,
        request: SyncPlanRequest,
        snapshot: dict[str, ArtifactServerDistribution],
        target_placements: list[PlacementRecord],
        suggested: dict[str, str],
    ) -> ArtifactSyncItem:
        label = f"{artifact.name}:{artifact.version}" if artifact.version else artifact.name
        target = next(
            (
                placement
                for placement in self._workspace.placements_of(artifact.artifact_id)
                if placement.server_id == request.target_server_id
            ),
            None,
        )

        target_status = DistributionState.MISSING  # no declared placement: not there
        target_path: str | None = None
        if target is not None:
            observed = snapshot.get(target.placement_id)
            target_status = observed.state if observed is not None else DistributionState.DECLARED
            target_path = normalize_remote_path(target.remote_path)
            if target_status is DistributionState.DECLARED:
                # sync-plan is an explicit operation: a declared target is
                # checked NOW (one bounded round trip) so the decision uses the
                # freshest observation instead of an unchecked declaration.
                target_status = await self._inspect_target(target.placement_id)

        if target_status is DistributionState.SYNCING:
            return _item(
                artifact,
                label,
                target_status,
                SyncAction.SKIP,
                "target placement is currently syncing",
                target_path=target_path,
                warnings=["decision may change after the active transfer finishes"],
            )
        if target_status is DistributionState.UNAVAILABLE:
            return _item(
                artifact,
                label,
                target_status,
                SyncAction.UNRESOLVED,
                "target server unavailable; placement cannot be checked now",
                target_path=target_path,
            )

        present = target_status in (DistributionState.VERIFIED, DistributionState.DECLARED)
        if not _needs_transfer(artifact.kind, present, request.refresh_code):
            reason = (
                "target placement is verified; code refresh not requested"
                if artifact.kind is ArtifactKind.CODE
                else "target placement is verified"
            )
            return _item(
                artifact, label, target_status, SyncAction.SKIP, reason, target_path=target_path
            )
        motive = _motive(artifact.kind, present, request.refresh_code, target is not None)

        # TRANSFER: resolve the target path first (local, no SSH) so a missing
        # root is reported without any remote round trips, then pick a source.
        if target_path is None:
            base = self._suggested_target_path(artifact, request.target_server_id)
            if base is None:
                return _item(
                    artifact,
                    label,
                    target_status,
                    SyncAction.UNRESOLVED,
                    "target root not configured on server",
                )
            taken = {
                normalize_remote_path(placement.remote_path)
                for placement in target_placements
                if placement.artifact_id != artifact.artifact_id
            }
            taken.update(suggested)
            target_path = _claim_suggestion(base, artifact.artifact_id, taken)
            if target_path is None:
                return _item(
                    artifact,
                    label,
                    target_status,
                    SyncAction.UNRESOLVED,
                    "suggested target path is already used by another artifact",
                )
            suggested[target_path] = artifact.artifact_id

        selected, alternatives, failures = await self._select_source(
            artifact, request.target_server_id, snapshot
        )
        if selected is None:
            summary = "; ".join(failures) if failures else "no other server declares this artifact"
            return _item(
                artifact,
                label,
                target_status,
                SyncAction.UNRESOLVED,
                f"no usable source placement: {summary}"[:500],
                target_path=target_path,
            )

        try:
            # Construction inside the try: a validation failure (e.g. a
            # suggested path beyond the 512-char wire bound) degrades the
            # item instead of failing the whole plan.
            transfer_request = TransferRequest(
                artifact_id=artifact.artifact_id,
                source_placement_id=selected.placement_id,
                target_server_id=request.target_server_id,
                target_path=target_path,
                strategy=request.strategy,
            )
            plan = await self._plan_transfer(transfer_request)
        except Exception as exc:
            return _item(
                artifact,
                label,
                target_status,
                SyncAction.UNRESOLVED,
                f"transfer planning failed: {_short(str(exc))}",
                target_path=target_path,
            )
        warnings: list[str] = []
        if plan.reason:
            _add_warning(warnings, plan.reason)
        if plan.space_warning:
            _add_warning(warnings, plan.space_warning)
        if plan.strategy_selected is None:
            return _item(
                artifact,
                label,
                target_status,
                SyncAction.UNRESOLVED,
                plan.reason or "no transfer strategy available",
                target_path=target_path,
                warnings=warnings,
            )
        return _item(
            artifact,
            label,
            target_status,
            SyncAction.TRANSFER,
            f"{motive}; transfer from {selected.server_id}:{selected.remote_path}",
            target_path=target_path,
            source_placement_id=selected.placement_id,
            source_server_id=selected.server_id,
            source_path=selected.remote_path,
            strategy_selected=plan.strategy_selected,
            alternatives=[f"{p.server_id}:{p.remote_path}" for p in alternatives],
            warnings=warnings,
        )

    # ---- bounded SSH pieces --------------------------------------------------------

    async def _inspect_target(self, placement_id: str) -> DistributionState:
        try:
            inspection = await self._distribution.inspect_placement(placement_id)
        except AppError:
            # Shell-hostile declared path rejected locally, or the placement
            # vanished concurrently: degrade this item, never strand the plan.
            return DistributionState.UNAVAILABLE
        return _INSPECTION_TO_STATE.get(inspection.state, DistributionState.DECLARED)

    async def _select_source(
        self,
        artifact: ArtifactRecord,
        target_server_id: str,
        snapshot: dict[str, ArtifactServerDistribution],
    ) -> tuple[PlacementRecord | None, list[PlacementRecord], list[str]]:
        """Pick a safe source: (selected, alternatives, per-candidate failures).

        Candidates are the artifact's placements on other servers, ordered
        VERIFIED-in-cache first, ties in declaration (creation) order — the
        sort is stable, so the result is deterministic for identical inputs.
        """
        candidates = [
            placement
            for placement in self._workspace.placements_of(artifact.artifact_id)
            if placement.server_id != target_server_id
        ]

        def _not_verified(placement: PlacementRecord) -> bool:
            observed = snapshot.get(placement.placement_id)
            return observed is None or observed.state is not DistributionState.VERIFIED

        candidates.sort(key=_not_verified)

        passing: list[PlacementRecord] = []
        failures: list[str] = []
        for placement in candidates:
            failure = await self._preflight_source(placement)
            if failure is None:
                passing.append(placement)
            else:
                failures.append(f"{placement.server_id}:{placement.remote_path} ({failure})")
        if not passing:
            return None, [], failures
        return passing[0], passing[1:], failures

    async def _preflight_source(self, placement: PlacementRecord) -> str | None:
        """None = usable source root; otherwise a short failure reason."""
        try:
            executor = self._executor_for(placement.server_id)
        except Exception as exc:
            return _short(str(exc) or "server unavailable")
        command = _PREFLIGHT_COMMAND.format(path=shlex.quote(placement.remote_path))
        try:
            result = await executor.run(command, timeout_s=_PREFLIGHT_TIMEOUT_S)
        except Exception as exc:
            return _short(str(exc) or "source preflight failed")
        if result.exit_code != 0:
            return f"preflight exited with code {result.exit_code}"
        output = result.stdout.strip()
        if output == "SYMLINK":
            return "source root is a symlink (unsafe for automatic sync)"
        if output == "MISSING":
            return "source path is missing"
        if output == "OK":
            return None
        return "unexpected preflight output"

    # ---- local helpers ---------------------------------------------------------------

    def _suggested_target_path(self, artifact: ArtifactRecord, server_id: str) -> str | None:
        """Base server-root suggestion (before collision escalation); never a
        guessed default path."""
        try:
            roots = self._workspace.server_roots(server_id)
        except NotFoundError:
            return None
        root = _root_for(roots, artifact.kind) if roots is not None else None
        if not root:
            return None
        leaf = safe_artifact_leaf(artifact.name, artifact.version, artifact.artifact_id)
        return normalize_remote_path(f"{root.rstrip('/')}/{leaf}")


def _claim_suggestion(base: str, artifact_id: str, taken: set[str]) -> str | None:
    """Claim a non-colliding automatic target path; None when exhausted.

    Escalates exactly like :func:`app.workspace.paths.dedupe_leaves`: the base
    leaf keeps its path when free, otherwise "--" plus a prefix of the
    artifact id (6, 8, 12 characters, then the full id) is appended. The
    caller records the claimed path in the plan-scoped registry; repeated
    calls with the same inputs return the same path (no ``hash()``).
    """
    if base not in taken:
        return base
    for width in (6, 8, 12, len(artifact_id)):
        candidate = f"{base}--{artifact_id[:width]}"
        if candidate not in taken:
            return candidate
    return None


def _needs_transfer(kind: ArtifactKind, present: bool, refresh_code: bool) -> bool:
    """Decision table: dataset/model sync when absent; code also on refresh."""
    if kind is ArtifactKind.CODE:
        return (not present) or refresh_code
    return not present


def _motive(kind: ArtifactKind, present: bool, refresh_code: bool, declared: bool) -> str:
    """Why this item needs a transfer (only called when it does)."""
    if kind is ArtifactKind.CODE and present and refresh_code:
        return "code refresh requested"
    if not declared:
        return "no placement declared on the target server"
    return "target path checked missing"


def _root_for(roots: ServerRoots, kind: ArtifactKind) -> str | None:
    if kind is ArtifactKind.CODE:
        return roots.project_root
    if kind is ArtifactKind.DATASET:
        return roots.dataset_root
    if kind is ArtifactKind.MODEL:
        return roots.model_root
    return None


def _item(
    artifact: ArtifactRecord,
    label: str,
    target_status: DistributionState,
    action: SyncAction,
    reason: str,
    *,
    target_path: str | None = None,
    source_placement_id: str | None = None,
    source_server_id: str | None = None,
    source_path: str | None = None,
    strategy_selected: TransferStrategy | None = None,
    alternatives: list[str] | None = None,
    warnings: list[str] | None = None,
) -> ArtifactSyncItem:
    return ArtifactSyncItem(
        artifact_id=artifact.artifact_id,
        artifact_label=label,
        artifact_kind=artifact.kind.value,
        target_status=target_status,
        action=action,
        reason=reason,
        source_placement_id=source_placement_id,
        source_server_id=source_server_id,
        source_path=source_path,
        target_path=target_path,
        strategy_selected=strategy_selected,
        alternatives=alternatives or [],
        warnings=warnings or [],
    )


def _overlap_error(items: list[ArtifactSyncItem]) -> tuple[bool, str]:
    """TRANSFER items must never write into each other's target tree.

    Plan-level marker only: refusing execution is the transfer phase's job.
    """
    transfers = [item for item in items if item.action is SyncAction.TRANSFER]
    conflicts: list[str] = []
    for index, first in enumerate(transfers):
        path_a = first.target_path
        if path_a is None:
            continue
        for second in transfers[index + 1 :]:
            path_b = second.target_path
            if path_b is None:
                continue
            if paths_overlap(path_a, path_b):
                conflicts.append(
                    f"{first.artifact_label} ({path_a}) vs {second.artifact_label} ({path_b})"
                )
    if not conflicts:
        return True, ""
    return False, ("target path overlap: " + "; ".join(conflicts))[:600]


def _add_warning(warnings: list[str], message: str) -> None:
    if len(warnings) < 20:
        warnings.append(message[:300])


def _short(text: str, limit: int = 200) -> str:
    return text.strip()[:limit]
