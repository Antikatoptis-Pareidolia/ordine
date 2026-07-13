"""LLM-powered product features (draft, diagnose, branch suggestion).

Owns feature orchestration atop the Step 12 connector. Must never auto-save or auto-run pipelines.
"""

from conveyor.llm.features.branches import BranchSuggestion, apply_branch, suggest_branch
from conveyor.llm.features.diagnosis import Diagnosis, diagnose, load_diagnosis, save_diagnosis
from conveyor.llm.features.drafting import DraftResult, draft_playbook

__all__ = [
    "BranchSuggestion",
    "Diagnosis",
    "DraftResult",
    "apply_branch",
    "diagnose",
    "draft_playbook",
    "load_diagnosis",
    "save_diagnosis",
    "suggest_branch",
]
