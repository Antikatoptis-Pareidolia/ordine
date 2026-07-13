"""Dry-run lab web UI tests."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from conveyor.core.config import load_config
from conveyor.core.ledger import Ledger
from conveyor.core.playbook import loads_playbook
from conveyor.web.app import create_app
from conveyor.web.routes.lab import LabSessionStore
from tests.test_branch_regression import _parse_form_fields_from_html
from tests.test_web import POST_HEADERS, _write_config

FIVE_STEP_YAML = """version: 1
name: lab-masterplan
trigger:
  type: manual
  path: ~/input
steps:
  - util.noop
  - util.copy
  - util.fail:
      message: step 3 fails
      times: -1
  - util.noop
  - util.noop
"""


@pytest.fixture
def lab_client(tmp_path: Path) -> tuple[TestClient, Ledger, Path]:
    config_file = _write_config(tmp_path)
    config = load_config(config_file)
    client = TestClient(create_app(config))
    ledger = client.app.state.ledger
    return client, ledger, tmp_path


def _register_pipeline(ledger: Ledger, yaml_text: str) -> tuple[int, str]:
    playbook = loads_playbook(yaml_text)
    return ledger.register_pipeline(playbook, yaml_text, note="lab seed")


def _start_lab(
    client: TestClient,
    pipeline_id: int,
    sample_dir: Path,
) -> str:
    response = client.post(
        f"/pipelines/{pipeline_id}/lab",
        data={
            "sample_dir": str(sample_dir),
            "glob": "*",
            "max_samples": "5",
        },
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    return location.rsplit("/", 1)[-1]


def test_masterplan_fix_from_here_and_resume(lab_client: tuple[TestClient, Ledger, Path]) -> None:
    client, ledger, tmp_path = lab_client
    samples = tmp_path / "samples"
    samples.mkdir()
    sample = samples / "item.txt"
    sample.write_text("payload", encoding="utf-8")
    pipeline_id, v1 = _register_pipeline(ledger, FIVE_STEP_YAML)
    _, v2_current = ledger.register_pipeline(
        loads_playbook(FIVE_STEP_YAML),
        FIVE_STEP_YAML,
        note="noop bump",
    )
    assert v2_current != v1
    current_before, _ = ledger.get_current_playbook(pipeline_id)
    assert current_before == v2_current

    response = client.post(
        f"/pipelines/{pipeline_id}/lab",
        data={
            "sample_dir": str(samples),
            "glob": "*",
            "max_samples": "5",
            "version_id": v1,
        },
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert response.status_code == 303
    sid = response.headers["location"].rsplit("/", 1)[-1]
    client.post(f"/lab/{sid}/tasks/0/next", headers=POST_HEADERS, follow_redirects=False)
    client.post(f"/lab/{sid}/tasks/0/next", headers=POST_HEADERS, follow_redirects=False)
    failed = client.post(f"/lab/{sid}/tasks/0/next", headers=POST_HEADERS, follow_redirects=False)
    assert failed.status_code == 303
    session_page = client.get(f"/lab/{sid}")
    assert "step 3 fails" in session_page.text
    assert "Fix from here" in session_page.text
    assert "anchor=steps-2" in session_page.text
    assert f"from_lab={sid}" in session_page.text
    assert f"version={v1}" in session_page.text

    edit = client.get(f"/pipelines/{pipeline_id}/edit?version={v1}&anchor=steps-2&from_lab={sid}")
    assert edit.status_code == 200
    assert 'id="steps-2"' in edit.text
    assert 'name="from_lab" value="' + sid + '"' in edit.text
    form = _parse_form_fields_from_html(edit.text)
    assert form.get("from_lab") == sid
    assert form.get("base_version") == v1
    form["steps-2-id"] = "util.noop"
    form["steps-2-params"] = ""
    form["tab"] = "form"
    form["note"] = "lab fix step 3"
    saved = client.post(
        f"/pipelines/{pipeline_id}/versions",
        data=form,
        headers=POST_HEADERS,
    )
    assert saved.status_code == 200
    assert "branch of" in saved.text
    assert "Resume lab" in saved.text
    assert "lab fix step 3" in saved.text
    v2, _yaml_v2 = ledger.get_current_playbook(pipeline_id)
    assert v2 == v2_current
    versions = ledger.list_versions(pipeline_id)
    saved_version = next(v for v in versions if v.note == "lab fix step 3")
    assert saved_version.public_id != v1

    resume = client.post(
        f"/lab/{sid}/resume",
        data={"version_id": saved_version.public_id},
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert resume.status_code == 303
    new_sid = resume.headers["location"].rsplit("/", 1)[-1].split("?")[0]
    resumed = client.get(f"/lab/{new_sid}")
    assert resumed.status_code == 200
    client.post(f"/lab/{new_sid}/run-all", headers=POST_HEADERS, follow_redirects=False)
    done = client.get(f"/lab/{new_sid}")
    assert "replayed" in done.text
    assert "(done)" in done.text

    v1_yaml = loads_playbook(ledger.get_version_yaml(pipeline_id, v1))
    saved_playbook = loads_playbook(ledger.get_version_yaml(pipeline_id, saved_version.public_id))
    assert v1_yaml.steps[0].id == saved_playbook.steps[0].id
    assert v1_yaml.steps[1].id == saved_playbook.steps[1].id
    assert saved_playbook.steps[2].id == "util.noop"


def test_lab_resume_when_session_uses_current_version(
    lab_client: tuple[TestClient, Ledger, Path],
) -> None:
    """Resume banner must appear when lab rehearses the current version (no redirect on save)."""
    client, ledger, tmp_path = lab_client
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "item.txt").write_text("payload", encoding="utf-8")
    pipeline_id, v1 = _register_pipeline(ledger, FIVE_STEP_YAML)
    current, _ = ledger.get_current_playbook(pipeline_id)
    assert current == v1

    sid = _start_lab(client, pipeline_id, samples)
    client.post(f"/lab/{sid}/tasks/0/next", headers=POST_HEADERS, follow_redirects=False)
    client.post(f"/lab/{sid}/tasks/0/next", headers=POST_HEADERS, follow_redirects=False)
    client.post(f"/lab/{sid}/tasks/0/next", headers=POST_HEADERS, follow_redirects=False)

    edit = client.get(f"/pipelines/{pipeline_id}/edit?version={v1}&anchor=steps-2&from_lab={sid}")
    form = _parse_form_fields_from_html(edit.text)
    assert form.get("from_lab") == sid
    form["steps-2-id"] = "util.noop"
    form["steps-2-params"] = ""
    form["tab"] = "form"
    form["note"] = "fix on current"
    saved = client.post(
        f"/pipelines/{pipeline_id}/versions",
        data=form,
        headers=POST_HEADERS,
    )
    assert saved.status_code == 200
    assert "Resume lab" in saved.text
    assert "fix on current" in saved.text
    assert current == ledger.get_current_playbook(pipeline_id)[0]


def test_lab_setup_shows_output_redirections(
    lab_client: tuple[TestClient, Ledger, Path],
) -> None:
    client, ledger, tmp_path = lab_client
    prod_out = tmp_path / "prod-out"
    yaml_text = f"""version: 1
