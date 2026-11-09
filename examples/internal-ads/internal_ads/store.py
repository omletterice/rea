from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

from .providers_base import AdsError


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Store:
    def __init__(self, path: str, key: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path, self.cipher = path, Fernet(key.encode())
        with self.db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS operations (
              id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, plan TEXT NOT NULL,
              digest TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
              result TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS receipts (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, step TEXT NOT NULL, body TEXT NOT NULL, at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, event TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS context (brand TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS secrets (account TEXT PRIMARY KEY, body BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS oauth_states (state TEXT PRIMARY KEY, account TEXT NOT NULL, verifier TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
        os.chmod(path, 0o600)

    @contextmanager
    def process_lease(self):
        """One HTTP or stdio process owns recovery and execution for this DB.

        Keep the lock inode after release: deleting it would let waiters and new
        owners lock different files. The OS releases this lease on process exit.
        Supported deployment hosts are Unix/Linux with local filesystems.
        """
        try:
            import fcntl
        except ImportError:
            raise RuntimeError(
                "Database process ownership requires Unix fcntl; deploy on Linux"
            ) from None
        lock_path = str(Path(self.path).resolve()) + ".process.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                raise AdsError(
                    "Another HTTP or stdio process already owns this database; stop it before starting a replacement",
                    "database_in_use",
                    409,
                ) from None
            yield
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def bind_mode(self, demo):
        mode = "demo" if demo else "live"
        with self.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", ("mode",)).fetchone()
            if row and row["value"] != mode:
                raise ValueError("Use separate ADS_DATABASE paths for demo and live modes")
            db.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", ("mode", mode))

    def recover(self):
        # Called only while the server holds process_lease().
        with self.db() as db:
            rows = db.execute("SELECT id FROM operations WHERE state='executing'").fetchall()
            db.execute(
                "UPDATE operations SET state='needs_reconciliation', error=? WHERE state='executing'",
                (
                    "Process stopped during execution; inspect platform and receipts before any replacement",
                ),
            )
            for row in rows:
                self._audit(db, "recovered_ambiguous_execution", row["id"], {})

    @staticmethod
    def _audit(db, event, subject, body):
        db.execute(
            "INSERT INTO audit(at,event,subject,body) VALUES (?,?,?,?)",
            (time.time(), event, subject, canonical(body)),
        )

    def audit(self, event, subject, body):
        with self.db() as db:
            self._audit(db, event, subject, body)

    def prepare(self, key, plan, ttl):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM operations WHERE request_key=?", (key,)).fetchone()
            if old:
                if digest(json.loads(old["plan"])["request"]) != digest(plan["request"]):
                    raise AdsError(
                        "Idempotency key already belongs to a different request", "conflict", 409
                    )
                return self._operation(old)
            op_id, now = str(uuid.uuid4()), time.time()
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    op_id,
                    key,
                    canonical(plan),
                    digest(plan),
                    "pending",
                    now,
                    now + ttl,
                    None,
                    None,
                ),
            )
            self._audit(db, "prepared", op_id, {"digest": digest(plan)})
            return self._operation(
                db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
            )

    def _operation(self, row):
        if not row:
            raise AdsError("Operation not found", "not_found", 404)
        op = dict(row)
        op["plan"] = json.loads(op["plan"])
        op["result"] = json.loads(op["result"]) if op["result"] else None
        return op

    def get(self, op_id):
        with self.db() as db:
            op = self._operation(
                db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
            )
            op["receipts"] = [
                dict(r) | {"body": json.loads(r["body"])}
                for r in db.execute(
                    "SELECT * FROM receipts WHERE operation_id=? ORDER BY seq", (op_id,)
                )
            ]
            return op

    def list_operations(self, limit=50):
        with self.db() as db:
            return [
                self._operation(r)
                for r in db.execute(
                    "SELECT * FROM operations ORDER BY created DESC LIMIT ?", (limit,)
                )
            ]

    def approve(self, op_id, expected_digest):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            op = self._operation(
                db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
            )
            if op["digest"] != expected_digest or digest(op["plan"]) != expected_digest:
                raise AdsError("Preview digest does not match", "conflict", 409)
            if op["expires"] < time.time() or op["state"] != "pending":
                raise AdsError(
                    "Only an unexpired pending operation can be approved", "conflict", 409
                )
            db.execute("UPDATE operations SET state='approved' WHERE id=?", (op_id,))
            self._audit(db, "operator_approved", op_id, {"digest": expected_digest})
        return self.get(op_id)

    def claim(self, op_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            op = self._operation(
                db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
            )
            if op["state"] == "succeeded":
                return op, False
            if (
                op["state"] != "approved"
                or op["expires"] < time.time()
                or digest(op["plan"]) != op["digest"]
            ):
                raise AdsError(
                    "Operation requires a current operator approval; executing/ambiguous operations cannot be retried",
                    "approval_required",
                    409,
                )
            db.execute("UPDATE operations SET state='executing' WHERE id=?", (op_id,))
            self._audit(db, "execution_started", op_id, {})
            return op, True

    def receipt(self, op_id, step, body):
        with self.db() as db:
            db.execute(
                "INSERT INTO receipts(operation_id,step,body,at) VALUES (?,?,?,?)",
                (op_id, step, canonical(body), time.time()),
            )
            self._audit(db, "provider_receipt", op_id, {"step": step})

    def finish(self, op_id, state, result=None, error=None):
        with self.db() as db:
            db.execute(
                "UPDATE operations SET state=?,result=?,error=? WHERE id=?",
                (state, canonical(result) if result is not None else None, error, op_id),
            )
            self._audit(db, state, op_id, {"error": error} if error else {})
        return self.get(op_id)

    def assert_no_ambiguity(self, account):
        with self.db() as db:
            rows = db.execute(
                "SELECT id,plan FROM operations WHERE state='needs_reconciliation'"
            ).fetchall()
        if any(json.loads(row["plan"])["request"]["draft"]["account"] == account for row in rows):
            raise AdsError(
                "Resolve the ambiguous operation on this account before preparing or executing replacements",
                "reconciliation_required",
                409,
            )

    def cancel(self, op_id):
        with self.db() as db:
            changed = db.execute(
                "UPDATE operations SET state='cancelled' WHERE id=? AND state IN ('pending','approved')",
                (op_id,),
            ).rowcount
            if not changed:
                raise AdsError("Only pending/approved operations can be cancelled", "conflict", 409)
            self._audit(db, "cancelled", op_id, {})
        return self.get(op_id)

    def reconcile(self, op_id, note):
        with self.db() as db:
            changed = db.execute(
                "UPDATE operations SET state='reconciled' WHERE id=? AND state='needs_reconciliation'",
                (op_id,),
            ).rowcount
            if not changed:
                raise AdsError("Only ambiguous operations can be reconciled", "conflict", 409)
            self._audit(db, "operator_reconciled", op_id, {"note": note})
        return self.get(op_id)

    def tokens(self, account):
        with self.db() as db:
            row = db.execute("SELECT body FROM secrets WHERE account=?", (account,)).fetchone()
        return json.loads(self.cipher.decrypt(row["body"])) if row else {}

    def save_tokens(self, account, data):
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO secrets VALUES (?,?)",
                (account, self.cipher.encrypt(canonical(data).encode())),
            )
            self._audit(db, "credentials_updated", account, {"fields": sorted(data)})

    def oauth_state(self, account, state, verifier):
        with self.db() as db:
            db.execute("DELETE FROM oauth_states WHERE expires < ?", (time.time(),))
            db.execute(
                "INSERT INTO oauth_states VALUES (?,?,?,?)",
                (state, account, verifier, time.time() + 600),
            )

    def consume_state(self, state):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "DELETE FROM oauth_states WHERE state=? AND expires>? RETURNING *",
                (state, time.time()),
            ).fetchone()
        if not row:
            raise AdsError("OAuth state is expired, missing or already used", "invalid_state", 400)
        return dict(row)

    def brand(self, brand, value=None):
        with self.db() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO context VALUES (?,?)", (brand, canonical(value)))
                self._audit(db, "brand_saved", brand, {})
            row = db.execute("SELECT body FROM context WHERE brand=?", (brand,)).fetchone()
        return json.loads(row["body"]) if row else None

    def audit_page(self, after=0, limit=100):
        with self.db() as db:
            rows = [
                dict(r) | {"body": json.loads(r["body"])}
                for r in db.execute(
                    "SELECT * FROM audit WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)
                )
            ]
        return {
            "items": rows,
            "next_cursor": rows[-1]["seq"] if rows else after,
            "page_full": len(rows) == limit,
        }
