import os
from pathlib import Path


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    return value if value != "" else default


LIBRARY = Path(env("LIBRARY_DIR", "/library"))
CONFIG = Path(env("CONFIG_DIR", "/config"))
DATA = Path(env("DATA_DIR", "/data"))
ENGINES_BIN = Path(env("ENGINES_BIN", "/opt/engines/bin"))
ENGINES_REQUIREMENTS = env("ENGINES_REQUIREMENTS", "/tmp/engines.txt")
WEB = Path(__file__).resolve().parent.parent / "web"

COOKIES_DIR = CONFIG / "cookies"
TMP = DATA / "tmp"
THUMBS = DATA / "thumbs"

USERNAME = env("APP_USERNAME", "admin")
PASSWORD_HASH = env("APP_PASSWORD_HASH")
SESSION_DAYS = float(env("SESSION_DAYS", "7"))
LOGIN_MAX_ATTEMPTS = int(env("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCK_MINUTES = float(env("LOGIN_LOCK_MINUTES", "15"))
COOKIE_SECURE = env("COOKIE_SECURE", "auto").lower()

MAX_CONCURRENT = max(1, int(env("MAX_CONCURRENT", "2")))
