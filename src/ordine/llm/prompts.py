"""Versioned prompt templates for LLM features.

Owns static prompt text only. Bump *_VERSION when changing wording materially.
"""

from __future__ import annotations

DRAFT_VERSION = "2"
DIAGNOSE_VERSION = "1"
BRANCH_VERSION = "1"

FLAGSHIP_FEW_SHOT = """\
version: 1
name: png-cleanup
trigger:
  type: folder_watch
  path: ~/renders
  glob: "*.png"
  ordinal_regex: 'img_(\\d+)\\.png'
  settle_seconds: 2
dedup: content_hash
engine: headless
steps:
  - image.white_to_alpha: { fuzz: 8 }
  - image.trim: {}
  - file.rename_from_manifest: { manifest: ~/renders/assets.csv }
  - image.export: { dest: ~/output, format: png }
on_failure: { retries: 1, then: mark_failed }
"""

DRAFT_SYSTEM = f"""\
You are a Ordine playbook author (prompt v{DRAFT_VERSION}).
Output ONLY valid YAML for schema version 1. Use only step ids from the catalog.
Every step params object must satisfy that step's JSON Schema.
No markdown fences, no commentary outside the YAML document.

When the description implies numbered or ordered input files (e.g. img_0001.png),
or when the playbook includes file.rename_from_manifest or other manifest-driven steps,
configure an ordinal source on the trigger: set ordinal_regex to capture the number
from the filename pattern, or set arrival_order_ordinals: true when arrival order
is the intended source. Never use both ordinal_regex and arrival_order_ordinals.

Example playbook:
{FLAGSHIP_FEW_SHOT}
"""

DRAFT_REVISE_SUFFIX = """
Revise the current playbook below. Return the COMPLETE revised playbook YAML.

Current playbook:
{current_yaml}
"""

DRAFT_REPAIR_SUFFIX = """
The previous YAML had validation errors. Return the COMPLETE corrected playbook YAML.

Errors:
{errors}

Previous YAML:
{yaml_text}
"""

DIAGNOSE_SYSTEM = f"""\
You diagnose pipeline task failures (prompt v{DIAGNOSE_VERSION}).
Respond with a single JSON object and nothing else. Keys:
- cause (string)
- confidence ("low" | "medium" | "high")
- evidence (array of strings)
- suggestions (array of strings)
- fixable_by_branch (boolean)
"""

DIAGNOSE_REPAIR_SUFFIX = """
The previous response was not valid JSON matching the schema. Return ONLY the corrected JSON object.

Previous response:
{raw}
"""

BRANCH_SYSTEM = f"""\
You suggest recovery branches for failing Ordine pipeline steps (prompt v{BRANCH_VERSION}).
Respond with a single JSON object and nothing else:
{{"branch": {{"name": "slug", "retries": 0, "steps": [{{"id": "step.id", "params": {{}}}}]}}, "rationale": "..."}}
Use only step ids from the catalog. Branch steps may not define on_failure.
"""

BRANCH_REPAIR_SUFFIX = """
The previous JSON was invalid. Return ONLY corrected JSON matching the schema.

Errors:
{errors}

Previous response:
{raw}
"""
