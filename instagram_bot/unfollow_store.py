"""Atomic, account-scoped export candidates and individual operation history."""

from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from threading import RLock

from .export_lists import ExportLists


_TERMINAL = {"unfollowed", "already_not_following", "unavailable"}
_STATUSES = _TERMINAL | {"error", "unconfirmed", "restricted", "stopped"}
_MAX_STATE_BYTES = 64 * 1024 * 1024


def _username(value):
    if not isinstance(value, str):
        raise ValueError("Kullanıcı adı metin olmalı.")
    value = value.strip()
    if value.startswith("@"):
        value = value[1:]
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", value):
        raise ValueError("Geçerli Instagram kullanıcı adını gir.")
    return value.casefold()


def _names(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError("Kullanıcı listesi eksik veya bozuk.")
    return sorted({_username(value) for value in values})


def _strings(values):
    if not isinstance(values, (list, tuple)) or not all(isinstance(value, str) for value in values):
        raise ValueError("Kaynak veya açıklama listesi bozuk.")
    return list(values)


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _record(username, status, detail, submitted, target_verified=False):
    result = {"username": username, "status": status, "detail": detail, "at": _now(), "submitted": submitted}
    if target_verified:
        result['target_verified'] = True
    return result


def _append_record(account, record):
    account["history"].insert(0, record)
    account["last_results"][record["username"]] = deepcopy(record)


def _validate_state(state):
    """Reject damaged history rather than resetting it into a fresh candidate list."""
    try:
        if not isinstance(state, dict) or state["version"] != 1 or not isinstance(state["accounts"], dict):
            raise ValueError
        active = state["active_account"]
        if not isinstance(active, str) or (active and active not in state["accounts"]) or (state["accounts"] and not active):
            raise ValueError
        for name, account in state["accounts"].items():
            if _username(name) != name or not isinstance(account, dict):
                raise ValueError
            for key in ("followers", "following"):
                if _names(account[key]) != account[key]:
                    raise ValueError
            for key in ("sources", "warnings"):
                _strings(account[key])
            if not isinstance(account["fingerprint"], str) or not account["fingerprint"]:
                raise ValueError
            datetime.fromisoformat(account["imported_at"])
            pending = account["in_progress"]
            candidates = set(account["following"]) - set(account["followers"])
            if not isinstance(pending, str) or (pending and (_username(pending) != pending or pending not in candidates)):
                raise ValueError
            if not isinstance(account["completed"], dict) or not isinstance(account["history"], list) or not isinstance(account["last_results"], dict):
                raise ValueError
            for fingerprint, names in account["completed"].items():
                if not isinstance(fingerprint, str) or not fingerprint or _names(names) != names:
                    raise ValueError
            for record in account["history"] + list(account["last_results"].values()):
                if (not isinstance(record, dict) or _username(record["username"]) != record["username"]
                        or record["status"] not in _STATUSES or not isinstance(record["detail"], str)
                        or not isinstance(record["submitted"], bool)
                        or not isinstance(record.get('target_verified', False), bool)):
                    raise ValueError
                datetime.fromisoformat(record["at"])
            latest = {}
            for record in account["history"]:
                latest.setdefault(record["username"], record)
            if latest != account["last_results"]:
                raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("İşlem kaydı bozuk veya desteklenmeyen biçimde; mevcut dosya korundu.") from exc


class UnfollowStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = RLock()
        self._state = {"version": 1, "active_account": "", "accounts": {}}
        if self.path.exists():
            try:
                with self.path.open("rb") as stream:
                    payload = stream.read(_MAX_STATE_BYTES + 1)
                if len(payload) > _MAX_STATE_BYTES:
                    raise ValueError("İşlem kaydı boyut sınırını aşıyor.")
                state = json.loads(payload.decode("utf-8"))
                _validate_state(state)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("İşlem kaydı okunamadı; mevcut dosya korundu.") from exc
            self._state = state
            recovered = deepcopy(state)
            changed = False
            for account in recovered["accounts"].values():
                # Earlier unavailable checks could reuse the preceding page.
                # Recheck only unaudited skips, retaining their original history.
                for username, record in list(account['last_results'].items()):
                    if record['status'] != 'unavailable' or record.get('target_verified', False):
                        continue
                    for names in account['completed'].values():
                        if username in names:
                            names.remove(username)
                    _append_record(account, _record(username, 'error',
                        'Önceki atlamada hedef sayfaya gidildiği doğrulanmamıştı; yeniden kontrol için listeye alındı.',
                        record['submitted']))
                    changed = True
                if account["in_progress"]:
                    _append_record(account, _record(account["in_progress"], "unconfirmed",
                        "Önceki oturum işlem sırasında kapandı; sonuç doğrulanamadı, kullanıcı listede bırakıldı.", False))
                    account["in_progress"] = ""
                    changed = True
            if changed:
                self._commit(recovered)

    def _commit(self, state):
        # Assign in-memory state only after the durable replacement succeeds.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=self.path.parent,
                                             prefix=self.path.name + ".", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            self._state = state
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _active(self, state=None):
        state = self._state if state is None else state
        account = state["active_account"]
        if not account:
            raise ValueError("Önce takipçi ve takip edilen dosyalarını içe aktar.")
        return state["accounts"][account]

    def load_export(self, export: ExportLists, account: str = "") -> dict:
        with self._lock:
            if not isinstance(account, str) or not isinstance(export.account_hint, str):
                raise ValueError("Hesap adı metin olmalı.")
            selected = _username(account) if account else ""
            hint = _username(export.account_hint) if export.account_hint else ""
            if selected and hint and selected != hint:
                raise ValueError("Girilen hesap adı ile dışa aktarma hesabı çelişiyor.")
            selected = selected or hint
            if not selected:
                raise ValueError("Bu dosyalarda hesap adı yok; kendi Instagram kullanıcı adını gir.")
            followers, following = _names(export.followers), _names(export.following)
            sources, warnings = _strings(export.sources), _strings(export.warnings)
            fingerprint = export.fingerprint
            if not isinstance(fingerprint, str):
                raise ValueError("Dışa aktarma kimliği bozuk.")
            if not fingerprint:
                fingerprint = sha256(json.dumps([followers, following], separators=(",", ":")).encode()).hexdigest()
            if any(value["in_progress"] for value in self._state["accounts"].values()):
                raise ValueError("Devam eden işlem bitmeden yeni dosya yüklenemez.")
            state = deepcopy(self._state)
            previous = state["accounts"].get(selected, {})
            state["accounts"][selected] = {
                "followers": followers, "following": following, "sources": sources, "warnings": warnings,
                "fingerprint": fingerprint, "imported_at": _now(), "in_progress": "",
                "history": previous.get("history", []), "last_results": previous.get("last_results", {}),
                "completed": previous.get("completed", {}),
            }
            state["accounts"][selected]["completed"].setdefault(fingerprint, [])
            state["active_account"] = selected
            self._commit(state)
            return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            if not self._state["active_account"]:
                return {"account": "", "followers_count": 0, "following_count": 0, "mutual_count": 0,
                        "candidate_count": 0, "remaining": [], "history": [], "sources": [], "warnings": [],
                        "imported_at": "", "in_progress": "", "last_results": {}}
            account = self._active()
            followers, following = set(account["followers"]), set(account["following"])
            candidates = following - followers
            completed = set(account["completed"].get(account["fingerprint"], []))
            return deepcopy({"account": self._state["active_account"], "followers_count": len(followers),
                "following_count": len(following), "mutual_count": len(followers & following),
                "candidate_count": len(candidates), "remaining": sorted(candidates - completed),
                "history": account["history"], "sources": account["sources"], "warnings": account["warnings"],
                "imported_at": account["imported_at"], "in_progress": account["in_progress"], "last_results": account["last_results"]})

    def begin(self, username) -> None:
        with self._lock:
            username = _username(username)
            active = self._active()
            if active["in_progress"]:
                raise ValueError("Başka bir kullanıcı işlemi devam ediyor.")
            if username not in self.snapshot()["remaining"]:
                raise ValueError("Bu kullanıcı kalan adaylar arasında değil.")
            state = deepcopy(self._state)
            self._active(state)["in_progress"] = username
            self._commit(state)

    def finish(self, username, status, detail="", submitted=False, target_verified=False) -> dict:
        with self._lock:
            username = _username(username)
            if (not isinstance(status, str) or status not in _STATUSES or not isinstance(detail, str)
                    or not isinstance(submitted, bool) or not isinstance(target_verified, bool)):
                raise ValueError("İşlem sonucu geçersiz.")
            if self._active()["in_progress"] != username:
                raise ValueError("Sonuç, başlatılmış kullanıcı işlemiyle eşleşmiyor.")
            state = deepcopy(self._state)
            active = self._active(state)
            _append_record(active, _record(username, status, detail, submitted, target_verified))
            active["in_progress"] = ""
            if status in _TERMINAL:
                completed = active["completed"][active["fingerprint"]]
                if username not in completed:
                    completed.append(username)
                    completed.sort()
            self._commit(state)
            return self.snapshot()
