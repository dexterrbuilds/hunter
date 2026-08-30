"""SQLite persistence for benchmark sessions, observations, and failures."""

# Persistence boundaries use explicit validation and multi-field record methods.
# ruff: noqa: PLR0913, TC001, TRY003

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmark.live_models import BenchmarkAttempt, BenchmarkKind, DetectionObservation

SCHEMA_VERSION = 1


class BenchmarkStore:
    """Canonical credential-free benchmark data store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS benchmark_schema (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS benchmark_sessions (
                    session_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    region_label TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    live_authorized INTEGER NOT NULL,
                    dedicated_wallet INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS detection_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    mint TEXT NOT NULL,
                    creation_signature TEXT,
                    correlation_key TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    observed_mono_ns INTEGER NOT NULL,
                    launch_slot INTEGER,
                    detection_slot INTEGER,
                    transaction_slot INTEGER,
                    processing_started_mono_ns INTEGER,
                    trade_request_mono_ns INTEGER
                );
                CREATE INDEX IF NOT EXISTS detection_correlation_idx
                    ON detection_observations(session_id, correlation_key);
                CREATE TABLE IF NOT EXISTS benchmark_attempts (
                    session_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    route_mode TEXT NOT NULL,
                    connection_state TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY(session_id, attempt_id, provider_id)
                );
                CREATE TABLE IF NOT EXISTS benchmark_economic_guard (
                    session_id TEXT NOT NULL,
                    logical_trade_id TEXT NOT NULL,
                    execution_variant TEXT NOT NULL,
                    mint TEXT NOT NULL,
                    signature TEXT,
                    quote_spend_raw INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    PRIMARY KEY(session_id, logical_trade_id, execution_variant)
                );
                """
            )
            row = self.connection.execute(
                "SELECT version FROM benchmark_schema LIMIT 1"
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO benchmark_schema(version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported benchmark schema version {row['version']}"
                )

    def create_session(
        self,
        session_id: str,
        kind: BenchmarkKind,
        *,
        region_label: str,
        live_authorized: bool = False,
        dedicated_wallet: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO benchmark_sessions
                (session_id, kind, region_label, started_at, live_authorized,
                 dedicated_wallet, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    kind.value,
                    region_label,
                    datetime.now(UTC).isoformat(),
                    int(live_authorized),
                    int(dedicated_wallet),
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )

    def complete_session(self, session_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE benchmark_sessions SET completed_at=? WHERE session_id=?",
                (datetime.now(UTC).isoformat(), session_id),
            )

    def record_detection(self, observation: DetectionObservation) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO detection_observations
                (session_id, source, mint, creation_signature, correlation_key,
                 observed_at, observed_mono_ns, launch_slot, detection_slot,
                 transaction_slot, processing_started_mono_ns, trade_request_mono_ns)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation.session_id,
                    observation.source,
                    observation.mint,
                    observation.creation_signature,
                    observation.correlation_key,
                    observation.observed_at.isoformat(),
                    observation.observed_mono_ns,
                    observation.launch_slot,
                    observation.detection_slot,
                    observation.transaction_slot,
                    observation.processing_started_mono_ns,
                    observation.trade_request_mono_ns,
                ),
            )

    def record_attempt(self, attempt: BenchmarkAttempt) -> None:
        data = asdict(attempt)
        data["kind"] = attempt.kind.value
        data["connection_state"] = attempt.connection_state.value
        data["started_at"] = attempt.started_at.isoformat()
        if attempt.error_classification is not None:
            data["error_classification"] = attempt.error_classification.value
        with self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO benchmark_attempts
                (session_id, attempt_id, kind, provider_id, endpoint_id,
                 route_mode, connection_state, started_at, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    attempt.session_id,
                    attempt.attempt_id,
                    attempt.kind.value,
                    attempt.provider_id,
                    attempt.endpoint_id,
                    attempt.route_mode,
                    attempt.connection_state.value,
                    attempt.started_at.isoformat(),
                    json.dumps(data, sort_keys=True),
                ),
            )

    def reserve_economic_trial(
        self,
        *,
        session_id: str,
        logical_trade_id: str,
        execution_variant: str,
        mint: str,
        quote_spend_raw: int,
    ) -> None:
        """Atomically prevent a duplicate economic variant in one session."""
        try:
            with self.connection:
                self.connection.execute(
                    """INSERT INTO benchmark_economic_guard
                    (session_id, logical_trade_id, execution_variant, mint,
                     quote_spend_raw, state)
                    VALUES (?, ?, ?, ?, ?, 'reserved')""",
                    (
                        session_id,
                        logical_trade_id,
                        execution_variant,
                        mint,
                        quote_spend_raw,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise RuntimeError(
                "economic benchmark trial already exists; inspect its signature/state"
            ) from error

    def update_economic_trial(
        self,
        session_id: str,
        logical_trade_id: str,
        execution_variant: str,
        *,
        state: str,
        signature: str | None = None,
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE benchmark_economic_guard SET state=?, signature=COALESCE(?, signature)
                WHERE session_id=? AND logical_trade_id=? AND execution_variant=?""",
                (
                    state,
                    signature,
                    session_id,
                    logical_trade_id,
                    execution_variant,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError("economic benchmark trial was not reserved")

    def economic_totals(self, session_id: str) -> tuple[int, int]:
        row = self.connection.execute(
            """SELECT COUNT(*) AS count, COALESCE(SUM(quote_spend_raw), 0) AS spend
            FROM benchmark_economic_guard WHERE session_id=?""",
            (session_id,),
        ).fetchone()
        return int(row["count"]), int(row["spend"])

    def list_attempts(self, session_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT data_json FROM benchmark_attempts"
        parameters: tuple[str, ...] = ()
        if session_id is not None:
            query += " WHERE session_id=?"
            parameters = (session_id,)
        return [
            json.loads(row["data_json"])
            for row in self.connection.execute(query, parameters).fetchall()
        ]

    def list_sessions(self, session_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM benchmark_sessions"
        parameters: tuple[str, ...] = ()
        if session_id is not None:
            query += " WHERE session_id=?"
            parameters = (session_id,)
        sessions = []
        for row in self.connection.execute(query, parameters):
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            sessions.append(item)
        return sessions

    def list_detections(self, session_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM detection_observations"
        parameters: tuple[str, ...] = ()
        if session_id is not None:
            query += " WHERE session_id=?"
            parameters = (session_id,)
        return [dict(row) for row in self.connection.execute(query, parameters)]
