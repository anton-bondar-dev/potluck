"""Durable pool store — the pool is the source of truth, the agent is disposable.

SQLite in WAL mode, single writer behind an asyncio lock. Three rules:

  1. MONEY IS INTEGER CENTS, everywhere, with no exceptions.
  2. PHASE TRANSITIONS GO THROUGH advance(), which is rowcount-guarded. Two concurrent
     callers can never both win, which is what makes "settle exactly once" true rather
     than hopeful.
  3. goal_text and policy_text are VERBATIM HUMAN TEXT AND CODE NEVER PARSES THEM.
     They are stored raw, shown raw and quoted back raw. There is deliberately no
     machine-readable representation of the success condition anywhere in this file —
     that is what makes the RELEASE/UNWIND decision genuinely the model's.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import aiosqlite

from config import settings

# DRAFT -> COLLECTING -> RESOLVING -> SETTLING -> CLOSED, with ESCALATED as the
# always-available safe harbour. Anything not in this table is impossible by construction.
ALLOWED_EDGES: set[tuple[str, str]] = {
    ("DRAFT", "COLLECTING"),
    ("COLLECTING", "RESOLVING"),
    ("COLLECTING", "ESCALATED"),
    ("RESOLVING", "SETTLING"),
    ("RESOLVING", "ESCALATED"),
    ("SETTLING", "CLOSED"),
    ("SETTLING", "ESCALATED"),
    # A human approving the refund on an escalated pool executes it through the same
    # settlement path the agent would have used, so it lands in SETTLING like any other.
    ("ESCALATED", "SETTLING"),
    ("ESCALATED", "CLOSED"),
}

TERMINAL_INVOICE_STATUSES = {"PAID", "MARKED_AS_PAID", "CANCELLED", "REFUNDED"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (
  pool_id      TEXT PRIMARY KEY,
  phase        TEXT NOT NULL,
  goal_text    TEXT NOT NULL,
  policy_text  TEXT NOT NULL,
  success_condition_restated TEXT,
  recipient_cid TEXT,
  target_cents INTEGER NOT NULL DEFAULT 0,
  currency     TEXT NOT NULL DEFAULT 'USD',
  deadline_at  REAL,
  deadline_label TEXT,
  clock_scale  INTEGER NOT NULL DEFAULT 1,
  overage      TEXT NOT NULL DEFAULT 'refund_prorata',
  ledger_version INTEGER NOT NULL DEFAULT 0,
  episodes_used  INTEGER NOT NULL DEFAULT 0,
  claimed_until  REAL NOT NULL DEFAULT 0,
  demo         INTEGER NOT NULL DEFAULT 0,
  created_at   REAL NOT NULL,
  closed_at    REAL
);

CREATE TABLE IF NOT EXISTS contributors (
  pool_id      TEXT NOT NULL,
  cid          TEXT NOT NULL,
  name         TEXT NOT NULL,
  email        TEXT NOT NULL,
  share_cents  INTEGER NOT NULL,
  is_recipient INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (pool_id, cid)
);

CREATE TABLE IF NOT EXISTS invoices (
  pool_id    TEXT NOT NULL,
  cid        TEXT NOT NULL,
  invoice_id TEXT,
  payer_url  TEXT,
  status     TEXT NOT NULL DEFAULT 'pending',
  paid_cents INTEGER NOT NULL DEFAULT 0,
  source     TEXT,
  reminders  INTEGER NOT NULL DEFAULT 0,
  settled_cents INTEGER NOT NULL DEFAULT 0,
  last_reminder_at REAL,
  sent_at    REAL,
  paid_at    REAL,
  PRIMARY KEY (pool_id, cid)
);

CREATE TABLE IF NOT EXISTS settlements (
  pool_id   TEXT PRIMARY KEY,
  decision  TEXT NOT NULL,
  reason    TEXT,
  policy_quote TEXT,
  frozen_collected_cents INTEGER NOT NULL,
  sender_batch_id TEXT NOT NULL,
  batch_id  TEXT,
  batch_status TEXT,
  items_json TEXT NOT NULL,
  decided_by TEXT NOT NULL DEFAULT 'agent',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
  episode_id TEXT PRIMARY KEY,
  pool_id    TEXT NOT NULL,
  trigger    TEXT NOT NULL,
  turns      INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  reason     TEXT,
  messages_json TEXT,
  started_at REAL NOT NULL,
  ended_at   REAL
);

CREATE TABLE IF NOT EXISTS events (
  seq     INTEGER PRIMARY KEY AUTOINCREMENT,
  pool_id TEXT NOT NULL,
  ts      REAL NOT NULL,
  type    TEXT NOT NULL,
  data    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_pool_seq ON events(pool_id, seq);
CREATE INDEX IF NOT EXISTS idx_pools_phase ON pools(phase);
"""

