"""Target-profile follow actions; persistence belongs to the task worker.

An absent/slow/changing profile is retryable. Only a confirmed relationship or
an invalid username is terminal; page-wide text is never relationship evidence.
"""

from dataclasses import dataclass
import logging
import random
import re
from threading import Event
from urllib.parse import unquote, urlparse

from playwright.sync_api import TimeoutError as PWTimeout

from . import browser
from .config import _scaled
from .paths import IG_URL
from .timing import stoppable_wait


@dataclass(frozen=True)
class FollowResult:
    status: str
    reason: str
    detail: str = ""
    action_submitted: bool = False


_LABELS = {
    "follow": "follow",
    "follow back": "follow",
    "takip et": "follow",
    "sen de takip et": "follow",
    "sen de onu takip et": "follow",
    "following": "following",
    "takiptesin": "following",
    "requested": "requested",
    "istek gönderildi": "requested",
}


def _normalized(text: str) -> str:
    # Unicode casefold turns Turkish capital İ into i + combining dot.
    return " ".join(text.casefold().replace("\u0307", "").split())


def _stopped(stop_event: Event | None) -> bool:
    return stop_event is not None and stop_event.is_set()


def _log(cb, message: str) -> None:
    if cb is not None:
        cb(message)
    else:
        logging.getLogger("instagramtakip").info(message)


def _target_url_matches(page, username: str) -> bool:
    parsed = urlparse(page.url)
    target = urlparse(IG_URL)
    return (
        parsed.hostname in {target.hostname, "instagram.com"}
        and unquote(parsed.path).strip("/").casefold() == username.casefold()
    )


def _visible_items(locator):
    return [locator.nth(i) for i in range(locator.count())
            if locator.nth(i).is_visible()]


def _profile_control(page, username: str):
    """Return (relationship, locator, error) using only the target header.

    Header-less/redesigned layouts deliberately return a retryable error. A
    global header is usable only when it contains the target username heading.
    Multiple relationship controls are ambiguous, including recommendation
    cards inadvertently embedded in the profile header.
    """
    if not _target_url_matches(page, username):
        return None, None, "profile_redirected"
    headers = _visible_items(page.locator("main header"))
    fallback = not headers
    if fallback:
        headers = _visible_items(page.locator("header"))
    if len(headers) != 1:
        return None, None, "profile_header_ambiguous" if headers else "profile_header_missing"
    header = headers[0]
    headings = _visible_items(header.locator("h1, h2, [role='heading']"))
    matches_username = any(
        _normalized(heading.inner_text()) == _normalized(username)
        for heading in headings
    )
    if (headings or fallback) and not matches_username:
        return None, None, "profile_identity_unconfirmed"

    controls = []
    for button in _visible_items(header.locator("button, [role='button']")):
        # Navigation, dialogs and recommendation cards do not describe the
        # profile relationship, even when nested inside a header.
        if button.evaluate("el => !!el.closest('nav, aside, article, [role=dialog]')"):
            continue
        label = button.get_attribute("aria-label") or button.inner_text()
        relationship = _LABELS.get(_normalized(label))
        if relationship is not None:
            controls.append((relationship, button))
    if len(controls) != 1:
        return None, None, "profile_control_ambiguous" if controls else "profile_control_missing"
    return controls[0][0], controls[0][1], None


