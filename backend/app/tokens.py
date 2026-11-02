"""App tokens: self-registration and per-token limits (test app only).

The gate is on when APP_TOKEN is set (as before). Then an install with
no token calls POST /v1/register with its install id and gets one;
/ask and /followup take either a minted token or APP_TOKEN itself (the
owner's, unlimited). In production this is replaced by real login and
Play Integrity; here the limits bound what an open registration costs:

    mint_cap    tokens ever minted (re-registering an install reuses
                its slot)
    mint_rate   mints per window, all installs (stops one script
                draining the cap; global, since a demo room shares an IP)
    ask_rate    asks per window per token (follow-ups count)
    ask_cap     asks per window across all minted tokens (the bill)

Limits live in the database and change without a restart
(tools/tokens.py). Revoking bans the install id: a revoked app gets a
401, clears its token and re-registers, so revoking only the token
would do nothing.

Tokens are stored as SHA-256 hashes in DATA_DIR/tokens.db (default
~/alumbra-data, outside the git checkout). Ask counters are in memory:
the server runs one worker, and a restart resetting them is fine.
"""
from __future__ import annotations

import collections
import contextlib
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from .pipeline import _load_secret

DEFAULT_LIMITS = {
    "mint_cap": "100",
    "mint_rate": "20/3600",
    "ask_rate": "10/300",
    "ask_cap": "300/3600",
}
INSTALL_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
OWNER = "owner"

_lock = threading.Lock()
_asks: dict[str, collections.deque] = collections.defaultdict(
    collections.deque)
_all_asks: collections.deque = collections.deque()


class Refused(Exception):
    """A request the limits turn away. status is the HTTP code; error
    the machine-readable reason; retry_after seconds, for a 429."""

    def __init__(self, status: int, error: str, retry_after: int = 0):
        super().__init__(error)
        self.status = status
        self.error = error
        self.retry_after = retry_after


def owner_token() -> str:
    return _load_secret("APP_TOKEN").strip()


def gate_on() -> bool:
    return bool(owner_token())


def registration_enabled() -> bool:
    """Kill switch: REGISTRATION_ENABLED=0 closes /v1/register (minted
    tokens keep working). Only meaningful with the gate on."""
    return gate_on() and os.environ.get(
        "REGISTRATION_ENABLED", "").strip().lower() not in (
        "0", "false", "no", "off")


def data_dir() -> Path:
    """DATA_DIR from the environment or the repo .env (so the CLI over
    ssh finds the same store as the service); default ~/alumbra-data."""
    d = _load_secret("DATA_DIR").strip()
    return Path(d).expanduser() if d else Path.home() / "alumbra-data"


