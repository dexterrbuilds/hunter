"""Versioned SQLite persistence for positions and logical executions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from solders.pubkey import Pubkey

from domain.accounting import PositionAccounting
from domain.lifecycle import (
    ExecutionState,
    PositionStatus,
    require_transition,
)
from execution.errors import ErrorClassification
from execution.telemetry import ExecutionTelemetry

SCHEMA_VERSION = 2


@dataclass(slots=True)
class StoredPosition:
    """Position aggregate plus monitoring/recovery metadata."""

    accounting: PositionAccounting
    strategy_metadata: dict[str, Any]
    take_profit_bps: int | None = None
    stop_loss_bps: int | None = None
    trailing_stop_bps: int | None = None
    recovery_reason: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class StoredExecution:
    logical_execution_id: str
    position_id: str | None
    side: str
    state: ExecutionState
    submission_attempt: int
    signature: str | None
    blockhash: str | None
    last_valid_block_height: int | None
    error_classification: ErrorClassification | None
    created_at: str
    updated_at: str


class SQLitePositionStore:
    """Small transactional repository; never stores secrets or endpoint URLs."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._configure()
        self.migrate()

    def _configure(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize a local transaction and roll it back on failure."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def migrate(self) -> None:
        """Apply every schema migration in order."""
        with self.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            if 1 not in applied:
                self._migration_1(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, _now()),
                )
            if 2 not in applied:
                self._migration_2(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, _now()),
                )

    @staticmethod
    def _migration_1(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE positions (
                position_id TEXT PRIMARY KEY,
                token_mint TEXT NOT NULL,
                quote_mint TEXT NOT NULL,
                token_decimals INTEGER NOT NULL,
                quote_decimals INTEGER NOT NULL,
                status TEXT NOT NULL,
                acquired_quantity_raw INTEGER NOT NULL,
                sold_quantity_raw INTEGER NOT NULL,
                quote_cost_raw INTEGER NOT NULL,
                quote_proceeds_raw INTEGER NOT NULL,
                remaining_cost_basis_raw INTEGER NOT NULL,
                realized_gross_pnl_raw INTEGER NOT NULL,
                realized_net_pnl_raw INTEGER,
                entry_network_fee_lamports INTEGER,
                exit_network_fee_lamports INTEGER,
                entry_priority_fee_lamports INTEGER,
                exit_priority_fee_lamports INTEGER,
                other_entry_cost_lamports INTEGER,
                other_exit_cost_lamports INTEGER,
                remaining_entry_cost_lamports INTEGER,
                unknown_costs_json TEXT NOT NULL,
                take_profit_bps INTEGER,
                stop_loss_bps INTEGER,
                trailing_stop_bps INTEGER,
                strategy_metadata_json TEXT NOT NULL,
                recovery_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE position_fills (
                fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL REFERENCES positions(position_id),
                side TEXT NOT NULL,
                signature TEXT NOT NULL,
                token_quantity_raw INTEGER NOT NULL,
                quote_amount_raw INTEGER NOT NULL,
                network_fee_lamports INTEGER,
                priority_fee_lamports INTEGER,
                other_cost_lamports INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(position_id, side, signature)
            )""",
            """CREATE TABLE lifecycle_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL REFERENCES positions(position_id),
                from_status TEXT NOT NULL,
                to_status TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE executions (
                logical_execution_id TEXT PRIMARY KEY,
                position_id TEXT REFERENCES positions(position_id),
                side TEXT NOT NULL,
                state TEXT NOT NULL,
                submission_attempt INTEGER NOT NULL DEFAULT 0,
                signature TEXT UNIQUE,
                blockhash TEXT,
                last_valid_block_height INTEGER,
                error_classification TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE execution_attempts (
                logical_execution_id TEXT NOT NULL
                    REFERENCES executions(logical_execution_id),
                submission_attempt INTEGER NOT NULL,
                signature TEXT NOT NULL UNIQUE,
                blockhash TEXT,
                last_valid_block_height INTEGER,
                created_at TEXT NOT NULL,
                PRIMARY KEY(logical_execution_id, submission_attempt)
            )""",
            """CREATE TABLE execution_telemetry (
                logical_execution_id TEXT NOT NULL,
                submission_attempt INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(logical_execution_id, submission_attempt)
            )""",
            """CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            "CREATE INDEX positions_status_idx ON positions(status)",
            "CREATE INDEX executions_state_idx ON executions(state)",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migration_2(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS execution_attempts (
                logical_execution_id TEXT NOT NULL
                    REFERENCES executions(logical_execution_id),
                submission_attempt INTEGER NOT NULL,
                signature TEXT NOT NULL UNIQUE,
                blockhash TEXT,
                last_valid_block_height INTEGER,
                created_at TEXT NOT NULL,
                PRIMARY KEY(logical_execution_id, submission_attempt)
            )"""
        )

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
        return int(row[0])

    def save_position(self, position: StoredPosition) -> None:
        """Insert or update the complete position aggregate atomically."""
        accounting = position.accounting
        now = _now()
        created_at = position.created_at or now
        values = (
            accounting.position_id,
            str(accounting.token_mint),
            str(accounting.quote_mint),
            accounting.token_decimals,
            accounting.quote_decimals,
            accounting.status.value,
            _encode_int(accounting.acquired_quantity_raw),
            _encode_int(accounting.sold_quantity_raw),
            _encode_int(accounting.quote_cost_raw),
            _encode_int(accounting.quote_proceeds_raw),
            _encode_int(accounting.remaining_cost_basis_raw),
            _encode_int(accounting.realized_gross_pnl_raw),
            _encode_int(accounting.realized_net_pnl_raw),
            _encode_int(accounting.entry_network_fee_lamports),
            _encode_int(accounting.exit_network_fee_lamports),
            _encode_int(accounting.entry_priority_fee_lamports),
            _encode_int(accounting.exit_priority_fee_lamports),
            _encode_int(accounting.other_entry_cost_lamports),
            _encode_int(accounting.other_exit_cost_lamports),
            _encode_int(accounting.remaining_entry_cost_lamports),
            json.dumps(sorted(accounting.unknown_costs)),
            position.take_profit_bps,
            position.stop_loss_bps,
            position.trailing_stop_bps,
            json.dumps(position.strategy_metadata, sort_keys=True),
            position.recovery_reason,
            created_at,
            now,
        )
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO positions VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                ) ON CONFLICT(position_id) DO UPDATE SET
                    token_mint=excluded.token_mint,
                    quote_mint=excluded.quote_mint,
                    token_decimals=excluded.token_decimals,
                    quote_decimals=excluded.quote_decimals,
                    status=excluded.status,
                    acquired_quantity_raw=excluded.acquired_quantity_raw,
                    sold_quantity_raw=excluded.sold_quantity_raw,
                    quote_cost_raw=excluded.quote_cost_raw,
                    quote_proceeds_raw=excluded.quote_proceeds_raw,
                    remaining_cost_basis_raw=excluded.remaining_cost_basis_raw,
                    realized_gross_pnl_raw=excluded.realized_gross_pnl_raw,
                    realized_net_pnl_raw=excluded.realized_net_pnl_raw,
                    entry_network_fee_lamports=excluded.entry_network_fee_lamports,
                    exit_network_fee_lamports=excluded.exit_network_fee_lamports,
                    entry_priority_fee_lamports=excluded.entry_priority_fee_lamports,
                    exit_priority_fee_lamports=excluded.exit_priority_fee_lamports,
                    other_entry_cost_lamports=excluded.other_entry_cost_lamports,
                    other_exit_cost_lamports=excluded.other_exit_cost_lamports,
                    remaining_entry_cost_lamports=excluded.remaining_entry_cost_lamports,
                    unknown_costs_json=excluded.unknown_costs_json,
                    take_profit_bps=excluded.take_profit_bps,
                    stop_loss_bps=excluded.stop_loss_bps,
                    trailing_stop_bps=excluded.trailing_stop_bps,
                    strategy_metadata_json=excluded.strategy_metadata_json,
                    recovery_reason=excluded.recovery_reason,
                    updated_at=excluded.updated_at""",
                values,
            )

    def get_position(self, position_id: str) -> StoredPosition | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM positions WHERE position_id = ?", (position_id,)
            ).fetchone()
        return _row_to_position(row) if row is not None else None

    def list_positions(
        self, statuses: set[PositionStatus] | None = None
    ) -> list[StoredPosition]:
        with self._lock:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = self._connection.execute(
                    f"SELECT * FROM positions WHERE status IN ({placeholders}) "  # noqa: S608
                    "ORDER BY created_at",
                    tuple(status.value for status in statuses),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM positions ORDER BY created_at"
                ).fetchall()
        return [_row_to_position(row) for row in rows]

    def transition_position(
        self, position_id: str, target: PositionStatus, reason: str
    ) -> StoredPosition:
        """Validate and persist one lifecycle transition."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM positions WHERE position_id = ?", (position_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown position: {position_id}")
            current = PositionStatus(row[0])
            require_transition(current, target)
            now = _now()
            connection.execute(
                "UPDATE positions SET status = ?, updated_at = ? WHERE position_id = ?",
                (target.value, now, position_id),
            )
            connection.execute(
                "INSERT INTO lifecycle_events(position_id, from_status, to_status, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (position_id, current.value, target.value, reason, now),
            )
        result = self.get_position(position_id)
        if result is None:
            raise RuntimeError("position disappeared after transition")
        return result

    def record_fill(
        self,
        *,
        position_id: str,
        side: str,
        signature: str,
        token_quantity_raw: int,
        quote_amount_raw: int,
        network_fee_lamports: int | None,
        priority_fee_lamports: int | None,
        other_cost_lamports: int | None,
    ) -> bool:
        """Persist one actual fill; return False when already recorded."""
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO position_fills(
                    position_id, side, signature, token_quantity_raw,
                    quote_amount_raw, network_fee_lamports,
                    priority_fee_lamports, other_cost_lamports, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    position_id,
                    side,
                    signature,
                    _encode_int(token_quantity_raw),
                    _encode_int(quote_amount_raw),
                    _encode_int(network_fee_lamports),
                    _encode_int(priority_fee_lamports),
                    _encode_int(other_cost_lamports),
                    _now(),
                ),
            )
        return cursor.rowcount == 1

    def list_fills(self, position_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM position_fills WHERE position_id = ? ORDER BY fill_id",
                (position_id,),
            ).fetchall()
        result = [dict(row) for row in rows]
        for fill in result:
            for key in (
                "token_quantity_raw",
                "quote_amount_raw",
                "network_fee_lamports",
                "priority_fee_lamports",
                "other_cost_lamports",
            ):
                fill[key] = _decode_int(fill[key])
        return result

    def mark_reconciliation_required(self, position_id: str, reason: str) -> None:
        position = self.get_position(position_id)
        if position is None:
            raise KeyError(f"unknown position: {position_id}")
        if position.accounting.status != PositionStatus.RECONCILIATION_REQUIRED:
            self.transition_position(
                position_id, PositionStatus.RECONCILIATION_REQUIRED, reason
            )
        with self._lock:
            self._connection.execute(
                "UPDATE positions SET recovery_reason = ?, updated_at = ? "
                "WHERE position_id = ?",
                (reason, _now(), position_id),
            )

    def create_execution(
        self,
        logical_execution_id: str,
        *,
        position_id: str | None,
        side: str,
    ) -> StoredExecution:
        """Claim a logical trade ID; repeated calls return the original."""
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO executions(logical_execution_id, position_id, side, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    logical_execution_id,
                    position_id,
                    side,
                    ExecutionState.PLANNED.value,
                    now,
                    now,
                ),
            )
        execution = self.get_execution(logical_execution_id)
        if execution is None:
            raise RuntimeError("failed to create logical execution")
        return execution

    def update_execution(
        self,
        logical_execution_id: str,
        *,
        state: ExecutionState,
        signature: str | None = None,
        blockhash: str | None = None,
        last_valid_block_height: int | None = None,
        increment_attempt: bool = False,
        error_classification: ErrorClassification | None = None,
    ) -> StoredExecution:
        """Persist submission identity before confirmation is awaited."""
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM executions WHERE logical_execution_id = ?",
                (logical_execution_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"unknown execution: {logical_execution_id}")
            attempt = int(existing["submission_attempt"]) + int(increment_attempt)
            if increment_attempt and signature is None:
                raise ValueError("a submission attempt requires a signature")
            connection.execute(
                """UPDATE executions SET state = ?, submission_attempt = ?,
                    signature = COALESCE(?, signature),
                    blockhash = COALESCE(?, blockhash),
                    last_valid_block_height = COALESCE(?, last_valid_block_height),
                    error_classification = ?, updated_at = ?
                    WHERE logical_execution_id = ?""",
                (
                    state.value,
                    attempt,
                    signature,
                    blockhash,
                    last_valid_block_height,
                    error_classification.value if error_classification else None,
                    _now(),
                    logical_execution_id,
                ),
            )
            if increment_attempt:
                connection.execute(
                    """INSERT INTO execution_attempts(
                        logical_execution_id, submission_attempt, signature,
                        blockhash, last_valid_block_height, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        logical_execution_id,
                        attempt,
                        signature,
                        blockhash,
                        last_valid_block_height,
                        _now(),
                    ),
                )
        result = self.get_execution(logical_execution_id)
        if result is None:
            raise RuntimeError("execution disappeared after update")
        return result

    def get_execution(self, logical_execution_id: str) -> StoredExecution | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM executions WHERE logical_execution_id = ?",
                (logical_execution_id,),
            ).fetchone()
        return _row_to_execution(row) if row is not None else None

    def attach_execution_position(
        self, logical_execution_id: str, position_id: str
    ) -> None:
        """Link a completed buy identity to its durable position."""
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE executions SET position_id = ?, updated_at = ?
                   WHERE logical_execution_id = ?
                     AND (position_id IS NULL OR position_id = ?)""",
                (position_id, _now(), logical_execution_id, position_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("execution is already linked to a different position")

    def get_pending_execution(
        self, position_id: str, *, side: str
    ) -> StoredExecution | None:
        """Return the latest non-terminal logical execution for a position."""
        terminal = (
            ExecutionState.CONFIRMED.value,
            ExecutionState.FINALIZED.value,
            ExecutionState.FAILED_ON_CHAIN.value,
        )
        with self._lock:
            row = self._connection.execute(
                """SELECT * FROM executions
                   WHERE position_id = ? AND side = ?
                     AND state NOT IN (?, ?, ?)
                   ORDER BY updated_at DESC LIMIT 1""",
                (position_id, side, *terminal),
            ).fetchone()
        return _row_to_execution(row) if row is not None else None

    def get_latest_execution(
        self, position_id: str, *, side: str
    ) -> StoredExecution | None:
        """Return the most recently updated execution, including terminal state."""
        with self._lock:
            row = self._connection.execute(
                """SELECT * FROM executions
                   WHERE position_id = ? AND side = ?
                   ORDER BY updated_at DESC LIMIT 1""",
                (position_id, side),
            ).fetchone()
        return _row_to_execution(row) if row is not None else None

    def list_execution_attempts(
        self, logical_execution_id: str
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM execution_attempts
                   WHERE logical_execution_id = ? ORDER BY submission_attempt""",
                (logical_execution_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_telemetry(self, telemetry: ExecutionTelemetry, attempt: int) -> None:
        """Persist a credential-safe telemetry snapshot as JSON."""
        payload = _telemetry_json(telemetry)
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO execution_telemetry "
                "(logical_execution_id, submission_attempt, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (telemetry.execution_id, attempt, payload, _now()),
            )

    def get_telemetry(
        self, logical_execution_id: str, attempt: int
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT payload_json FROM execution_telemetry
                   WHERE logical_execution_id = ? AND submission_attempt = ?""",
                (logical_execution_id, attempt),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, json.dumps(value, sort_keys=True), _now()),
            )

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT key, value_json FROM settings ORDER BY key"
            ).fetchall()
        return {row["key"]: json.loads(row["value_json"]) for row in rows}

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _row_to_position(row: sqlite3.Row) -> StoredPosition:
    accounting = PositionAccounting(
        position_id=row["position_id"],
        token_mint=Pubkey.from_string(row["token_mint"]),
        quote_mint=Pubkey.from_string(row["quote_mint"]),
        token_decimals=row["token_decimals"],
        quote_decimals=row["quote_decimals"],
        status=PositionStatus(row["status"]),
        acquired_quantity_raw=_decode_int(row["acquired_quantity_raw"]),
        sold_quantity_raw=_decode_int(row["sold_quantity_raw"]),
        quote_cost_raw=_decode_int(row["quote_cost_raw"]),
        quote_proceeds_raw=_decode_int(row["quote_proceeds_raw"]),
        remaining_cost_basis_raw=_decode_int(row["remaining_cost_basis_raw"]),
        realized_gross_pnl_raw=_decode_int(row["realized_gross_pnl_raw"]),
        realized_net_pnl_raw=_decode_int(row["realized_net_pnl_raw"]),
        entry_network_fee_lamports=_decode_int(row["entry_network_fee_lamports"]),
        exit_network_fee_lamports=_decode_int(row["exit_network_fee_lamports"]),
        entry_priority_fee_lamports=_decode_int(row["entry_priority_fee_lamports"]),
        exit_priority_fee_lamports=_decode_int(row["exit_priority_fee_lamports"]),
        other_entry_cost_lamports=_decode_int(row["other_entry_cost_lamports"]),
        other_exit_cost_lamports=_decode_int(row["other_exit_cost_lamports"]),
        remaining_entry_cost_lamports=_decode_int(row["remaining_entry_cost_lamports"]),
        unknown_costs=set(json.loads(row["unknown_costs_json"])),
    )
    return StoredPosition(
        accounting=accounting,
        strategy_metadata=json.loads(row["strategy_metadata_json"]),
        take_profit_bps=row["take_profit_bps"],
        stop_loss_bps=row["stop_loss_bps"],
        trailing_stop_bps=row["trailing_stop_bps"],
        recovery_reason=row["recovery_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_execution(row: sqlite3.Row) -> StoredExecution:
    classification = row["error_classification"]
    return StoredExecution(
        logical_execution_id=row["logical_execution_id"],
        position_id=row["position_id"],
        side=row["side"],
        state=ExecutionState(row["state"]),
        submission_attempt=row["submission_attempt"],
        signature=row["signature"],
        blockhash=row["blockhash"],
        last_valid_block_height=row["last_valid_block_height"],
        error_classification=(
            ErrorClassification(classification) if classification else None
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _telemetry_json(telemetry: ExecutionTelemetry) -> str:
    payload = asdict(telemetry)
    for key, value in tuple(payload.items()):
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
        elif isinstance(value, ErrorClassification):
            payload[key] = value.value
    return json.dumps(payload, sort_keys=True)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _encode_int(value: int | None) -> str | None:
    """Force exact integer storage even in legacy INTEGER-affinity columns."""
    return None if value is None else f"n:{value}"


def _decode_int(value: object) -> int | None:
    if value is None:
        return None
    text = str(value)
    return int(text[2:] if text.startswith("n:") else text)
