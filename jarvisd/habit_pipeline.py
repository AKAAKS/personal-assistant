"""Habit scan pipeline: mine -> phrase once -> queue.

Orchestrates `habit_miner.mine_candidates` and `ProposalStore` with an
injected `phraser` callable. This module never calls an LLM itself — the
phraser is supplied by the caller (bootstrap wires it to a Haiku call in
production; tests use a spy). This keeps the cost-safety guarantee simple to
verify: a re-scan of already-open habits calls the phraser zero times.
"""

import logging

from jarvisd.habit_miner import mine_candidates

logger = logging.getLogger(__name__)


def run_habit_scan(audit_path, proposal_store, phraser, now=None, **mine_kwargs) -> int:
    """Mine `audit_path` for habits and queue new proposals.

    For each mined candidate: skip (no phraser call) if a proposal with the
    same signature was already proposed in any status (pending/accepted/
    dismissed). Otherwise, call `phraser(candidate)` exactly once to get
    natural-language proposal text, then store it via `proposal_store.add(...)`.

    Never raises: any exception while phrasing or storing a single candidate
    is logged and that candidate is skipped; the rest of the scan continues.

    Args:
        audit_path: Path to the audit.jsonl file to mine.
        proposal_store: A ProposalStore instance (or compatible:
            already_proposed, add).
        phraser: callable(candidate) -> str. Injected so this module never
            makes the LLM call itself.
        now: Optional reference "current time", forwarded to
            mine_candidates and used as the `now` timestamp for `add`.
        **mine_kwargs: Forwarded to mine_candidates (e.g. min_occurrences,
            window_days, hour_tolerance).

    Returns:
        int: count of NEW proposals actually stored (add() returned a real
            id). Candidates skipped by already_proposed, that failed to
            phrase/add, or that hit a duplicate signature at add() time do
            not count.
    """
    try:
        candidates = mine_candidates(audit_path, now=now, **mine_kwargs)
    except Exception:
        logger.exception("habit scan: mine_candidates failed for %s", audit_path)
        return 0

    stored = 0
    for candidate in candidates:
        try:
            if proposal_store.already_proposed(candidate.signature):
                continue
            text = phraser(candidate)
            new_id = proposal_store.add(
                candidate.signature, candidate.kind, candidate.payload, text, now
            )
            if new_id is not None:
                stored += 1
        except Exception:
            logger.exception(
                "habit scan: failed to phrase/store candidate %s", candidate.signature
            )
            continue

    return stored