_db: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()


# Columns added after the first deploy. CREATE TABLE IF NOT EXISTS does NOT add a column
# to a database that already exists, so a schema change made this way is silently absent
# on the server and fails later, inside the settle path, on camera. Each entry is applied
# idempotently at boot.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("invoices", "settled_cents", "INTEGER NOT NULL DEFAULT 0"),
]


async def _migrate(conn: aiosqlite.Connection) -> list[str]:
    applied = []
    for table, column, decl in MIGRATIONS:
        cur = await conn.execute(f"PRAGMA table_info({table})")
        have = {r["name"] for r in await cur.fetchall()}
        if column not in have:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            applied.append(f"{table}.{column}")
    if applied:
        await conn.commit()
    return applied


async def init_db(path: str | None = None) -> aiosqlite.Connection:
    """Open (once) and migrate. WAL + busy_timeout so the ticker and a request can
    interleave without 'database is locked'."""
    global _db
    if _db is not None:
        return _db
    _db = await aiosqlite.connect(path or settings.db_path)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA busy_timeout=5000")
    await _db.execute("PRAGMA foreign_keys=ON")
    await _db.executescript(SCHEMA)
    await _db.commit()
    for added in await _migrate(_db):
        print(f"[store] migrated: added {added}")
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("store.init_db() has not been called")
    return _db


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --- pools ------------------------------------------------------------------
async def create_pool(*, goal_text: str, policy_text: str, currency: str = "USD",
                      clock_scale: int = 1, demo: bool = False) -> str:
    """A pool starts in DRAFT holding nothing but the human's verbatim words."""
    pool_id = new_id("pool")
    async with _write_lock:
        await db().execute(
            "INSERT INTO pools (pool_id, phase, goal_text, policy_text, currency, "
            "clock_scale, demo, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (pool_id, "DRAFT", goal_text, policy_text, currency, clock_scale,
             1 if demo else 0, time.time()),
        )
        await db().commit()
    return pool_id


