"""Eski veri dosyalarının konumu; testler ayrı veri dizini kullanabilir."""
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
def default_data_dir():
    if os.name == "nt":
        parent = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        parent = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return parent / "InstagramFollowManager"


BASE_DIR = Path(os.environ.get("INSTAGRAMTAKIP_DATA_DIR") or default_data_dir()).expanduser().resolve()
CONFIG_FILE = BASE_DIR / "config.json"
IDSIFRE_PATH = BASE_DIR / "idsifre.txt"
IG_URL = "https://www.instagram.com"
TAKIPLIST_PATH = BASE_DIR / "Takiplist.txt"
KARALIST_PATH = BASE_DIR / "karalist.txt"
KULLANICI_PATH = BASE_DIR / "kullanıcı.txt"
STATS_FILE = BASE_DIR / "stats.json"
HISTORY_FILE = BASE_DIR / "daily_history.json"
PROFILE_DIR = BASE_DIR / "ig_profile"
GONDERILER_PATH = BASE_DIR / "gönderiler.txt"
LOG_FILE = BASE_DIR / "instagramtakip.log"
STATUS_DB_PATH = BASE_DIR / "kullanici_durumlari.sqlite3"
