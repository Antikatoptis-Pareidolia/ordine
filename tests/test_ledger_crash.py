"""Crash-injection property tests for ledger durability and exactly-once invariants."""

from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from ordine.core.db import create_engine_for, init_db, session_factory
from ordine.core.ledger import Ledger
from ordine.core.models import BranchAttempt
from ordine.core.playbook import load_playbook

FIXTURE = Path(__file__).parent / "fixtures" / "playbooks" / "valid" / "v01_minimal.yml"
CRASH_POINTS: tuple[str | None, ...] = (
    None,
    "after_claim",
    "after_start_attempt",
    "after_finish_attempt",
)


class SimulatedCrash(Exception):  # noqa: N818 — plan name for crash-injection helper
    """Raised to simulate a process dying between ledger operations."""


def _register(ledger: Ledger) -> int:
    playbook = load_playbook(FIXTURE)
    pipeline_id, _ = ledger.register_pipeline(playbook, FIXTURE.read_text(encoding="utf-8"))
    return pipeline_id


def run_lifecycle(
    ledger: Ledger,
    pipeline_id: int,
    task_id: int,
    crash_at: str | None,
) -> None:
    """Canonical claim → attempt → finish → done sequence with optional crash point."""
    task = ledger.get_task(task_id)
    if task.status == "done":
        return

    if task.status == "pending":
        claimed = ledger.claim_next(pipeline_id)
        if claimed is None:
            raise RuntimeError(f"no pending task to claim for task {task_id}")
        if claimed.id != task_id:
            run_lifecycle(ledger, pipeline_id, claimed.id, None)
            return run_lifecycle(ledger, pipeline_id, task_id, crash_at)
        if crash_at == "after_claim":
            raise SimulatedCrash(crash_at)

    current = ledger.get_task(task_id)
    attempt_id = ledger.start_attempt(task_id, None, current.attempts)
    if crash_at == "after_start_attempt":
        raise SimulatedCrash(crash_at)

    ledger.finish_attempt(attempt_id, ok=True, last_step_id="image.trim", error=None)
    if crash_at == "after_finish_attempt":
        raise SimulatedCrash(crash_at)

    ledger.transition(task_id, "done")


def _ok_attempts_by_branch(engine, task_id: int) -> dict[str | None, list[BranchAttempt]]:
    factory = session_factory(engine)
    session = factory()
    try:
        rows = session.scalars(
            select(BranchAttempt).where(
                BranchAttempt.task_id == task_id,
                BranchAttempt.ok.is_(True),
            )
        ).all()
    finally:
        session.close()
    by_branch: dict[str | None, list[BranchAttempt]] = {}
    for row in rows:
        by_branch.setdefault(row.branch_name, []).append(row)
    return by_branch


def _assert_no_overlapping_ok_completions(engine, task_id: int) -> None:
    """§5 invariant: no two ok attempts on the same branch with overlapping lifecycles."""
    by_branch = _ok_attempts_by_branch(engine, task_id)
    for attempts in by_branch.values():
        ordered = sorted(attempts, key=lambda row: row.started_at)
        for index in range(1, len(ordered)):
            previous = ordered[index - 1]
            current = ordered[index]
            assert previous.finished_at is not None
            assert current.started_at >= previous.finished_at


def test_crash_injection_lifecycle(tmp_path: Path) -> None:
    db_path = tmp_path / "crash.db"
    engine = create_engine_for(db_path)
    init_db(engine)
    ledger = Ledger(engine)
    pipeline_id = _register(ledger)
    rng = random.Random(42)

    task_ids: list[int] = []
    for i in range(50):
        task_id = ledger.create_task(pipeline_id, f"/img-{i}.png", f"dedup-{i}")
        assert task_id is not None
        task_ids.append(task_id)

    for index, task_id in enumerate(task_ids):
        crash_at = rng.choice(CRASH_POINTS)
        while ledger.get_task(task_id).status != "done":
            try:
                run_lifecycle(ledger, pipeline_id, task_id, crash_at)
            except SimulatedCrash:
                ledger.reconcile(pipeline_id, stale_after=timedelta(0), policy="retry")
                crash_at = None

        if index == 25:
            engine2 = create_engine_for(db_path)
            init_db(engine2)
            ledger2 = Ledger(engine2)
            assert ledger2.counts(pipeline_id)["done"] == index + 1

    counts = ledger.counts(pipeline_id)
    assert counts["done"] == 50
    assert counts["pending"] == 0
    assert counts["processing"] == 0
    assert ledger.integrity_check() == "ok"

    for task_id in task_ids:
        _assert_no_overlapping_ok_completions(engine, task_id)


def test_crash_test_reruns_cleanly(tmp_path: Path) -> None:
    """Two independent crash runs both complete (no leftover-file assumptions)."""
    test_crash_injection_lifecycle(tmp_path / "run-a")
    test_crash_injection_lifecycle(tmp_path / "run-b")
