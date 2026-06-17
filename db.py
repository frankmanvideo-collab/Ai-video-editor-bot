from __future__ import annotations
import json, sqlite3, threading
from contextlib import contextmanager
from typing import Optional
from config import DB_PATH, MAX_WALLET_BALANCE_PAISA

_local = threading.local()

def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return c

@contextmanager
def tx():
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback(); raise

def init_db() -> None:
    with tx() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(
          user_id INTEGER PRIMARY KEY,
          username TEXT,
          balance_paisa INTEGER DEFAULT 0 CHECK(balance_paisa>=0),
          free_sample_used INTEGER DEFAULT 0,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          event_type TEXT NOT NULL,
          delta_paisa INTEGER NOT NULL,
          balance_after INTEGER NOT NULL,
          note TEXT,
          job_id TEXT,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS order_payments(
          client_txn_id TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL,
          amount_paisa INTEGER NOT NULL,
          status TEXT DEFAULT 'PENDING',
          gateway_ref TEXT,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
          processed_at DATETIME
        );
        CREATE TABLE IF NOT EXISTS manual_recharges(
          request_id TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL,
          amount_paisa INTEGER NOT NULL,
          secret_code TEXT NOT NULL,
          utr TEXT UNIQUE,
          screenshot_file_id TEXT,
          status TEXT DEFAULT 'WAITING_UTR',
          attempts INTEGER DEFAULT 0,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
          expires_at DATETIME NOT NULL,
          submitted_at DATETIME,
          approved_at DATETIME,
          approved_by INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_manual_user_status ON manual_recharges(user_id,status);
        CREATE INDEX IF NOT EXISTS idx_manual_utr ON manual_recharges(utr);
        CREATE TABLE IF NOT EXISTS sessions(
          user_id INTEGER PRIMARY KEY,
          data TEXT NOT NULL,
          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS jobs(
          job_id TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL,
          kind TEXT NOT NULL,
          price_paisa INTEGER NOT NULL DEFAULT 0,
          config TEXT NOT NULL,
          status TEXT DEFAULT 'QUEUED',
          error TEXT,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
          started_at DATETIME,
          completed_at DATETIME
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id);
        ''')

def get_user(user_id: int, username: str = "") -> dict:
    with tx() as c:
        r = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not r:
            c.execute("INSERT INTO users(user_id,username) VALUES(?,?)", (user_id, username))
            return {"balance_paisa":0, "free_sample_used":False}
        return {"balance_paisa":r["balance_paisa"], "free_sample_used":bool(r["free_sample_used"])}

def set_free_sample_used(user_id: int) -> None:
    with tx() as c:
        c.execute("UPDATE users SET free_sample_used=1, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (user_id,))

def credit_wallet(user_id: int, amount: int, note: str, job_id: Optional[str]=None) -> int:
    with tx() as c:
        r = c.execute("SELECT balance_paisa FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not r: c.execute("INSERT INTO users(user_id) VALUES(?)", (user_id,)); bal = 0
        else: bal = int(r["balance_paisa"])
        new = bal + amount
        if new > MAX_WALLET_BALANCE_PAISA: raise ValueError("Max wallet balance exceeded")
        c.execute("UPDATE users SET balance_paisa=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (new,user_id))
        c.execute("INSERT INTO ledger(user_id,event_type,delta_paisa,balance_after,note,job_id) VALUES(?,?,?,?,?,?)", (user_id,"CREDIT",amount,new,note,job_id))
        return new

def debit_wallet(user_id: int, amount: int, note: str, job_id: Optional[str]=None) -> tuple[bool,int]:
    with tx() as c:
        c.execute("UPDATE users SET balance_paisa=balance_paisa-?, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND balance_paisa>=?", (amount,user_id,amount))
        if c.rowcount == 0:
            r = c.execute("SELECT balance_paisa FROM users WHERE user_id=?", (user_id,)).fetchone()
            return False, int(r["balance_paisa"]) if r else 0
        new = int(c.execute("SELECT balance_paisa FROM users WHERE user_id=?", (user_id,)).fetchone()["balance_paisa"])
        c.execute("INSERT INTO ledger(user_id,event_type,delta_paisa,balance_after,note,job_id) VALUES(?,?,?,?,?,?)", (user_id,"DEBIT",-amount,new,note,job_id))
        return True,new

def refund_wallet(user_id:int, amount:int, note:str, job_id:Optional[str]=None) -> int:
    return credit_wallet(user_id, amount, note, job_id)

def set_balance(user_id:int, amount:int, note:str="admin") -> None:
    get_user(user_id)
    with tx() as c:
        old = int(c.execute("SELECT balance_paisa FROM users WHERE user_id=?", (user_id,)).fetchone()["balance_paisa"])
        c.execute("UPDATE users SET balance_paisa=? WHERE user_id=?", (amount,user_id))
        c.execute("INSERT INTO ledger(user_id,event_type,delta_paisa,balance_after,note) VALUES(?,?,?,?,?)", (user_id,"ADMIN_SET",amount-old,amount,note))

def save_session(user_id:int, data:dict) -> None:
    with tx() as c:
        c.execute("INSERT INTO sessions(user_id,data) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated_at=CURRENT_TIMESTAMP", (user_id,json.dumps(data)))

def load_session(user_id:int) -> Optional[dict]:
    r = conn().execute("SELECT data FROM sessions WHERE user_id=?", (user_id,)).fetchone()
    return json.loads(r["data"]) if r else None

def clear_session(user_id:int) -> None:
    with tx() as c: c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

def log_order(txn: str, user_id:int, amount:int) -> None:
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO order_payments(client_txn_id,user_id,amount_paisa) VALUES(?,?,?)", (txn,user_id,amount))

def confirm_order(txn: str, ref: str) -> Optional[dict]:
    with tx() as c:
        c.execute("UPDATE order_payments SET status='PAID', gateway_ref=?, processed_at=CURRENT_TIMESTAMP WHERE client_txn_id=? AND status='PENDING' AND processed_at IS NULL", (ref,txn))
        if c.rowcount == 0: return None
        r = c.execute("SELECT user_id,amount_paisa FROM order_payments WHERE client_txn_id=?", (txn,)).fetchone()
        return {"user_id":r["user_id"], "amount_paisa":r["amount_paisa"]}

def create_job(job_id:str, user_id:int, kind:str, price:int, config:dict) -> None:
    with tx() as c:
        c.execute("INSERT INTO jobs(job_id,user_id,kind,price_paisa,config) VALUES(?,?,?,?,?)", (job_id,user_id,kind,price,json.dumps(config)))

def update_job(job_id:str, status:str, error: str|None=None) -> None:
    with tx() as c:
        if status == "PROCESSING": c.execute("UPDATE jobs SET status=?, started_at=CURRENT_TIMESTAMP WHERE job_id=?", (status,job_id))
        elif status in ("COMPLETED","FAILED"): c.execute("UPDATE jobs SET status=?, error=?, completed_at=CURRENT_TIMESTAMP WHERE job_id=?", (status,error,job_id))
        else: c.execute("UPDATE jobs SET status=? WHERE job_id=?", (status,job_id))

def pending_jobs() -> list[dict]:
    rows = conn().execute("SELECT * FROM jobs WHERE status IN ('QUEUED','PROCESSING') ORDER BY created_at").fetchall()
    return [{"job_id":r["job_id"],"user_id":r["user_id"],"kind":r["kind"],"price_paisa":r["price_paisa"],"config":json.loads(r["config"])} for r in rows]

def stats() -> dict:
    c=conn()
    users=c.execute("SELECT COUNT(*), COALESCE(SUM(balance_paisa),0) FROM users").fetchone()
    rev=c.execute("SELECT COALESCE(SUM(amount_paisa),0) FROM order_payments WHERE status='PAID'").fetchone()[0]
    jobs=c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('QUEUED','PROCESSING')").fetchone()[0]
    return {"users":users[0],"wallet":users[1],"revenue":rev,"jobs":jobs}


# ─────────────────────────────────────────────────────────────────────────────
# Manual UPI recharge fallback
# ─────────────────────────────────────────────────────────────────────────────

def create_manual_recharge(request_id: str, user_id: int, amount_paisa: int, secret_code: str, expires_at: str) -> None:
    with tx() as c:
        c.execute(
            """INSERT INTO manual_recharges(request_id,user_id,amount_paisa,secret_code,expires_at)
               VALUES(?,?,?,?,?)""",
            (request_id, user_id, amount_paisa, secret_code, expires_at),
        )

def get_manual_recharge(request_id: str) -> Optional[dict]:
    r = conn().execute("SELECT * FROM manual_recharges WHERE request_id=?", (request_id,)).fetchone()
    return dict(r) if r else None

def get_latest_waiting_manual(user_id: int) -> Optional[dict]:
    r = conn().execute(
        """SELECT * FROM manual_recharges
           WHERE user_id=? AND status IN ('WAITING_UTR','WAITING_CODE','SUBMITTED')
           ORDER BY created_at DESC LIMIT 1""",
        (user_id,),
    ).fetchone()
    return dict(r) if r else None

def manual_utr_exists(utr: str) -> bool:
    r = conn().execute("SELECT request_id FROM manual_recharges WHERE utr=?", (utr,)).fetchone()
    return bool(r)

def submit_manual_utr(request_id: str, utr: str) -> None:
    with tx() as c:
        c.execute(
            """UPDATE manual_recharges
               SET utr=?, status='WAITING_CODE', submitted_at=CURRENT_TIMESTAMP
               WHERE request_id=? AND status='WAITING_UTR'""",
            (utr, request_id),
        )

def mark_manual_submitted(request_id: str) -> None:
    with tx() as c:
        c.execute("UPDATE manual_recharges SET status='SUBMITTED' WHERE request_id=? AND status='WAITING_CODE'", (request_id,))

def increment_manual_attempt(request_id: str) -> int:
    with tx() as c:
        c.execute("UPDATE manual_recharges SET attempts=attempts+1 WHERE request_id=?", (request_id,))
        r = c.execute("SELECT attempts FROM manual_recharges WHERE request_id=?", (request_id,)).fetchone()
        return int(r["attempts"]) if r else 0

def fail_manual_recharge(request_id: str, status: str = 'FAILED') -> None:
    with tx() as c:
        c.execute("UPDATE manual_recharges SET status=? WHERE request_id=?", (status, request_id))

def approve_manual_recharge(request_id: str, admin_id: int) -> Optional[dict]:
    with tx() as c:
        r = c.execute("SELECT * FROM manual_recharges WHERE request_id=?", (request_id,)).fetchone()
        if not r or r["status"] != "SUBMITTED":
            return None
        c.execute(
            "UPDATE manual_recharges SET status='APPROVED', approved_at=CURRENT_TIMESTAMP, approved_by=? WHERE request_id=? AND status='SUBMITTED'",
            (admin_id, request_id),
        )
        if c.rowcount == 0:
            return None
        return dict(r)

def reject_manual_recharge(request_id: str, admin_id: int) -> Optional[dict]:
    with tx() as c:
        r = c.execute("SELECT * FROM manual_recharges WHERE request_id=?", (request_id,)).fetchone()
        if not r or r["status"] not in ('SUBMITTED','WAITING_CODE','WAITING_UTR'):
            return None
        c.execute("UPDATE manual_recharges SET status='REJECTED', approved_by=? WHERE request_id=?", (admin_id, request_id))
        return dict(r)

def count_manual_approved_today(user_id: int) -> int:
    r = conn().execute(
        """SELECT COUNT(*) AS c FROM manual_recharges
           WHERE user_id=? AND status='APPROVED' AND date(approved_at)=date('now','localtime')""",
        (user_id,),
    ).fetchone()
    return int(r["c"] if r else 0)