def follow_user(username: str, cb=None, stop_event: Event | None = None) -> FollowResult:
    """Attempt one user and return a reasoned result without editing any lists."""
    clean_username = username.strip()

    def result(status, reason, detail="", submitted=False):
        return FollowResult(status, reason, detail, submitted)

    if _stopped(stop_event):
        return result("cancelled", "stopped_before_action")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", clean_username):
        _log(cb, f"⚠ {clean_username} – geçersiz kullanıcı adı biçimi.")
        return result("excluded", "invalid_username")

    try:
        if browser.page is None:
            _log(cb, "⚠ Oturum kapalıydı – tekrar giriş yapılıyor…")
            browser.run_bot(cb)
        page = browser.page
        if page is None:
            return result("retryable_error", "session_unavailable")
        if _stopped(stop_event):
            return result("cancelled", "stopped_before_action")
        if browser.is_rate_limited(page):
            _log(cb, "🚫 Kısıtlama algılandı – mevcut bekleme süresi uygulanıyor.")
            browser.cooldown_wait(cb, stop_event=stop_event)
            return result("cancelled" if _stopped(stop_event) else "retryable_error", "rate_limited")
    except Exception as exc:
        return result("retryable_error", "session_error", str(exc))

    _log(cb, f"► {clean_username} profiline gidiliyor…")
    try:
        page.goto(f"{IG_URL}/{clean_username}/", timeout=60_000, wait_until="load")
        if _stopped(stop_event):
            return result("cancelled", "stopped_before_action")
        if not _target_url_matches(page, clean_username):
            return result("retryable_error", "profile_redirected")
        page.wait_for_selector("main header, header", timeout=15_000)
        if stoppable_wait(stop_event, _scaled(1.5, 3.0, min_lo=0.5, min_hi=1.0)):
            return result("cancelled", "stopped_before_action")
        page.mouse.wheel(0, random.randint(200, 500))
        if stoppable_wait(stop_event, _scaled(2.0, 4.0, min_lo=0.5, min_hi=1.5)):
            return result("cancelled", "stopped_before_action")
    except PWTimeout as exc:
        _log(cb, f"✗ {clean_username} – profil zamanında yüklenemedi; yeniden denemek için listede tutuluyor.")
        return result("retryable_error", "profile_timeout", str(exc))
    except Exception as exc:
        return result("retryable_error", "profile_navigation_error", str(exc))

    try:
        relationship, button, error = _profile_control(page, clean_username)
    except Exception as exc:
        return result("retryable_error", "profile_control_error", str(exc))
    if error:
        _log(cb, f"✗ {clean_username} – hedef profilin takip düğmesi doğrulanamadı ({error}); listede tutuluyor.")
        return result("retryable_error", error)
    if _stopped(stop_event):
        return result("cancelled", "stopped_before_action")
    if relationship == "following":
        _log(cb, f"✓ {clean_username} zaten takip ediliyor (profil düğmesinde doğrulandı).")
        return result("already_following", "profile_following_confirmed")
    if relationship == "requested":
        _log(cb, f"✓ {clean_username} için bekleyen istek zaten var (profil düğmesinde doğrulandı).")
        return result("requested_existing", "profile_request_confirmed")

    try:
        # Recheck identity immediately before submitting an external action.
        if not _target_url_matches(page, clean_username):
            return result("retryable_error", "profile_redirected")
        if not browser.human_like_click(page, button):
            return result("retryable_error", "follow_click_failed")
    except Exception as exc:
        return result("retryable_error", "follow_click_failed", str(exc))
    _log(cb, f"→ {clean_username} takip düğmesine basıldı, profil durumu doğrulanıyor…")

    if stoppable_wait(stop_event, _scaled(10, 18, min_lo=3.0, min_hi=6.0)):
        return result("cancelled", "stopped_after_action", submitted=True)
    try:
        if browser.is_rate_limited(page):
            _log(cb, "🚫 Kısıtlama algılandı – mevcut bekleme süresi uygulanıyor.")
            browser.cooldown_wait(cb, stop_event=stop_event)
            return result("cancelled" if _stopped(stop_event) else "unconfirmed", "rate_limited_after_action", submitted=True)
    except Exception as exc:
        return result("unconfirmed", "verification_error", str(exc), submitted=True)

    waited = 0
    last_error = None
    while waited < 45:
        if _stopped(stop_event):
            return result("cancelled", "stopped_after_action", submitted=True)
        try:
            relationship, _, last_error = _profile_control(page, clean_username)
            if relationship in {"following", "requested"}:
                status = "followed" if relationship == "following" else "requested"
                _log(cb, f"★ {clean_username} – {'takip' if status == 'followed' else 'bekleyen istek'} profil düğmesinde doğrulandı.")
                return result(status, "profile_relationship_confirmed", submitted=True)
            if last_error == "profile_redirected":
                return result("unconfirmed", last_error, submitted=True)
        except Exception as exc:
            last_error = str(exc)
        if stoppable_wait(stop_event, 2):
            return result("cancelled", "stopped_after_action", submitted=True)
        waited += 2
    _log(cb, f"⚠ {clean_username} – takip durumu onaylanamadı; sonraki denemede önce mevcut durum kontrol edilecek.")
    return result("unconfirmed", "verification_timeout", last_error or "", submitted=True)