name: lab-export-redir
trigger: {{type: manual, path: ~/in}}
steps:
  - id: image.export
    params:
      dest: {prod_out}
"""
    pipeline_id, _ = ledger.register_pipeline(loads_playbook(yaml_text), yaml_text)
    setup = client.get(f"/pipelines/{pipeline_id}/lab")
    assert setup.status_code == 200
    assert "Output redirections" in setup.text
    assert str(prod_out) in setup.text
    assert "outputs/" in setup.text
    assert "→" in setup.text


def test_editor_anchor_and_from_lab_banner(
    lab_client: tuple[TestClient, Ledger, Path],
) -> None:
    client, ledger, tmp_path = lab_client
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "item.txt").write_text("payload", encoding="utf-8")
    pipeline_id, version_id = _register_pipeline(ledger, FIVE_STEP_YAML)
    sid = _start_lab(client, pipeline_id, samples)
    client.post(f"/lab/{sid}/tasks/0/next", headers=POST_HEADERS, follow_redirects=False)
    client.post(f"/lab/{sid}/tasks/0/next", headers=POST_HEADERS, follow_redirects=False)
    client.post(f"/lab/{sid}/tasks/0/next", headers=POST_HEADERS, follow_redirects=False)

    edit = client.get(
        f"/pipelines/{pipeline_id}/edit?version={version_id}&anchor=steps-2&from_lab={sid}"
    )
    assert edit.status_code == 200
    assert 'id="steps-2"' in edit.text
    assert (
        'class="step-row lab-anchor"' in edit.text.replace("\n", " ") or "lab-anchor" in edit.text
    )
    assert "You are fixing this step" in edit.text
    assert f'name="from_lab" value="{sid}"' in edit.text


def test_lab_artifact_route_blocks_traversal(lab_client: tuple[TestClient, Ledger, Path]) -> None:
    client, ledger, tmp_path = lab_client
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "one.txt").write_text("1", encoding="utf-8")
    pipeline_id, _ = _register_pipeline(
        ledger,
        """version: 1
