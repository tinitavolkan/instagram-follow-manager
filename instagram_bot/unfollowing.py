"""One explicit unfollow action, scoped to a verified account and target profile.

This module neither edits result lists nor schedules retries. Only the worker
records each result. Explicit unavailable-page results may be skipped by the
worker; other uncertain actions must remain in the pending list.
"""
from dataclasses import dataclass
import json
import re
import time
from urllib.parse import urlparse

from .instagram_session import session_username


@dataclass(frozen=True)
class UnfollowResult:
    status: str
    detail: str = ""
    action_submitted: bool = False


_MAX_SECONDS = 40
_HOSTS = {"instagram.com", "www.instagram.com"}
_RESERVED = {"accounts", "about", "api", "challenge", "checkpoint", "direct",
             "explore", "login", "p", "reel", "reels", "stories", "web"}
_LABELS = {"following": "following", "takiptesin": "following", "takip ediliyor": "following",
           "follow": "follow", "follow back": "follow", "takip et": "follow",
           "sen de takip et": "follow", "sen de onu takip et": "follow",
           "requested": "requested", "istek gönderildi": "requested"}
_UNFOLLOW = {"unfollow", "takibi bırak", "takipten çık"}
_UNAVAILABLE_HEADINGS = {
    'üzgünüz, bu sayfaya ulaşılamıyor',
    "sorry, this page isn't available",
    'sorry, this page isn’t available',
    "this page isn't available",
    'this page isn’t available',
}
_UNAVAILABLE_EVIDENCE_JS = """() => {
    const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
    const candidates = Array.from(document.querySelectorAll('h1,h2,[role="heading"]'))
        .filter(visible).map(el => el.innerText || '');
    // Instagram's current unavailable-page heading is a plain span.
    // Require the explanatory block and a return link, rather than matching bios.
    for (const el of document.querySelectorAll('main span,main p,[role="main"] span,[role="main"] p')) {
        if (!visible(el) || el.children.length) continue;
        let block = el.parentElement;
        for (let depth = 0; block && depth < 3; depth++, block = block.parentElement) {
            const text = block.innerText || '';
            const context = /tıkladığın bağlantı bozuk olabilir|sayfa kaldırılmış olabilir|link you followed may be broken|page may have been removed/i.test(text);
            const back = Array.from(block.querySelectorAll('a[href="/"]'))
                .some(a => visible(a) && /instagram/i.test(a.innerText));
            if (text.length < 1200 && context && back) {
                candidates.push(el.innerText || '');
                break;
            }
            if (block.matches('main,[role="main"]')) break;
        }
    }
    return candidates;
}"""
_RESTRICTION = re.compile(
    r"try again later|we restrict certain activity|we limit how often|action blocked|"
    r"please wait a few minutes|confirm (?:it'?s you|your identity)|verify your identity|"
    r"security code|suspicious (?:login|activity)|daha sonra tekrar dene|"
    r"birkaç dakika (?:bekle|sonra)|bazı (?:hareketleri|işlemleri)|işlem engellendi|"
    r"kimliğini doğrula|sen olduğunu doğrula|hesabını doğrula|güvenlik kodu|şüpheli (?:giriş|hareket)",
    re.IGNORECASE,
)


def _normalized(text):
    return " ".join((text or "").casefold().replace("\u0307", "").split())


