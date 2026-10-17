"""Arayüzden bağımsız, iş parçacığı güvenli çalışma durumu ve mola bildirimi."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import math
import threading
import time


class DashboardState:
    """Sadece gözlem yapar; işlem kararlarını, sayaçları veya dosyaları değiştirmez."""

    STATES = {"ready", "running", "waiting", "stopping", "stopped", "completed", "attention"}

    def __init__(self, remaining=0, *, monotonic=None, now=None):
        self._lock = threading.RLock()
        self._monotonic = monotonic or time.monotonic
        self._now = now or datetime.now
        self._state = "ready"
        self._detail = "Henüz bir işlem başlatılmadı."
        self._next_action = "Yapmak istediğiniz işlemi soldaki menüden seçin."
        self._remaining = max(0, int(remaining))
        self._last_success_at = None
        self._last_success_user = ""
        self._waits = []
        self._wait_base = None
        self._next_token = 0

    def update(self, state=None, detail=None, next_action=None, remaining=None):
        if state is not None and state not in self.STATES:
            raise ValueError(f"Bilinmeyen çalışma durumu: {state}")
        with self._lock:
            if state is not None:
                self._state = state
                if state != "waiting":
                    self._waits.clear()
                    self._wait_base = None
            if detail is not None:
                self._detail = str(detail)
            if next_action is not None:
                self._next_action = str(next_action)
            if remaining is not None:
                self._remaining = max(0, int(remaining))

    def begin_wait(self, seconds, reason, next_action="Mola bitince mevcut işleme devam edilecek."):
        with self._lock:
            # Geç ulaşan bir bekleme bildirimi durdurma/hata ekranını gizlemesin.
            if self._state in {"stopping", "stopped", "attention"}:
                return None
            if not self._waits:
                self._wait_base = (self._state, self._detail, self._next_action)
            seconds = max(0.0, float(seconds))
            self._next_token += 1
            token = self._next_token
            self._waits.append({
                "token": token, "deadline": self._monotonic() + seconds,
                "finish_at": self._now() + timedelta(seconds=seconds),
                "detail": str(reason), "next_action": str(next_action),
            })
            self._state = "waiting"
            self._detail = str(reason)
            self._next_action = str(next_action)
            return token

    def end_wait(self, token, stopped=False):
        with self._lock:
            found = next((index for index, frame in enumerate(self._waits)
                          if frame["token"] == token), None)
            if found is None:
                return
            was_current = found == len(self._waits) - 1
            del self._waits[found]
            if stopped:
                self.update(state="stopped", detail="İşlem durduruldu; kalan kullanıcılar korundu.",
                            next_action="Devam etmek için ilgili işlemi yeniden başlatın.")
            elif was_current:
                if self._waits:
                    frame = self._waits[-1]
                    self._state, self._detail, self._next_action = (
                        "waiting", frame["detail"], frame["next_action"])
                else:
                    self._state, self._detail, self._next_action = self._wait_base
                    self._wait_base = None

    def mark_success(self, username):
        with self._lock:
            self._last_success_at = self._now()
            self._last_success_user = str(username)

    def snapshot(self):
        with self._lock:
            frame = self._waits[-1] if self._waits else None
            return {
                "state": self._state, "detail": self._detail,
                "next_action": self._next_action, "remaining": self._remaining,
                # Sürenin dolması, worker'ın devam ettiğini tek başına kanıtlamaz.
                "wait_remaining": max(0, math.ceil(frame["deadline"] - self._monotonic())) if frame else None,
                "wait_finish_at": frame["finish_at"] if frame else None,
                "last_success_at": self._last_success_at,
                "last_success_user": self._last_success_user,
            }


_reporting = threading.local()


@contextmanager
def reporting_scope(dashboard):
    previous = getattr(_reporting, "dashboard", None)
    _reporting.dashboard = dashboard
    try:
        yield
    finally:
        _reporting.dashboard = previous


@contextmanager
def wait_description(reason, next_action="Mola bitince mevcut işleme devam edilecek."):
    previous = getattr(_reporting, "description", None)
    _reporting.description = (reason, next_action)
    try:
        yield
    finally:
        _reporting.description = previous


def reporting_note(detail, next_action):
    """Tarayıcıdan gelen açıklama, durdurma veya müdahale durumunu ezmesin."""
    observer = getattr(_reporting, "dashboard", None)
    if observer is not None and observer.snapshot()["state"] in {"ready", "running", "waiting"}:
        observer.update(detail=detail, next_action=next_action)


@contextmanager
def reported_wait(seconds, reason=None, *, next_action=None, stop_event=None, dashboard=None):
    """Mevcut beklemeyi gösterir; bekleme süresine veya sonuca müdahale etmez."""
    observer = dashboard if dashboard is not None else getattr(_reporting, "dashboard", None)
    description = getattr(_reporting, "description", None)
    reason = reason or (description[0] if description else "İşlemler arasında bekleniyor.")
    next_action = next_action or (description[1] if description else "Mola bitince mevcut işleme devam edilecek.")
    token = observer.begin_wait(seconds, reason, next_action) if observer is not None else None
    try:
        yield
    finally:
        if observer is not None:
            observer.end_wait(token, stopped=bool(stop_event and stop_event.is_set()))
