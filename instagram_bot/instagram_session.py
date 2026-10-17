"""A visible, credential-free Instagram session owned by one worker thread."""
from pathlib import Path
import re
import threading
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright


_HOSTS = {"instagram.com", "www.instagram.com"}
_RESERVED = {"accounts", "about", "api", "challenge", "checkpoint", "direct",
             "directory", "explore", "legal", "login", "p", "privacy", "reel",
             "reels", "stories", "terms", "web"}
_VISIBLE_JS = """el => !!(el.getClientRects().length &&
    getComputedStyle(el).visibility !== 'hidden' &&
    !el.closest('[hidden], [aria-hidden="true"]'))"""


def _username_from_href(href):
    try:
        parsed = urlparse(urljoin("https://www.instagram.com/", href))
        if (parsed.scheme != "https" or parsed.hostname not in _HOSTS or parsed.query
                or parsed.fragment or parsed.username or parsed.port not in {None, 443}):
            return None
        match = re.fullmatch(r"/([A-Za-z0-9._]{1,30})/?", parsed.path)
        if match and match[1].casefold() not in _RESERVED:
            return match[1].casefold()
    except (TypeError, ValueError):
        pass
    return None


def session_username(page) -> str | None:
    """Read an unambiguous own-profile navigation link, never a profile card."""
    if page is None:
        return None
    try:
        current = urlparse(page.url)
        if current.scheme != "https" or current.hostname not in _HOSTS:
            return None
        if any(part in {"login", "challenge", "checkpoint"} for part in current.path.casefold().split("/")):
            return None
        candidates = page.evaluate("""() => {
          const ordinaryVisible = """ + _VISIBLE_JS + """;
          const modalOpen=Array.from(document.querySelectorAll('[role="dialog"],[role="menu"]')).some(ordinaryVisible);
          // Modal libraries hide the still-rendered background from assistive
          // technology. Read its current nav evidence, without accepting CSS-
          // hidden templates or any account link inside the popup itself.
          const visible = el => ordinaryVisible(el) || (modalOpen &&
            el.getClientRects().length && getComputedStyle(el).visibility!=='hidden' &&
            !el.closest('[hidden],[role="dialog"],[role="menu"]'));
          const inSidebar = a => {
            for(let p=a.parentElement;p && p!==document.body && p!==document.documentElement;p=p.parentElement) {
              if(p.matches('main,article,[role="dialog"],[role="menu"]') ||
                 p.querySelector('main,article,[role="dialog"],[role="menu"]')) break;
              if(p.getBoundingClientRect().width>360) continue;
              const paths=new Set(Array.from(p.querySelectorAll('a[href]')).filter(visible).map(link=> {
                try {
                  const url=new URL(link.getAttribute('href'),location.href);
                  return url.protocol==='https:' && ['instagram.com','www.instagram.com'].includes(url.hostname)
                    ? url.pathname.replace(/\\/$/,'') || '/' : '';
                } catch { return ''; }
              }));
              if(paths.has('/') && ['/reels','/direct/inbox','/explore'].some(path=>paths.has(path))) return true;
            }
            return false;
          };
          return Array.from(document.querySelectorAll('a[href]')).filter(a=>
            visible(a) && !a.closest('main,article,[role="dialog"],[role="menu"]')).map(a=> {
              const labels=[a.getAttribute('aria-label'),a.innerText,
                ...Array.from(a.querySelectorAll('svg[aria-label]')).map(s=>s.getAttribute('aria-label'))];
              const sidebar=inSidebar(a);
              const labelled=labels.some(t=>/^(profile|profil)$/i.test((t||'').trim())) &&
                (sidebar || !!a.closest('nav,[role="navigation"]'));
              return {href:a.getAttribute('href'),labelled,
                avatarLabels:sidebar ? Array.from(a.querySelectorAll('img[alt]')).filter(visible).map(i=>i.getAttribute('alt')) : []};
            });
        }""")
        names = set()
        for candidate in candidates:
            name = _username_from_href(candidate["href"])
            if name is None:
                continue
            avatar_pattern = (re.escape(name) + r"(?:['’]s profile picture|"
                              r"(?:['’](?:in|ın|un|ün))? profil resmi| adlı kişinin profil resmi)")
            if candidate["labelled"] or any(
                    re.fullmatch(avatar_pattern, " ".join(label.casefold().replace("\u0307", "").split()))
                    for label in candidate["avatarLabels"]):
                names.add(name)
        return next(iter(names)) if len(names) == 1 else None
    except Exception:
        # A closed/loading page cannot establish account ownership.
        return None


class InstagramSession:
    def __init__(self, profile_dir: Path):
        self.profile_dir = Path(profile_dir).resolve()
        self.page = None
        self._playwright = None
        self._context = None
        self._owner_thread = None

    def _check_thread(self):
        if self._owner_thread is not None and self._owner_thread != threading.get_ident():
            raise RuntimeError("Instagram oturumu yalnız onu açan worker iş parçacığında kullanılabilir.")

    def open(self, stop_event=None):
        """Open/reuse a visible persistent browser; the user signs in manually."""
        self._check_thread()
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("Oturum açma durduruldu.")
        if self.page is not None and not self.page.is_closed():
            return self.page
        if self._context is not None or self._playwright is not None:
            self.close()
        self._owner_thread = threading.get_ident()
        try:
            self._playwright = sync_playwright().start()
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("Oturum açma durduruldu.")
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir), headless=False,
                viewport={"width": 1280, "height": 900},
            )
            self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("Oturum açma durduruldu.")
            self.page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=15000)
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("Oturum açma durduruldu.")
            return self.page
        except Exception:
            self.close()
            raise

    def close(self):
        """Close on the creating worker, without deleting persistent profile data."""
        self._check_thread()
        try:
            if self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    pass
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
        finally:
            self.page = self._context = self._playwright = None
            self._owner_thread = None
