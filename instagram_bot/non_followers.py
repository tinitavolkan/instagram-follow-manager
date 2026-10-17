"""Compare the signed-in account's two lists through its existing browser UI.

No credentials, private endpoints, relationship actions, or persistence belong
here. A negative result is available only after both exact counts are verified.
"""

from dataclasses import dataclass
from datetime import datetime
import re
import time
from urllib.parse import urljoin, urlparse


@dataclass(frozen=True)
class NonFollowersResult:
    status: str
    username: str = ""
    followers: tuple[str, ...] = ()
    following: tuple[str, ...] = ()
    detail: str = ""
    checked_at: str = ""
    expected_followers: int | None = None
    expected_following: int | None = None

    @property
    def not_following_back(self) -> tuple[str, ...]:
        if self.status != "completed":
            return ()
        return tuple(sorted({name.casefold() for name in self.following}
                            - {name.casefold() for name in self.followers}))


_RESERVED = {"accounts", "about", "api", "challenge", "checkpoint", "developer",
             "direct", "directory", "emails", "explore", "legal", "login", "p",
             "privacy", "push", "reel", "reels", "stories", "terms", "web"}
_VISIBLE_JS = """el => !!(el.getClientRects().length &&
    getComputedStyle(el).visibility !== 'hidden' &&
    !el.closest('[hidden], [aria-hidden="true"]'))"""
_MAX_SECONDS = 6 * 60 * 60
_MAX_SCROLLS = 5000
_SCROLL_DELAY = 5.0
_COUNT_LABELS = {"followers": r"followers?|takipçi(?:ler)?",
                 "following": r"following|takip edilen(?:ler)?|takip"}
_RESTRICTION = re.compile(
    r"try again later|we restrict certain activity|we limit how often|action blocked|"
    r"please wait a few minutes|something went wrong|could(?:n't| not) load|"
    r"confirm (?:it'?s you|your identity)|verify your identity|security code|suspicious (?:login|activity)|"
    r"daha sonra tekrar dene|birkaç dakika (?:bekle|sonra)|bazı (?:hareketleri|işlemleri)|"
    r"işlem engellendi|kimliğini doğrula|sen olduğunu doğrula|hesabını doğrula|"
    r"güvenlik kodu|şüpheli (?:giriş|hareket)|bir sorun oluştu|yüklenemedi", re.IGNORECASE)


