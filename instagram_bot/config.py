"""Kalıcı uygulama ayarları ve mevcut hız ölçeklemesi."""
import json
import random
from typing import Any, Dict
from .paths import CONFIG_FILE
from .storage import atomic_write_text

DEFAULT_CONFIG = {
    "DAILY_LIMIT": 80,  # Varsayılan limit 80'e çıkarıldı
    "HEADLESS_MODE": False,
    "GONDERIDEN_EXCLUDE_FOLLOWING": True,
}

def deep_merge_config(cfg: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Eski config.json'larda eksik alanları DEFAULT_CONFIG ile tamamlar (basit migration)."""
    for k, v in defaults.items():
        if k not in cfg or cfg[k] is None:
            cfg[k] = v.copy() if isinstance(v, dict) else v
            continue
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            deep_merge_config(cfg[k], v)
    return cfg

def load_config() -> Dict[str, Any]:
    """Ayarları bellekte tamamlar; dosya yalnızca açıkça kaydedilince değişir."""
    cfg: Dict[str, Any] = {}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    cfg = loaded
        except (json.JSONDecodeError, IOError):
            cfg = {}

    # Eksik alanları bellekte tamamla; eski/özel alanları ve dosyayı koru.
    return deep_merge_config(cfg, DEFAULT_CONFIG.copy())

def save_config(config: Dict[str, Any]):
    payload = json.dumps(config, indent=4, ensure_ascii=False)
    atomic_write_text(CONFIG_FILE, payload, encoding="utf-8")

CONFIG = load_config()
DAILY_LIMIT = CONFIG.get("DAILY_LIMIT", 80)
_BASE_DAILY = 80

def _speed_scale() -> float:
    """
    DAILY_LIMIT'e göre hız ölçeği döner.
    - DAILY_LIMIT=80  → scale=1.0  (orijinal gecikmeler)
    - DAILY_LIMIT=160 → scale=0.5  (yarı gecikme)
    - DAILY_LIMIT=500 → scale=0.16 (çok kısa gecikme)
    Minimum 0.08 ile sınırlandırılmıştır (mutlak alt sınır).
    """
    limit = CONFIG.get("DAILY_LIMIT", _BASE_DAILY)
    return max(0.08, _BASE_DAILY / limit)

def _scaled(lo: float, hi: float, min_lo: float = 3.0, min_hi: float = 6.0) -> float:
    """Bir bekleme aralığını scale ile çarpar, minimum değerlerin altına düşmez."""
    s = _speed_scale()
    return random.uniform(max(min_lo, lo * s), max(min_hi, hi * s))
