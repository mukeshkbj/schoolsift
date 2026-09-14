from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import domain
from .domain import ActionPacket, ProposalPayload, ProposalVersion
from .errors import (
    BadRequestError,
    ConflictError,
    MigrationError,
    NotFoundError,
)
from .identity import LOCAL_PRINCIPAL, Principal, normalize_email
from .models import (
    CalendarRecord,
    Child,
    Connection,
    DocumentRecord,
    ExecutionRecord,
    FetchedMessage,
    Household,
    Invitation,
    Membership,
    MessageHeader,
    MessageRecord,
    OAuthState,
    SchoolSource,
    WebhookSubscription,
)
from .notifications import IngestionEvent
from .proposal_policy import validate_proposal_payload

OAUTH_STATE_TTL = timedelta(minutes=10)

SCHEMA_V1 = """
CREATE TABLE households (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE children (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    name TEXT NOT NULL,
    school TEXT NOT NULL,
    grade TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE connections (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    provider TEXT NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL,
    last_sync_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    connection_id TEXT NOT NULL REFERENCES connections(id),
    provider_message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    reply_to TEXT NOT NULL,
    subject TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source_confirmed INTEGER NOT NULL,
    body_ref TEXT,
    attachment_ids TEXT NOT NULL
);
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    message_id TEXT NOT NULL REFERENCES messages(id),
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    acroform_fields TEXT NOT NULL
);
CREATE TABLE calendar_records (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL
);
CREATE TABLE packets (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    source_message_id TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
"""

CONNECTION_COLS = (
    "id, household_id, provider, provider_subject, email, status,"
    " last_sync_at, created_at, sync_cursor, sync_status"
)

EXECUTION_COLS = (
    "id",
    "household_id",
    "proposal_id",
    "proposal_version",
    "connection_id",
    "idempotency_key",
    "status",
    "attempts",
    "provider_operation_id",
    "output_ref",
    "safe_error",
    "lease_until",
    "created_at",
    "updated_at",
)

MESSAGE_COLS = (
    "id, household_id, connection_id, provider_message_id, thread_id,"
    " sender_name, sender_email, reply_to, subject, received_at,"
    " source_confirmed, status, body_ref, attachment_ids,"
    " manual_review_reason"
)

DOCUMENT_COLS = "id, household_id, message_id, name, mime, content_ref, acroform_fields"

SOURCE_COLS = (
    "id, household_id, connection_id, sender_email, sender_domain,"
    " status, first_seen_at, last_seen_at"
)

_UNSET = object()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _add_column(con: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _columns(con, table):
        con.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate_to_1(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA_V1)


def _migrate_to_2(con: sqlite3.Connection) -> None:
    if "id" not in _columns(con, "schema_version"):
        con.execute("ALTER TABLE schema_version RENAME TO schema_version_legacy")
        con.execute(
            "CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK (id = 1),"
            " version INTEGER NOT NULL)"
        )
        con.execute(
            "INSERT INTO schema_version (id, version)"
            " SELECT 1, version FROM schema_version_legacy LIMIT 1"
        )
        con.execute("DROP TABLE schema_version_legacy")
    _add_column(
        con,
        "connections",
        "provider_subject",
        "provider_subject TEXT NOT NULL DEFAULT ''",
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_states (
            state_hash TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id),
            provider TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        )
        """
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS connections_identity"
        " ON connections (household_id, provider, provider_subject)"
    )


def _migrate_to_3(con: sqlite3.Connection) -> None:
    _add_column(con, "connections", "sync_cursor", "sync_cursor TEXT")
    _add_column(
        con,
        "connections",
        "sync_status",
        "sync_status TEXT NOT NULL DEFAULT 'idle'",
    )
    _add_column(
        con, "messages", "status", "status TEXT NOT NULL DEFAULT 'awaiting_source'"
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS school_sources (
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id),
            connection_id TEXT NOT NULL REFERENCES connections(id),
            sender_email TEXT NOT NULL,
            sender_domain TEXT NOT NULL,
            status TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE (connection_id, sender_email)
        )
        """
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS messages_provider_id"
        " ON messages (connection_id, provider_message_id)"
    )


def _migrate_to_4(con: sqlite3.Connection) -> None:
    _add_column(
        con,
        "messages",
        "manual_review_reason",
        "manual_review_reason TEXT",
    )


def _migrate_to_5(con: sqlite3.Connection) -> None:
    duplicates = con.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM packets"
        " GROUP BY household_id, source_message_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    if duplicates:
        raise MigrationError(
            f"Cannot migrate: {duplicates} message(s) have duplicate packets."
            " Resolve them before upgrading."
        )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS packets_household_source_unique"
        " ON packets (household_id, source_message_id)"
    )


def _migrate_to_6(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS ingestion_events (
            id TEXT PRIMARY KEY,
            dedupe_key TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_subscriptions (
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id),
            connection_id TEXT NOT NULL REFERENCES connections(id),
            provider TEXT NOT NULL,
            provider_subscription_id TEXT NOT NULL,
            client_state_hash TEXT NOT NULL,
            cursor TEXT,
            expires_at TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (provider, provider_subscription_id)
        )
        """
    )


def _migrate_to_8(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS memberships (
            household_id TEXT NOT NULL REFERENCES households(id),
            user_id TEXT NOT NULL,
            email TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (household_id, user_id)
        )
        """
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS memberships_email"
        " ON memberships (household_id, email)"
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS invitations (
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id),
            email TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS invitations_pending"
        " ON invitations (household_id, email) WHERE status = 'pending'"
    )
    now = _now()
    for (hid,) in con.execute("SELECT id FROM households").fetchall():
        con.execute(
            "INSERT INTO memberships"
            " (household_id, user_id, email, role, created_at)"
            " VALUES (?, 'local-caregiver', 'local@schoolsift.invalid',"
            " 'owner', ?)",
            (hid, now),
        )


def _migrate_to_7(con: sqlite3.Connection) -> None:
    duplicates = con.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM connections"
        " WHERE provider_subject != ''"
        " GROUP BY provider, provider_subject HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    if duplicates:
        raise MigrationError(
            f"Cannot migrate: {duplicates} provider identit(ies) are claimed"
            " by multiple households. Resolve them before upgrading."
        )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS connections_global_identity"
        " ON connections (provider, provider_subject)"
        " WHERE provider_subject != ''"
    )


def _migrate_to_9(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS executions (
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id),
            proposal_id TEXT NOT NULL,
            proposal_version INTEGER NOT NULL,
            connection_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            provider_operation_id TEXT,
            output_ref TEXT,
            safe_error TEXT,
            lease_until TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS executions_proposal"
        " ON executions (household_id, proposal_id, proposal_version)"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS executions_idempotency"
        " ON executions (idempotency_key)"
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_outbox (
            id TEXT PRIMARY KEY,
            execution_id TEXT NOT NULL UNIQUE REFERENCES executions(id),
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS execution_outbox_pending"
        " ON execution_outbox (status) WHERE status = 'pending'"
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id),
            kind TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS audit_events_household"
        " ON audit_events (household_id, created_at)"
    )


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migrate_to_1,
    _migrate_to_2,
    _migrate_to_3,
    _migrate_to_4,
    _migrate_to_5,
    _migrate_to_6,
    _migrate_to_7,
    _migrate_to_8,
    _migrate_to_9,
]