def _db() -> sqlite3.Connection:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / "tokens.db", timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS tokens (
            id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL,
            install_id TEXT UNIQUE NOT NULL, created REAL NOT NULL,
            rotated REAL, last_used REAL, asks INTEGER NOT NULL DEFAULT 0,
            revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS bans (
            install_id TEXT PRIMARY KEY, at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS limits (
            key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    return con


@contextlib.contextmanager
def _tx():
    """One connection, committed (or rolled back) and closed."""
    con = _db()
    try:
        with con:
            yield con
    finally:
        con.close()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def parse_rate(value: str) -> tuple[int, int]:
    """'10/300' -> (10, 300): count per window seconds."""
    n, _, w = value.partition("/")
    n_i, w_i = int(n), int(w)
    if n_i < 0 or w_i <= 0:
        raise ValueError(value)
    return n_i, w_i


def limits(con: sqlite3.Connection | None = None) -> dict[str, str]:
    if con is None:
        with _tx() as con:
            return limits(con)
    rows = dict(con.execute("SELECT key, value FROM limits").fetchall())
    return {k: rows.get(k, v) for k, v in DEFAULT_LIMITS.items()}


def set_limit(key: str, value: str) -> None:
    if key not in DEFAULT_LIMITS:
        raise KeyError(key)
    if key == "mint_cap":
        if int(value) < 0:
            raise ValueError(value)
    else:
        parse_rate(value)
    with _tx() as con:
        con.execute("INSERT OR REPLACE INTO limits VALUES (?, ?)",
                    (key, value.strip()))


def register(install_id: str) -> dict[str, str]:
    """A token for this install: the existing slot rotated, or a new
    one within the cap and mint rate. Refused when banned or full."""
    install_id = (install_id or "").strip()
    if not INSTALL_ID_RE.match(install_id):
        raise Refused(400, "bad_install_id")
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock, _tx() as con:
        if con.execute("SELECT 1 FROM bans WHERE install_id = ?",
                       (install_id,)).fetchone():
            raise Refused(403, "install_banned")
        row = con.execute("SELECT id FROM tokens WHERE install_id = ?",
                          (install_id,)).fetchone()
        if row:
            con.execute("UPDATE tokens SET hash = ?, rotated = ? "
                        "WHERE id = ?", (_hash(token), now, row["id"]))
            return {"token": token, "id": row["id"]}
        lim = limits(con)
        minted = con.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
        if minted >= int(lim["mint_cap"]):
            raise Refused(403, "registration_closed")
        n, window = parse_rate(lim["mint_rate"])
        recent = con.execute(
            "SELECT created FROM tokens WHERE created > ? ORDER BY created",
            (now - window,)).fetchall()
        if len(recent) >= n:
            wait = recent[0]["created"] + window - now if recent else window
            raise Refused(429, "mint_rate", max(1, int(wait) + 1))
        tid = f"t_{minted + 1:03d}"
        con.execute("INSERT INTO tokens (id, hash, install_id, created) "
                    "VALUES (?, ?, ?, ?)", (tid, _hash(token), install_id, now))
    return {"token": token, "id": tid}


def check(given: str | None) -> str | None:
    """The token's id (OWNER for APP_TOKEN) when the gate is on and it
    is valid; None with the gate off. Refused(401) otherwise."""
    owner = owner_token()
    if not owner:
        return None
    given = (given or "").strip()
    if given and hmac.compare_digest(given.encode(), owner.encode()):
        return OWNER
    if given:
        with _tx() as con:
            row = con.execute(
                "SELECT t.id FROM tokens t WHERE t.hash = ? AND "
                "t.revoked = 0 AND t.install_id NOT IN "
                "(SELECT install_id FROM bans)", (_hash(given),)).fetchone()
        if row:
            return row["id"]
    raise Refused(401, "bad_app_token")


def _trim(q: collections.deque, now: float, window: int) -> None:
    while q and q[0] <= now - window:
        q.popleft()


def admit_ask(token_id: str | None) -> None:
    """Count one ask (or follow-up) against the token's rate and the
    global cap; Refused(429) with retry_after when either is full. The
    owner and the gate-off case are not limited."""
    if token_id is None or token_id == OWNER:
        return
    lim = limits()
    n, window = parse_rate(lim["ask_rate"])
    cap, cap_window = parse_rate(lim["ask_cap"])
    now = time.time()
    with _lock:
        q = _asks[token_id]
        _trim(q, now, window)
        _trim(_all_asks, now, cap_window)
        if len(q) >= n:
            raise Refused(429, "ask_rate",
                          max(1, int(q[0] + window - now) + 1) if q
                          else window)
        if len(_all_asks) >= cap:
            raise Refused(429, "ask_cap",
                          max(1, int(_all_asks[0] + cap_window - now) + 1)
                          if _all_asks else cap_window)
        q.append(now)
        _all_asks.append(now)
    with _tx() as con:
        con.execute("UPDATE tokens SET asks = asks + 1, last_used = ? "
                    "WHERE id = ?", (now, token_id))


def asks_in_window() -> int:
    """Minted-token asks inside the ask_cap window (this process)."""
    _, cap_window = parse_rate(limits()["ask_cap"])
    with _lock:
        _trim(_all_asks, time.time(), cap_window)
        return len(_all_asks)


def revoke(token_id: str) -> str | None:
    """Revoke a token and ban its install id; the install id, or None
    for an unknown token."""
    with _tx() as con:
        row = con.execute("SELECT install_id FROM tokens WHERE id = ?",
                          (token_id,)).fetchone()
        if not row:
            return None
        con.execute("UPDATE tokens SET revoked = 1 WHERE id = ?",
                    (token_id,))
        con.execute("INSERT OR IGNORE INTO bans VALUES (?, ?)",
                    (row["install_id"], time.time()))
        return row["install_id"]


def revoke_all() -> int:
    """Revoke every token and ban every install id; the count."""
    with _tx() as con:
        now = time.time()
        con.execute("INSERT OR IGNORE INTO bans "
                    "SELECT install_id, ? FROM tokens", (now,))
        return con.execute(
            "UPDATE tokens SET revoked = 1 WHERE revoked = 0").rowcount


def unban(token_id: str) -> bool:
    """Lift a token's ban: its install can register again (its old
    token stays revoked; re-registering rotates in a new one)."""
    with _tx() as con:
        row = con.execute("SELECT install_id FROM tokens WHERE id = ?",
                          (token_id,)).fetchone()
        if not row:
            return False
        con.execute("DELETE FROM bans WHERE install_id = ?",
                    (row["install_id"],))
        con.execute("UPDATE tokens SET revoked = 0 WHERE id = ?",
                    (token_id,))
        return True


def rows() -> list[dict]:
    with _tx() as con:
        return [dict(r) for r in con.execute(
            "SELECT t.id, t.install_id, t.created, t.last_used, t.asks, "
            "t.revoked, (b.install_id IS NOT NULL) AS banned FROM tokens t "
            "LEFT JOIN bans b ON b.install_id = t.install_id ORDER BY t.id")]
