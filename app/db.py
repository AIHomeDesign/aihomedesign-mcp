"""Persistence for the AI HomeDesign MCP server.

Two responsibilities, both backed by the shared Postgres instance:

1. **Opaque connector tokens** (`token_map`). The connector URL must NOT carry the
   user's raw AI HomeDesign key. Instead the landing page validates the key, then we
   mint a random opaque token (`aihd_...`) and store the *encrypted* key against it.
   On every MCP request the middleware swaps the token back for the real key. The
   key is encrypted at rest with a server-side secret (TOKEN_SECRET / Fernet), so a
   database dump alone never exposes anyone's key.

2. **Usage log** (`request_log`). One row per MCP tool call: which token, which tool,
   ok/error, duration, a truncated argument summary and error message — so we can see
   what users do, what they call, timings and failures.

Everything here is best-effort: if the database is unreachable the MCP must keep
serving (legacy raw-key links still work and tools still run); we just lose logging
and new-token minting. Callers should treat a None / False return as "DB down".
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from typing import Any

try:
    import psycopg2
    from psycopg2.pool import ThreadedConnectionPool
except Exception:  # pragma: no cover - import guard so the app still boots
    psycopg2 = None
    ThreadedConnectionPool = None

try:
    from cryptography.fernet import Fernet
except Exception:  # pragma: no cover
    Fernet = None

# --------------------------------------------------------------------------- #
# Config (all from env; everything degrades gracefully when unset)
# --------------------------------------------------------------------------- #
DB_DSN = os.environ.get("DB_DSN", "").strip()
TOKEN_SECRET = os.environ.get("TOKEN_SECRET", "").strip()
TOKEN_PREFIX = "aihd_"

_pool: Any = None
_fernet: Any = None
_lock = threading.Lock()
_enabled = False


def _log(msg: str) -> None:
    print(f"[db] {msg}", flush=True)


def init() -> bool:
    """Connect, build the Fernet cipher and create tables. Idempotent. Returns
    True when persistence is fully available, False when it is disabled/unreachable
    (in which case the server runs without logging or token minting)."""
    global _pool, _fernet, _enabled
    if _enabled:
        return True
    if not DB_DSN:
        _log("DB_DSN not set — persistence disabled (legacy raw-key links still work).")
        return False
    if psycopg2 is None:
        _log("psycopg2 not installed — persistence disabled.")
        return False
    if Fernet is None or not TOKEN_SECRET:
        _log("cryptography/TOKEN_SECRET missing — persistence disabled.")
        return False
    try:
        _fernet = Fernet(TOKEN_SECRET.encode())
    except Exception as e:  # bad secret
        _log(f"invalid TOKEN_SECRET ({e!r}) — persistence disabled.")
        return False
    # Retry the connection a few times: on a cold compose start Postgres may not
    # be ready the instant this container boots.
    last = None
    for attempt in range(10):
        try:
            _pool = ThreadedConnectionPool(1, 8, dsn=DB_DSN)
            _create_schema()
            _enabled = True
            _log("persistence ready (token_map + request_log).")
            return True
        except Exception as e:
            last = e
            time.sleep(2)
    _log(f"could not connect after retries ({last!r}) — persistence disabled.")
    return False


def enabled() -> bool:
    return _enabled


def _create_schema() -> None:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS token_map (
                token        TEXT PRIMARY KEY,
                enc_key      TEXT NOT NULL,
                key_fp       TEXT NOT NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_used_at TIMESTAMPTZ,
                use_count    BIGINT NOT NULL DEFAULT 0,
                revoked      BOOLEAN NOT NULL DEFAULT false
            );
            CREATE TABLE IF NOT EXISTS request_log (
                id          BIGSERIAL PRIMARY KEY,
                ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
                token       TEXT,
                key_fp      TEXT,
                tool        TEXT,
                status      TEXT,
                duration_ms INTEGER,
                error       TEXT,
                args        JSONB
            );
            CREATE INDEX IF NOT EXISTS request_log_ts_idx   ON request_log (ts DESC);
            CREATE INDEX IF NOT EXISTS request_log_tool_idx ON request_log (tool);
            CREATE INDEX IF NOT EXISTS request_log_fp_idx   ON request_log (key_fp);
            """
        )
        conn.commit()


class _conn:
    """Borrow a pooled connection as a context manager; always return it."""

    def __enter__(self):
        self._c = _pool.getconn()
        return self._c

    def __exit__(self, *exc):
        try:
            if exc[0] is not None:
                self._c.rollback()
        finally:
            _pool.putconn(self._c)
        return False


def key_fingerprint(key: str) -> str:
    """A short, non-reversible label for a key so logs/tables can identify a user
    without storing the raw key. sha256 prefix — never the key itself."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Token mapping
# --------------------------------------------------------------------------- #
def mint_token(key: str) -> str | None:
    """Store an (encrypted) key under a fresh opaque token and return the token.
    Returns None if persistence is unavailable."""
    if not _enabled:
        return None
    token = TOKEN_PREFIX + secrets.token_urlsafe(24)
    enc = _fernet.encrypt(key.encode()).decode()
    fp = key_fingerprint(key)
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO token_map (token, enc_key, key_fp) VALUES (%s, %s, %s)",
                (token, enc, fp),
            )
            conn.commit()
        return token
    except Exception as e:
        _log(f"mint_token failed: {e!r}")
        return None


def resolve_token(token: str) -> str | None:
    """Return the real AIHD key for an opaque token, or None if unknown/revoked/down.
    Also bumps last_used_at / use_count (best effort)."""
    if not _enabled or not token:
        return None
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT enc_key FROM token_map WHERE token = %s AND NOT revoked",
                (token,),
            )
            row = cur.fetchone()
            if not row:
                return None
            key = _fernet.decrypt(row[0].encode()).decode()
            cur.execute(
                "UPDATE token_map SET last_used_at = now(), use_count = use_count + 1 "
                "WHERE token = %s",
                (token,),
            )
            conn.commit()
            return key
    except Exception as e:
        _log(f"resolve_token failed: {e!r}")
        return None


# --------------------------------------------------------------------------- #
# Usage log
# --------------------------------------------------------------------------- #
def log_event(token: str | None, key_fp: str | None, tool: str, status: str,
              duration_ms: int, error: str | None, args: Any) -> None:
    """Record one tool call. Never raises — logging must not break a request."""
    if not _enabled:
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO request_log (token, key_fp, tool, status, duration_ms, error, args) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (token, key_fp, tool, status, duration_ms, error,
                 json.dumps(_safe_args(args))[:8000]),
            )
            conn.commit()
    except Exception as e:
        _log(f"log_event failed: {e!r}")


_SECRET_HINTS = ("base64", "_b64", "image_base64", "data:")


def _safe_args(args: Any) -> Any:
    """Truncate big/binary fields so we log the *shape* of a call (the prompt/params)
    without dumping megabytes of image bytes into the database."""
    if not isinstance(args, dict):
        return {"value": str(args)[:500]}
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str):
            if any(h in k.lower() for h in _SECRET_HINTS) or v.startswith("data:"):
                out[k] = f"<{len(v)} chars omitted>"
            else:
                out[k] = v[:500]
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list):
            out[k] = f"<list len={len(v)}>"
        else:
            out[k] = str(v)[:200]
    return out