SCHEMA_VERSION = len(MIGRATIONS)


def _connection_row(row: tuple[Any, ...]) -> Connection:
    return Connection(
        id=row[0],
        household_id=row[1],
        provider=row[2],
        provider_subject=row[3],
        email=row[4],
        status=row[5],
        last_sync_at=_ts(row[6]),
        created_at=row[7],
        sync_cursor=row[8],
        sync_status=row[9],
    )


def _message_row(row: tuple[Any, ...]) -> MessageRecord:
    return MessageRecord(
        id=row[0],
        household_id=row[1],
        connection_id=row[2],
        provider_message_id=row[3],
        thread_id=row[4],
        sender_name=row[5],
        sender_email=row[6],
        reply_to=row[7],
        subject=row[8],
        received_at=row[9],
        source_confirmed=bool(row[10]),
        status=row[11],
        body_ref=row[12],
        attachment_ids=json.loads(row[13]),
        manual_review_reason=row[14],
    )


def _source_row(row: tuple[Any, ...]) -> SchoolSource:
    return SchoolSource(
        id=row[0],
        household_id=row[1],
        connection_id=row[2],
        sender_email=row[3],
        sender_domain=row[4],
        status=row[5],
        first_seen_at=row[6],
        last_seen_at=row[7],
    )


