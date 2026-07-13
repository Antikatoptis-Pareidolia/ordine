"""Human hints for known ledger flag kinds in the web UI."""

from __future__ import annotations

FLAG_KIND_HINTS: dict[str, str] = {
    "manifest_exhausted": "Add rows to the manifest, then Retry.",
    "manifest_unreadable": "Fix the manifest file on disk, then rescan.",
    "generation_refused": "Edit the prompt in the manifest, then Retry.",
    "corrupt_input": "Replace or repair the source image, then Retry.",
    "runner_error": "Inspect step logs; fix the playbook or environment, then Retry.",
    "task_skipped": "Review skip reason; adjust inputs or playbook if unintended.",
    "task_failure": "Inspect the failure message and step logs, then Retry.",
}


def hint_for_kind(kind: str) -> str | None:
    """Return a one-line operator hint for *kind*, if known."""
    return FLAG_KIND_HINTS.get(kind)