class _Abort(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


class _Guard:
    def __init__(self, page, stop_event, target):
        self.page, self.stop_event = page, stop_event
        self.target = target
        self.target_loaded = False
        self.deadline = time.monotonic() + _MAX_SECONDS
        self.restriction = ""
        self.action_submitted = False

    def response(self, response):
        host = urlparse(response.url).hostname or ""
        if (host == "instagram.com" or host.endswith(".instagram.com")) and response.status in {401, 403, 429}:
            self.restriction = f"Instagram HTTP {response.status} döndürdü; işlem durduruldu."

    def check(self):
        if self.restriction:
            raise _Abort("restricted", self.restriction)
        if self.stop_event is not None and self.stop_event.is_set():
            raise _Abort("stopped", "İşlem durduruldu; doğrulanmayan sonuç listede korunur.")
        if time.monotonic() >= self.deadline:
            raise _Abort("unconfirmed" if self.action_submitted else "error", "İşlem süresi doldu; sonuç doğrulanamadı.")
        parsed = urlparse(self.page.url)
        if parsed.scheme != "https" or parsed.hostname not in _HOSTS:
            raise _Abort("unconfirmed" if self.action_submitted else "error", "Programın Instagram sayfası doğrulanamadı.")
        if any(part in {"login", "challenge", "checkpoint"} for part in parsed.path.casefold().split("/")):
            raise _Abort("restricted", "Instagram giriş veya hesap doğrulama ekranı gösteriyor.")
        text = self.page.evaluate("() => document.body ? document.body.innerText : ''")
        if _RESTRICTION.search(_normalized(text)):
            raise _Abort("restricted", "Instagram kısıtlama veya doğrulama uyarısı gösteriyor.")
        # Do not classify the previous profile's error page as this target.
        on_target = self.target_loaded and parsed.path.rstrip('/').casefold() == '/' + self.target
        headings = self.page.evaluate(_UNAVAILABLE_EVIDENCE_JS) if on_target else []
        if self.restriction:
            raise _Abort("restricted", self.restriction)
        if any(_normalized(heading).rstrip('.!') in _UNAVAILABLE_HEADINGS for heading in headings):
            detail = ('Takibi bırakma sonucu doğrulanmadı.' if self.action_submitted else 'Takibi bırakma işlemi gönderilmedi.')
            raise _Abort('unavailable',
                         'Hedef profile ulaşılamıyor: Instagram “Bu sayfaya ulaşılamıyor” uyarısı gösteriyor. ' + detail)

    def timeout_ms(self):
        self.check()
        return max(1, min(4000, int((self.deadline - time.monotonic()) * 1000)))

    def wait(self, seconds):
        until = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < until:
            self.check()
            self.page.wait_for_timeout(min(250, max(1, (until - time.monotonic()) * 1000)))
        self.check()


def _wait_for(guard, read, seconds=6):
    until = min(guard.deadline, time.monotonic() + seconds)
    while True:
        guard.check()
        value = read()
        guard.check()
        if value is not None:
            return value
        if time.monotonic() >= until:
            return None
        guard.wait(min(.25, until - time.monotonic()))


def _account(guard, expected):
    actual = session_username(guard.page)
    if actual is not None and actual != expected:
        raise _Abort("unconfirmed" if guard.action_submitted else "error", "Açık Instagram hesabı dosyaların sahibiyle uyuşmuyor.")
    return actual


def _require_account(guard, expected):
    guard.check()
    if _account(guard, expected) != expected:
        raise _Abort("unconfirmed" if guard.action_submitted else "error", "Oturum sahibi doğrulanamadı; işlem yapılmadı.")


def _visible(locator):
    return [locator.nth(i) for i in range(locator.count()) if locator.nth(i).is_visible()]


def _profile_control(guard, username):
    parsed = urlparse(guard.page.url)
    if (parsed.scheme != "https" or parsed.hostname not in _HOSTS or parsed.username
            or parsed.port not in {None, 443} or parsed.query or parsed.fragment
            or parsed.path.rstrip("/").casefold() != "/" + username):
        raise _Abort("unconfirmed" if guard.action_submitted else "error", "Hedef profil adresi değişti; işlem doğrulanamadı.")
    snapshot = guard.page.evaluate("""({username, labels}) => {
        const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
        const normalized = text => (text || '').toLowerCase().replace(/\\u0307/g, '').trim().replace(/\\s+/g, ' ');
        const headers = Array.from(document.querySelectorAll('main header')).filter(visible);
        if (headers.length !== 1) return {state: headers.length ? 'ambiguous_header' : 'loading'};
        const header = headers[0];
        const headings = Array.from(header.querySelectorAll('h1,h2,[role="heading"]')).filter(visible);
        if (!headings.some(el => normalized(el.innerText) === username)) return {state: 'loading'};
        const controls = Array.from(header.querySelectorAll('button,[role="button"]')).filter(el =>
            visible(el) && !el.closest('nav,aside,article,[role="dialog"],[role="menu"]')).map(el => {
                const aria = el.getAttribute('aria-label');
                const label = aria || el.innerText || '';
                return {relationship: labels[normalized(label)], label, aria: !!aria};
            }).filter(el => el.relationship);
        if (controls.length > 1) return {state: 'ambiguous_controls'};
        return controls.length ? {state: 'found', ...controls[0]} : {state: 'loading'};
    }""", {'username': username, 'labels': _LABELS})
    if snapshot['state'] == 'ambiguous_header':
        raise _Abort("unconfirmed" if guard.action_submitted else "error", "Hedef profil başlığı belirsiz.")
    if snapshot['state'] == 'loading':
        return None
    if snapshot['state'] == 'ambiguous_controls':
        raise _Abort("unconfirmed" if guard.action_submitted else "error", "Hedef profilde birden fazla ilişki düğmesi var.")
    relationship = snapshot['relationship']
    if relationship != 'following':
        return relationship, None
    # The click target must still say Following; never retain positional nth()
    # locators while Instagram replaces the header or inserts recommendations.
    header = guard.page.locator('main header:visible')
    if snapshot['aria']:
        label = json.dumps(snapshot['label'], ensure_ascii=False)
        button = header.locator(f'button[aria-label={label}]:visible,[role="button"][aria-label={label}]:visible')
    else:
        # Accessible names separate an SVG icon's title from the visible label.
        # Keep the relationship label exact, allowing only trailing icon text.
        expression = r'^\s*' + r'\s+'.join(re.escape(part) for part in snapshot['label'].split()) + r'(?:\s+.*)?\s*$'
        button = header.get_by_role('button', name=re.compile(expression, re.IGNORECASE))
    return relationship, button


def _confirmation(guard):
    containers = _visible(guard.page.locator('[role="dialog"], [role="menu"]'))
    # A dialog containing its menu is one popup, not two independent scopes.
    containers = [item for item in containers if not item.evaluate(
        "el => !!el.parentElement.closest('[role=dialog],[role=menu]')", timeout=guard.timeout_ms())]
    actionable = []
    for container in containers:
        matches = []
        for item in _visible(container.locator("button, [role='button'], [role='menuitem']")):
            label = item.get_attribute("aria-label", timeout=guard.timeout_ms()) or item.inner_text(timeout=guard.timeout_ms())
            if _normalized(label) in _UNFOLLOW:
                matches.append(item)
        # Story highlights also use role=menu. Only an unfollow control makes
        # a container a candidate confirmation scope.
        if matches:
            actionable.append(matches)
    if not actionable:
        return None
    if len(actionable) != 1:
        raise _Abort("error", "Birden fazla Takibi bırak menüsü var; hedef belirsiz.")
    matches = actionable[0]
    if len(matches) > 1:
        raise _Abort("error", "Menüde birden fazla Takibi bırak düğmesi var; işlem yapılmadı.")
    return matches[0] if matches else None


def unfollow_user(page, username: str, expected_account: str, stop_event=None) -> UnfollowResult:
    """Submit at most one scoped unfollow, then verify against a fresh profile."""
    if stop_event is not None and stop_event.is_set():
        return UnfollowResult("stopped", "İşlem başlamadan durduruldu.")
    clean = username.strip().casefold() if isinstance(username, str) else ""
    owner = expected_account.strip().casefold() if isinstance(expected_account, str) else ""
    if (not re.fullmatch(r"(?=.*[a-z0-9_])[a-z0-9._]{1,30}", clean) or clean in _RESERVED
            or not re.fullmatch(r"(?=.*[a-z0-9_])[a-z0-9._]{1,30}", owner) or owner in _RESERVED):
        return UnfollowResult("error", "Hedef kullanıcı adı veya dosya sahibi geçersiz.")
    if clean == owner:
        return UnfollowResult("error", "Kendi hesabınıza takipten çıkma işlemi uygulanamaz.")
    guard = _Guard(page, stop_event, clean)
    attached = False
    try:
        page.on("response", guard.response)
        attached = True
        if _wait_for(guard, lambda: _account(guard, owner)) != owner:
            raise _Abort("error", "Oturum sahibi doğrulanamadı; önce programın tarayıcısında giriş yapın.")
        page.goto(f"https://www.instagram.com/{clean}/", wait_until="domcontentloaded", timeout=guard.timeout_ms())
        guard.target_loaded = True
        if _wait_for(guard, lambda: _account(guard, owner)) != owner:
            raise _Abort("error", "Hedef profil açılınca oturum sahibi doğrulanamadı.")
        control = _wait_for(guard, lambda: _profile_control(guard, clean))
        if control is None:
            raise _Abort("error", "Hedef kullanıcı adı ve tek ilişki düğmesi doğrulanamadı.")
        relationship, button = control
        _require_account(guard, owner)
        if relationship == "follow":
            return UnfollowResult("already_not_following", "Hedef profilde zaten takip edilmediği doğrulandı.")
        if relationship == "requested":
            raise _Abort("error", "Bekleyen takip isteği var; istek iptali yapılmadı.")
        click_timeout = guard.timeout_ms()
        try:
            button.click(timeout=click_timeout)
        except Exception:
            guard.action_submitted = True  # A click exception does not prove that no action reached the UI.
            raise
        confirmation = _wait_for(guard, lambda: _confirmation(guard))
        if confirmation is None:
            raise _Abort("error", "Hedef profilin Takibi bırak menüsü doğrulanamadı; işlem gönderilmedi.")
        _require_account(guard, owner)
        current = _profile_control(guard, clean)
        if current is None or current[0] != "following":
            raise _Abort("error", "Onaydan önce hedef profil ilişkisi değişti; işlem gönderilmedi.")
        # Check the menu once more, with current ownership and target verified.
        confirmation = _confirmation(guard)
        if confirmation is None:
            raise _Abort("error", "Takibi bırak menüsü kayboldu; işlem gönderilmedi.")
        click_timeout = guard.timeout_ms()
        guard.action_submitted = True
        confirmation.click(timeout=click_timeout)
        guard.wait(.25)

        def confirmed_follow():
            _require_account(guard, owner)
            current = _profile_control(guard, clean)
            return True if current is not None and current[0] == "follow" else None

        if not _wait_for(guard, confirmed_follow, seconds=8):
            raise _Abort("unconfirmed", "Tıklama sonrası takipten çıkma hedef profilde doğrulanamadı.")
        # Optimistic UI is insufficient: verify once on a newly loaded profile.
        page.goto(f"https://www.instagram.com/{clean}/", wait_until="domcontentloaded", timeout=guard.timeout_ms())
        if _wait_for(guard, lambda: _account(guard, owner)) != owner:
            raise _Abort("unconfirmed", "Son doğrulamada oturum sahibi belirlenemedi.")
        if not _wait_for(guard, confirmed_follow, seconds=8):
            raise _Abort("unconfirmed", "Yenilenen hedef profilde takipten çıkma doğrulanamadı.")
        guard.check()
        return UnfollowResult("unfollowed", "Takipten çıkma yenilenen hedef profilde doğrulandı.", True)
    except _Abort as exc:
        return UnfollowResult(exc.status, exc.detail, guard.action_submitted)
    except Exception as exc:
        return UnfollowResult("unconfirmed" if guard.action_submitted else "error",
                              f"İşlem doğrulanamadı: {exc}", guard.action_submitted)
    finally:
        if attached:
            page.remove_listener("response", guard.response)
