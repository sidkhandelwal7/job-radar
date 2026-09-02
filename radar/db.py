"""SQLite access: connection factory, migrations, small helpers.

One database file, WAL mode, foreign keys on. Migrations are numbered .sql files in
radar/migrations applied in order and recorded in schema_migrations.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from radar.util import utcnow_iso

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(db_path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(
            db_path, timeout=30, isolation_level=None
        )  # autocommit; we manage txns
        # a replay/backfill can leave a multi-GB WAL file behind; truncate it at the next checkpoint
        conn.execute("PRAGMA journal_size_limit = 268435456")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations. Returns the list of applied migration names."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version in done:
            continue
        sql = path.read_text()
        # executescript() commits any open transaction first, so wrap the script itself.
        conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, path.name, utcnow_iso()),
        )
        applied.append(path.name)
    return applied


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction. Nested use is not supported; keep transactions short."""
    if conn.in_transaction:
        # Already inside one (caller composed us) — just run.
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def all_rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def scalar(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> Any:
    row = conn.execute(sql, tuple(params)).fetchone()
    return None if row is None else row[0]


def insert(conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> int:
    cols = list(values)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    cur = conn.execute(sql, [values[c] for c in cols])
    return int(cur.lastrowid or 0)


def update(conn: sqlite3.Connection, table: str, row_id: int, values: dict[str, Any]) -> None:
    if not values:
        return
    sets = ", ".join(f"{c} = ?" for c in values)
    conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*values.values(), row_id])


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k, v in list(d.items()):
        if k.endswith("_json") and isinstance(v, str):
            try:
                d[k[:-5]] = json.loads(v)
            except json.JSONDecodeError:
                d[k[:-5]] = None
    return d


def kv_get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = one(conn, "SELECT value_json FROM kv WHERE key = ?", (key,))
    return default if row is None else json.loads(row[0])


def kv_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO kv (key, value_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
        (key, json.dumps(value), utcnow_iso()),
    )


def add_event(
    conn: sqlite3.Connection,
    posting_id: int,
    event_type: str,
    data: dict[str, Any] | None = None,
    run_id: int | None = None,
) -> int:
    return insert(
        conn,
        "posting_events",
        {
            "posting_id": posting_id,
            "run_id": run_id,
            "event_type": event_type,
            "at": utcnow_iso(),
            "data_json": json.dumps(data or {}, default=str),
        },
    )


def start_run(conn: sqlite3.Connection, kind: str, host: str = "laptop") -> int:
    # a process killed mid-run (sleep, power, launchctl unload) never calls finish_run; don't let
    # those rows sit as "running" forever in health/Health
    conn.execute(
        "UPDATE runs SET status = 'aborted', finished_at = ?, error = COALESCE(error, 'no finish recorded (process killed?)') "
        "WHERE status = 'running' AND started_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-12 hours')",
        (utcnow_iso(),),
    )
    return insert(conn, "runs", {"kind": kind, "started_at": utcnow_iso(), "host": host})


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str = "ok",
    stats: dict[str, Any] | None = None,
    error: str | None = None,
    llm_calls: int | None = None,
    llm_models: dict[str, int] | None = None,
) -> None:
    values: dict[str, Any] = {
        "finished_at": utcnow_iso(),
        "status": status,
        "stats_json": json.dumps(stats or {}, default=str),
        "error": error,
    }
    # the enrichment pipeline writes its own accounting onto the run; don't zero it here
    if llm_calls is not None:
        values["llm_calls"] = llm_calls
    if llm_models is not None:
        values["llm_models_json"] = json.dumps(llm_models)
    update(conn, "runs", run_id, values)


DOC_COLUMNS = (
    "description_md",
    "score_explanation_json",
    "beats_baseline_decomposition_json",
    "requirements_json",
)


def upsert_doc(conn: sqlite3.Connection, posting_id: int, **cols: Any) -> None:
    """Write big-text columns for a posting into posting_docs (insert or update only the given keys)."""
    cols = {k: v for k, v in cols.items() if k in DOC_COLUMNS or k in ("title", "company_name")}
    if not cols:
        return
    names = ["posting_id", *cols]
    sets = ", ".join(f"{k} = excluded.{k}" for k in cols)
    conn.execute(
        f"INSERT INTO posting_docs ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)}) "
        f"ON CONFLICT(posting_id) DO UPDATE SET {sets}",
        [posting_id, *cols.values()],
    )


def insert_posting(conn: sqlite3.Connection, values: dict[str, Any]) -> int:
    """Insert a posting row, routing description/explanation/requirements text to posting_docs."""
    docs = {k: values.pop(k) for k in list(values) if k in DOC_COLUMNS}
    pid = insert(conn, "postings", values)
    upsert_doc(
        conn, pid, title=values.get("title"), company_name=values.get("company_name"), **docs
    )
    return pid


def get_doc(conn: sqlite3.Connection, posting_id: int) -> dict[str, Any]:
    row = one(conn, "SELECT * FROM posting_docs WHERE posting_id = ?", (posting_id,))
    return dict(row) if row else {}
