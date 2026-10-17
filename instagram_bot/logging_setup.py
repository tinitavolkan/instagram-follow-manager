"""Boyutu sınırlı, UTF-8 işlem ve hata kayıtları."""
import logging
from logging.handlers import RotatingFileHandler
from .paths import LOG_FILE

def create_app_logger():
    logger = logging.getLogger("instagramtakip")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        try:
            handler = RotatingFileHandler(
                LOG_FILE,
                maxBytes=2_000_000, backupCount=3, encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
        except OSError:
            # Kayıt dosyası açılamasa da arayüz ve takip görevi çalışabilsin.
            logger.addHandler(logging.NullHandler())
    return logger

APP_LOG = create_app_logger()