name: traversal
trigger: {type: manual, path: ~/in}
steps:
  - util.noop
""",
    )
    sid = _start_lab(client, pipeline_id, samples)
    blocked = client.get(f"/lab/{sid}/artifacts/../../samples/one.txt")
    assert blocked.status_code == 404


def test_lab_nav_links_on_editor_and_versions(lab_client: tuple[TestClient, Ledger, Path]) -> None:
    client, ledger, _tmp_path = lab_client
    pipeline_id, _ = _register_pipeline(
        ledger,
        """version: 1
name: nav-links
trigger: {type: manual, path: ~/in}
steps:
  - util.noop
""",
    )
    editor = client.get(f"/pipelines/{pipeline_id}/edit")
    assert editor.status_code == 200
    assert f'href="/pipelines/{pipeline_id}/lab"' in editor.text
    assert "Dry-run lab" in editor.text

    versions = client.get(f"/pipelines/{pipeline_id}/versions")
    assert versions.status_code == 200
    assert f'href="/pipelines/{pipeline_id}/lab"' in versions.text
    assert "Dry-run lab" in versions.text


def test_unknown_lab_session_returns_404(lab_client: tuple[TestClient, Ledger, Path]) -> None:
    client, _, _ = lab_client
    missing = "lab_missing_session"
    assert client.get(f"/lab/{missing}").status_code == 404
    assert (
        client.post(
            f"/lab/{missing}/tasks/0/next",
            headers=POST_HEADERS,
            follow_redirects=False,
        ).status_code
        == 404
    )
    assert client.get(f"/lab/{missing}/artifacts/foo.txt").status_code == 404


def test_closed_lab_session_returns_404(lab_client: tuple[TestClient, Ledger, Path]) -> None:
    client, ledger, tmp_path = lab_client
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "a.txt").write_text("a", encoding="utf-8")
    pipeline_id, _ = _register_pipeline(
        ledger,
        """version: 1
name: closed-session
trigger: {type: manual, path: ~/in}
steps:
  - util.noop
""",
    )
    sid = _start_lab(client, pipeline_id, samples)
    closed = client.post(
        f"/lab/{sid}/close",
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert closed.status_code == 303
    assert client.get(f"/lab/{sid}").status_code == 404
    assert (
        client.post(
            f"/lab/{sid}/tasks/0/next",
            headers=POST_HEADERS,
            follow_redirects=False,
        ).status_code
        == 404
    )


def test_illegal_lab_action_shows_flash(lab_client: tuple[TestClient, Ledger, Path]) -> None:
    client, ledger, tmp_path = lab_client
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "item.txt").write_text("payload", encoding="utf-8")
    pipeline_id, _ = _register_pipeline(ledger, FIVE_STEP_YAML)
    sid = _start_lab(client, pipeline_id, samples)
    client.post(f"/lab/{sid}/tasks/0/next", headers=POST_HEADERS, follow_redirects=False)
    client.post(f"/lab/{sid}/tasks/0/next", headers=POST_HEADERS, follow_redirects=False)
    client.post(f"/lab/{sid}/tasks/0/next", headers=POST_HEADERS, follow_redirects=False)

    illegal = client.post(
        f"/lab/{sid}/tasks/0/next",
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert illegal.status_code == 303
    assert "flash=" in illegal.headers["location"]
    assert "paused" in illegal.headers["location"].lower()

    session_page = client.get(illegal.headers["location"])
    assert session_page.status_code == 200
    assert "paused on failure" in session_page.text
    assert "flash-error" in session_page.text


def test_retry_when_not_paused_shows_flash(lab_client: tuple[TestClient, Ledger, Path]) -> None:
    client, ledger, tmp_path = lab_client
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "item.txt").write_text("payload", encoding="utf-8")
    pipeline_id, _ = _register_pipeline(ledger, FIVE_STEP_YAML)
    sid = _start_lab(client, pipeline_id, samples)

    bad_retry = client.post(
        f"/lab/{sid}/tasks/0/retry",
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert bad_retry.status_code == 303
    assert "retry is only available" in unquote(bad_retry.headers["location"])


def test_lab_create_invalid_sample_shows_flash(lab_client: tuple[TestClient, Ledger, Path]) -> None:
    client, ledger, tmp_path = lab_client
    pipeline_id, _ = _register_pipeline(
        ledger,
        """version: 1
