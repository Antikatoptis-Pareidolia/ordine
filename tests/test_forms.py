"""Tests for editor form ⟷ playbook conversion."""

from __future__ import annotations

from pathlib import Path

import pytest

from conveyor.core.playbook import load_playbook
from conveyor.web.forms import FormConversionError, form_to_dict, playbook_to_form

FLAGSHIP = Path(__file__).parent / "fixtures" / "playbooks" / "valid" / "v02_flagship.yml"
BRANCHY = Path(__file__).parent / "fixtures" / "playbooks" / "valid" / "v04_pipeline_branches.yml"


def _round_trip_form(playbook_path: Path) -> None:
    playbook = load_playbook(playbook_path)
    form = playbook_to_form(playbook)
    restored = form_to_dict(form)
    again = playbook_to_form(load_playbook_from_dict(restored))
    assert again == form


def load_playbook_from_dict(data: dict) -> object:
    from conveyor.core.playbook import Playbook

    return Playbook.model_validate(data)


def test_flagship_form_round_trip() -> None:
    _round_trip_form(FLAGSHIP)


def test_branchy_form_round_trip() -> None:
    _round_trip_form(BRANCHY)


def test_bad_params_yaml_surfaces_field_error() -> None:
    form = playbook_to_form(load_playbook(FLAGSHIP))
    form["steps-0-params"] = "not: valid: yaml: ["
    with pytest.raises(FormConversionError) as exc_info:
        form_to_dict(form)
    assert any(error.path == "steps.0.params" for error in exc_info.value.errors)


def test_index_gaps_tolerated() -> None:
    form = playbook_to_form(load_playbook(FLAGSHIP))
    form["steps-5-id"] = form.pop("steps-3-id")
    form["steps-5-params"] = form.pop("steps-3-params")
    data = form_to_dict(form)
    assert len(data["steps"]) == 4
    assert data["steps"][-1]["id"] == "image.export"


def test_checkbox_absence_means_false() -> None:
    form = playbook_to_form(load_playbook(BRANCHY))
    assert "onfail-enabled" in form
    del form["onfail-enabled"]
    data = form_to_dict(form)
    assert "on_failure" not in data


def test_step_on_failure_round_trip() -> None:
    playbook = load_playbook(
        Path(__file__).parent / "fixtures" / "playbooks" / "valid" / "v03_step_on_failure.yml"
    )
    form = playbook_to_form(playbook)
    assert form.get("steps-0-onfail-enabled") == "on"
    restored = form_to_dict(form)
    assert restored["steps"][0]["on_failure"]["retries"] == 1
