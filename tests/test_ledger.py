"""Ledger unit tests — one dedicated test per §4 behavior rule."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from ordine.core.db import create_engine_for, init_db
from ordine.core.errors import IllegalTransitionError, LedgerError, SchemaVersionError
from ordine.core.ledger import Ledger
from ordine.core.playbook import load_playbook

FIXTURE = Path(__file__).parent / "fixtures" / "playbooks" / "valid" / "v01_minimal.yml"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


@pytest.fixture
def engine(db_path: Path):
    eng = create_engine_for(db_path)
    init_db(eng)
    return eng


@pytest.fixture
def ledger(engine) -> Ledger:
    return Ledger(engine)


def _register(ledger: Ledger, yaml_path: Path = FIXTURE) -> tuple[int, str]:
    playbook = load_playbook(yaml_path)
    yaml_text = yaml_path.read_text(encoding="utf-8")
    return ledger.register_pipeline(playbook, yaml_text)


def test_db_smoke_init(tmp_path: Path) -> None:
    path = tmp_path / "smoke.db"
    engine = create_engine_for(path)
    init_db(engine)
    with engine.connect() as conn:
        version = conn.execute(text("PRAGMA user_version")).scalar_one()
        assert version == 1
        journal = conn.execute(text("PRAGMA journal_mode")).scalar_one()
        assert str(journal).lower() == "wal"


def test_schema_version_guard(tmp_path: Path) -> None:
    path = tmp_path / "bad.db"
    engine = create_engine_for(path)
    with engine.connect() as conn:
        conn.execute(text("PRAGMA user_version = 99"))
        conn.commit()
    with pytest.raises(SchemaVersionError):
        init_db(engine)


def test_rule_1_duplicate_forever(ledger: Ledger) -> None:
    pipeline_id, _ = _register(ledger)
    first = ledger.create_task(pipeline_id, "/a.png", "hash-1")
    assert first is not None
    dup = ledger.create_task(pipeline_id, "/a.png", "hash-1")
    assert dup is None
    claimed = ledger.claim_next(pipeline_id)
    assert claimed is not None
    ledger.transition(claimed.id, "done")
    again = ledger.create_task(pipeline_id, "/a.png", "hash-1")
    assert again is None


def test_rule_2_concurrent_claim(ledger: Ledger) -> None:
    pipeline_id, _ = _register(ledger)
    task_ids = [ledger.create_task(pipeline_id, f"/file-{i}.png", f"key-{i}") for i in range(20)]
    assert all(tid is not None for tid in task_ids)

    claimed_ids: list[int] = []

    def worker() -> int | None:
        view = ledger.claim_next(pipeline_id)
        return None if view is None else view.id

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker) for _ in range(40)]
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                claimed_ids.append(result)

    assert sorted(claimed_ids) == sorted(task_ids)
    assert len(claimed_ids) == len(set(claimed_ids))


def test_rule_3_transitions(ledger: Ledger) -> None:
    pipeline_id, _ = _register(ledger)
    task_id = ledger.create_task(pipeline_id, "/a.png", "t3")
    assert task_id is not None

    claimed = ledger.claim_next(pipeline_id)
    assert claimed is not None
    tid = claimed.id

    ledger.transition(tid, "failed", error="boom")
    ledger.transition(tid, "pending")
    re_claimed = ledger.claim_next(pipeline_id)
    assert re_claimed is not None
    ledger.transition(re_claimed.id, "flagged", error="flagged")
    ledger.transition(re_claimed.id, "pending")

    cancel_id = ledger.create_task(pipeline_id, "/b.png", "t3-cancel")
    assert cancel_id is not None
    ledger.transition(cancel_id, "skipped")

    done_id = ledger.create_task(pipeline_id, "/c.png", "t3-done")
    assert done_id is not None
    claimed_done = ledger.claim_next(pipeline_id)
    assert claimed_done is not None
    ledger.transition(claimed_done.id, "done")

    with pytest.raises(IllegalTransitionError):
        ledger.transition(claimed_done.id, "pending")
    with pytest.raises(IllegalTransitionError):
        ledger.transition(cancel_id, "pending")

    assert ledger.get_task(cancel_id).status == "skipped"


def test_rule_4_escalation(ledger: Ledger) -> None:
    pipeline_id, _ = _register(ledger)
    task_id = ledger.create_task(pipeline_id, "/a.png", "esc")
    assert task_id is not None
    ledger.claim_next(pipeline_id)

    step_id = "image.trim"
    branch_names = ["b1", "b2"]

    primary = ledger.start_attempt(task_id, None, 1)
    ledger.finish_attempt(primary, ok=False, last_step_id=step_id, error="primary failed")
    assert ledger.next_flag_level(task_id, step_id=step_id, branch_names=branch_names) == 1

    b1 = ledger.start_attempt(task_id, "b1", 1)
    ledger.finish_attempt(b1, ok=False, last_step_id=step_id, error="b1 failed")
    assert ledger.next_flag_level(task_id, step_id=step_id, branch_names=branch_names) == 2

    b2 = ledger.start_attempt(task_id, "b2", 1)
    ledger.finish_attempt(b2, ok=False, last_step_id=step_id, error="b2 failed")
    assert ledger.next_flag_level(task_id, step_id=step_id, branch_names=branch_names) == 3

    b2_ok = ledger.start_attempt(task_id, "b2", 2)
    ledger.finish_attempt(b2_ok, ok=True, last_step_id=step_id, error=None)
    assert ledger.next_flag_level(task_id, step_id=step_id, branch_names=branch_names) == 2


def test_exhausted_primary_groups_by_last_step_id(ledger: Ledger) -> None:
    """Earlier successful primary attempts must not mask later primary exhaustion."""
    pipeline_id, _ = _register(ledger)
    task_id = ledger.create_task(pipeline_id, "/a.png", "k1")
    assert task_id is not None

    ok1 = ledger.start_attempt(task_id, None, 1)
    ledger.finish_attempt(ok1, ok=True, last_step_id="util.noop", error=None)
    ok2 = ledger.start_attempt(task_id, None, 1)
    ledger.finish_attempt(ok2, ok=True, last_step_id="util.copy", error=None)
    fail3 = ledger.start_attempt(task_id, None, 1)
    ledger.finish_attempt(fail3, ok=False, last_step_id="util.fail", error="boom")

    assert ledger.exhausted_branches(task_id, step_id="util.fail", branch_names=[]) == 1
    assert ledger.next_flag_level(task_id, step_id="util.fail", branch_names=[]) == 1


def test_unfinished_attempt_rows_ignored(ledger: Ledger) -> None:
    """Open attempt rows without finished_at must not count toward exhaustion."""
    pipeline_id, _ = _register(ledger)
    task_id = ledger.create_task(pipeline_id, "/a.png", "open")
    assert task_id is not None

    ledger.start_attempt(task_id, None, 1)
    assert ledger.exhausted_branches(task_id, step_id="image.trim", branch_names=[]) == 0
    assert ledger.next_flag_level(task_id, step_id="image.trim", branch_names=[]) == 0


def test_rule_5_reserve_name_idempotent(ledger: Ledger) -> None:
    pipeline_id, _ = _register(ledger)
    first = ledger.reserve_name(pipeline_id, 7, "goat.png", task_id=None)
    second = ledger.reserve_name(pipeline_id, 7, "sheep.png", task_id=None)
    assert first == "goat.png"
    assert second == "goat.png"
    assert ledger.reserved_name(pipeline_id, 7) == "goat.png"


def test_rule_6_reconcile(ledger: Ledger) -> None:
    pipeline_id, _ = _register(ledger)
    stale_id = ledger.create_task(pipeline_id, "/stale.png", "stale")
    assert stale_id is not None
    ledger.claim_next(pipeline_id)

    touched_retry = ledger.reconcile(pipeline_id, stale_after=timedelta(0), policy="retry")
    assert touched_retry == 1
    assert ledger.get_task(stale_id).status == "pending"
    ledger.transition(stale_id, "skipped")

    fresh_id = ledger.create_task(pipeline_id, "/fresh.png", "fresh")
    assert fresh_id is not None
    claimed_fresh = ledger.claim_next(pipeline_id)
    assert claimed_fresh is not None
    touched_none = ledger.reconcile(pipeline_id, stale_after=timedelta(hours=1), policy="retry")
    assert touched_none == 0
    assert ledger.get_task(fresh_id).status == "processing"

    from ordine.core.playbook import loads_playbook

    fail_yaml = FIXTURE.read_text(encoding="utf-8").replace("name: minimal", "name: fail-pipeline")
    fail_playbook = loads_playbook(fail_yaml)
    pipeline_fail, _ = ledger.register_pipeline(fail_playbook, fail_yaml)
    fail_id = ledger.create_task(pipeline_fail, "/fail.png", "fail-one")
    assert fail_id is not None
    ledger.claim_next(pipeline_fail)
    touched_fail = ledger.reconcile(pipeline_fail, stale_after=timedelta(0), policy="fail")
    assert touched_fail == 1
    assert ledger.get_task(fail_id).status == "failed"
    flags = ledger.open_flags(pipeline_fail)
    assert len(flags) == 1
    assert flags[0].kind == "stale_processing"
    assert flags[0].level == 1


def test_rule_7_versioning(ledger: Ledger) -> None:
    playbook = load_playbook(FIXTURE)
    yaml_v1 = FIXTURE.read_text(encoding="utf-8")
    pipeline_id, public_v1 = ledger.register_pipeline(playbook, yaml_v1, note="v1")

    yaml_v2 = yaml_v1.replace("name: minimal", "name: minimal")
    _, public_v2 = ledger.register_pipeline(playbook, yaml_v2, note="v2")

    versions = ledger.list_versions(pipeline_id)
    assert len(versions) == 2
    current_id, current_yaml = ledger.get_current_playbook(pipeline_id)
    assert current_id == public_v2
    assert current_yaml == yaml_v2

    by_id = {v.public_id: v for v in versions}
    assert by_id[public_v2].parent_public_id == public_v1


def test_set_current_version_and_get_version_yaml(ledger: Ledger) -> None:
    playbook = load_playbook(FIXTURE)
    yaml_text = FIXTURE.read_text(encoding="utf-8")
    pipeline_id, public_v1 = ledger.register_pipeline(playbook, yaml_text, note="v1")
    _, public_v2 = ledger.register_pipeline(playbook, yaml_text, note="v2")

    ledger.set_current_version(pipeline_id, public_v1)
    current_id, _ = ledger.get_current_playbook(pipeline_id)
    assert current_id == public_v1
    assert ledger.get_version_yaml(pipeline_id, public_v2) == yaml_text

    with pytest.raises(LedgerError):
        ledger.set_current_version(pipeline_id, "pv_9999")


def test_register_pipeline_parent_and_make_current(ledger: Ledger) -> None:
    playbook = load_playbook(FIXTURE)
    yaml_text = FIXTURE.read_text(encoding="utf-8")
    pipeline_id, public_v1 = ledger.register_pipeline(playbook, yaml_text)

    _, public_v2 = ledger.register_pipeline(
        playbook,
        yaml_text,
        parent_public_id=public_v1,
        make_current=False,
    )
    current_id, _ = ledger.get_current_playbook(pipeline_id)
    assert current_id == public_v1

    versions = ledger.list_versions(pipeline_id)
    by_id = {v.public_id: v for v in versions}
    assert by_id[public_v2].parent_public_id == public_v1


def test_next_arrival_ordinal(ledger: Ledger) -> None:
    pipeline_id, _ = _register(ledger)
    assert ledger.next_arrival_ordinal(pipeline_id) == 1
    ledger.create_task(pipeline_id, "/a.png", "o1", ordinal=1)
    assert ledger.next_arrival_ordinal(pipeline_id) == 2
    ledger.create_task(pipeline_id, "/b.png", "o2", ordinal=3)
    assert ledger.next_arrival_ordinal(pipeline_id) == 4


def test_next_arrival_ordinal_concurrent(ledger: Ledger) -> None:
    """BEGIN IMMEDIATE reads are safe; callers must assign ordinals immediately (Step 6)."""
    pipeline_id, _ = _register(ledger)
    results: list[int] = []

    def worker() -> int:
        return ledger.next_arrival_ordinal(pipeline_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker) for _ in range(8)]
        for future in as_completed(futures):
            results.append(future.result())

    assert all(r >= 1 for r in results)


def test_flags_raise_and_resolve(ledger: Ledger) -> None:
    pipeline_id, _ = _register(ledger)
    task_id = ledger.create_task(pipeline_id, "/a.png", "flag")
    assert task_id is not None
    flag_id = ledger.raise_flag(
        pipeline_id,
        task_id=task_id,
        level=2,
        kind="task_failure",
        message="something broke",
    )
    open_flags = ledger.open_flags(pipeline_id, min_level=1)
    assert len(open_flags) == 1
    assert open_flags[0].id == flag_id
    ledger.resolve_flag(flag_id, "fixed manually")
    assert ledger.open_flags(pipeline_id) == []


def test_set_workdir_and_list_tasks(ledger: Ledger, tmp_path: Path) -> None:
    pipeline_id, _ = _register(ledger)
    task_id = ledger.create_task(pipeline_id, "/a.png", "wd")
    assert task_id is not None
    workdir = tmp_path / "work" / "task-1"
    ledger.set_workdir(task_id, workdir)
    task = ledger.get_task(task_id)
    assert task.workdir == str(workdir)
    listed = ledger.list_tasks(pipeline_id, status="pending")
    assert len(listed) == 1
    assert ledger.counts(pipeline_id)["pending"] == 1
