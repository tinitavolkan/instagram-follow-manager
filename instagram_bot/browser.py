"""Worker iş parçacığının sahip olduğu tarayıcı oturumu ve kısıtlama molaları."""
import random
import re
import threading
import time
from datetime import datetime
from typing import Callable
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from .config import CONFIG
from .dashboard import reported_wait, reporting_note, wait_description
from .paths import IG_URL, PROFILE_DIR
from .timing import stoppable_wait
import os

_play = None
_context = None
page = None
RATE_LIMIT_HITS = 0
rate_limit_lock_until = 0



def _log(cb: Callable[[str], None] | None, msg: str):
    if cb:
        cb(msg)
    else:
        # Loga gerçek zamanlı tarih-saat ekle
        try:
            if not re.match(r"^\[\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}\]", msg):
                msg = f"[{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}] {msg}"
        except Exception:
            pass
        print(msg)

def human_like_click(page, locator):
    """Bir locator'a insansı bir tıklama yapar."""
    try:
        box = locator.bounding_box()
        if not box: return False

        target_x = box['x'] + box['width'] / 2
        target_y = box['y'] + box['height'] / 2

        start_offset_x = random.uniform(-150, 150)
        start_offset_y = random.uniform(-150, 150)
        start_x = target_x + start_offset_x
        start_y = target_y + start_offset_y

        page.mouse.move(start_x, start_y)
        time.sleep(random.uniform(0.2, 0.5))

        steps = random.randint(20, 30)
        for i in range(steps + 1):
            t = i / steps
            speed_factor = 1.0 - abs(t - 0.5) * 0.7

            mid_x = (start_x + target_x) / 2 + random.uniform(-100, 100)
            mid_y = (start_y + target_y) / 2 - random.randint(50, 150)

            x = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * mid_x + t ** 2 * target_x
            y = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * mid_y + t ** 2 * target_y

            page.mouse.move(x, y)
            time.sleep(random.uniform(0.01, 0.04) * speed_factor)

        time.sleep(random.uniform(0.1, 0.3))
        locator.click()
        return True
    except Exception as e:
        print(f"human_like_click hatası: {e}")
        return False

def idle_wait(cb: Callable[[str], None] | None = None):
    """Tarayıcıyı Instagram ana sayfasında boşta bekletir."""
    msg = "🌙 Bekleme modunda (Instagram ana sayfada)."
    if cb: cb(msg)
    else: print(msg)

def perform_safe_wait(duration: float, cb=None, return_to_insta: bool = False, stop_event: threading.Event | None = None) -> bool:
    """
    Ağ'a gitmeden, stop edilebilir bekleme yapar.
    - stop_event verilirse stop geldiği an bekleme kesilir.
    - return_to_insta=True ise bekleme bitince (stop edilmediyse) IG ana sayfaya döner.
    - Stop ile kesilirse True, normal tamamlanırsa False döner.
    """
    global page
    if duration <= 0:
        return False

    return_error = None
    with reported_wait(duration, stop_event=stop_event):
        stopped = stoppable_wait(stop_event, duration, tick=0.5)
        if stopped:
            return True

        if return_to_insta and page is not None:
            try:
                _log(cb, "⏳ Bekleme süresi doldu, Instagram ana sayfaya dönülüyor...")
                page.goto(IG_URL, timeout=60000)
            except Exception as e:
                return_error = e
                _log(cb, f"⚠️ Instagram'a dönüş hatası: {e}")

    if return_error is not None:
        reporting_note("Mola bitti, ancak Instagram ana sayfasına dönüş doğrulanamadı.",
                       "Sonraki işlemde bağlantı ve profil durumu yeniden kontrol edilecek.")

    return False

def is_rate_limited(page) -> bool:
    try:
        return (
            page.locator("text=Daha Sonra Tekrar Dene").is_visible(timeout=1500) or
            page.locator("text=Try Again Later").is_visible(timeout=1500)
        )
    except: return False

def cooldown_wait(cb=None, stop_event: threading.Event | None = None):
    global RATE_LIMIT_HITS, rate_limit_lock_until
    RATE_LIMIT_HITS += 1
    base = 15
    minutes = min(base * (1.7 ** (RATE_LIMIT_HITS - 1)), 1440)
    idle_wait(cb)
    msg_start = f"⏳ RATE LIMIT #{RATE_LIMIT_HITS} – COOLDOWN: {minutes:.1f} dk"
    if cb: cb(msg_start)
    else: print(msg_start)

    # --- Safe Wait kullanarak example.com'a git ---
    with wait_description("Instagram kısıtlama uyarısı sonrası bekleniyor.",
                          "Bu mola sonrası mevcut uzun dinlenme uygulanacak."):
        if perform_safe_wait(minutes * 60, cb, stop_event=stop_event):
            _log(cb, "⏹️ Cooldown kullanıcı tarafından durduruldu.")
            return True

    # --- Cooldown sonrası Ekstra Uzun Dinlenme ---
    extra_rest = random.uniform(120 * 60, 180 * 60)
    if cb: cb(f"🛑 Instagram Riski Algılandı. **GÜVENLİK MOLASI:** {int(extra_rest / 60)} dakika bekleniyor.")

    with wait_description("Instagram kısıtlama uyarısı sonrası uzun dinlenme.",
                          "Mola sonrası mevcut güvenlik kilidi uygulanacak."):
        if perform_safe_wait(extra_rest, cb, return_to_insta=True, stop_event=stop_event):
            _log(cb, "⏹️ Güvenlik molası kullanıcı tarafından durduruldu.")
            return True

    RATE_LIMIT_HITS = 0
    _log(cb, "✅ Cooldown tamamlandı. Temiz başlangıç yapılıyor.")

    rate_limit_lock_until = time.time() + random.uniform(60 * 60, 90 * 60)
    return False

def run_bot(cb=None):
    """Legacy callers must use the shared, manually authenticated session."""
    raise RuntimeError("Önce arayüzden Instagram oturumunu açıp elle giriş yapın.")


def close_bot():
    """Tarayıcı ve Playwright kaynaklarını güvenli şekilde temizler."""
    global _play, _context, page
    try:
        if _context: _context.close()
    except Exception: pass
    try:
        if _play: _play.stop()
    except Exception: pass
    _play = _context = page = None
