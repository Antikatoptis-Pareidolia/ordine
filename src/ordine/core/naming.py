"""Ledger-backed naming service for the pipeline runner.

Owns the NamingService implementation over name reservations. Must never execute steps.
"""

from __future__ import annotations

import logging

from ordine.core.ledger import Ledger

logger = logging.getLogger(__name__)


class LedgerNamingService:
    """Step 4 NamingService backed by the ledger name-reservation table."""

    def __init__(self, ledger: Ledger, pipeline_id: int, task_id: int) -> None:
        self._ledger = ledger
        self._pipeline_id = pipeline_id
        self._task_id = task_id

    def resolve(self, ordinal: int) -> str | None:
        return self._ledger.reserved_name(self._pipeline_id, ordinal)

    def bind(self, ordinal: int, name: str) -> str:
        existing = self._ledger.reserved_name(self._pipeline_id, ordinal)
        if existing is not None and existing != name:
            logger.warning(
                "ordinal %s already reserved as %r; ignoring new name %r",
                ordinal,
                existing,
                name,
            )
        return self._ledger.reserve_name(self._pipeline_id, ordinal, name, self._task_id)