name: bad-sample
trigger: {type: manual, path: ~/in}
steps:
  - util.noop
""",
    )
    missing = tmp_path / "no-such-samples"
    response = client.post(
        f"/pipelines/{pipeline_id}/lab",
        data={
            "sample_dir": str(missing),
            "glob": "*",
            "max_samples": "5",
        },
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/pipelines/{pipeline_id}/lab")
    assert "flash=" in response.headers["location"]
    setup = client.get(response.headers["location"])
    assert setup.status_code == 200
    assert "flash-error" in setup.text


def test_lab_resume_requires_version_id(lab_client: tuple[TestClient, Ledger, Path]) -> None:
    client, ledger, tmp_path = lab_client
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "a.txt").write_text("a", encoding="utf-8")
    pipeline_id, _ = _register_pipeline(
        ledger,
        """version: 1
name: resume-version
trigger: {type: manual, path: ~/in}
steps:
  - util.noop
""",
    )
    sid = _start_lab(client, pipeline_id, samples)
    missing_version = client.post(
        f"/lab/{sid}/resume",
        data={},
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert missing_version.status_code == 400


def test_lab_artifact_serves_sandbox_file(lab_client: tuple[TestClient, Ledger, Path]) -> None:
    client, ledger, tmp_path = lab_client
    samples = tmp_path / "samples"
    samples.mkdir()
    sample = samples / "item.txt"
    sample.write_text("payload", encoding="utf-8")
    pipeline_id, _ = _register_pipeline(
        ledger,
        """version: 1
name: artifact-serve
trigger: {type: manual, path: ~/in}
steps:
  - util.copy
""",
    )
    sid = _start_lab(client, pipeline_id, samples)
    client.post(f"/lab/{sid}/tasks/0/next", headers=POST_HEADERS, follow_redirects=False)
    session = client.get(f"/lab/{sid}")
    assert session.status_code == 200
    assert "/lab/" in session.text and "/artifacts/" in session.text

    artifact_href = session.text.split(f"/lab/{sid}/artifacts/", 1)[1].split('"', 1)[0]
    served = client.get(f"/lab/{sid}/artifacts/{artifact_href}")
    assert served.status_code == 200
    assert served.content


def test_second_lab_session_closes_first(lab_client: tuple[TestClient, Ledger, Path]) -> None:
    client, ledger, tmp_path = lab_client
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "a.txt").write_text("a", encoding="utf-8")
    pipeline_id, _ = _register_pipeline(
        ledger,
        """version: 1
name: one-active
trigger: {type: manual, path: ~/in}
steps:
  - util.noop
""",
    )
    sid1 = _start_lab(client, pipeline_id, samples)
    sid2 = _start_lab(client, pipeline_id, samples)
    assert sid1 != sid2
    assert client.get(f"/lab/{sid1}").status_code == 404
    assert client.get(f"/lab/{sid2}").status_code == 200


def test_shutdown_closes_lab_sessions(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path)
    config = load_config(config_file)
    app = create_app(config)
    store: LabSessionStore = app.state.lab_sessions
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "a.txt").write_text("a", encoding="utf-8")
    ledger: Ledger = app.state.ledger
    pipeline_id, _ = ledger.register_pipeline(
        loads_playbook(
            """version: 1
name: shutdown-lab
trigger: {type: manual, path: ~/in}
steps:
  - util.noop
"""
        ),
        "version: 1\nname: shutdown-lab\ntrigger: {type: manual, path: ~/in}\nsteps:\n  - util.noop\n",
    )
    from conveyor.core.dryrun import DryRunSession

    session = DryRunSession.create(
        playbook=loads_playbook(
            """version: 1
name: shutdown-lab
trigger: {type: manual, path: ~/in}
steps:
  - util.noop
"""
        ),
        version_public_id="pv_test",
        sample_dir=samples,
        glob="*",
        registry=app.state.registry,
        engines=app.state.engines,
        sandbox_root=tmp_path / "workdirs" / "lab",
        yaml_text="version: 1\nname: shutdown-lab\ntrigger: {type: manual, path: ~/in}\nsteps:\n  - util.noop\n",
    )
    sandbox = session.sandbox
    from conveyor.web.routes.lab import LabSessionRecord

    store.put(LabSessionRecord(sid=session.session_id, pipeline_id=pipeline_id, session=session))
    assert sandbox.exists()
    store.close_all()
    assert not sandbox.exists()
