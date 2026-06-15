"""
================================================================================
GOD MODE VIDEO BOT — Production-Grade Telegram Video Editing SaaS
Version: 2.0.1 (syntax/runtime fixes)
Python 3.10+ | python-telegram-bot==20.8 | moviepy==2.1.1
openai==1.58.1 | flask==3.0.3 | gunicorn==22.0.0
================================================================================
"""

from __future__ import annotations

import atexit
import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests
from flask import Flask, jsonify, request
from openai import OpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from moviepy import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    concatenate_videoclips,
)

try:
    # MoviePy 2.x effect class
    from moviepy.audio.fx.MultiplyVolume import MultiplyVolume
except Exception:  # pragma: no cover
    MultiplyVolume = None  # type: ignore

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

BOT_VERSION = "2.0.1"

# ── Environment Variables ─────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ["BOT_TOKEN"]
ADMIN_USER_ID: int = int(os.environ.get("ADMIN_USER_ID", "123456789"))
AICREDITS_API_KEY: str = os.environ["AICREDITS_API_KEY"]
UPIGATEWAY_API_KEY: str = os.environ.get("UPIGATEWAY_API_KEY", "")
UPIGATEWAY_SECRET: str = os.environ.get("UPIGATEWAY_SECRET", "")
WEBHOOK_URL: str = os.environ.get("WEBHOOK_URL", "").rstrip("/")
FLASK_PORT: int = int(os.environ.get("FLASK_PORT", "8080"))

# ── Paths ─────────────────────────────────────────────────────────────────────
DB_PATH: str = "/data/godmode_wallet.db" if os.path.exists("/data") else "godmode_wallet.db"
DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)
ASSETS_DIR = Path("assets")
FONT_PATH = os.environ.get(
    "FONT_PATH", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
)

# ── Pricing ───────────────────────────────────────────────────────────────────
PER_MINUTE_PRICE_PAISA: int = 5000  # Legacy fallback
PRICE_UPTO_60_SEC_PAISA: int = 2900   # ₹29 per reel up to 60 seconds
PRICE_UPTO_120_SEC_PAISA: int = 4900  # ₹49 per reel up to 2 minutes
FREE_TRIAL_SECONDS: int = 15
MIN_VIDEO_DURATION_SEC: float = 3.0
MAX_VIDEO_DURATION_SEC: float = 120.0

# ── Billing Limits ────────────────────────────────────────────────────────────
MAX_SINGLE_RECHARGE_PAISA: int = 1_000_000
MIN_RECHARGE_PAISA: int = 4900
MAX_WALLET_BALANCE_PAISA: int = 5_000_000
RECHARGE_RATE_LIMIT_PER_HOUR: int = 5

# ── File Limits ───────────────────────────────────────────────────────────────
MAX_FILE_SIZE_MB: int = 150
MAX_SCREENSHOTS: int = 3
ALLOWED_VIDEO_MIMES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/webm",
    "video/x-matroska",
}
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}

# ── Queue Limits ──────────────────────────────────────────────────────────────
MAX_QUEUE_SIZE: int = 100

PUNCHWORDS_DEFAULT = {"STOP", "CASH", "SECRET", "FREE", "HACK", "WIN", "LOSE", "NOW"}

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("GodModeBot")

# ═══════════════════════════════════════════════════════════════════════════════
# CLIENTS / APP
# ═══════════════════════════════════════════════════════════════════════════════

ai_client = OpenAI(
    api_key=AICREDITS_API_KEY,
    base_url=os.environ.get("AICREDITS_BASE_URL", "https://aicredits.in/v1"),
)

flask_app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class BotState:
    """Thread-safe container for global mutable state."""

    video_queue: Optional[asyncio.Queue] = None
    active_tasks: int = 0
    active_jobs: dict[int, str] = field(default_factory=dict)
    worker_heartbeat: float = 0.0
    shutdown_event: Optional[asyncio.Event] = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def increment_tasks(self) -> int:
        with self.lock:
            self.active_tasks += 1
            return self.active_tasks

    def decrement_tasks(self) -> int:
        with self.lock:
            self.active_tasks = max(0, self.active_tasks - 1)
            return self.active_tasks

    def set_user_job(self, user_id: int, job_id: str) -> bool:
        with self.lock:
            if user_id in self.active_jobs:
                return False
            self.active_jobs[user_id] = job_id
            return True

    def clear_user_job(self, user_id: int) -> None:
        with self.lock:
            self.active_jobs.pop(user_id, None)

    def has_active_job(self, user_id: int) -> bool:
        with self.lock:
            return user_id in self.active_jobs

    def update_heartbeat(self) -> None:
        with self.lock:
            self.worker_heartbeat = time.time()

    def is_worker_healthy(self, max_age_sec: float = 60.0) -> bool:
        with self.lock:
            return (time.time() - self.worker_heartbeat) < max_age_sec


state = BotState()
recharge_timestamps: dict[int, list[float]] = defaultdict(list)
recharge_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="godmode_")
tg_app: Optional[Application] = None
main_loop: Optional[asyncio.AbstractEventLoop] = None
_db_local = threading.local()

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE LAYER
# ═══════════════════════════════════════════════════════════════════════════════