async def get_pool(pool_id: str) -> dict | None:
    cur = await db().execute("SELECT * FROM pools WHERE pool_id=?", (pool_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def advance(pool_id: str, frm: str, to: str) -> bool:
    """Rowcount-guarded phase transition. Returns False if the pool was not in `frm`,
    which is how a concurrent second caller loses cleanly instead of double-acting."""
    if (frm, to) not in ALLOWED_EDGES:
        raise ValueError(f"illegal phase edge {frm} -> {to}")
    async with _write_lock:
        cur = await db().execute(
            "UPDATE pools SET phase=?, closed_at=CASE WHEN ?='CLOSED' THEN ? ELSE closed_at END "
            "WHERE pool_id=? AND phase=?",
            (to, to, time.time(), pool_id, frm),
        )
        await db().commit()
        return cur.rowcount == 1


async def set_plan(pool_id: str, *, success_condition_restated: str, recipient_cid: str,
                   target_cents: int, deadline_at: float, deadline_label: str,
                   overage: str) -> None:
    async with _write_lock:
        await db().execute(
            "UPDATE pools SET success_condition_restated=?, recipient_cid=?, target_cents=?, "
            "deadline_at=?, deadline_label=?, overage=? WHERE pool_id=?",
            (success_condition_restated, recipient_cid, target_cents, deadline_at,
             deadline_label, overage, pool_id),
        )
        await db().commit()


async def bump_ledger_version(pool_id: str) -> int:
    async with _write_lock:
        await db().execute(
            "UPDATE pools SET ledger_version = ledger_version + 1 WHERE pool_id=?", (pool_id,))
        await db().commit()
    cur = await db().execute("SELECT ledger_version FROM pools WHERE pool_id=?", (pool_id,))
    row = await cur.fetchone()
    return int(row["ledger_version"]) if row else 0


async def claim_pool(pool_id: str, ttl_s: float) -> bool:
    """Ticker row lock: only one worker may act on a pool at a time."""
    now = time.time()
    async with _write_lock:
        cur = await db().execute(
            "UPDATE pools SET claimed_until=? WHERE pool_id=? AND claimed_until < ?",
            (now + ttl_s, pool_id, now),
        )
        await db().commit()
        return cur.rowcount == 1


async def release_pool(pool_id: str) -> None:
    async with _write_lock:
        await db().execute("UPDATE pools SET claimed_until=0 WHERE pool_id=?", (pool_id,))
        await db().commit()


async def live_pools() -> list[dict]:
    """Every pool the ticker still has work on — DRAFT included.

    A pool in DRAFT is waiting for its very first episode, so leaving it out would mean
    nothing ever plans it. CLOSED and ESCALATED are the only states with nothing left to
    do, and escalated pools are revived only by a human pressing the refund button.
    """
    cur = await db().execute(
        "SELECT * FROM pools WHERE phase IN ('DRAFT','COLLECTING','RESOLVING','SETTLING') "
        "ORDER BY created_at")
    return [dict(r) for r in await cur.fetchall()]


async def count_open_pools() -> int:
    cur = await db().execute(
        "SELECT COUNT(*) AS n FROM pools WHERE phase NOT IN ('CLOSED','ESCALATED')")
    row = await cur.fetchone()
    return int(row["n"])


# --- contributors (the frozen cast) ----------------------------------------
async def add_contributors(pool_id: str, rows: list[dict]) -> None:
    """rows: [{cid, name, email, share_cents, is_recipient?}]. The roster is frozen here:
    every later tool refers to a cid, so the model can never introduce an email address."""
    async with _write_lock:
        await db().executemany(
            "INSERT INTO contributors (pool_id, cid, name, email, share_cents, is_recipient) "
            "VALUES (?,?,?,?,?,?)",
            [(pool_id, r["cid"], r["name"], r["email"], int(r["share_cents"]),
              1 if r.get("is_recipient") else 0) for r in rows],
        )
        await db().executemany(
            "INSERT OR IGNORE INTO invoices (pool_id, cid) VALUES (?,?)",
            [(pool_id, r["cid"]) for r in rows if not r.get("is_recipient")],
        )
        await db().commit()


async def seed_cast(pool_id: str, people: list[dict]) -> None:
    """Freeze the cast BEFORE the agent runs. people: [{cid, name, email}].

    Everything downstream refers to a cid, so the model can never introduce an email
    address — which is both the anti-hallucination guarantee and what stops a public
    demo being turned into an invoice-email spam cannon.
    """
    async with _write_lock:
        await db().executemany(
            "INSERT OR IGNORE INTO contributors (pool_id, cid, name, email, share_cents) "
            "VALUES (?,?,?,?,0)",
            [(pool_id, p["cid"], p["name"], p["email"]) for p in people],
        )
        await db().commit()


async def apply_plan(pool_id: str, *, shares: dict[str, int], recipient_cid: str) -> None:
    """The agent's plan lands here: shares are set, the recipient is marked, and an
    invoice row is opened for each contributor. Amounts are frozen at this point —
    no later tool can re-price them."""
    async with _write_lock:
        await db().executemany(
            "UPDATE contributors SET share_cents=? WHERE pool_id=? AND cid=?",
            [(int(amt), pool_id, cid) for cid, amt in shares.items()],
        )
        await db().execute(
            "UPDATE contributors SET is_recipient=1, share_cents=0 WHERE pool_id=? AND cid=?",
            (pool_id, recipient_cid),
        )
        await db().executemany(
            "INSERT OR IGNORE INTO invoices (pool_id, cid) VALUES (?,?)",
            [(pool_id, cid) for cid in shares],
        )
        await db().commit()


async def get_contributors(pool_id: str) -> list[dict]:
    cur = await db().execute(
        "SELECT * FROM contributors WHERE pool_id=? ORDER BY is_recipient, cid", (pool_id,))
    return [dict(r) for r in await cur.fetchall()]


async def get_contributor(pool_id: str, cid: str) -> dict | None:
    cur = await db().execute(
        "SELECT * FROM contributors WHERE pool_id=? AND cid=?", (pool_id, cid))
    row = await cur.fetchone()
    return dict(row) if row else None


# --- invoices ---------------------------------------------------------------
async def set_invoice_sent(pool_id: str, cid: str, invoice_id: str, payer_url: str) -> None:
    async with _write_lock:
        await db().execute(
            "UPDATE invoices SET invoice_id=?, payer_url=?, status='SENT', sent_at=? "
            "WHERE pool_id=? AND cid=?",
            (invoice_id, payer_url, time.time(), pool_id, cid),
        )
        await db().commit()


async def set_invoice_state(pool_id: str, cid: str, *, status: str, paid_cents: int,
                            source: str | None) -> bool:
    """Returns True if anything actually changed — the ticker uses that to decide whether
    an event (and possibly a model episode) is warranted."""
    cur = await db().execute(
        "SELECT status, paid_cents FROM invoices WHERE pool_id=? AND cid=?", (pool_id, cid))
    row = await cur.fetchone()
    if row and row["status"] == status and int(row["paid_cents"]) == int(paid_cents):
        return False
    paid_at = time.time() if paid_cents and not (row and row["paid_cents"]) else None
    async with _write_lock:
        await db().execute(
            "UPDATE invoices SET status=?, paid_cents=?, source=COALESCE(?, source), "
            "paid_at=COALESCE(?, paid_at) WHERE pool_id=? AND cid=?",
            (status, int(paid_cents), source, paid_at, pool_id, cid),
        )
        await db().commit()
    return True


async def freeze_paid_snapshot(pool_id: str) -> None:
    """Stamp each invoice with what it had paid at settlement time.

    This is the whole mechanism behind the late-payment sweep: afterwards,
    `paid_cents > settled_cents` means money arrived after the pool was frozen, the
    difference is exactly what has to go back, and refunding it twice is impossible
    because the stamp moves with it.
    """
    async with _write_lock:
        await db().execute(
            "UPDATE invoices SET settled_cents = paid_cents WHERE pool_id=?", (pool_id,))
        await db().commit()


async def late_arrivals(pool_id: str) -> list[dict]:
    """Invoices holding more than was frozen for them."""
    cur = await db().execute(
        "SELECT * FROM invoices WHERE pool_id=? AND paid_cents > settled_cents", (pool_id,))
    return [dict(r) for r in await cur.fetchall()]


async def mark_late_refunded(pool_id: str, cid: str, up_to_cents: int) -> bool:
    """Move the stamp. Guarded on the old value so two sweeps racing one late payment
    cannot both refund it."""
    async with _write_lock:
        cur = await db().execute(
            "UPDATE invoices SET settled_cents=? WHERE pool_id=? AND cid=? AND settled_cents<?",
            (int(up_to_cents), pool_id, cid, int(up_to_cents)))
        await db().commit()
        return cur.rowcount == 1


async def bump_reminder(pool_id: str, cid: str) -> None:
    async with _write_lock:
        await db().execute(
            "UPDATE invoices SET reminders = reminders + 1, last_reminder_at=? "
            "WHERE pool_id=? AND cid=?", (time.time(), pool_id, cid))
        await db().commit()


async def get_invoices(pool_id: str) -> list[dict]:
    cur = await db().execute("SELECT * FROM invoices WHERE pool_id=? ORDER BY cid", (pool_id,))
    return [dict(r) for r in await cur.fetchall()]


async def collected_cents(pool_id: str) -> int:
    cur = await db().execute(
        "SELECT COALESCE(SUM(paid_cents),0) AS c FROM invoices WHERE pool_id=?", (pool_id,))
    row = await cur.fetchone()
    return int(row["c"])


# --- settlements (exactly once) ---------------------------------------------
async def insert_settlement(pool_id: str, *, decision: str, reason: str, policy_quote: str,
                            frozen_collected_cents: int, sender_batch_id: str,
                            items: list[dict], decided_by: str = "agent") -> bool:
    """Write-ahead the settlement BEFORE any HTTP call. UNIQUE(pool_id) means a second
    attempt loses here rather than at PayPal."""
    async with _write_lock:
        try:
            await db().execute(
                "INSERT INTO settlements (pool_id, decision, reason, policy_quote, "
                "frozen_collected_cents, sender_batch_id, items_json, decided_by, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (pool_id, decision, reason, policy_quote, int(frozen_collected_cents),
                 sender_batch_id, json.dumps(items), decided_by, time.time()),
            )
            await db().commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def set_settlement_batch(pool_id: str, batch_id: str, batch_status: str) -> None:
    async with _write_lock:
        await db().execute(
            "UPDATE settlements SET batch_id=?, batch_status=? WHERE pool_id=?",
            (batch_id, batch_status, pool_id))
        await db().commit()


async def get_settlement(pool_id: str) -> dict | None:
    cur = await db().execute("SELECT * FROM settlements WHERE pool_id=?", (pool_id,))
    row = await cur.fetchone()
    if not row:
        return None
    d = dict(row)
    d["items"] = json.loads(d.pop("items_json") or "[]")
    return d


async def payouts_today_cents() -> int:
    since = time.time() - 86400
    cur = await db().execute(
        "SELECT COALESCE(SUM(frozen_collected_cents),0) AS c FROM settlements WHERE created_at > ?",
        (since,))
    row = await cur.fetchone()
    return int(row["c"])


# --- episodes ---------------------------------------------------------------
async def start_episode(pool_id: str, trigger: str) -> str:
    episode_id = new_id("ep")
    async with _write_lock:
        await db().execute(
            "INSERT INTO episodes (episode_id, pool_id, trigger, started_at) VALUES (?,?,?,?)",
            (episode_id, pool_id, trigger, time.time()))
        await db().execute(
            "UPDATE pools SET episodes_used = episodes_used + 1 WHERE pool_id=?", (pool_id,))
        await db().commit()
    return episode_id


async def end_episode(episode_id: str, *, turns: int, prompt_tokens: int,
                      completion_tokens: int, reason: str, messages: list | None = None) -> None:
    async with _write_lock:
        await db().execute(
            "UPDATE episodes SET turns=?, prompt_tokens=?, completion_tokens=?, reason=?, "
            "messages_json=?, ended_at=? WHERE episode_id=?",
            (turns, prompt_tokens, completion_tokens, reason,
             json.dumps(messages) if messages is not None else None, time.time(), episode_id))
        await db().commit()


async def last_episode(pool_id: str) -> dict | None:
    cur = await db().execute(
        "SELECT * FROM episodes WHERE pool_id=? ORDER BY started_at DESC LIMIT 1", (pool_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


# --- events (the append-only log the SSE stream projects) -------------------
async def append_event(pool_id: str, etype: str, data: dict) -> int:
    async with _write_lock:
        cur = await db().execute(
            "INSERT INTO events (pool_id, ts, type, data) VALUES (?,?,?,?)",
            (pool_id, time.time(), etype, json.dumps(data, ensure_ascii=False)))
        await db().commit()
        return int(cur.lastrowid)


async def read_events(pool_id: str, from_seq: int = 0, limit: int = 2000) -> list[dict]:
    cur = await db().execute(
        "SELECT seq, ts, type, data FROM events WHERE pool_id=? AND seq > ? "
        "ORDER BY seq LIMIT ?", (pool_id, from_seq, limit))
    out = []
    for r in await cur.fetchall():
        out.append({"seq": int(r["seq"]), "ts": r["ts"], "type": r["type"],
                    **json.loads(r["data"])})
    return out


async def tail_events(pool_id: str, limit: int = 1200) -> list[dict]:
    """The most recent events, oldest-first.

    read_events() pages FORWARD from a cursor, which is what the stream needs. A repaint
    needs the opposite end: a pool left open for an hour accumulates thousands of clock
    ticks, and handing a late joiner the oldest two thousand of them would paint a stale
    screen that looks like a bug.
    """
    cur = await db().execute(
        "SELECT seq, ts, type, data FROM events WHERE pool_id=? ORDER BY seq DESC LIMIT ?",
        (pool_id, limit))
    rows = list(await cur.fetchall())
    return [{"seq": int(r["seq"]), "ts": r["ts"], "type": r["type"], **json.loads(r["data"])}
            for r in reversed(rows)]


async def last_settlement_at() -> float | None:
    cur = await db().execute("SELECT MAX(created_at) AS t FROM settlements")
    row = await cur.fetchone()
    return row["t"] if row and row["t"] else None


async def prune_events(older_than_s: float = 86400 * 2) -> int:
    cutoff = time.time() - older_than_s
    async with _write_lock:
        cur = await db().execute(
            "DELETE FROM events WHERE ts < ? AND pool_id IN "
            "(SELECT pool_id FROM pools WHERE phase IN ('CLOSED','ESCALATED'))", (cutoff,))
        await db().commit()
        return cur.rowcount