class _Abort(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class _Guard:
    def __init__(self, page, stop_event):
        self.page = page
        self.stop_event = stop_event
        self.deadline = time.monotonic() + _MAX_SECONDS
        self.scrolls = 0
        self.restriction = ""

    def response(self, response):
        parsed = urlparse(response.url)
        host = parsed.hostname or ""
        if (host == "instagram.com" or host.endswith(".instagram.com")) and response.status in {401, 403, 429}:
            self.restriction = f"Instagram HTTP {response.status} döndürdü; oturum veya erişim kısıtlaması nedeniyle tarama durdu."

    def check(self):
        if self.restriction:
            raise _Abort("restricted", self.restriction)
        if self.stop_event is not None and self.stop_event.is_set():
            raise _Abort("stopped", "Tarama durduruldu.")
        if time.monotonic() >= self.deadline:
            raise _Abort("incomplete", "Taramanın süre sınırına ulaşıldı; karşılaştırma yapılmadı.")
        current = urlparse(self.page.url)
        if current.hostname not in {"instagram.com", "www.instagram.com"}:
            raise _Abort("incomplete", "Mevcut sayfa Instagram oturumu değil; önce kendi hesabınla Instagram'ı aç.")
        if any(part in {"login", "challenge", "checkpoint"} for part in current.path.casefold().split("/")):
            raise _Abort("restricted", "Instagram giriş veya hesap doğrulama ekranı gösteriyor; tarama durduruldu.")
        messages = self.page.evaluate("""() => {
          const visible = """ + _VISIBLE_JS + """;
          const body=document.body;
          return body && visible(body) ? [body.innerText||''] : [];
        }""")
        if any(_RESTRICTION.search(" ".join(message.casefold().replace("\u0307", "").split())) for message in messages):
            raise _Abort("restricted", "Instagram kısıtlama, doğrulama veya yükleme hatası gösteriyor; tarama durduruldu.")
        if self.restriction:
            raise _Abort("restricted", self.restriction)

    def timeout_ms(self):
        self.check()
        return max(1, min(5000, int((self.deadline - time.monotonic()) * 1000)))

    def wait(self, seconds):
        end = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < end:
            self.check()
            self.page.wait_for_timeout(min(250, max(1, (end - time.monotonic()) * 1000)))
        self.check()


def _username_from_href(href: str) -> str | None:
    parsed = urlparse(urljoin("https://www.instagram.com/", href))
    if (parsed.scheme != "https" or parsed.hostname not in {"instagram.com", "www.instagram.com"}
            or parsed.query or parsed.fragment or parsed.username or parsed.port not in {None, 443}):
        return None
    match = re.fullmatch(r"/([A-Za-z0-9._]{1,30})/?", parsed.path)
    if match is None or match[1].casefold() in _RESERVED:
        return None
    return match[1].casefold()


def _own_username(page) -> str | None:
    candidates = page.evaluate("""() => {
      const visible = """ + _VISIBLE_JS + """;
      const inSidebar = a => {
        for(let p=a.parentElement; p && p!==document.body && p!==document.documentElement; p=p.parentElement) {
          if(p.matches('main,article,[role="dialog"]') || p.querySelector('main,article,[role="dialog"]')) break;
          // A shared page wrapper is not evidence of sidebar membership.
          const box=p.getBoundingClientRect();
          if(box.width>360) continue;
          const paths=new Set(Array.from(p.querySelectorAll('a[href]')).filter(visible).map(link=> {
            try {
              const url=new URL(link.getAttribute('href'),location.href);
              return url.protocol==='https:' && ['instagram.com','www.instagram.com'].includes(url.hostname)
                ? url.pathname.replace(/\\/$/,'') || '/' : '';
            } catch {return '';}
          }));
          if(paths.has('/') && ['/reels','/direct/inbox','/explore'].some(path=>paths.has(path))) return true;
        }
        return false;
      };
      return Array.from(document.querySelectorAll('a[href]')).filter(a =>
        visible(a) && !a.closest('main, article, [role="dialog"]')).map(a=> {
        const labels = [a.getAttribute('aria-label'), a.innerText,
          ...Array.from(a.querySelectorAll('svg[aria-label]')).map(s=>s.getAttribute('aria-label'))];
        const labelled=labels.some(t=>/^(profile|profil)$/i.test((t||'').trim()));
        const images=Array.from(a.querySelectorAll('img[alt]')).filter(visible);
        return {href:a.getAttribute('href'),labelled,
          avatarLabels:images.length && inSidebar(a) ? images.map(i=>i.getAttribute('alt')) : []};
      });
    }""")
    names = set()
    for candidate in candidates:
        name = _username_from_href(candidate["href"])
        if name is None:
            continue
        # Compact navigation uses the account avatar instead of a Profile SVG.
        # Its accessible name must identify this same linked account.
        avatar_pattern = (re.escape(name) + r"(?:['’]s profile picture|"
                          r"(?:['’](?:in|ın|un|ün))? profil resmi| adlı kişinin profil resmi)")
        if candidate["labelled"] or any(
                re.fullmatch(avatar_pattern, " ".join(label.casefold().split()))
                for label in candidate["avatarLabels"]):
            names.add(name)
    return next(iter(names)) if len(names) == 1 else None


def _profile_links(page, username: str, guard):
    current = urlparse(page.url)
    if (current.hostname not in {"instagram.com", "www.instagram.com"}
            or current.path.strip("/").casefold() != username):
        return {}
    headers = page.locator("main header")
    visible = [headers.nth(i) for i in range(headers.count()) if headers.nth(i).is_visible()]
    if len(visible) != 1:
        return {}
    header = visible[0]
    headings = header.locator("h1, h2, [role='heading']")
    if not any(headings.nth(i).is_visible() and headings.nth(i).inner_text(timeout=guard.timeout_ms()).strip().casefold() == username
               for i in range(headings.count())):
        return {}
    links = {}
    candidates = header.locator("a[href]")
    for i in range(candidates.count()):
        anchor = candidates.nth(i)
        if not anchor.is_visible():
            continue
        href = anchor.get_attribute("href", timeout=guard.timeout_ms()) or ""
        parsed = urlparse(urljoin(page.url, href))
        hash_link = href == "#" and anchor.get_attribute("role", timeout=guard.timeout_ms()) == "link"
        label = (" ".join(anchor.inner_text(timeout=guard.timeout_ms()).casefold().split())
                 if hash_link else "")
        for kind in ("followers", "following"):
            route_link = (parsed.scheme == "https" and parsed.hostname in {"instagram.com", "www.instagram.com"}
                    and not parsed.username and parsed.port in {None, 443}
                    and parsed.path.rstrip("/").casefold() == f"/{username}/{kind}"
                    and not parsed.query and not parsed.fragment)
            # Current Instagram opens lists from '#' links in this verified
            # own-profile header; select them by their exact count label.
            labelled_hash = hash_link and re.fullmatch(rf"\S.*\s+(?:{_COUNT_LABELS[kind]})", label)
            if route_link or labelled_hash:
                if kind in links:
                    return {}
                links[kind] = anchor
    return links


def _parse_count(text: str, kind: str) -> int | None:
    text = " ".join(text.casefold().replace("\u0307", "").split())
    labels = _COUNT_LABELS[kind]
    text = re.sub(rf"\s+(?:{labels})$", "", text)
    if re.fullmatch(r"[0-9]+", text):
        return int(text)
    if re.fullmatch(r"[0-9]{1,3}([., ])[0-9]{3}(?:\1[0-9]{3})*", text):
        return int(re.sub(r"[., ]", "", text))
    return None


def _read_count(anchor, kind: str, guard) -> int | None:
    titles = anchor.evaluate("""anchor => {
      const visible = """ + _VISIBLE_JS + """;
      return [anchor, ...anchor.querySelectorAll('[title]')].filter(visible)
        .map(el=>el.getAttribute('title')).filter(Boolean);
    }""", timeout=guard.timeout_ms())
    exact = {number for title in titles if (number := _parse_count(title, kind)) is not None}
    if len(exact) > 1:
        return None
    displayed = _parse_count(anchor.inner_text(timeout=guard.timeout_ms()), kind)
    if exact:
        title_count = next(iter(exact))
        return title_count if displayed is None or displayed == title_count else None
    return displayed


def _visible_dialog(page):
    dialogs = page.locator("[role='dialog']")
    visible = [dialogs.nth(i) for i in range(dialogs.count()) if dialogs.nth(i).is_visible()]
    return visible[0] if len(visible) == 1 else None


def _collect_rows(dialog, guard) -> set[str]:
    hrefs = dialog.evaluate("""dialog => {
      const visible = """ + _VISIBLE_JS + """;
      const normalized = t => (t||'').toLowerCase().replace(/\u0307/g,'').trim();
      const suggestion = t => /^(suggested for you|suggestions for you|suggested|recommended|recommendations|senin için önerilenler|senin için öneriler|önerilenler|öneriler)$/.test(normalized(t));
      const boundaries = Array.from(dialog.querySelectorAll('h1,h2,h3,h4,[role="heading"],div,span'))
        .filter(el=>visible(el) && suggestion(el.innerText));
      const relationship = t => /^(follow|follow back|following|requested|remove|takip et|sen de takip et|sen de onu takip et|takiptesin|istek gönderildi|kaldır|çıkar)$/.test(normalized(t));
      return Array.from(dialog.querySelectorAll('a[href]')).filter(a=> {
        if (!visible(a) || boundaries.some(b=>b.compareDocumentPosition(a)&Node.DOCUMENT_POSITION_FOLLOWING)) return false;
        if (a.closest('nav,header,aside')) return false;
        let ancestor = a.parentElement;
        while (ancestor && ancestor !== dialog) {
          if (suggestion(ancestor.getAttribute('aria-label'))) return false;
          ancestor=ancestor.parentElement;
        }
        let row = a.parentElement;
        while (row && row !== dialog) {
          if (suggestion(row.getAttribute('aria-label'))) return false;
          if (row.matches('li,[role="listitem"]')) return true;
          const buttons = Array.from(row.querySelectorAll('button,[role="button"]')).filter(visible);
          if (buttons.some(b=>relationship(b.getAttribute('aria-label')||b.innerText))) {
            const addresses = new Set(Array.from(row.querySelectorAll('a[href]')).map(link=>link.getAttribute('href')));
            if (addresses.size <= 2) return true;
          }
          row = row.parentElement;
        }
        return false;
      }).map(a=>a.getAttribute('href'));
    }""", timeout=guard.timeout_ms())
    return {name for href in hrefs if (name := _username_from_href(href)) is not None}


def _close_dialog(page, dialog, guard):
    guard.check()
    close_buttons = dialog.get_by_role("button", name=re.compile(r"^(Close|Kapat)$", re.IGNORECASE))
    if close_buttons.count() == 1 and close_buttons.first.is_visible():
        close_buttons.first.click(timeout=guard.timeout_ms())
    else:
        page.keyboard.press("Escape")
        guard.check()
    return _wait_for(page, lambda: _visible_dialog(page) is None, guard)


def _wait_for(page, read, guard, seconds=5):
    deadline = min(guard.deadline, time.monotonic() + seconds)
    while True:
        guard.check()
        value = read()
        guard.check()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        guard.wait(min(.1, max(0, deadline - time.monotonic())))


def _scroll_dialog(dialog, guard):
    """Scroll only the current list container, once, without profile visits."""
    return dialog.evaluate("""dialog => {
      const visible = """ + _VISIBLE_JS + """;
      const containers = [dialog, ...dialog.querySelectorAll('div, ul, section')].filter(el=>
        visible(el) && el.querySelector('a[href]') &&
        /auto|scroll/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight);
      containers.sort((a,b)=>a.clientHeight-b.clientHeight);
      const list=containers[0]; if(!list) return false;
      const before=list.scrollTop;
      list.scrollTop=Math.min(list.scrollHeight-list.clientHeight, before+Math.max(200,list.clientHeight*.85));
      return list.scrollTop > before;
    }""", timeout=guard.timeout_ms())


def scan_non_followers(page, stop_event, log_cb=None, progress_cb=None) -> NonFollowersResult:
    """Perform one bounded UI scan with the caller's synchronous Playwright Page."""
    username = ""
    counts = {}
    lists = {"followers": set(), "following": set()}
    guard = _Guard(page, stop_event)
    attached = False

    def finish(status, detail):
        if log_cb is not None:
            try:
                log_cb(detail)
            except Exception:
                pass
        return NonFollowersResult(status, username,
                                  tuple(sorted(lists["followers"])), tuple(sorted(lists["following"])),
                                  detail, datetime.now().astimezone().isoformat(timespec="seconds"),
                                  counts.get("followers"), counts.get("following"))

    if stop_event is not None and stop_event.is_set():
        return finish("stopped", "Tarama durduruldu.")
    try:
        page.on("response", guard.response)
        attached = True
        guard.check()
        username = _wait_for(page, lambda: _own_username(page), guard, seconds=10) or ""
        if not username:
            return finish("incomplete", "Programın kullandığı Instagram oturumunda kendi profil bağlantısı doğrulanamadı. Programın açtığı tarayıcıdaki girişi kontrol et.")
        try:
            page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded", timeout=guard.timeout_ms())
        except Exception as exc:
            guard.check()
            raise _Abort("incomplete", f"Kendi profilin açılamadı; otomatik yeniden deneme yapılmadı: {exc}") from exc
        guard.check()
        links = _wait_for(page, lambda: _profile_links(page, username, guard), guard) or {}
        counts = {kind: _read_count(links[kind], kind, guard) if kind in links else None
                  for kind in lists}
        if any(value is None for value in counts.values()):
            return finish("incomplete", "Kendi profilindeki tam takipçi ve takip edilen sayıları doğrulanamadı.")
        for kind in lists:
            guard.check()
            label = "Takipçiler" if kind == "followers" else "Takip edilenler"
            if progress_cb is not None:
                progress_cb(f"{label}: liste okunuyor.")
            if counts[kind] == 0:
                continue
            links = _profile_links(page, username, guard)
            if kind not in links:
                raise _Abort("incomplete", "Kendi profilinin liste bağlantısı kayboldu; karşılaştırma yapılmadı.")
            try:
                links[kind].click(timeout=guard.timeout_ms())
            except Exception as exc:
                guard.check()
                raise _Abort("incomplete", f"Liste penceresi açılamadı; otomatik yeniden deneme yapılmadı: {exc}") from exc
            dialog = _wait_for(page, lambda: _visible_dialog(page), guard)
            if dialog is None:
                return finish("incomplete", "Takip listesi penceresi açılamadı.")
            lists[kind].update(_collect_rows(dialog, guard))
            stalled = 0
            while len(lists[kind]) < counts[kind]:
                guard.check()
                if guard.scrolls >= _MAX_SCROLLS:
                    raise _Abort("incomplete", f"İki liste için toplam {_MAX_SCROLLS} kaydırmalık tarama sınırına ulaşıldı; karşılaştırma yapılmadı.")
                before = len(lists[kind])
                guard.wait(_SCROLL_DELAY)
                lists[kind].update(_collect_rows(dialog, guard))
                if len(lists[kind]) >= counts[kind]:
                    break
                moved = _scroll_dialog(dialog, guard)
                guard.scrolls += 1
                # Pump browser events so virtualized rows can replace earlier rows.
                guard.wait(.25)
                lists[kind].update(_collect_rows(dialog, guard))
                # All currently loaded rows may already have been collected.
                # Moving through that batch is progress toward its load edge.
                stalled = stalled + 1 if not moved and len(lists[kind]) == before else 0
                if progress_cb is not None:
                    progress_cb(f"{label}: {len(lists[kind])}/{counts[kind]} kullanıcı okundu.")
                if stalled >= 3:
                    raise _Abort("incomplete", "Liste ilerlemiyor veya bazı satırlar yüklenmedi; karşılaştırma yapılmadı.")
            if len(lists[kind]) != counts[kind]:
                return finish("incomplete", "Listenin tamamı okunamadı; karşılaştırma yapılmadı.")
            if not _close_dialog(page, dialog, guard):
                return finish("incomplete", "Liste penceresi kapatılamadı.")
        guard.check()
        links = _profile_links(page, username, guard)
        rechecked = {kind: _read_count(links[kind], kind, guard) if kind in links else None for kind in lists}
        if rechecked != counts:
            return finish("incomplete", "Profil sayıları tarama sırasında değişti; karşılaştırma yapılmadı.")
        # A retained profile DOM can hold stale counts while a dialog is open.
        # One fresh visit to this already verified own profile confirms both
        # counts against a fresh page, with no retry after a failed refresh.
        try:
            page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded", timeout=guard.timeout_ms())
            guard.check()
            links = _wait_for(page, lambda: _profile_links(page, username, guard), guard) or {}
            refreshed = {kind: _read_count(links[kind], kind, guard) if kind in links else None for kind in lists}
        except _Abort:
            raise
        except Exception as exc:
            guard.check()
            raise _Abort("incomplete", f"Son profil doğrulaması yapılamadı; karşılaştırma yapılmadı: {exc}") from exc
        if refreshed != counts:
            return finish("incomplete", "Profil yenilendikten sonra sayılar doğrulanamadı veya değişti; karşılaştırma yapılmadı.")
        guard.check()
        return finish("completed", "İki liste eksiksiz okundu; karşılaştırma tamamlandı.")
    except _Abort as exc:
        return finish(exc.status, exc.detail)
    except Exception as exc:
        return finish("error", f"Tarama hatası: {exc}")
    finally:
        if attached:
            page.remove_listener("response", guard.response)
