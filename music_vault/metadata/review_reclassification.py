"""Resumable, network-free reclassification of persisted review evidence."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Mapping

from .intelligence_schema import MetadataIntelligenceJobStore
from .review_policy import ReviewDecision, ReviewOutcome, classify_stored_review_evidence
from .service import AutomaticMetadataField, MetadataService


@dataclass(frozen=True)
class ReviewReclassificationReport:
    scanned: int
    changed: int
    applied: int
    applied_with_gaps: int
    source_fallback: int
    needs_review: int
    safe_fields_applied: int
    last_item_id: int
    remaining: int
    dry_run: bool
    outcome_counts: Mapping[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "changed": self.changed,
            "applied": self.applied,
            "applied_with_gaps": self.applied_with_gaps,
            "source_fallback": self.source_fallback,
            "needs_review": self.needs_review,
            "safe_fields_applied": self.safe_fields_applied,
            "last_item_id": self.last_item_id,
            "remaining": self.remaining,
            "dry_run": self.dry_run,
            "outcome_counts": dict(self.outcome_counts),
        }


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


class MetadataReviewReclassifier:
    """Re-evaluate saved review rows without constructing any provider client."""

    def __init__(self, database: object) -> None:
        self.database = database
        self.conn: sqlite3.Connection = getattr(database, "conn", database)
        if not isinstance(self.conn, sqlite3.Connection):
            raise TypeError("MetadataReviewReclassifier requires a SQLite database.")
        self.conn.row_factory = sqlite3.Row

    @contextmanager
    def _transaction(self):
        if not self.conn.in_transaction:
            with self.conn:
                yield
            return
        name = f"review_reclassification_{uuid.uuid4().hex}"
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            self.conn.execute(f"RELEASE SAVEPOINT {name}")

    @staticmethod
    def decision_for_row(row: sqlite3.Row) -> ReviewDecision:
        return classify_stored_review_evidence(
            parsed_hints=row["parsed_hints"],
            field_proposal=row["field_proposal"],
            field_confidence=row["field_confidence"],
            provider_agreement=row["provider_agreement"],
            review_reason=row["review_reason"],
        )

    def _safe_automatic_fields(
        self,
        row: sqlite3.Row,
        decision: ReviewDecision,
        effective_states: Mapping[str, tuple[object, bool, bool]],
    ) -> dict[str, AutomaticMetadataField]:
        proposal = _mapping(row["field_proposal"])
        confidence = _mapping(row["field_confidence"])
        sources = _mapping(proposal.get("_sources"))
        discogs = _mapping(proposal.get("_discogs"))
        musicbrainz = _mapping(proposal.get("_musicbrainz"))
        result: dict[str, AutomaticMetadataField] = {}
        for field_name in decision.safe_critical_fields:
            value = proposal.get(field_name)
            # Reclassification is intentionally gap-only.  Consult current DB
            # state, not a potentially stale proposal snapshot, and never
            # replace a non-empty/manual/locked critical identity.
            effective_value, is_manual, is_locked = effective_states.get(
                field_name, (None, False, False)
            )
            if (
                value in (None, "")
                or effective_value not in (None, "")
                or is_manual
                or is_locked
            ):
                continue
            source = str(sources.get(field_name) or "stored_metadata_evidence")
            reference = None
            if source == "discogs":
                reference = discogs.get("provider_reference")
            elif source == "musicbrainz":
                reference = musicbrainz.get("provider_reference")
            try:
                score = float(confidence.get(field_name, 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if score < 85.0:
                continue
            result[field_name] = AutomaticMetadataField(
                value=value,
                confidence=score,
                provider=source,
                provider_reference=str(reference) if reference else None,
                conflict=False,
            )
        return result

    def reclassify(
        self,
        *,
        job_id: str | None = None,
        after_item_id: int = 0,
        limit: int = 250,
        apply: bool = False,
    ) -> ReviewReclassificationReport:
        if limit < 1 or limit > 5000:
            raise ValueError("Reclassification limit must be between 1 and 5000.")
        clauses = ["state IN ('review','ready')", "id > ?"]
        parameters: list[object] = [int(after_item_id)]
        if job_id is not None:
            clauses.append("job_id=?")
            parameters.append(str(job_id))
        rows = list(
            self.conn.execute(
                "SELECT * FROM metadata_intelligence_items WHERE "
                + " AND ".join(clauses)
                + " ORDER BY id LIMIT ?",
                (*parameters, int(limit)),
            ).fetchall()
        )
        outcomes = {outcome.value: 0 for outcome in ReviewOutcome}
        decisions: list[tuple[sqlite3.Row, ReviewDecision]] = []
        for row in rows:
            decision = self.decision_for_row(row)
            decisions.append((row, decision))
            outcomes[decision.outcome.value] += 1

        effective_by_track: dict[int, dict[str, tuple[object, bool, bool]]] = {
            int(row["track_id"]): {} for row in rows
        }
        if rows:
            track_ids = tuple(effective_by_track)
            placeholders = ",".join("?" for _ in track_ids)
            materialized = {
                int(row["id"]): {
                    "title": (row["title"], False, False),
                    "artist": (row["artist"], False, False),
                    "version_type": (row["version_type"], False, False),
                }
                for row in self.conn.execute(
                    f"SELECT id,title,artist,version_type FROM tracks WHERE id IN ({placeholders})",
                    track_ids,
                ).fetchall()
            }
            effective_by_track.update(materialized)
            for field_row in self.conn.execute(
                "SELECT track_id,field_name,value,is_manual,is_locked "
                "FROM track_metadata_fields WHERE track_id IN ("
                + placeholders
                + ") AND field_name IN ('title','artist','version_type')",
                track_ids,
            ).fetchall():
                effective_by_track.setdefault(int(field_row["track_id"]), {})[
                    str(field_row["field_name"])
                ] = (
                    field_row["value"],
                    bool(field_row["is_manual"]),
                    bool(field_row["is_locked"]),
                )

        changed = 0
        safe_fields_applied = 0
        touched_jobs: set[str] = set()
        if apply and decisions:
            if not hasattr(self.database, "get_track"):
                raise TypeError("Applying reclassification requires MusicVaultDB.")
            metadata = MetadataService(self.database)
            with self._transaction():
                for row, decision in decisions:
                    automatic = self._safe_automatic_fields(
                        row,
                        decision,
                        effective_by_track.get(int(row["track_id"]), {}),
                    )
                    history_group = None
                    if automatic:
                        result = metadata.apply_automatic_fields(
                            int(row["track_id"]),
                            automatic,
                            actor="metadata_review_reclassification",
                            reason="stored_high_confidence_review_evidence",
                            commit=False,
                        )
                        history_group = result.change_group_id
                        safe_fields_applied += len(result.changed_fields)
                    new_reason = decision.reason
                    state_or_reason_changed = (
                        str(row["state"]) != decision.outcome.value
                        or str(row["review_reason"] or "") != str(new_reason or "")
                    )
                    if not state_or_reason_changed and history_group is None:
                        continue
                    update = self.conn.execute(
                        """
                        UPDATE metadata_intelligence_items
                        SET state=?, review_reason=?,
                            applied_history_group=COALESCE(?, applied_history_group),
                            completed_at=COALESCE(completed_at, CURRENT_TIMESTAMP),
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND state IN ('review','ready')
                        """,
                        (
                            decision.outcome.value,
                            new_reason,
                            history_group,
                            int(row["id"]),
                        ),
                    )
                    changed += int(update.rowcount == 1 and state_or_reason_changed)
                    touched_jobs.add(str(row["job_id"]))
                store = MetadataIntelligenceJobStore(self.database)
                for touched_job_id in sorted(touched_jobs):
                    store._refresh_job(touched_job_id)

        last_item_id = int(rows[-1]["id"]) if rows else int(after_item_id)
        remaining_clauses = ["state IN ('review','ready')", "id > ?"]
        remaining_parameters: list[object] = [last_item_id]
        if job_id is not None:
            remaining_clauses.append("job_id=?")
            remaining_parameters.append(str(job_id))
        remaining = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items WHERE "
                + " AND ".join(remaining_clauses),
                tuple(remaining_parameters),
            ).fetchone()[0]
        )
        return ReviewReclassificationReport(
            scanned=len(rows),
            changed=changed,
            applied=outcomes[ReviewOutcome.APPLIED.value],
            applied_with_gaps=outcomes[ReviewOutcome.APPLIED_WITH_GAPS.value],
            source_fallback=outcomes[ReviewOutcome.SOURCE_FALLBACK.value],
            needs_review=outcomes[ReviewOutcome.NEEDS_REVIEW.value],
            safe_fields_applied=safe_fields_applied,
            last_item_id=last_item_id,
            remaining=remaining,
            dry_run=not apply,
            outcome_counts=outcomes,
        )


def reclassify_stored_review_items(
    database: object,
    *,
    job_id: str | None = None,
    batch_size: int = 250,
    apply: bool = True,
) -> ReviewReclassificationReport:
    """Run bounded batches over all saved review rows, returning aggregates only.

    The function is safe to call again: terminal outcomes are not selected and
    unchanged critical-review rows do not receive timestamp-only writes.  No
    provider client is imported or constructed by this module.
    """

    reclassifier = MetadataReviewReclassifier(database)
    cursor = 0
    reports: list[ReviewReclassificationReport] = []
    while True:
        report = reclassifier.reclassify(
            job_id=job_id,
            after_item_id=cursor,
            limit=batch_size,
            apply=apply,
        )
        reports.append(report)
        if report.scanned == 0 or report.remaining == 0:
            break
        cursor = report.last_item_id
    outcome_counts = {outcome.value: 0 for outcome in ReviewOutcome}
    for report in reports:
        for state, count in report.outcome_counts.items():
            outcome_counts[state] = outcome_counts.get(state, 0) + int(count)
    return ReviewReclassificationReport(
        scanned=sum(report.scanned for report in reports),
        changed=sum(report.changed for report in reports),
        applied=outcome_counts[ReviewOutcome.APPLIED.value],
        applied_with_gaps=outcome_counts[ReviewOutcome.APPLIED_WITH_GAPS.value],
        source_fallback=outcome_counts[ReviewOutcome.SOURCE_FALLBACK.value],
        needs_review=outcome_counts[ReviewOutcome.NEEDS_REVIEW.value],
        safe_fields_applied=sum(report.safe_fields_applied for report in reports),
        last_item_id=reports[-1].last_item_id,
        remaining=0,
        dry_run=not apply,
        outcome_counts=outcome_counts,
    )


__all__ = [
    "MetadataReviewReclassifier",
    "ReviewReclassificationReport",
    "reclassify_stored_review_items",
]