def get_db_connection() -> sqlite3.Connection:
    """Get or create a thread-local database connection."""
    conn = getattr(_db_local, "connection", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.row_factory = sqlite3.Row
        _db_local.connection = conn
    return conn


@contextmanager
def db_transaction():
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance_paisa INTEGER DEFAULT 0 CHECK (balance_paisa >= 0),
            free_trial_used INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS order_payments (
            client_txn_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            amount_paisa INTEGER NOT NULL CHECK (amount_paisa > 0),
            status TEXT DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'PAID', 'FAILED', 'EXPIRED')),
            gateway_ref TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            processed_at DATETIME DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            delta_paisa INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            note TEXT,
            job_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS pending_jobs (
            job_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            video_file_id TEXT NOT NULL,
            screenshots TEXT,
            lang TEXT DEFAULT 'English',
            placement TEXT DEFAULT 'bottom',
            preset TEXT DEFAULT 'mrbeast',
            status TEXT DEFAULT 'QUEUED' CHECK (status IN ('QUEUED', 'PROCESSING', 'COMPLETED', 'FAILED')),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME,
            completed_at DATETIME,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS user_sessions (
            user_id INTEGER PRIMARY KEY,
            video_file_id TEXT,
            screenshots TEXT,
            lang TEXT,
            placement TEXT,
            preset TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id);
        CREATE INDEX IF NOT EXISTS idx_ledger_job ON ledger(job_id);
        CREATE INDEX IF NOT EXISTS idx_orders_user ON order_payments(user_id);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON order_payments(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_user ON pending_jobs(user_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON pending_jobs(status);
        """
    )
    conn.commit()
    logger.info("Database initialized at %s", DB_PATH)


def get_user(user_id: int, username: str = "") -> dict:
    with db_transaction() as conn:
        cur = conn.cursor()
        cur.execute("SELECT balance_paisa, free_trial_used FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO users (user_id, username, balance_paisa, free_trial_used) VALUES (?, ?, 0, 0)",
                (user_id, username),
            )
            return {"balance_paisa": 0, "balance_rs": 0.0, "free_trial_used": False}
        return {
            "balance_paisa": row["balance_paisa"],
            "balance_rs": row["balance_paisa"] / 100,
            "free_trial_used": bool(row["free_trial_used"]),
        }


def credit_wallet(user_id: int, amount_paisa: int, note: str = "recharge", job_id: Optional[str] = None) -> int:
    with db_transaction() as conn:
        cur = conn.cursor()
        cur.execute("SELECT balance_paisa FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"User {user_id} not found")
        new_balance = row["balance_paisa"] + amount_paisa
        if new_balance > MAX_WALLET_BALANCE_PAISA:
            raise ValueError(f"Would exceed max wallet balance of ₹{MAX_WALLET_BALANCE_PAISA / 100:.2f}")
        cur.execute(
            "UPDATE users SET balance_paisa = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (new_balance, user_id),
        )
        cur.execute(
            "INSERT INTO ledger (user_id, event_type, delta_paisa, balance_after, note, job_id) VALUES (?, 'CREDIT', ?, ?, ?, ?)",
            (user_id, amount_paisa, new_balance, note, job_id),
        )
        return new_balance


def debit_wallet(user_id: int, amount_paisa: int, note: str = "video_render", job_id: Optional[str] = None) -> tuple[bool, int]:
    with db_transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET balance_paisa = balance_paisa - ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND balance_paisa >= ?
            """,
            (amount_paisa, user_id, amount_paisa),
        )
        if cur.rowcount == 0:
            cur.execute("SELECT balance_paisa FROM users WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            return False, row["balance_paisa"] if row else 0
        cur.execute("SELECT balance_paisa FROM users WHERE user_id = ?", (user_id,))
        new_balance = cur.fetchone()["balance_paisa"]
        cur.execute(
            "INSERT INTO ledger (user_id, event_type, delta_paisa, balance_after, note, job_id) VALUES (?, 'DEBIT', ?, ?, ?, ?)",
            (user_id, -amount_paisa, new_balance, note, job_id),
        )
        return True, new_balance


def refund_wallet(user_id: int, amount_paisa: int, note: str = "refund", job_id: Optional[str] = None) -> int:
    with db_transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET balance_paisa = balance_paisa + ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (amount_paisa, user_id),
        )
        cur.execute("SELECT balance_paisa FROM users WHERE user_id = ?", (user_id,))
        new_balance = cur.fetchone()["balance_paisa"]
        cur.execute(
            "INSERT INTO ledger (user_id, event_type, delta_paisa, balance_after, note, job_id) VALUES (?, 'REFUND', ?, ?, ?, ?)",
            (user_id, amount_paisa, new_balance, note, job_id),
        )
        return new_balance


def set_balance_paisa(user_id: int, new_paisa: int, note: str = "admin_set") -> None:
    with db_transaction() as conn:
        cur = conn.cursor()
        cur.execute("SELECT balance_paisa FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        old_balance = row["balance_paisa"] if row else 0
        if row:
            cur.execute(
                "UPDATE users SET balance_paisa = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (new_paisa, user_id),
            )
        else:
            cur.execute(
                "INSERT INTO users (user_id, balance_paisa, free_trial_used) VALUES (?, ?, 0)",
                (user_id, new_paisa),
            )
        cur.execute(
            "INSERT INTO ledger (user_id, event_type, delta_paisa, balance_after, note) VALUES (?, 'ADMIN_SET', ?, ?, ?)",
            (user_id, new_paisa - old_balance, new_paisa, note),
        )


def mark_free_trial_used(user_id: int) -> None:
    with db_transaction() as conn:
        conn.execute(
            "UPDATE users SET free_trial_used = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (user_id,),
        )


def log_order(client_txn_id: str, user_id: int, amount_paisa: int) -> None:
    with db_transaction() as conn:
        conn.execute(
            """
            INSERT INTO order_payments (client_txn_id, user_id, amount_paisa, status)
            VALUES (?, ?, ?, 'PENDING')
            ON CONFLICT(client_txn_id) DO NOTHING
            """,
            (client_txn_id, user_id, amount_paisa),
        )


def confirm_order(client_txn_id: str, gateway_ref: str) -> Optional[dict]:
    with db_transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE order_payments
            SET status = 'PAID', gateway_ref = ?, updated_at = CURRENT_TIMESTAMP, processed_at = CURRENT_TIMESTAMP
            WHERE client_txn_id = ? AND status = 'PENDING' AND processed_at IS NULL
            """,
            (gateway_ref, client_txn_id),
        )
        if cur.rowcount == 0:
            return None
        cur.execute("SELECT user_id, amount_paisa FROM order_payments WHERE client_txn_id = ?", (client_txn_id,))
        row = cur.fetchone()
        return {"user_id": row["user_id"], "amount_paisa": row["amount_paisa"]}


def create_pending_job(
    job_id: str,
    user_id: int,
    video_file_id: str,
    screenshots: list[str],
    lang: str,
    placement: str,
    preset: str,
) -> None:
    with db_transaction() as conn:
        conn.execute(
            """
            INSERT INTO pending_jobs (job_id, user_id, video_file_id, screenshots, lang, placement, preset, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'QUEUED')
            """,
            (job_id, user_id, video_file_id, json.dumps(screenshots), lang, placement, preset),
        )


def update_job_status(job_id: str, status: str, error_message: Optional[str] = None) -> None:
    with db_transaction() as conn:
        if status == "PROCESSING":
            conn.execute(
                "UPDATE pending_jobs SET status = ?, started_at = CURRENT_TIMESTAMP WHERE job_id = ?",
                (status, job_id),
            )
        elif status in ("COMPLETED", "FAILED"):
            conn.execute(
                "UPDATE pending_jobs SET status = ?, completed_at = CURRENT_TIMESTAMP, error_message = ? WHERE job_id = ?",
                (status, error_message, job_id),
            )
        else:
            conn.execute("UPDATE pending_jobs SET status = ? WHERE job_id = ?", (status, job_id))


def get_pending_jobs() -> list[dict]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT job_id, user_id, video_file_id, screenshots, lang, placement, preset
        FROM pending_jobs
        WHERE status IN ('QUEUED', 'PROCESSING')
        ORDER BY created_at ASC
        """
    )
    rows = cur.fetchall()
    return [
        {
            "job_id": row["job_id"],
            "user_id": row["user_id"],
            "video_file_id": row["video_file_id"],
            "screenshots": json.loads(row["screenshots"] or "[]"),
            "lang": row["lang"],
            "placement": row["placement"],
            "preset": row["preset"],
        }
        for row in rows
    ]


def save_user_session(user_id: int, session: dict) -> None:
    with db_transaction() as conn:
        conn.execute(
            """
            INSERT INTO user_sessions (user_id, video_file_id, screenshots, lang, placement, preset)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                video_file_id = excluded.video_file_id,
                screenshots = excluded.screenshots,
                lang = excluded.lang,
                placement = excluded.placement,
                preset = excluded.preset,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                session.get("video_file_id"),
                json.dumps(session.get("screenshots", [])),
                session.get("lang"),
                session.get("placement"),
                session.get("preset"),
            ),
        )


def load_user_session(user_id: int) -> Optional[dict]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT video_file_id, screenshots, lang, placement, preset FROM user_sessions WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    if not row or not row["video_file_id"]:
        return None
    return {
        "video_file_id": row["video_file_id"],
        "screenshots": json.loads(row["screenshots"] or "[]"),
        "lang": row["lang"] or "English",
        "placement": row["placement"] or "bottom",
        "preset": row["preset"] or "mrbeast",
    }


def clear_user_session(user_id: int) -> None:
    with db_transaction() as conn:
        conn.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))


def get_stats() -> dict:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COALESCE(SUM(balance_paisa), 0) FROM users")
    users_row = cur.fetchone()
    cur.execute("SELECT COALESCE(SUM(amount_paisa), 0) FROM order_payments WHERE status = 'PAID'")
    revenue_row = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM pending_jobs WHERE status IN ('QUEUED', 'PROCESSING')")
    pending_row = cur.fetchone()
    return {
        "total_users": users_row[0],
        "total_wallet_paisa": users_row[1],
        "total_revenue_paisa": revenue_row[0],
        "pending_jobs": pending_row[0],
    }

# ═══════════════════════════════════════════════════════════════════════════════
# UTILS
# ═══════════════════════════════════════════════════════════════════════════════


def rupees_str(paisa: int) -> str:
    return f"₹{paisa / 100:.2f}"


def compute_cost_paisa(duration_seconds: float) -> int:
    """Flat reels/shorts pricing.

    - First free trial: handled separately by FREE_TRIAL_SECONDS.
    - Paid reel up to 60s: ₹29
    - Paid reel 60-120s: ₹49
    """
    if duration_seconds <= 60:
        return PRICE_UPTO_60_SEC_PAISA
    return PRICE_UPTO_120_SEC_PAISA


def escape_md(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", str(text))


def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    if not UPIGATEWAY_SECRET:
        logger.warning("UPIGATEWAY_SECRET not set; skipping signature verification")
        return True
    expected = hmac.new(UPIGATEWAY_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def check_recharge_rate_limit(user_id: int) -> bool:
    with recharge_lock:
        now = time.time()
        hour_ago = now - 3600
        recharge_timestamps[user_id] = [ts for ts in recharge_timestamps[user_id] if ts > hour_ago]
        if len(recharge_timestamps[user_id]) >= RECHARGE_RATE_LIMIT_PER_HOUR:
            return False
        recharge_timestamps[user_id].append(now)
        return True


def scan_assets(sub: str, exts: tuple[str, ...] = (".mp3", ".wav")) -> list[str]:
    d = ASSETS_DIR / sub
    if not d.exists():
        return []
    return [f.name for f in d.iterdir() if f.is_file() and f.suffix.lower() in exts]


def apply_volume(clip: AudioFileClip, factor: float):
    """MoviePy 2.x compatible volume helper."""
    if MultiplyVolume is not None:
        return clip.with_effects([MultiplyVolume(factor)])
    if hasattr(clip, "with_volume_scaled"):
        return clip.with_volume_scaled(factor)
    return clip


def safe_json_loads(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return fallback

# ═══════════════════════════════════════════════════════════════════════════════
# FFMPEG UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════


def ffprobe_duration(path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def ffmpeg_silence_remove(input_path: str, output_path: str) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-af",
        "silenceremove=start_periods=1:start_duration=0.15:start_threshold=-35dB",
        "-c:v",
        "copy",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)


def ffmpeg_extract_audio(video_path: str, audio_path: str) -> None:
    cmd = ["ffmpeg", "-y", "-i", video_path, "-q:a", "0", "-map", "a", audio_path]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)


def ffmpeg_speed_video(input_path: str, output_path: str, speed: float = 1.1) -> None:
    # atempo supports 0.5-100 in modern FFmpeg; 1.1 is safe.
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-filter_complex",
        f"[0:v]setpts={1 / speed:.4f}*PTS[v];[0:a]atempo={speed}[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)

# ═══════════════════════════════════════════════════════════════════════════════
# AI DIRECTOR
# ═══════════════════════════════════════════════════════════════════════════════


def ai_analyze_vibe(transcript: str, target_language: str = "English") -> dict:
    pop_files = scan_assets("pop_sounds")
    bgm_files = scan_assets("backgrounds")
    sfx_files = scan_assets("transition_sfx")
    system_prompt = """
You are a Hollywood AI Video Director. Analyze the transcript and return ONLY a compact JSON object — no prose, no markdown fences.
Keys required:
mood: one of [motivational, calm_luxury, tech_crypto, urgent, neutral]
cuts: array of {start_sec: float, end_sec: float} segments to remove
chosen_bgm: filename from available_bgm or null
chosen_pop_heavy: filename from available_pop or null
chosen_pop_soft: filename from available_pop or null
chosen_sfx: filename from available_sfx or null
punchwords: array of strings in ALL CAPS
broll_timestamps: array of {image_index:int, at_sec:float} max 3
translated_lines: array of {start_sec,end_sec,text} in target_language, empty if same
hook_text: a short 4-8 word viral hook sentence for first 3 seconds
""".strip()
    payload = {
        "transcript": transcript[:4000],
        "target_language": target_language,
        "available_bgm": bgm_files,
        "available_pop": pop_files,
        "available_sfx": sfx_files,
    }
    try:
        resp = ai_client.chat.completions.create(
            model=os.environ.get("AI_DIRECTOR_MODEL", "gpt-4o"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
            max_tokens=800,
            timeout=30.0,
        )
        return safe_json_loads(resp.choices[0].message.content or "{}", {})
    except Exception as e:
        logger.warning("AI Director fallback due to: %s", e)
        return {
            "mood": "neutral",
            "cuts": [],
            "chosen_bgm": bgm_files[0] if bgm_files else None,
            "chosen_pop_heavy": pop_files[0] if pop_files else None,
            "chosen_pop_soft": pop_files[-1] if len(pop_files) > 1 else (pop_files[0] if pop_files else None),
            "chosen_sfx": sfx_files[0] if sfx_files else None,
            "punchwords": ["STOP", "CASH", "SECRET"],
            "broll_timestamps": [],
            "translated_lines": [],
            "hook_text": "Don't Skip This Watch Now",
        }


def _fake_word_timestamps(text: str, duration_sec: float) -> list[dict]:
    """
    Create approximate word timestamps when provider does not support word-level Whisper.
    This keeps caption rendering working even if AICredits rejects timestamp_granularities.
    """
    words = [w for w in str(text or "").split() if w.strip()]
    if not words:
        return []

    total = max(float(duration_sec or 1.0), 1.0)
    step = total / max(len(words), 1)

    out = []
    for i, word in enumerate(words):
        start = i * step
        end = min(total, start + max(0.20, step * 0.85))
        out.append({
            "word": word,
            "start": start,
            "end": end,
        })

    return out


def ai_get_word_timestamps(audio_path: str, duration_sec: float) -> dict:
    """
    Get transcription with word timestamps.

    Some OpenAI-compatible providers reject:
        timestamp_granularities=["word"]

    In that case they may return:
        Invalid request body

    So this function:
    1. Tries true word-level timestamps.
    2. If that fails, tries normal verbose_json transcription.
    3. If that fails, tries plain text transcription.
    4. If all fail, returns empty text/words so render can continue.
    """
    timeout = max(60, int(duration_sec * 2))
    model = os.environ.get("WHISPER_MODEL", "whisper-1")

    # Try 1: True word-level timestamps
    try:
        with open(audio_path, "rb") as f:
            resp = ai_client.audio.transcriptions.create(
                model=model,
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["word"],
                timeout=timeout,
            )

        data = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)

        if data.get("words"):
            return data

        text = data.get("text", "")
        data["words"] = _fake_word_timestamps(text, duration_sec)
        return data

    except Exception as e:
        logger.warning("Whisper word timestamps failed, falling back: %s", e)

    # Try 2: verbose_json without word timestamp option
    try:
        with open(audio_path, "rb") as f:
            resp = ai_client.audio.transcriptions.create(
                model=model,
                file=f,
                response_format="verbose_json",
                timeout=timeout,
            )

        data = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
        text = data.get("text", "")
        data["words"] = data.get("words") or _fake_word_timestamps(text, duration_sec)
        return data

    except Exception as e:
        logger.warning("Whisper verbose_json fallback failed: %s", e)

    # Try 3: plain text transcription
    try:
        with open(audio_path, "rb") as f:
            resp = ai_client.audio.transcriptions.create(
                model=model,
                file=f,
                response_format="text",
                timeout=timeout,
            )

        text = str(resp)
        return {
            "text": text,
            "words": _fake_word_timestamps(text, duration_sec),
        }

    except Exception as e:
        logger.warning("Whisper text fallback failed: %s", e)

    # Last fallback: no captions, but do not fail full render
    return {
        "text": "",
        "words": [],
        }

# ═══════════════════════════════════════════════════════════════════════════════
# VIDEO COMPOSITION
# ═══════════════════════════════════════════════════════════════════════════════


def load_audio_track(filename: Optional[str], sub: str) -> Optional[AudioFileClip]:
    if not filename:
        return None
    path = ASSETS_DIR / sub / filename
    return AudioFileClip(str(path)) if path.exists() else None


def apply_zoom_segments(base: VideoFileClip, v_w: int, v_h: int) -> tuple[VideoFileClip, list[Any]]:
    segments: list[Any] = []
    t = 0.0
    zoom_on = False
    dur = float(base.duration or 0)
    while t < dur:
        chunk = random.uniform(2.5, 3.2)
        end = min(t + chunk, dur)
        clip = base.subclipped(t, end)
        if zoom_on:
            clip = clip.resized(1.20).cropped(
                x_center=v_w * 0.6,
                y_center=v_h * 0.6,
                width=v_w,
                height=v_h,
            )
        segments.append(clip)
        zoom_on = not zoom_on
        t = end
    if not segments:
        return base, []
    return concatenate_videoclips(segments), segments


def make_caption_clips(
    whisper_words: list[dict],
    v_w: int,
    y_pos: int,
    font_size: int,
    punchwords: set[str],
    pop_heavy_path: Optional[str],
    pop_soft_path: Optional[str],
) -> tuple[list[TextClip], list[AudioFileClip]]:
    txt_clips: list[TextClip] = []
    audio_clips: list[AudioFileClip] = []
    i = 0
    while i < len(whisper_words):
        group = whisper_words[i : i + 3]
        i += 3
        text = " ".join(str(w.get("word", "")).strip() for w in group).strip()
        if not text:
            continue
        t_start = float(group[0].get("start", 0))
        t_end = float(group[-1].get("end", t_start + 1.0))
        duration = max(t_end - t_start, 0.3)
        is_punch = any(str(w.get("word", "")).upper().strip(".,!?:;\"'") in punchwords for w in group)
        colour = "#CCFF00" if is_punch else "white"
        fsize = int(font_size * 1.3) if is_punch else font_size
        try:
            tc = (
                TextClip(
                    text=text,
                    font=FONT_PATH,
                    font_size=fsize,
                    color=colour,
                    stroke_color="black",
                    stroke_width=6 if is_punch else 4,
                    method="label",
                )
                .with_start(t_start)
                .with_duration(duration)
                .with_position(("center", y_pos))
            )
            txt_clips.append(tc)
            sfx_path = pop_heavy_path if is_punch else pop_soft_path
            if sfx_path and Path(sfx_path).exists():
                ac = AudioFileClip(sfx_path).with_start(t_start).with_duration(min(0.4, duration))
                audio_clips.append(ac)
        except Exception as e:
            logger.warning("Caption clip error for %r: %s", text, e)
    return txt_clips, audio_clips


def hook_banner(v_w: int, font_size: int, hook_text: str) -> TextClip:
    return (
        TextClip(
            text=hook_text.upper(),
            font=FONT_PATH,
            font_size=font_size,
            color="#FFD700",
            stroke_color="black",
            stroke_width=8,
            method="label",
            bg_color=(0, 0, 0, 180),
        )
        .with_start(0)
        .with_duration(3.0)
        .with_position(("center", 60))
    )


def broll_overlays(
    screenshots: list[str], broll_ts: list[dict], v_w: int, v_h: int, sfx_path: Optional[str]
) -> tuple[list[ImageClip], list[AudioFileClip]]:
    vid_clips: list[ImageClip] = []
    aud_clips: list[AudioFileClip] = []
    for entry in broll_ts[:MAX_SCREENSHOTS]:
        idx = int(entry.get("image_index", 0))
        at = float(entry.get("at_sec", 0.0))
        if idx < 0 or idx >= len(screenshots):
            continue
        img_path = screenshots[idx]
        if not Path(img_path).exists():
            continue
        try:
            img_w = int(v_w * 0.45)
            ic = (
                ImageClip(img_path)
                .resized(width=img_w)
                .with_start(at)
                .with_duration(2.5)
                .with_position(("right", "center"))
            )
            if hasattr(ic, "with_effects"):
                # crossfade methods exist on many MoviePy clips; guard for portability
                try:
                    ic = ic.crossfadein(0.3).crossfadeout(0.3)
                except Exception:
                    pass
            vid_clips.append(ic)
            if sfx_path and Path(sfx_path).exists():
                ac = AudioFileClip(sfx_path).with_start(at).with_duration(0.8)
                aud_clips.append(ac)
        except Exception as e:
            logger.warning("B-roll overlay error: %s", e)
    return vid_clips, aud_clips


def apply_retake_cuts(base_clip: VideoFileClip, cuts: list[dict]) -> tuple[VideoFileClip, list[Any]]:
    if not cuts:
        return base_clip, []
    dur = float(base_clip.duration or 0)
    keep_ranges: list[tuple[float, float]] = []
    prev = 0.0
    for c in sorted(cuts, key=lambda x: float(x.get("start_sec", 0))):
        s = max(0.0, float(c.get("start_sec", 0)))
        e = min(dur, float(c.get("end_sec", 0)))
        if e <= s:
            continue
        if s > prev:
            keep_ranges.append((prev, s))
        prev = max(prev, e)
    if prev < dur:
        keep_ranges.append((prev, dur))
    segments = [base_clip.subclipped(s, e) for s, e in keep_ranges if e > s]
    if not segments:
        return base_clip, []
    return concatenate_videoclips(segments), segments


def duck_bgm(bgm_clip: AudioFileClip, whisper_words: list[dict], total_dur: float) -> CompositeAudioClip:
    duck_segments = []
    t = 0.0
    bgm_dur = float(bgm_clip.duration or 0)
    if bgm_dur <= 0:
        return CompositeAudioClip([])

    def bgm_segment(start: float, duration: float, factor: float):
        remaining = duration
        cursor = start
        while remaining > 0.01:
            seg_start = cursor % bgm_dur
            seg_len = min(remaining, bgm_dur - seg_start)
            seg = bgm_clip.subclipped(seg_start, seg_start + seg_len)
            seg = apply_volume(seg, factor).with_start(cursor).with_duration(seg_len)
            duck_segments.append(seg)
            cursor += seg_len
            remaining -= seg_len

    for w in whisper_words:
        ws = max(0.0, float(w.get("start", t)))
        we = max(ws, float(w.get("end", ws)))
        if ws > t:
            bgm_segment(t, ws - t, 0.35)
        if we > ws:
            bgm_segment(ws, we - ws, 0.05)
        t = we
    if t < total_dur:
        bgm_segment(t, total_dur - t, 0.35)
    return CompositeAudioClip(duck_segments) if duck_segments else CompositeAudioClip([apply_volume(bgm_clip, 0.15).with_duration(total_dur)])


def render_video(
    input_path: str,
    output_path: str,
    screenshots: list[str],
    style_preset: str = "mrbeast",
    placement: str = "bottom",
    target_lang: str = "English",
    job_id: str = "",
) -> float:
    base_stem = DOWNLOADS_DIR / job_id
    silence_path = str(base_stem) + "_silence.mp4"
    audio_path = str(base_stem) + "_audio.mp3"
    pre_speed_path = str(base_stem) + "_prespeed.mp4"
    temp_files = [silence_path, audio_path, pre_speed_path]
    clips_to_close: list[Any] = []
    try:
        logger.info("[%s] Removing silence", job_id)
        ffmpeg_silence_remove(input_path, silence_path)

        logger.info("[%s] Extracting audio", job_id)
        ffmpeg_extract_audio(silence_path, audio_path)
        silence_duration = ffprobe_duration(silence_path)

        logger.info("[%s] Running Whisper", job_id)
        whisper_data = ai_get_word_timestamps(audio_path, silence_duration)
        full_text = whisper_data.get("text", "")
        whisper_words = whisper_data.get("words", []) or []

        logger.info("[%s] AI Director analysis", job_id)
        decision = ai_analyze_vibe(full_text, target_language=target_lang)
        cuts = decision.get("cuts", []) or []
        hook_text = decision.get("hook_text", "Watch This Now") or "Watch This Now"
        punchwords = {str(x).upper() for x in decision.get("punchwords", [])} | PUNCHWORDS_DEFAULT
        broll_ts = decision.get("broll_timestamps", []) or []
        chosen_bgm = decision.get("chosen_bgm")
        chosen_sfx = decision.get("chosen_sfx")
        chosen_pop_h = decision.get("chosen_pop_heavy")
        chosen_pop_s = decision.get("chosen_pop_soft")

        logger.info("[%s] Applying retake cuts", job_id)
        base_clip = VideoFileClip(silence_path)
        clips_to_close.append(base_clip)
        cut_clip, cut_segments = apply_retake_cuts(base_clip, cuts)
        clips_to_close.extend(cut_segments)
        if cut_clip is not base_clip:
            clips_to_close.append(cut_clip)

        v_w, v_h = int(cut_clip.w), int(cut_clip.h)
        is_short = v_h > v_w
        font_size = 105 if is_short else 65
        if placement == "top":
            caption_y = int(v_h * 0.18)
        elif placement == "center":
            caption_y = int(v_h * 0.48)
        else:
            caption_y = int(v_h * 0.78)

        logger.info("[%s] Creating zoom segments", job_id)
        zoomed, zoom_segments = apply_zoom_segments(cut_clip, v_w, v_h)
        clips_to_close.extend(zoom_segments)
        if zoomed is not cut_clip:
            clips_to_close.append(zoomed)

        logger.info("[%s] Generating captions", job_id)
        pop_h_path = str(ASSETS_DIR / "pop_sounds" / chosen_pop_h) if chosen_pop_h else None
        pop_s_path = str(ASSETS_DIR / "pop_sounds" / chosen_pop_s) if chosen_pop_s else None
        sfx_path = str(ASSETS_DIR / "transition_sfx" / chosen_sfx) if chosen_sfx else None
        caption_clips, caption_audio = make_caption_clips(
            whisper_words, v_w, caption_y, font_size, punchwords, pop_h_path, pop_s_path
        )
        clips_to_close.extend(caption_clips)
        clips_to_close.extend(caption_audio)

        hook_clip = hook_banner(v_w, font_size, hook_text)
        clips_to_close.append(hook_clip)

        broll_vid, broll_aud = broll_overlays(screenshots, broll_ts, v_w, v_h, sfx_path)
        clips_to_close.extend(broll_vid)
        clips_to_close.extend(broll_aud)

        logger.info("[%s] Compositing video", job_id)
        composite = CompositeVideoClip([zoomed] + caption_clips + [hook_clip] + broll_vid, size=(v_w, v_h))
        clips_to_close.append(composite)

        logger.info("[%s] Assembling audio", job_id)
        speech_audio = AudioFileClip(audio_path).with_duration(composite.duration)
        clips_to_close.append(speech_audio)
        all_audio: list[Any] = [speech_audio] + caption_audio + broll_aud

        if chosen_bgm:
            bgm_full = load_audio_track(chosen_bgm, "backgrounds")
            if bgm_full:
                clips_to_close.append(bgm_full)
                bgm_ducked = duck_bgm(bgm_full, whisper_words, composite.duration)
                clips_to_close.append(bgm_ducked)
                all_audio.append(bgm_ducked)

        final_audio = CompositeAudioClip(all_audio)
        clips_to_close.append(final_audio)
        composite = composite.with_audio(final_audio)

        logger.info("[%s] Writing intermediate video", job_id)
        composite.write_videofile(
            pre_speed_path,
            codec="libx264",
            audio_codec="aac",
            fps=30,
            preset="veryfast",
            threads=max(1, min(4, os.cpu_count() or 1)),
            ffmpeg_params=["-crf", "23", "-movflags", "+faststart"],
            logger=None,
        )

        logger.info("[%s] Applying 1.1x speed", job_id)
        ffmpeg_speed_video(pre_speed_path, output_path, speed=1.1)
        final_duration = ffprobe_duration(output_path)
        logger.info("[%s] Render complete: %.1fs", job_id, final_duration)
        return final_duration
    finally:
        for clip in reversed(clips_to_close):
            try:
                close = getattr(clip, "close", None)
                if close:
                    close()
            except Exception:
                pass
        for tf in temp_files:
            try:
                p = Path(tf)
                if p.exists():
                    p.unlink()
            except Exception:
                pass

# ═══════════════════════════════════════════════════════════════════════════════
# PAYMENTS / WEBHOOKS
# ═══════════════════════════════════════════════════════════════════════════════


def create_upi_order(user_id: int, amount_rs: float) -> dict:
    amount_paisa = int(round(amount_rs * 100))
    if amount_paisa < MIN_RECHARGE_PAISA:
        return {"ok": False, "error": f"Minimum recharge is ₹{MIN_RECHARGE_PAISA / 100:.0f}"}
    if amount_paisa > MAX_SINGLE_RECHARGE_PAISA:
        return {"ok": False, "error": f"Maximum single recharge is ₹{MAX_SINGLE_RECHARGE_PAISA / 100:.0f}"}
    if not check_recharge_rate_limit(user_id):
        return {"ok": False, "error": f"Too many recharge attempts. Max {RECHARGE_RATE_LIMIT_PER_HOUR} per hour."}

    user_data = get_user(user_id)
    if user_data["balance_paisa"] + amount_paisa > MAX_WALLET_BALANCE_PAISA:
        return {"ok": False, "error": f"Would exceed max wallet balance of ₹{MAX_WALLET_BALANCE_PAISA / 100:.0f}"}
    if not UPIGATEWAY_API_KEY:
        return {"ok": False, "error": "Payment gateway is not configured."}

    client_txn_id = f"GMB-{user_id}-{uuid.uuid4().hex[:8].upper()}"
    log_order(client_txn_id, user_id, amount_paisa)
    payload = {
        "key": UPIGATEWAY_API_KEY,
        "client_txn_id": client_txn_id,
        "amount": f"{amount_rs:.2f}",
        "p_info": "GodMode Credits",
        "customer_name": f"User{user_id}",
        "customer_email": f"{user_id}@godmodebot.in",
        "customer_mobile": "9999999999",
        "redirect_url": f"{WEBHOOK_URL}/webhook/payment" if WEBHOOK_URL else "",
        "udf1": str(user_id),
    }
    try:
        resp = requests.post("https://api.upigateway.com/v1/create_order", json=payload, timeout=10)
        data = resp.json()
        if data.get("status"):
            return {"ok": True, "url": data["data"]["payment_url"], "txn_id": client_txn_id}
        return {"ok": False, "error": data.get("msg", "Unknown error")}
    except Exception as e:
        logger.error("Payment gateway error: %s", e)
        return {"ok": False, "error": "Payment service unavailable"}


@flask_app.route("/health", methods=["GET"])
def health():
    worker_healthy = state.is_worker_healthy()
    queue_size = state.video_queue.qsize() if state.video_queue else 0
    status_code = 200 if worker_healthy else 503
    return (
        jsonify(
            {
                "status": "ok" if worker_healthy else "degraded",
                "version": BOT_VERSION,
                "worker_healthy": worker_healthy,
                "queue_size": queue_size,
                "active_tasks": state.active_tasks,
            }
        ),
        status_code,
    )


@flask_app.route("/webhook/payment", methods=["POST"])
def payment_webhook():
    try:
        raw_body = request.get_data()
        signature = request.headers.get("X-Signature", "")
        if not verify_webhook_signature(raw_body, signature):
            logger.warning("Invalid webhook signature")
            return jsonify({"error": "invalid_signature"}), 401

        data = request.get_json(silent=True) or request.form.to_dict()
        logger.info("Payment webhook: %s", {k: v for k, v in data.items() if k != "signature"})

        status = str(data.get("status", "")).upper()
        tx_status = str(data.get("txStatus", "")).upper()
        client_txn_id = str(data.get("client_txn_id", data.get("clientTxnId", "")))
        gateway_ref = str(data.get("orderId", data.get("utr", data.get("gateway_ref", ""))))

        if status not in ("SUCCESS", "TRUE") and tx_status != "SUCCESS":
            return jsonify({"result": "ignored", "reason": "not_success"}), 200
        if not client_txn_id:
            return jsonify({"error": "missing_client_txn_id"}), 400

        order = confirm_order(client_txn_id, gateway_ref)
        if not order:
            logger.info("Order already processed or not found: %s", client_txn_id)
            return jsonify({"result": "already_processed"}), 200

        user_id = order["user_id"]
        amount_paisa = order["amount_paisa"]
        new_balance = credit_wallet(user_id, amount_paisa, note=f"recharge:{client_txn_id}")

        if main_loop and tg_app:
            asyncio.run_coroutine_threadsafe(
                notify_payment_success(user_id, amount_paisa, new_balance, client_txn_id), main_loop
            )
        return jsonify({"result": "credited"}), 200
    except Exception:
        logger.exception("Payment webhook error")
        return jsonify({"error": "internal_error"}), 500


async def notify_payment_success(user_id: int, amount_paisa: int, new_balance: int, txn_id: str) -> None:
    if not tg_app:
        return
    try:
        await tg_app.bot.send_message(
            chat_id=user_id,
            text=(
                "✅ *Payment Confirmed\\!*\n\n"
                f"💰 Credited: *{escape_md(rupees_str(amount_paisa))}*\n"
                f"👛 New Balance: *{escape_md(rupees_str(new_balance))}*\n"
                f"🔑 Ref: `{escape_md(txn_id)}`\n\n"
                "Send your video to start editing\\!"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        logger.error("Failed to notify user %s: %s", user_id, e)
    try:
        await tg_app.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=(
                "📋 *PAYMENT*\n"
                f"User: `{user_id}`\n"
                f"Amount: {escape_md(rupees_str(amount_paisa))}\n"
                f"Ref: `{escape_md(txn_id)}`"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    get_user(user.id, user.username or "")
    await update.message.reply_text(
        f"🎬 *Welcome to GodMode Video Bot, {escape_md(user.first_name)}\\!*\n\n"
        "I transform raw footage into *Hollywood\\-grade cinematic clips*\\.\n\n"
        "📌 *How it works:*\n"
        "1\\. Send your video \\(mp4/mov\\)\n"
        "2\\. Optionally attach up to 3 screenshots\n"
        "3\\. Choose your style\n"
        "4\\. Confirm and pay\n\n"
        "💰 *Pricing:* ₹29 up to 60s, ₹49 up to 2min\n"
        "🆓 *Free trial:* First 15 seconds free\\!\n\n"
        "💳 /recharge — Top up wallet\n"
        "👛 /balance — Check balance\n"
        "❓ /help — Features",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "🛠 *GodMode Features:*\n\n"
        "✂️ Auto jump\\-cuts & silence removal\n"
        "🤖 AI retake & stutter cleaner\n"
        "📝 Viral hook banner \\(3s\\)\n"
        "🎵 Mood\\-matched BGM with ducking\n"
        "💬 Kinetic captions \\+ punchword FX\n"
        "🖼 B\\-roll overlays \\(up to 3\\)\n"
        "🌐 Translation to English\n"
        "⚡ 1\\.1x speed optimization\n\n"
        f"💰 *Pricing:* ₹29 up to 60s, ₹49 up to 2min\n"
        f"🆓 *Free trial:* First {FREE_TRIAL_SECONDS}s free\\!",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    data = get_user(user.id)
    trial = "✅ Used" if data["free_trial_used"] else "🆓 Available"
    await update.message.reply_text(
        f"👛 *Your Wallet*\n\n"
        f"💰 Balance: *{escape_md(rupees_str(data['balance_paisa']))}*\n"
        f"🎁 Free Trial: {trial}\n\n"
        "Use /recharge to top up\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_recharge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    keyboard = [
        [InlineKeyboardButton("₹49", callback_data="pay_49"), InlineKeyboardButton("₹99", callback_data="pay_99"), InlineKeyboardButton("₹199", callback_data="pay_199")],
        [InlineKeyboardButton("₹499", callback_data="pay_499"), InlineKeyboardButton("₹999", callback_data="pay_999")],
        [InlineKeyboardButton("₹2999", callback_data="pay_2999"), InlineKeyboardButton("₹4999", callback_data="pay_4999")],
        [InlineKeyboardButton("₹9999 (Max)", callback_data="pay_9999")],
    ]
    await update.message.reply_text(
        f"💳 *Choose Recharge Amount:*\n\n"
        f"Max single recharge: ₹{MAX_SINGLE_RECHARGE_PAISA / 100:.0f}\n"
        f"Max wallet balance: ₹{MAX_WALLET_BALANCE_PAISA / 100:.0f}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id != ADMIN_USER_ID or not update.message:
        return
    s = get_stats()
    queue_size = state.video_queue.qsize() if state.video_queue else 0
    worker_text = "Healthy" if state.is_worker_healthy() else "Unhealthy"
    await update.message.reply_text(
        f"📊 *System Stats*\n\n"
        f"👥 Users: *{s['total_users']}*\n"
        f"💰 Revenue: *{escape_md(rupees_str(s['total_revenue_paisa']))}*\n"
        f"🏦 Wallets: *{escape_md(rupees_str(s['total_wallet_paisa']))}*\n"
        f"⚙️ Active: *{state.active_tasks}*\n"
        f"📋 Queue: *{queue_size}*\n"
        f"🔄 Pending: *{s['pending_jobs']}*\n"
        f"❤️ Worker: *{escape_md(worker_text)}*",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_setbalance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id != ADMIN_USER_ID or not update.message:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /setbalance <user_id> <amount_rupees>")
        return
    try:
        target_id = int(args[0])
        amount_paisa = int(round(float(args[1]) * 100))
    except ValueError:
        await update.message.reply_text("❌ Invalid values")
        return
    if amount_paisa < 0:
        await update.message.reply_text("❌ Balance cannot be negative")
        return
    if amount_paisa > MAX_WALLET_BALANCE_PAISA:
        await update.message.reply_text(f"❌ Max balance is ₹{MAX_WALLET_BALANCE_PAISA / 100:.0f}")
        return
    set_balance_paisa(target_id, amount_paisa, note=f"admin_set_by_{ADMIN_USER_ID}")
    await update.message.reply_text(
        f"✅ Balance for `{target_id}` set to *{escape_md(rupees_str(amount_paisa))}*",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"🔔 *Balance Updated*\n\nNew balance: *{escape_md(rupees_str(amount_paisa))}*",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACKS / KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════════════


def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🇬🇧 English", callback_data="lang_English"), InlineKeyboardButton("🇮🇳 Hindi→EN", callback_data="lang_Hindi_to_English")],
            [InlineKeyboardButton("🌐 Auto-Detect", callback_data="lang_Auto")],
        ]
    )


def placement_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬆️ Top", callback_data="place_top"), InlineKeyboardButton("⬛ Center", callback_data="place_center"), InlineKeyboardButton("⬇️ Bottom", callback_data="place_bottom")]]
    )


def preset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🦁 MrBeast", callback_data="preset_mrbeast"), InlineKeyboardButton("💎 Luxury", callback_data="preset_luxury")],
            [InlineKeyboardButton("⚡ Tech/Crypto", callback_data="preset_tech"), InlineKeyboardButton("🎯 Motivational", callback_data="preset_motivational")],
        ]
    )


async def show_confirmation(query, user_id: int, session: dict) -> None:
    user_data = get_user(user_id)
    text = (
        "📋 *Confirm Render*\n\n"
        f"🌐 Language: {escape_md(session.get('lang', 'English'))}\n"
        f"📍 Placement: {escape_md(session.get('placement', 'bottom'))}\n"
        f"🎨 Style: {escape_md(session.get('preset', 'mrbeast'))}\n"
        f"🖼 Screenshots: {len(session.get('screenshots', []))}\n\n"
        f"💰 *Pricing:* ₹29 up to 60s, ₹49 up to 2min\n"
        f"👛 Your Balance: *{escape_md(rupees_str(user_data['balance_paisa']))}*\n"
    )
    if FREE_TRIAL_SECONDS > 0 and not user_data["free_trial_used"]:
        text += f"🆓 Free trial available \\(first {FREE_TRIAL_SECONDS}s\\)\n"
    text += "\n⚠️ You will be charged based on final video duration\\."
    keyboard = [[InlineKeyboardButton("✅ Confirm & Render", callback_data="confirm_render"), InlineKeyboardButton("❌ Cancel", callback_data="cancel_render")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    user_id = query.from_user.id

    pay_amounts = {
        "pay_49": 49,
        "pay_99": 99,
        "pay_199": 199,
        "pay_499": 499,
        "pay_999": 999,
        "pay_2999": 2999,
        "pay_4999": 4999,
        "pay_9999": 9999,
    }
    if data in pay_amounts:
        amount = pay_amounts[data]
        result = create_upi_order(user_id, amount)
        if result["ok"]:
            await query.edit_message_text(
                f"🔗 Click below to pay *₹{amount}*\n\nYour wallet will be credited automatically\\.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay Now", url=result["url"])]]) ,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await query.edit_message_text(f"❌ {result['error']}")
        return

    if data.startswith("lang_"):
        lang = data.replace("lang_", "").replace("_", " ")
        session = load_user_session(user_id) or {}
        session["lang"] = lang
        save_user_session(user_id, session)
        await query.edit_message_text(
            f"✅ Language: *{escape_md(lang)}*\n\nChoose caption placement:",
            reply_markup=placement_keyboard(),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if data.startswith("place_"):
        placement = data.replace("place_", "")
        session = load_user_session(user_id) or {}
        session["placement"] = placement
        save_user_session(user_id, session)
        await query.edit_message_text(
            f"✅ Placement: *{escape_md(placement)}*\n\nChoose style:",
            reply_markup=preset_keyboard(),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if data.startswith("preset_"):
        preset = data.replace("preset_", "")
        session = load_user_session(user_id) or {}
        session["preset"] = preset
        save_user_session(user_id, session)
        await show_confirmation(query, user_id, session)
        return

    if data == "confirm_render":
        await query.edit_message_text("⏳ Adding to queue...")
        await enqueue_job(update, context, user_id)
        return

    if data == "cancel_render":
        clear_user_session(user_id)
        await query.edit_message_text("❌ Cancelled. Send a new video to start over.")

# ═══════════════════════════════════════════════════════════════════════════════
# MEDIA HANDLERS / WORKER
# ═══════════════════════════════════════════════════════════════════════════════


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return
    video = msg.video or msg.document
    if not video:
        await msg.reply_text("❌ Please send a video file.")
        return
    if state.has_active_job(user.id):
        await msg.reply_text("⚠️ You already have a video being processed. Please wait for it to complete.")
        return
    size_mb = (video.file_size or 0) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        await msg.reply_text(f"❌ File too large. Max {MAX_FILE_SIZE_MB}MB.")
        return
    mime = getattr(video, "mime_type", "") or ""
    if mime and mime not in ALLOWED_VIDEO_MIMES:
        await msg.reply_text("❌ Unsupported video format. Use MP4, MOV, AVI, MKV, or WebM.")
        return
    save_user_session(user.id, {"video_file_id": video.file_id, "screenshots": []})
    await msg.reply_text(
        "🎬 *Video received\\!*\n\n"
        "You can send up to 3 screenshots for B\\-roll \\(optional\\)\\.\n\n"
        "🌐 *Select output language:*",
        reply_markup=lang_keyboard(),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return
    session = load_user_session(user.id)
    if not session or "video_file_id" not in session:
        await msg.reply_text("⚠️ Send your video first, then screenshots.")
        return
    shots = session.get("screenshots", [])
    if len(shots) >= MAX_SCREENSHOTS:
        await msg.reply_text(f"⚠️ Max {MAX_SCREENSHOTS} screenshots already added.")
        return
    if not msg.photo:
        return
    photo = msg.photo[-1]
    shots.append(photo.file_id)
    session["screenshots"] = shots
    save_user_session(user.id, session)
    await msg.reply_text(f"🖼 Screenshot {len(shots)}/{MAX_SCREENSHOTS} added\\.", parse_mode=ParseMode.MARKDOWN_V2)


async def enqueue_job(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    if state.video_queue is None:
        await context.bot.send_message(user_id, "⚠️ Worker is not ready. Please try again in a moment.")
        return
    session = load_user_session(user_id)
    if not session or "video_file_id" not in session:
        await context.bot.send_message(user_id, "❌ No video found. Please send your video first.")
        return
    if state.video_queue.qsize() >= MAX_QUEUE_SIZE:
        await context.bot.send_message(user_id, "⚠️ Queue is full. Please try again in a few minutes.")
        return
    if not state.set_user_job(user_id, "pending"):
        await context.bot.send_message(user_id, "⚠️ You already have a video being processed.")
        return

    job_id = uuid.uuid4().hex[:12]
    try:
        create_pending_job(
            job_id,
            user_id,
            session["video_file_id"],
            session.get("screenshots", []),
            session.get("lang", "English"),
            session.get("placement", "bottom"),
            session.get("preset", "mrbeast"),
        )
        state.clear_user_job(user_id)
        state.set_user_job(user_id, job_id)
        pos = state.video_queue.qsize() + 1
        await context.bot.send_message(
            user_id,
            f"📋 *Queue Position: \\#{pos}*\n\nI'll notify you when processing starts\\!",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await state.video_queue.put(
            {
                "job_id": job_id,
                "user_id": user_id,
                "video_file_id": session["video_file_id"],
                "screenshots": session.get("screenshots", []),
                "lang": session.get("lang", "English"),
                "placement": session.get("placement", "bottom"),
                "preset": session.get("preset", "mrbeast"),
                "bot": context.bot,
            }
        )
        clear_user_session(user_id)
    except Exception:
        state.clear_user_job(user_id)
        raise


async def download_file(bot, file_id: str, dest: Path, max_retries: int = 3) -> Path:
    for attempt in range(max_retries):
        try:
            tg_file = await bot.get_file(file_id)
            await tg_file.download_to_drive(str(dest))
            return dest
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            logger.warning("Download attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(2**attempt)
    return dest


async def video_worker() -> None:
    logger.info("Video worker started")
    while True:
        state.update_heartbeat()
        if state.shutdown_event and state.shutdown_event.is_set():
            logger.info("Worker received shutdown signal")
            break
        try:
            try:
                assert state.video_queue is not None
                job = await asyncio.wait_for(state.video_queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

            user_id = job["user_id"]
            job_id = job["job_id"]
            bot = job["bot"]
            state.increment_tasks()
            update_job_status(job_id, "PROCESSING")
            input_path = DOWNLOADS_DIR / f"{job_id}_input.mp4"
            output_path = DOWNLOADS_DIR / f"{job_id}_output.mp4"
            shot_paths: list[str] = []
            charged_paisa = 0

            try:
                await bot.send_message(user_id, "⚙️ *Processing started\\!* This may take a few minutes…", parse_mode=ParseMode.MARKDOWN_V2)

                logger.info("[%s] Downloading video", job_id)
                await download_file(bot, job["video_file_id"], input_path)

                loop = asyncio.get_running_loop()
                raw_duration = await loop.run_in_executor(executor, ffprobe_duration, str(input_path))
                if raw_duration < MIN_VIDEO_DURATION_SEC:
                    raise ValueError(f"Video too short. Minimum {MIN_VIDEO_DURATION_SEC}s.")
                if raw_duration > MAX_VIDEO_DURATION_SEC:
                    raise ValueError(f"Video too long. Maximum {MAX_VIDEO_DURATION_SEC / 60:.0f} minutes.")

                for i, fid in enumerate(job.get("screenshots", [])):
                    sp = DOWNLOADS_DIR / f"{job_id}_shot{i}.jpg"
                    await download_file(bot, fid, sp)
                    shot_paths.append(str(sp))

                user_data = get_user(user_id)
                estimated_cost = compute_cost_paisa(raw_duration)
                use_free_trial = (not user_data["free_trial_used"]) and raw_duration <= FREE_TRIAL_SECONDS
                if use_free_trial:
                    mark_free_trial_used(user_id)
                elif user_data["balance_paisa"] < estimated_cost:
                    shortage = estimated_cost - user_data["balance_paisa"]
                    raise ValueError(
                        f"Insufficient balance. Need {rupees_str(estimated_cost)}, "
                        f"have {rupees_str(user_data['balance_paisa'])}. "
                        f"Top up at least {rupees_str(shortage)}."
                    )

                final_duration = await loop.run_in_executor(
                    executor,
                    render_video,
                    str(input_path),
                    str(output_path),
                    shot_paths,
                    job.get("preset", "mrbeast"),
                    job.get("placement", "bottom"),
                    job.get("lang", "English"),
                    job_id,
                )

                if not use_free_trial:
                    final_cost = compute_cost_paisa(final_duration)
                    success, _new_balance = debit_wallet(user_id, final_cost, note=f"render:{job_id}", job_id=job_id)
                    if not success:
                        raise ValueError("Balance changed during render. Please try again.")
                    charged_paisa = final_cost

                await bot.send_message(user_id, "✅ *Complete\\!* Uploading…", parse_mode=ParseMode.MARKDOWN_V2)
                with open(str(output_path), "rb") as vf:
                    cost_text = rupees_str(charged_paisa) if charged_paisa else "FREE TRIAL"
                    await bot.send_video(
                        chat_id=user_id,
                        video=vf,
                        caption=(
                            "🎬 *Your GodMode Video*\n"
                            f"⏱ Duration: {final_duration:.1f}s\n"
                            f"💰 Cost: {escape_md(cost_text)}"
                        ),
                        parse_mode=ParseMode.MARKDOWN_V2,
                        supports_streaming=True,
                    )

                update_job_status(job_id, "COMPLETED")
                try:
                    await bot.send_message(
                        ADMIN_USER_ID,
                        f"📹 *Render Complete*\nUser: `{user_id}`\nJob: `{escape_md(job_id)}`\nDuration: {final_duration:.1f}s\nCharged: {escape_md(rupees_str(charged_paisa))}",
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                except Exception:
                    pass
            except Exception as e:
                error_msg = str(e)[:200]
                logger.exception("[%s] Render failed", job_id)
                if charged_paisa > 0:
                    refund_wallet(user_id, charged_paisa, note=f"refund:{job_id}", job_id=job_id)
                    error_msg += f" (₹{charged_paisa / 100:.2f} refunded)"
                update_job_status(job_id, "FAILED", error_message=error_msg)
                try:
                    await bot.send_message(
                        user_id,
                        f"❌ *Render Failed*\n\n{escape_md(error_msg)}\n\nYou have not been charged\\.",
                        parse_mode=ParseMode.MARKDOWN_V2,
                    )
                except Exception:
                    pass
            finally:
                state.clear_user_job(user_id)
                state.decrement_tasks()
                for p in [input_path, output_path] + [Path(s) for s in shot_paths]:
                    try:
                        if p.exists():
                            p.unlink()
                    except Exception:
                        pass
                state.video_queue.task_done()
        except Exception:
            logger.exception("Worker loop error")
            await asyncio.sleep(1)

# ═══════════════════════════════════════════════════════════════════════════════
# RUNNER / SHUTDOWN
# ═══════════════════════════════════════════════════════════════════════════════


def run_flask() -> None:
    try:
        from waitress import serve

        logger.info("Starting Flask with waitress on port %d", FLASK_PORT)
        serve(flask_app, host="0.0.0.0", port=FLASK_PORT)
    except ImportError:
        logger.info("Starting Flask dev server on port %d", FLASK_PORT)
        flask_app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)


def cleanup_temp_files() -> None:
    try:
        for f in DOWNLOADS_DIR.glob("*"):
            if f.is_file():
                f.unlink()
        logger.info("Cleaned up temp files")
    except Exception as e:
        logger.warning("Temp cleanup error: %s", e)


def close_db_connections() -> None:
    try:
        conn = getattr(_db_local, "connection", None)
        if conn:
            conn.close()
            _db_local.connection = None
    except Exception:
        pass


def main() -> None:
    global tg_app, main_loop

    init_db()
    cleanup_temp_files()
    atexit.register(cleanup_temp_files)
    atexit.register(close_db_connections)

    for sub in ("pop_sounds", "backgrounds", "transition_sfx"):
        (ASSETS_DIR / sub).mkdir(parents=True, exist_ok=True)

    async def post_init(app: Application) -> None:
        global main_loop
        main_loop = asyncio.get_running_loop()
        state.video_queue = asyncio.Queue()
        state.shutdown_event = asyncio.Event()
        asyncio.create_task(video_worker())
        pending = get_pending_jobs()
        if pending:
            logger.info("Recovering %d pending jobs", len(pending))
            for job in pending:
                state.set_user_job(job["user_id"], job["job_id"])
                await state.video_queue.put({**job, "bot": app.bot})
        logger.info("GodMode Bot v%s started", BOT_VERSION)

    tg_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_help))
    tg_app.add_handler(CommandHandler("balance", cmd_balance))
    tg_app.add_handler(CommandHandler("recharge", cmd_recharge))
    tg_app.add_handler(CommandHandler("stats", cmd_stats))
    tg_app.add_handler(CommandHandler("setbalance", cmd_setbalance))
    tg_app.add_handler(CallbackQueryHandler(handle_callback))
    tg_app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    tg_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    def handle_shutdown(signum, frame):
        logger.info("Shutdown signal received")
        if state.shutdown_event:
            try:
                main_loop.call_soon_threadsafe(state.shutdown_event.set) if main_loop else state.shutdown_event.set()
            except Exception:
                pass

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    logger.info("Starting GodMode Bot v%s", BOT_VERSION)
    tg_app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
