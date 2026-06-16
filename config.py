from __future__ import annotations
import os
from pathlib import Path

BOT_VERSION = "3.0.0-ffmpeg"
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "123456789"))
AICREDITS_API_KEY = os.environ["AICREDITS_API_KEY"]
AICREDITS_BASE_URL = os.environ.get("AICREDITS_BASE_URL", "https://aicredits.in/v1")
AI_DIRECTOR_MODEL = os.environ.get("AI_DIRECTOR_MODEL", "gpt-4o")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")
UPIGATEWAY_API_KEY = os.environ.get("UPIGATEWAY_API_KEY", "")
UPIGATEWAY_SECRET = os.environ.get("UPIGATEWAY_SECRET", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "8080"))
FONT_PATH = os.environ.get("FONT_PATH", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")
WORKER_COUNT = max(1, int(os.environ.get("WORKER_COUNT", "1")))

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DATA_DIR = Path("/data") if Path("/data").exists() else BASE_DIR / "data"
ASSETS_DIR = BASE_DIR / "assets"
DOWNLOADS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = str(DATA_DIR / "godmode_wallet_v3.db")

MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "150"))
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "100"))
MIN_VIDEO_DURATION_SEC = 1.0
MAX_VIDEO_DURATION_SEC = 120.0
FREE_SAMPLE_SECONDS = 15.0

PRICE_MINI_AUDIT = 1900
PRICE_FULL_ROAST = 3900
PRICE_EDIT_60 = 4900
PRICE_EDIT_120 = 7900
MIN_RECHARGE_PAISA = 4900
MAX_SINGLE_RECHARGE_PAISA = 1_000_000
MAX_WALLET_BALANCE_PAISA = 5_000_000
RECHARGE_RATE_LIMIT_PER_HOUR = 5

ALLOWED_VIDEO_MIMES = {
    "video/mp4", "video/quicktime", "video/x-msvideo", "video/webm", "video/x-matroska"
}
PLATFORMS = ["Instagram Reels", "YouTube Shorts", "LinkedIn", "Ad Creative", "General"]
NICHES = ["Business", "Fitness", "Finance", "Tech/AI", "Motivation", "Real Estate", "Education", "Podcast", "Product Demo", "Other"]
GOALS = ["More views", "More followers", "More leads", "Sell product/service", "Educate audience", "Build personal brand"]
STYLES = ["MrBeast", "Luxury", "Tech", "Motivational", "Hormozi Clean", "Podcast Pro", "Ad UGC", "Minimal Premium"]