def _document_row(row: tuple[Any, ...]) -> DocumentRecord:
    return DocumentRecord(
        id=row[0],
        household_id=row[1],
        message_id=row[2],
        name=row[3],
        mime=row[4],
        content_ref=row[5],
        acroform_fields=json.loads(row[6]),
    )


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 5000")
        return con

    def _version(self, con: sqlite3.Connection) -> int:
        try:
            row = con.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0]) if row else 0

    def _set_version(self, con: sqlite3.Connection, version: int) -> None:
        con.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, ?)"
            " ON CONFLICT (id) DO UPDATE SET version = excluded.version",
            (version,),
        )

    def initialize(self) -> None:
        con = self._connect()
        try:
            con.execute("PRAGMA journal_mode = WAL")
            version = self._version(con)
            for target, migrate in enumerate(MIGRATIONS, start=1):
                if version >= target:
                    continue
                con.execute("BEGIN IMMEDIATE")
                try:
                    migrate(con)
                    self._set_version(con, target)
                except Exception:
                    con.rollback()
                    raise
                else:
                    con.commit()
                version = target
        finally:
            con.close()

    def get_household(self) -> Household | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT id, name, timezone, created_at FROM households LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return Household(id=row[0], name=row[1], timezone=row[2], created_at=row[3])

    def create_household(self, *, name: str, timezone: str) -> Household:
        return self.create_household_for_owner(
            LOCAL_PRINCIPAL, name=name, timezone=timezone
        )

    def list_children(self, household_id: str) -> list[Child]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id, household_id, name, school, grade, created_at"
                " FROM children WHERE household_id = ? ORDER BY created_at",
                (household_id,),
            ).fetchall()
        finally:
            con.close()
        return [
            Child(
                id=r[0],
                household_id=r[1],
                name=r[2],
                school=r[3],
                grade=r[4],
                created_at=r[5],
            )
            for r in rows
        ]

    def add_child(
        self, household_id: str, *, name: str, school: str, grade: str
    ) -> Child:
        child = Child(
            id=f"ch-{uuid.uuid4().hex[:12]}",
            household_id=household_id,
            name=name,
            school=school,
            grade=grade,
            created_at=datetime.now(UTC),
        )
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            if (
                con.execute(
                    "SELECT 1 FROM households WHERE id = ?", (household_id,)
                ).fetchone()
                is None
            ):
                raise ConflictError("Create a household before adding children.")
            con.execute(
                "INSERT INTO children (id, household_id, name, school, grade,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    child.id,
                    child.household_id,
                    child.name,
                    child.school,
                    child.grade,
                    child.created_at.isoformat(),
                ),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return child

    def _get_connection(
        self, con: sqlite3.Connection, household_id: str, connection_id: str
    ) -> Connection:
        row = con.execute(
            f"SELECT {CONNECTION_COLS} FROM connections"
            " WHERE id = ? AND household_id = ?",
            (connection_id, household_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Unknown connection: {connection_id}")
        return _connection_row(row)

    def get_connection(self, household_id: str, connection_id: str) -> Connection:
        con = self._connect()
        try:
            return self._get_connection(con, household_id, connection_id)
        finally:
            con.close()

    def list_connections(self, household_id: str) -> list[Connection]:
        con = self._connect()
        try:
            rows = con.execute(
                f"SELECT {CONNECTION_COLS} FROM connections"
                " WHERE household_id = ? ORDER BY created_at",
                (household_id,),
            ).fetchall()
        finally:
            con.close()
        return [_connection_row(r) for r in rows]

    def upsert_connection(
        self,
        household_id: str,
        *,
        provider: str,
        provider_subject: str,
        email: str,
    ) -> Connection:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(
                    "INSERT INTO connections (id, household_id, provider,"
                    " provider_subject, email, status, last_sync_at, created_at)"
                    " VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?)"
                    " ON CONFLICT (household_id, provider, provider_subject)"
                    " DO UPDATE SET email = excluded.email, status = 'pending'",
                    (
                        f"conn-{uuid.uuid4().hex[:12]}",
                        household_id,
                        provider,
                        provider_subject,
                        email,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as e:
                raise ConflictError(
                    "This inbox is already attached to a household."
                ) from e
            row = con.execute(
                f"SELECT {CONNECTION_COLS} FROM connections"
                " WHERE household_id = ? AND provider = ?"
                " AND provider_subject = ?",
                (household_id, provider, provider_subject),
            ).fetchone()
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        if row is None:
            raise ConflictError("Connection upsert failed.")
        return _connection_row(row)

    def set_connection_status(
        self,
        household_id: str,
        connection_id: str,
        status: Literal[
            "pending", "connected", "reauthorization_required", "disconnected"
        ],
    ) -> Connection:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            self._get_connection(con, household_id, connection_id)
            con.execute(
                "UPDATE connections SET status = ? WHERE id = ? AND household_id = ?",
                (status, connection_id, household_id),
            )
            updated = self._get_connection(con, household_id, connection_id)
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return updated

    def disconnect_connection(
        self, household_id: str, connection_id: str
    ) -> Connection:
        return self.set_connection_status(household_id, connection_id, "disconnected")

    def restore_connection(
        self,
        household_id: str,
        connection_id: str,
        *,
        email: str,
        status: Literal[
            "pending", "connected", "reauthorization_required", "disconnected"
        ],
    ) -> Connection:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            self._get_connection(con, household_id, connection_id)
            con.execute(
                "UPDATE connections SET email = ?, status = ?"
                " WHERE id = ? AND household_id = ?",
                (email, status, connection_id, household_id),
            )
            updated = self._get_connection(con, household_id, connection_id)
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return updated

    def update_connection_sync(
        self,
        household_id: str,
        connection_id: str,
        *,
        sync_status: object = _UNSET,
        sync_cursor: object = _UNSET,
        last_sync_at: object = _UNSET,
    ) -> Connection:
        assignments: list[str] = []
        values: list[str | None] = []
        if sync_status is not _UNSET:
            assignments.append("sync_status = ?")
            values.append(sync_status)  # type: ignore[arg-type]
        if sync_cursor is not _UNSET:
            assignments.append("sync_cursor = ?")
            values.append(sync_cursor)  # type: ignore[arg-type]
        if last_sync_at is not _UNSET:
            assignments.append("last_sync_at = ?")
            values.append(
                last_sync_at.isoformat()
                if isinstance(last_sync_at, datetime)
                else last_sync_at  # type: ignore[arg-type]
            )
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            self._get_connection(con, household_id, connection_id)
            if assignments:
                con.execute(
                    f"UPDATE connections SET {', '.join(assignments)}"
                    " WHERE id = ? AND household_id = ?",
                    (*values, connection_id, household_id),
                )
            updated = self._get_connection(con, household_id, connection_id)
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return updated

    def create_oauth_state(self, household_id: str, provider: str) -> str:
        raw = secrets.token_urlsafe(32)
        state_hash = hashlib.sha256(raw.encode()).hexdigest()
        expires = (datetime.now(UTC) + OAUTH_STATE_TTL).isoformat()
        con = self._connect()
        try:
            con.execute(
                "INSERT INTO oauth_states (state_hash, household_id, provider,"
                " expires_at) VALUES (?, ?, ?, ?)",
                (state_hash, household_id, provider, expires),
            )
            con.commit()
        finally:
            con.close()
        return raw

    def consume_oauth_state(self, state: str) -> OAuthState:
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "UPDATE oauth_states SET consumed_at = ?"
                " WHERE state_hash = ? AND consumed_at IS NULL"
                " AND expires_at > ?",
                (_now(), state_hash, _now()),
            )
            if cur.rowcount == 0:
                raise ConflictError("OAuth state is invalid, expired, or used.")
            row = con.execute(
                "SELECT household_id, provider FROM oauth_states WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return OAuthState(household_id=row[0], provider=row[1])

    def upsert_source_suggestion(
        self,
        household_id: str,
        connection_id: str,
        *,
        sender_email: str,
        seen_at: datetime,
    ) -> SchoolSource:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            self._get_connection(con, household_id, connection_id)
            domain = sender_email.rsplit("@", 1)[-1].lower()
            con.execute(
                "INSERT INTO school_sources (id, household_id, connection_id,"
                " sender_email, sender_domain, status, first_seen_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?, 'suggested', ?, ?)"
                " ON CONFLICT (connection_id, sender_email)"
                " DO UPDATE SET last_seen_at = excluded.last_seen_at",
                (
                    f"src-{uuid.uuid4().hex[:12]}",
                    household_id,
                    connection_id,
                    sender_email.lower(),
                    domain,
                    seen_at.isoformat(),
                    seen_at.isoformat(),
                ),
            )
            row = con.execute(
                f"SELECT {SOURCE_COLS} FROM school_sources"
                " WHERE connection_id = ? AND sender_email = ?",
                (connection_id, sender_email.lower()),
            ).fetchone()
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return _source_row(row)

    def list_sources(self, household_id: str) -> list[SchoolSource]:
        con = self._connect()
        try:
            rows = con.execute(
                f"SELECT {SOURCE_COLS} FROM school_sources"
                " WHERE household_id = ? ORDER BY last_seen_at DESC",
                (household_id,),
            ).fetchall()
        finally:
            con.close()
        return [_source_row(r) for r in rows]

    def set_source_status(
        self,
        household_id: str,
        source_id: str,
        status: Literal["confirmed", "rejected"],
    ) -> SchoolSource:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "UPDATE school_sources SET status = ?"
                " WHERE id = ? AND household_id = ?",
                (status, source_id, household_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"Unknown source: {source_id}")
            row = con.execute(
                f"SELECT {SOURCE_COLS} FROM school_sources WHERE id = ?",
                (source_id,),
            ).fetchone()
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return _source_row(row)

    def _check_connection_tenant(
        self, con: sqlite3.Connection, household_id: str, connection_id: str
    ) -> None:
        self._get_connection(con, household_id, connection_id)

    def upsert_message_header(
        self,
        household_id: str,
        connection_id: str,
        header: MessageHeader,
    ) -> MessageRecord:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            self._check_connection_tenant(con, household_id, connection_id)
            con.execute(
                "INSERT INTO messages (id, household_id, connection_id,"
                " provider_message_id, thread_id, sender_name, sender_email,"
                " reply_to, subject, received_at, source_confirmed, status,"
                " body_ref, attachment_ids)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'awaiting_source',"
                " NULL, '[]')"
                " ON CONFLICT (connection_id, provider_message_id)"
                " DO UPDATE SET thread_id = excluded.thread_id,"
                " sender_name = excluded.sender_name,"
                " sender_email = excluded.sender_email,"
                " reply_to = excluded.reply_to,"
                " subject = excluded.subject,"
                " received_at = excluded.received_at",
                (
                    f"msg-{uuid.uuid4().hex[:12]}",
                    household_id,
                    connection_id,
                    header.provider_message_id,
                    header.thread_id,
                    header.sender_name,
                    header.sender_email,
                    header.reply_to,
                    header.subject,
                    header.received_at.isoformat(),
                ),
            )
            row = con.execute(
                f"SELECT {MESSAGE_COLS} FROM messages"
                " WHERE connection_id = ? AND provider_message_id = ?",
                (connection_id, header.provider_message_id),
            ).fetchone()
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return _message_row(row)

    def save_fetched_message(
        self,
        household_id: str,
        connection_id: str,
        message: FetchedMessage,
        *,
        body_ref: str,
        attachments: list[tuple[str, str, str, list[str]]],
    ) -> MessageRecord:
        record = self.upsert_message_header(household_id, connection_id, message)
        doc_ids = [f"doc-{uuid.uuid4().hex[:12]}" for _ in attachments]
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            self._check_connection_tenant(con, household_id, connection_id)
            con.execute(
                "UPDATE messages SET body_ref = ?, source_confirmed = 1,"
                " status = 'awaiting_agent', attachment_ids = ?,"
                " manual_review_reason = NULL WHERE id = ?",
                (body_ref, json.dumps(doc_ids), record.id),
            )
            con.execute("DELETE FROM documents WHERE message_id = ?", (record.id,))
            for doc_id, (name, mime, content_ref, fields) in zip(
                doc_ids, attachments, strict=True
            ):
                con.execute(
                    "INSERT INTO documents (id, household_id, message_id, name,"
                    " mime, content_ref, acroform_fields)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        doc_id,
                        household_id,
                        record.id,
                        name,
                        mime,
                        content_ref,
                        json.dumps(fields),
                    ),
                )
            row = con.execute(
                f"SELECT {MESSAGE_COLS} FROM messages WHERE id = ?",
                (record.id,),
            ).fetchone()
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return _message_row(row)

    def set_message_status(
        self,
        household_id: str,
        connection_id: str,
        provider_message_id: str,
        status: Literal["awaiting_source", "awaiting_agent", "processed", "failed"],
        *,
        reason: str | None = None,
    ) -> None:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            self._check_connection_tenant(con, household_id, connection_id)
            cur = con.execute(
                "UPDATE messages SET status = ?, manual_review_reason = ?"
                " WHERE connection_id = ? AND provider_message_id = ?"
                " AND household_id = ?",
                (status, reason, connection_id, provider_message_id, household_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"Unknown message: {provider_message_id}")
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def list_messages(self, household_id: str) -> list[MessageRecord]:
        con = self._connect()
        try:
            rows = con.execute(
                f"SELECT {MESSAGE_COLS} FROM messages"
                " WHERE household_id = ? ORDER BY received_at",
                (household_id,),
            ).fetchall()
        finally:
            con.close()
        return [_message_row(r) for r in rows]

    def list_packets(self, household_id: str) -> list[ActionPacket]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT data FROM packets WHERE household_id = ?",
                (household_id,),
            ).fetchall()
        finally:
            con.close()
        return [ActionPacket.model_validate_json(r[0]) for r in rows]

    def get_packet(self, household_id: str, packet_id: str) -> ActionPacket:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT data FROM packets WHERE id = ? AND household_id = ?",
                (packet_id, household_id),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            raise NotFoundError(f"Unknown packet: {packet_id}")
        return ActionPacket.model_validate_json(row[0])

    def save_packet(self, household_id: str, packet: ActionPacket) -> ActionPacket:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT household_id FROM packets WHERE id = ?", (packet.id,)
            ).fetchone()
            if existing is not None and existing[0] != household_id:
                raise ConflictError(f"Packet {packet.id} belongs to another household.")
            message = con.execute(
                "SELECT household_id FROM messages WHERE id = ?",
                (packet.source_message_id,),
            ).fetchone()
            if message is None:
                raise NotFoundError(
                    f"Unknown source message: {packet.source_message_id}"
                )
            if message[0] != household_id:
                raise ConflictError(
                    "Packet source message belongs to another household."
                )
            if existing is None:
                try:
                    con.execute(
                        "INSERT INTO packets (id, household_id, source_message_id,"
                        " data, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (
                            packet.id,
                            household_id,
                            packet.source_message_id,
                            packet.model_dump_json(),
                            _now(),
                        ),
                    )
                except sqlite3.IntegrityError as e:
                    raise ConflictError(
                        "A packet already exists for this message."
                    ) from e
            else:
                con.execute(
                    "UPDATE packets SET data = ?, updated_at = ? WHERE id = ?",
                    (packet.model_dump_json(), _now(), packet.id),
                )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return packet

    def save_processed_packet(
        self,
        household_id: str,
        connection_id: str,
        provider_message_id: str,
        packet: ActionPacket,
    ) -> ActionPacket:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            self._get_connection(con, household_id, connection_id)
            row = con.execute(
                f"SELECT {MESSAGE_COLS} FROM messages"
                " WHERE household_id = ? AND connection_id = ?"
                " AND provider_message_id = ?",
                (household_id, connection_id, provider_message_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Unknown message: {provider_message_id}")
            record = _message_row(row)
            if packet.source_message_id != record.id:
                raise ConflictError("Packet is not bound to this message.")
            existing = con.execute(
                "SELECT data FROM packets"
                " WHERE household_id = ? AND source_message_id = ?",
                (household_id, record.id),
            ).fetchone()
            if existing is not None:
                con.commit()
                return ActionPacket.model_validate_json(existing[0])
            if record.status != "awaiting_agent" or not record.source_confirmed:
                raise ConflictError("Message is not ready to process.")
            try:
                con.execute(
                    "INSERT INTO packets (id, household_id, source_message_id,"
                    " data, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        packet.id,
                        household_id,
                        record.id,
                        packet.model_dump_json(),
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as e:
                raise ConflictError("A packet already exists for this message.") from e
            con.execute(
                "UPDATE messages SET status = 'processed',"
                " manual_review_reason = NULL WHERE id = ?",
                (record.id,),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return packet

    def _packet_for_proposal(
        self, con: sqlite3.Connection, household_id: str, proposal_id: str
    ) -> ActionPacket:
        rows = con.execute(
            "SELECT data FROM packets WHERE household_id = ?", (household_id,)
        ).fetchall()
        for (data,) in rows:
            packet = ActionPacket.model_validate_json(data)
            if packet.versions_for(proposal_id):
                return packet
        raise NotFoundError(f"Unknown proposal: {proposal_id}")

    def _write_packet(self, con: sqlite3.Connection, packet: ActionPacket) -> None:
        con.execute(
            "UPDATE packets SET data = ?, updated_at = ? WHERE id = ?",
            (packet.model_dump_json(), _now(), packet.id),
        )

    def edit_proposal(
        self,
        household_id: str,
        proposal_id: str,
        *,
        expected_version: int,
        payload: ProposalPayload,
    ) -> ProposalVersion:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            packet = self._packet_for_proposal(con, household_id, proposal_id)
            msg_row = con.execute(
                f"SELECT {MESSAGE_COLS} FROM messages"
                " WHERE id = ? AND household_id = ?",
                (packet.source_message_id, household_id),
            ).fetchone()
            if msg_row is None:
                raise NotFoundError(
                    f"Unknown source message: {packet.source_message_id}"
                )
            record = _message_row(msg_row)
            doc_rows = con.execute(
                f"SELECT {DOCUMENT_COLS} FROM documents"
                " WHERE message_id = ? AND household_id = ?",
                (record.id, household_id),
            ).fetchall()
            validate_proposal_payload(
                record, [_document_row(r) for r in doc_rows], payload
            )
            updated = domain.edit_proposal(
                packet.versions_for(proposal_id),
                proposal_id=proposal_id,
                expected_version=expected_version,
                new_payload=payload,
            )
            packet.proposals = [v for v in packet.proposals if v.id != proposal_id] + [
                v for v in updated if v.id == proposal_id
            ]
            self._write_packet(con, packet)
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return updated[-1]

    def approve_proposal(
        self,
        household_id: str,
        proposal_id: str,
        *,
        version: int,
        payload_hash: str,
    ) -> tuple[ProposalVersion, ExecutionRecord]:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            packet = self._packet_for_proposal(con, household_id, proposal_id)
            versions = packet.versions_for(proposal_id)
            head = max(versions, key=lambda v: v.version)
            if (
                head.status == "approved"
                and head.version == version
                and head.payload_hash == payload_hash
            ):
                execution = self._execution_for_proposal(
                    con, household_id, proposal_id, version
                )
                con.commit()
                return head, execution
            domain.approve_proposal(
                versions, version=version, payload_hash=payload_hash
            )
            packet.proposals = [
                v for v in packet.proposals if v.id != proposal_id
            ] + versions
            self._write_packet(con, packet)
            msg_row = con.execute(
                "SELECT connection_id FROM messages WHERE id = ? AND household_id = ?",
                (packet.source_message_id, household_id),
            ).fetchone()
            if msg_row is None:
                raise NotFoundError(
                    f"Unknown source message: {packet.source_message_id}"
                )
            approved = next(v for v in versions if v.version == version)
            idem = hashlib.sha256(
                f"{household_id}:{proposal_id}:{version}".encode()
            ).hexdigest()
            now = _now()
            execution_id = f"exec-{uuid.uuid4().hex[:12]}"
            try:
                con.execute(
                    "INSERT INTO executions (id, household_id, proposal_id,"
                    " proposal_version, connection_id, idempotency_key, status,"
                    " attempts, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'pending_dispatch', 0, ?, ?)",
                    (
                        execution_id,
                        household_id,
                        proposal_id,
                        version,
                        msg_row[0],
                        idem,
                        now,
                        now,
                    ),
                )
                con.execute(
                    "INSERT INTO execution_outbox"
                    " (id, execution_id, status, attempts, created_at, updated_at)"
                    " VALUES (?, ?, 'pending', 0, ?, ?)",
                    (f"out-{uuid.uuid4().hex[:12]}", execution_id, now, now),
                )
            except sqlite3.IntegrityError:
                row = con.execute(
                    f"SELECT {', '.join(EXECUTION_COLS)} FROM executions"
                    " WHERE household_id = ? AND proposal_id = ?"
                    " AND proposal_version = ?",
                    (household_id, proposal_id, version),
                ).fetchone()
                execution = _execution_row(row)
            else:
                execution = ExecutionRecord(
                    id=execution_id,
                    household_id=household_id,
                    proposal_id=proposal_id,
                    proposal_version=version,
                    connection_id=msg_row[0],
                    idempotency_key=idem,
                    status="pending_dispatch",
                    attempts=0,
                    provider_operation_id=None,
                    output_ref=None,
                    safe_error=None,
                    created_at=datetime.fromisoformat(now),
                    updated_at=datetime.fromisoformat(now),
                )
                self._audit(
                    con,
                    household_id,
                    "proposal_approved",
                    proposal_id,
                    f"version {version}",
                )
                self._audit(
                    con,
                    household_id,
                    "execution_created",
                    execution_id,
                    approved.payload.kind,
                )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return approved, execution

    def reject_proposal(
        self,
        household_id: str,
        proposal_id: str,
        *,
        version: int,
        payload_hash: str,
    ) -> ProposalVersion:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            packet = self._packet_for_proposal(con, household_id, proposal_id)
            versions = packet.versions_for(proposal_id)
            domain.reject_proposal(versions, version=version, payload_hash=payload_hash)
            packet.proposals = [
                v for v in packet.proposals if v.id != proposal_id
            ] + versions
            self._write_packet(con, packet)
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return next(v for v in versions if v.version == version)

    def get_message_record(self, household_id: str, message_id: str) -> MessageRecord:
        con = self._connect()
        try:
            row = con.execute(
                f"SELECT {MESSAGE_COLS} FROM messages"
                " WHERE id = ? AND household_id = ?",
                (message_id, household_id),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            raise NotFoundError(f"Unknown message: {message_id}")
        return _message_row(row)

    def get_document_record(
        self, household_id: str, document_id: str
    ) -> DocumentRecord:
        con = self._connect()
        try:
            row = con.execute(
                f"SELECT {DOCUMENT_COLS} FROM documents"
                " WHERE id = ? AND household_id = ?",
                (document_id, household_id),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            raise NotFoundError(f"Unknown document: {document_id}")
        return _document_row(row)

    def list_document_records(
        self, household_id: str, message_id: str
    ) -> list[DocumentRecord]:
        con = self._connect()
        try:
            owned = con.execute(
                "SELECT 1 FROM messages WHERE id = ? AND household_id = ?",
                (message_id, household_id),
            ).fetchone()
            if owned is None:
                raise NotFoundError(f"Unknown message: {message_id}")
            rows = con.execute(
                f"SELECT {DOCUMENT_COLS} FROM documents"
                " WHERE message_id = ? AND household_id = ? ORDER BY name",
                (message_id, household_id),
            ).fetchall()
        finally:
            con.close()
        return [_document_row(r) for r in rows]

    def list_calendar_records(self, household_id: str) -> list[CalendarRecord]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id, household_id, title, starts_at, ends_at"
                " FROM calendar_records WHERE household_id = ?",
                (household_id,),
            ).fetchall()
        finally:
            con.close()
        return [
            CalendarRecord(
                id=r[0],
                household_id=r[1],
                title=r[2],
                starts_at=r[3],
                ends_at=r[4],
            )
            for r in rows
        ]

    def get_packet_for_message(
        self, household_id: str, message_id: str
    ) -> ActionPacket | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT data FROM packets"
                " WHERE household_id = ? AND source_message_id = ?",
                (household_id, message_id),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return ActionPacket.model_validate_json(row[0])

    def find_connection_by_email(self, provider: str, email: str) -> Connection | None:
        con = self._connect()
        try:
            row = con.execute(
                f"SELECT {CONNECTION_COLS} FROM connections"
                " WHERE provider = ? AND lower(email) = lower(?)"
                " AND status = 'connected' LIMIT 1",
                (provider, email),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return _connection_row(row)

    def enqueue_ingestion_event(
        self, event: IngestionEvent, *, dedupe_key: str
    ) -> bool:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO ingestion_events (id, dedupe_key, payload, status,"
                " attempts, available_at, created_at)"
                " VALUES (?, ?, ?, 'pending', 0, ?, ?)",
                (
                    event.id,
                    dedupe_key,
                    event.model_dump_json(),
                    event.received_at.isoformat(),
                    _now(),
                ),
            )
            con.commit()
            return True
        except sqlite3.IntegrityError:
            con.rollback()
            return False
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def claim_ingestion_events(
        self, limit: int, now: datetime, lease_seconds: int
    ) -> list[IngestionEvent]:
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT id, payload FROM ingestion_events"
                " WHERE status IN ('pending', 'processing')"
                " AND available_at <= ? ORDER BY created_at LIMIT ?",
                (now.isoformat(), limit),
            ).fetchall()
            claimed: list[IngestionEvent] = []
            for event_id, payload in rows:
                con.execute(
                    "UPDATE ingestion_events SET status = 'processing',"
                    " attempts = attempts + 1, available_at = ? WHERE id = ?",
                    (lease_until, event_id),
                )
                claimed.append(IngestionEvent.model_validate_json(payload))
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return claimed

    def complete_ingestion_event(self, event_id: str) -> None:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "UPDATE ingestion_events SET status = 'completed' WHERE id = ?",
                (event_id,),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"Unknown ingestion event: {event_id}")
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def fail_ingestion_event(
        self,
        event_id: str,
        retry_at: datetime,
        max_attempts: int = 5,
    ) -> None:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT attempts FROM ingestion_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Unknown ingestion event: {event_id}")
            if row[0] >= max_attempts:
                con.execute(
                    "UPDATE ingestion_events SET status = 'failed' WHERE id = ?",
                    (event_id,),
                )
            else:
                con.execute(
                    "UPDATE ingestion_events SET status = 'pending',"
                    " available_at = ? WHERE id = ?",
                    (retry_at.isoformat(), event_id),
                )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def upsert_webhook_subscription(
        self,
        household_id: str,
        connection_id: str,
        *,
        provider: str,
        provider_subscription_id: str,
        client_state_hash: str,
        cursor: str | None = None,
        expires_at: datetime | None = None,
    ) -> WebhookSubscription:
        now = datetime.now(UTC)
        sub_id = f"sub-{uuid.uuid4().hex[:12]}"
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            self._get_connection(con, household_id, connection_id)
            existing = con.execute(
                "SELECT id FROM webhook_subscriptions"
                " WHERE household_id = ? AND connection_id = ?"
                " ORDER BY created_at DESC LIMIT 1",
                (household_id, connection_id),
            ).fetchone()
            try:
                if existing is not None:
                    con.execute(
                        "UPDATE webhook_subscriptions SET provider = ?,"
                        " provider_subscription_id = ?, client_state_hash = ?,"
                        " cursor = ?, expires_at = ?, status = 'active',"
                        " updated_at = ? WHERE id = ?",
                        (
                            provider,
                            provider_subscription_id,
                            client_state_hash,
                            cursor,
                            expires_at.isoformat() if expires_at else None,
                            now.isoformat(),
                            existing[0],
                        ),
                    )
                    sub_id = existing[0]
                else:
                    con.execute(
                        "INSERT INTO webhook_subscriptions (id, household_id,"
                        " connection_id, provider, provider_subscription_id,"
                        " client_state_hash, cursor, expires_at, status,"
                        " created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                        (
                            sub_id,
                            household_id,
                            connection_id,
                            provider,
                            provider_subscription_id,
                            client_state_hash,
                            cursor,
                            expires_at.isoformat() if expires_at else None,
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )
            except sqlite3.IntegrityError as e:
                raise ConflictError(
                    "This subscription is already attached elsewhere."
                ) from e
            row = con.execute(
                "SELECT id, household_id, connection_id, provider,"
                " provider_subscription_id, client_state_hash, cursor,"
                " expires_at, status, created_at, updated_at"
                " FROM webhook_subscriptions"
                " WHERE provider = ? AND provider_subscription_id = ?",
                (provider, provider_subscription_id),
            ).fetchone()
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        if row is None:
            raise ConflictError("Webhook subscription upsert failed.")
        return _subscription_row(row)

    def get_webhook_subscription(
        self, provider: str, provider_subscription_id: str
    ) -> WebhookSubscription | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT id, household_id, connection_id, provider,"
                " provider_subscription_id, client_state_hash, cursor,"
                " expires_at, status, created_at, updated_at"
                " FROM webhook_subscriptions"
                " WHERE provider = ? AND provider_subscription_id = ?",
                (provider, provider_subscription_id),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return _subscription_row(row)

    def get_subscription_for_connection(
        self, household_id: str, connection_id: str
    ) -> WebhookSubscription | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT id, household_id, connection_id, provider,"
                " provider_subscription_id, client_state_hash, cursor,"
                " expires_at, status, created_at, updated_at"
                " FROM webhook_subscriptions"
                " WHERE household_id = ? AND connection_id = ?"
                " ORDER BY created_at DESC LIMIT 1",
                (household_id, connection_id),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return _subscription_row(row)

    def get_active_subscription_for_connection(
        self, household_id: str, connection_id: str
    ) -> WebhookSubscription | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT id, household_id, connection_id, provider,"
                " provider_subscription_id, client_state_hash, cursor,"
                " expires_at, status, created_at, updated_at"
                " FROM webhook_subscriptions"
                " WHERE household_id = ? AND connection_id = ?"
                " AND status = 'active' ORDER BY created_at DESC LIMIT 1",
                (household_id, connection_id),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return _subscription_row(row)

    def set_webhook_subscription_status(
        self,
        household_id: str,
        subscription_id: str,
        status: Literal["active", "renewal_due", "expired", "reauthorization_required"],
    ) -> WebhookSubscription:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "UPDATE webhook_subscriptions SET status = ?, updated_at = ?"
                " WHERE id = ? AND household_id = ?",
                (status, _now(), subscription_id, household_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"Unknown webhook subscription: {subscription_id}")
            row = con.execute(
                "SELECT id, household_id, connection_id, provider,"
                " provider_subscription_id, client_state_hash, cursor,"
                " expires_at, status, created_at, updated_at"
                " FROM webhook_subscriptions WHERE id = ?",
                (subscription_id,),
            ).fetchone()
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return _subscription_row(row)

    def update_webhook_subscription(
        self,
        household_id: str,
        subscription_id: str,
        *,
        status: object = _UNSET,
        cursor: object = _UNSET,
    ) -> WebhookSubscription:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            sets = ["updated_at = ?"]
            values: list[Any] = [_now()]
            if status is not _UNSET:
                sets.append("status = ?")
                values.append(status)
            if cursor is not _UNSET:
                sets.append("cursor = ?")
                values.append(cursor)
            cur = con.execute(
                f"UPDATE webhook_subscriptions SET {', '.join(sets)}"
                " WHERE id = ? AND household_id = ?",
                (*values, subscription_id, household_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"Unknown webhook subscription: {subscription_id}")
            row = con.execute(
                "SELECT id, household_id, connection_id, provider,"
                " provider_subscription_id, client_state_hash, cursor,"
                " expires_at, status, created_at, updated_at"
                " FROM webhook_subscriptions WHERE id = ?",
                (subscription_id,),
            ).fetchone()
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return _subscription_row(row)

    def list_subscriptions_due(
        self,
        *,
        gmail_expiring_before: datetime,
        gmail_stale_before: datetime,
        outlook_expiring_before: datetime,
    ) -> list[WebhookSubscription]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id, household_id, connection_id, provider,"
                " provider_subscription_id, client_state_hash, cursor,"
                " expires_at, status, created_at, updated_at"
                " FROM webhook_subscriptions"
                " WHERE status IN ('active', 'renewal_due', 'expired')"
                " AND (status != 'active'"
                " OR (provider = 'gmail' AND (expires_at <= ? OR updated_at <= ?))"
                " OR (provider = 'outlook' AND expires_at <= ?))",
                (
                    gmail_expiring_before.isoformat(),
                    gmail_stale_before.isoformat(),
                    outlook_expiring_before.isoformat(),
                ),
            ).fetchall()
        finally:
            con.close()
        return [_subscription_row(r) for r in rows]

    def get_household_by_id(self, household_id: str) -> Household | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT id, name, timezone, created_at FROM households WHERE id = ?",
                (household_id,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return Household(id=row[0], name=row[1], timezone=row[2], created_at=row[3])

    def list_memberships_for_user(self, user_id: str) -> list[Membership]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT household_id, user_id, email, role, created_at"
                " FROM memberships WHERE user_id = ? ORDER BY created_at",
                (user_id,),
            ).fetchall()
        finally:
            con.close()
        return [_membership_row(r) for r in rows]

    def get_membership(self, household_id: str, user_id: str) -> Membership | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT household_id, user_id, email, role, created_at"
                " FROM memberships WHERE household_id = ? AND user_id = ?",
                (household_id, user_id),
            ).fetchone()
        finally:
            con.close()
        return _membership_row(row) if row else None

    def list_memberships(self, household_id: str) -> list[Membership]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT household_id, user_id, email, role, created_at"
                " FROM memberships WHERE household_id = ? ORDER BY created_at",
                (household_id,),
            ).fetchall()
        finally:
            con.close()
        return [_membership_row(r) for r in rows]

    def create_household_for_owner(
        self, principal: Principal, *, name: str, timezone: str
    ) -> Household:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ModuleNotFoundError, ValueError) as e:
            raise BadRequestError(f"Unknown IANA timezone: {timezone}") from e
        household = Household(
            id=f"hh-{uuid.uuid4().hex[:12]}",
            name=name,
            timezone=timezone,
            created_at=datetime.now(UTC),
        )
        membership = Membership(
            household_id=household.id,
            user_id=principal.user_id,
            email=normalize_email(principal.email),
            role="owner",
            created_at=household.created_at,
        )
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO households (id, name, timezone, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    household.id,
                    household.name,
                    household.timezone,
                    household.created_at.isoformat(),
                ),
            )
            con.execute(
                "INSERT INTO memberships"
                " (household_id, user_id, email, role, created_at)"
                " VALUES (?, ?, ?, 'owner', ?)",
                (
                    membership.household_id,
                    membership.user_id,
                    membership.email,
                    membership.created_at.isoformat(),
                ),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return household

    def create_invitation(
        self,
        household_id: str,
        *,
        email: str,
        role: Literal["editor", "viewer"],
        expires_at: datetime,
    ) -> tuple[Invitation, str]:
        if role == "owner":  # type: ignore[comparison-overlap]
            raise BadRequestError("The owner role cannot be invited.")
        normalized = normalize_email(email)
        if not normalized or "@" not in normalized or len(normalized) > 320:
            raise BadRequestError("Enter a valid email address.")
        invite_code = secrets.token_urlsafe(32)
        invitation = Invitation(
            id=f"inv-{uuid.uuid4().hex[:12]}",
            household_id=household_id,
            email=normalized,
            role=role,
            status="pending",
            expires_at=expires_at,
            created_at=datetime.now(UTC),
        )
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            if (
                con.execute(
                    "SELECT 1 FROM households WHERE id = ?", (household_id,)
                ).fetchone()
                is None
            ):
                raise NotFoundError(f"Unknown household: {household_id}")
            if (
                con.execute(
                    "SELECT 1 FROM memberships WHERE household_id = ? AND email = ?",
                    (household_id, normalized),
                ).fetchone()
                is not None
            ):
                raise ConflictError("That caregiver is already a member.")
            try:
                con.execute(
                    "INSERT INTO invitations (id, household_id, email, role,"
                    " status, token_hash, expires_at, created_at)"
                    " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
                    (
                        invitation.id,
                        household_id,
                        normalized,
                        role,
                        hashlib.sha256(invite_code.encode()).hexdigest(),
                        expires_at.isoformat(),
                        invitation.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as e:
                raise ConflictError(
                    "An invitation is already pending for this email."
                ) from e
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return invitation, invite_code

    def list_invitations(self, household_id: str) -> list[Invitation]:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE invitations SET status = 'expired'"
                " WHERE household_id = ? AND status = 'pending'"
                " AND expires_at <= ?",
                (household_id, _now()),
            )
            rows = con.execute(
                "SELECT id, household_id, email, role, status, expires_at,"
                " created_at FROM invitations WHERE household_id = ?"
                " ORDER BY created_at",
                (household_id,),
            ).fetchall()
            con.commit()
        finally:
            con.close()
        return [_invitation_row(r) for r in rows]

    def revoke_invitation(self, household_id: str, invitation_id: str) -> Invitation:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT id, household_id, email, role, status, expires_at,"
                " created_at FROM invitations"
                " WHERE id = ? AND household_id = ?",
                (invitation_id, household_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Unknown invitation: {invitation_id}")
            invitation = _invitation_row(row)
            if invitation.status != "pending":
                raise ConflictError("This invitation is no longer pending.")
            con.execute(
                "UPDATE invitations SET status = 'revoked' WHERE id = ?",
                (invitation_id,),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return invitation.model_copy(update={"status": "revoked"})

    def accept_invitation(self, principal: Principal, token: str) -> Membership:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT id, household_id, email, role, status, expires_at,"
                " created_at FROM invitations WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                raise ConflictError("This invitation is invalid or expired.")
            invitation = _invitation_row(row)
            if invitation.status != "pending":
                raise ConflictError("This invitation is invalid or expired.")
            if invitation.expires_at <= datetime.now(UTC):
                con.execute(
                    "UPDATE invitations SET status = 'expired' WHERE id = ?",
                    (invitation.id,),
                )
                con.commit()
                raise ConflictError("This invitation is invalid or expired.")
            if not hmac.compare_digest(
                normalize_email(principal.email), invitation.email
            ):
                raise ConflictError(
                    "This invitation was sent to a different email address."
                )
            if (
                con.execute(
                    "SELECT 1 FROM memberships WHERE household_id = ? AND user_id = ?",
                    (invitation.household_id, principal.user_id),
                ).fetchone()
                is not None
            ):
                raise ConflictError("You are already a member.")
            con.execute(
                "UPDATE invitations SET status = 'accepted' WHERE id = ?"
                " AND status = 'pending'",
                (invitation.id,),
            )
            membership = Membership(
                household_id=invitation.household_id,
                user_id=principal.user_id,
                email=invitation.email,
                role=invitation.role,
                created_at=datetime.now(UTC),
            )
            try:
                con.execute(
                    "INSERT INTO memberships"
                    " (household_id, user_id, email, role, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        membership.household_id,
                        membership.user_id,
                        membership.email,
                        membership.role,
                        membership.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as e:
                raise ConflictError("You are already a member.") from e
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return membership

    def _require_not_last_owner(
        self,
        con: sqlite3.Connection,
        household_id: str,
        membership: Membership,
    ) -> None:
        if membership.role != "owner":
            return
        owners = con.execute(
            "SELECT COUNT(*) FROM memberships WHERE household_id = ?"
            " AND role = 'owner'",
            (household_id,),
        ).fetchone()[0]
        if owners <= 1:
            raise ConflictError("A household must keep at least one owner.")

    def change_member_role(
        self,
        household_id: str,
        user_id: str,
        role: Literal["owner", "editor", "viewer"],
    ) -> Membership:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT household_id, user_id, email, role, created_at"
                " FROM memberships WHERE household_id = ? AND user_id = ?",
                (household_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Unknown member: {user_id}")
            membership = _membership_row(row)
            if role != "owner":
                self._require_not_last_owner(con, household_id, membership)
            con.execute(
                "UPDATE memberships SET role = ?"
                " WHERE household_id = ? AND user_id = ?",
                (role, household_id, user_id),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return membership.model_copy(update={"role": role})

    def remove_member(self, household_id: str, user_id: str) -> None:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT household_id, user_id, email, role, created_at"
                " FROM memberships WHERE household_id = ? AND user_id = ?",
                (household_id, user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Unknown member: {user_id}")
            self._require_not_last_owner(con, household_id, _membership_row(row))
            con.execute(
                "DELETE FROM memberships WHERE household_id = ? AND user_id = ?",
                (household_id, user_id),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def get_packet_by_proposal(
        self, household_id: str, proposal_id: str
    ) -> ActionPacket:
        con = self._connect()
        try:
            return self._packet_for_proposal(con, household_id, proposal_id)
        finally:
            con.close()

    def list_executions(self, household_id: str) -> list[ExecutionRecord]:
        con = self._connect()
        try:
            rows = con.execute(
                f"SELECT {', '.join(EXECUTION_COLS)} FROM executions"
                " WHERE household_id = ? ORDER BY created_at",
                (household_id,),
            ).fetchall()
        finally:
            con.close()
        return [_execution_row(r) for r in rows]

    def get_execution(self, household_id: str, execution_id: str) -> ExecutionRecord:
        con = self._connect()
        try:
            row = con.execute(
                f"SELECT {', '.join(EXECUTION_COLS)} FROM executions"
                " WHERE id = ? AND household_id = ?",
                (execution_id, household_id),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            raise NotFoundError(f"Unknown execution: {execution_id}")
        return _execution_row(row)

    def claim_execution(
        self,
        household_id: str,
        execution_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ExecutionRecord | None:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "UPDATE executions SET status = 'executing',"
                " attempts = attempts + 1, lease_until = ?, updated_at = ?"
                " WHERE id = ? AND household_id = ? AND ("
                " status IN ('pending_dispatch', 'queued')"
                " OR (status = 'executing' AND lease_until IS NOT NULL"
                " AND lease_until <= ?))",
                (
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    execution_id,
                    household_id,
                    now.isoformat(),
                ),
            )
            if cur.rowcount == 0:
                con.rollback()
                return None
            row = con.execute(
                f"SELECT {', '.join(EXECUTION_COLS)} FROM executions WHERE id = ?",
                (execution_id,),
            ).fetchone()
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return _execution_row(row)

    def list_dispatchable_executions(self, limit: int) -> list[ExecutionRecord]:
        con = self._connect()
        try:
            rows = con.execute(
                f"SELECT {', '.join('e.' + c for c in EXECUTION_COLS)}"
                " FROM executions e JOIN execution_outbox o"
                " ON o.execution_id = e.id"
                " WHERE o.status = 'pending' AND e.status = 'pending_dispatch'"
                " ORDER BY e.created_at LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            con.close()
        return [_execution_row(r) for r in rows]

    def mark_execution_dispatched(
        self, household_id: str, execution_id: str
    ) -> ExecutionRecord:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE execution_outbox SET status = 'dispatched',"
                " attempts = attempts + 1, updated_at = ?"
                " WHERE execution_id = ? AND status = 'pending'",
                (_now(), execution_id),
            )
            con.execute(
                "UPDATE executions SET status = 'queued', updated_at = ?"
                " WHERE id = ? AND household_id = ?"
                " AND status = 'pending_dispatch'",
                (_now(), execution_id, household_id),
            )
            self._audit(
                con, household_id, "execution_dispatched", execution_id, "queued"
            )
            row = con.execute(
                f"SELECT {', '.join(EXECUTION_COLS)} FROM executions WHERE id = ?",
                (execution_id,),
            ).fetchone()
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        if row is None:
            raise NotFoundError(f"Unknown execution: {execution_id}")
        return _execution_row(row)

    def record_outbox_failure(self, execution_id: str) -> None:
        con = self._connect()
        try:
            con.execute(
                "UPDATE execution_outbox SET attempts = attempts + 1,"
                " updated_at = ? WHERE execution_id = ? AND status = 'pending'",
                (_now(), execution_id),
            )
            con.commit()
        finally:
            con.close()

    def update_execution_checkpoint(
        self, household_id: str, execution_id: str, provider_operation_id: str
    ) -> None:
        con = self._connect()
        try:
            con.execute(
                "UPDATE executions SET provider_operation_id = ?, updated_at = ?"
                " WHERE id = ? AND household_id = ? AND status = 'executing'",
                (provider_operation_id, _now(), execution_id, household_id),
            )
            con.commit()
        finally:
            con.close()

    def update_execution_output(
        self, household_id: str, execution_id: str, output_ref: str
    ) -> None:
        con = self._connect()
        try:
            con.execute(
                "UPDATE executions SET output_ref = ?, updated_at = ?"
                " WHERE id = ? AND household_id = ? AND status = 'executing'",
                (output_ref, _now(), execution_id, household_id),
            )
            con.commit()
        finally:
            con.close()

    def settle_execution(
        self,
        household_id: str,
        execution_id: str,
        *,
        attempts: int,
        status: str,
        provider_operation_id: object = _UNSET,
        output_ref: object = _UNSET,
        safe_error: object = _UNSET,
    ) -> bool:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            sets = ["status = ?", "lease_until = NULL", "updated_at = ?"]
            values: list[Any] = [status, _now()]
            if provider_operation_id is not _UNSET:
                sets.append("provider_operation_id = ?")
                values.append(provider_operation_id)
            if output_ref is not _UNSET:
                sets.append("output_ref = ?")
                values.append(output_ref)
            if safe_error is not _UNSET:
                sets.append("safe_error = ?")
                values.append(safe_error)
            cur = con.execute(
                f"UPDATE executions SET {', '.join(sets)}"
                " WHERE id = ? AND household_id = ?"
                " AND status = 'executing' AND attempts = ?",
                (*values, execution_id, household_id, attempts),
            )
            if cur.rowcount == 0:
                con.rollback()
                return False
            if status in ("completed", "failed", "delivery_uncertain"):
                self._audit(
                    con,
                    household_id,
                    f"execution_{status}",
                    execution_id,
                    str(safe_error) if safe_error not in (_UNSET, None) else "",
                )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return True

    def record_audit(
        self, household_id: str, kind: str, entity_id: str, detail: str
    ) -> None:
        con = self._connect()
        try:
            self._audit(con, household_id, kind, entity_id, detail)
            con.commit()
        finally:
            con.close()

    def _execution_for_proposal(
        self,
        con: sqlite3.Connection,
        household_id: str,
        proposal_id: str,
        version: int,
    ) -> ExecutionRecord:
        row = con.execute(
            f"SELECT {', '.join(EXECUTION_COLS)} FROM executions"
            " WHERE household_id = ? AND proposal_id = ? AND proposal_version = ?",
            (household_id, proposal_id, version),
        ).fetchone()
        if row is None:
            raise ConflictError("Approved proposal has no execution record.")
        return _execution_row(row)

    @staticmethod
    def _audit(
        con: sqlite3.Connection,
        household_id: str,
        kind: str,
        entity_id: str,
        detail: str,
    ) -> None:
        con.execute(
            "INSERT INTO audit_events"
            " (id, household_id, kind, entity_id, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"aud-{uuid.uuid4().hex[:12]}",
                household_id,
                kind,
                entity_id,
                detail[:500],
                _now(),
            ),
        )


def _membership_row(row: tuple[Any, ...]) -> Membership:
    return Membership(
        household_id=row[0],
        user_id=row[1],
        email=row[2],
        role=row[3],
        created_at=row[4],
    )


def _invitation_row(row: tuple[Any, ...]) -> Invitation:
    return Invitation(
        id=row[0],
        household_id=row[1],
        email=row[2],
        role=row[3],
        status=row[4],
        expires_at=row[5],
        created_at=row[6],
    )


def _subscription_row(row: tuple[Any, ...]) -> WebhookSubscription:
    return WebhookSubscription(
        id=row[0],
        household_id=row[1],
        connection_id=row[2],
        provider=row[3],
        provider_subscription_id=row[4],
        client_state_hash=row[5],
        cursor=row[6],
        expires_at=_ts(row[7]),
        status=row[8],
        created_at=row[9],
        updated_at=row[10],
    )


def _execution_row(row: tuple[Any, ...]) -> ExecutionRecord:
    return ExecutionRecord(
        id=row[0],
        household_id=row[1],
        proposal_id=row[2],
        proposal_version=row[3],
        connection_id=row[4],
        idempotency_key=row[5],
        status=row[6],
        attempts=row[7],
        provider_operation_id=row[8],
        output_ref=row[9],
        safe_error=row[10],
        created_at=datetime.fromisoformat(row[12]),
        updated_at=datetime.fromisoformat(row[13]),
    )
