"""Serialized browser work, with explicit starts and interruptible unfollow pacing."""
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import queue
import re
import threading
import time
from urllib.parse import urlparse

from .export_lists import load_exports
from .instagram_session import InstagramSession
from .unfollow_store import UnfollowStore
from .unfollowing import session_username, unfollow_user


_RESULT_STATUSES = {'unfollowed', 'already_not_following', 'unavailable', 'error',
                    'unconfirmed', 'restricted', 'stopped'}
# Application defaults, not Instagram's published limits or a block guarantee.
UNFOLLOW_INTERVAL_SECONDS = 600
UNFOLLOW_BATCH_SIZE = 5


def _empty_snapshot():
    return dict(account='', followers_count=0, following_count=0, mutual_count=0,
                candidate_count=0, remaining=[], history=[], sources=[], warnings=[],
                imported_at='', in_progress='', last_results={})


class UnfollowController:
    def __init__(self, data_dir, event_cb, session_factory=None, action=None):
        self.data_dir = Path(data_dir)
        self._event_cb = event_cb
        self._session_factory = session_factory or InstagramSession
        self._action = action or unfollow_user
        self._session = None
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._closing = threading.Event()
        self._lock = threading.RLock()
        self._busy = False
        self._store = None
        self._store_error = ''
        try:
            self._store = UnfollowStore(self.data_dir / 'geri_takip_verileri.json')
        except Exception as exc:
            self._store_error = f'Kayıt dosyası açılamadı; mevcut dosyanın üzerine yazılmadı: {exc}'
            self._status('attention', self._store_error)

    @property
    def busy(self):
        with self._lock:
            return self._busy

    def _emit(self, kind, **fields):
        try:
            self._event_cb(dict(type=kind, **fields))
        except Exception:
            # A closed UI must not interrupt durable result recording.
            pass

    def _status(self, state, detail, username=''):
        self._emit('status', state=state, detail=detail, username=username)
        self._emit('log', detail=detail)

    def snapshot(self):
        with self._lock:
            return self._store.snapshot() if self._store is not None else _empty_snapshot()

    def _publish(self):
        self._emit('snapshot', data=self.snapshot())

    def _enqueue(self, command, **payload):
        with self._lock:
            if self._busy or self._closing.is_set():
                return False
            self._stop.clear()
            self._busy = True
            self._status('busy', 'İşlem hazırlanıyor.', payload.get('username', ''))
            self._queue.put((command, payload))
        return True

    def import_paths(self, paths, account=''):
        return self._enqueue('import', paths=tuple(str(p) for p in paths), account=account)

    def open_session(self):
        return self._enqueue('open')

    def check_session(self):
        return self._enqueue('check')

    def unfollow(self, username):
        return self._enqueue('unfollow', username=str(username).strip().lstrip('@').casefold())

    def unfollow_batch(self):
        return self._enqueue('unfollow_batch')

    def follow(self, callback):
        if not callable(callback):
            raise ValueError('Takip görevi başlatılamadı.')
        return self._enqueue('follow', callback=callback)

    def stop(self):
        self._stop.set()
        self._status('busy' if self.busy else 'stopped',
                     'Durdurma istendi; sonucu doğrulanmayan kişiler kalan listede korunur.')

    def shutdown(self):
        self._closing.set()
        self._stop.set()
        self._queue.put(None)

    def _open_page(self):
        if self._stop.is_set():
            return None
        if self._session is None:
            self._session = self._session_factory(self.data_dir / 'ig_profile')
        return self._session.open(stop_event=self._stop)

    def _check_identity(self, page):
        name = session_username(page) if page is not None else None
        expected = self.snapshot().get('account', '')
        if not name:
            detail = 'Programın açtığı tarayıcıda Instagram’a giriş yap. Hesap işlem öncesinde otomatik kontrol edilir.'
        elif expected and name != expected:
            detail = f'Açık hesap @{name}; yüklenen listelerin hesabı @{expected}. Hesaplar eşleşmiyor.'
        else:
            detail = f'Instagram oturumu: @{name}.'
        self._emit('session', username=name or '', detail=detail)
        return name, detail

    def _require_store(self):
        if self._store is None:
            raise ValueError(self._store_error)

    def _import(self, paths, account):
        self._require_store()
        self._status('busy', 'Dosyalar bilgisayarında okunuyor ve karşılaştırılıyor.')
        export = load_exports(paths)
        if self._stop.is_set():
            self._status('stopped', 'İçe aktarma durduruldu; önceki liste korundu.')
            return
        with self._lock:
            self._store.load_export(export, account=account)
        self._publish()
        state = self.snapshot()
        self._status('idle', f"@{state['account']}: {len(state['remaining'])} kişi kalan listede. "
                     'Otomatik takibi bırak ile sırayı başlatabilir veya bir kişinin düğmesine basabilirsin.')

    def _unfollow(self, username):
        self._require_store()
        state = self.snapshot()
        if not state.get('account'):
            raise ValueError('Önce kendi hesabına ait takipçi ve takip edilen dosyalarını yükle.')
        if not re.fullmatch(r'[A-Za-z0-9._]{1,30}', username) or username not in state['remaining']:
            raise ValueError('Bu kullanıcı kalan listede bulunmuyor; işlem yapılmadı.')
        page = self._open_page()
        if self._stop.is_set():
            self._status('stopped', 'İşlem başlamadan durduruldu; kişi listede korundu.', username)
            return
        identity, detail = self._check_identity(page)
        if identity != state['account']:
            self._status('attention', detail + ' Takibi bırakma işlemi yapılmadı.', username)
            return
        with self._lock:
            self._store.begin(username)
        self._publish()
        self._status('busy', f'@{username}: profil ve takibi bırakma sonucu kontrol ediliyor.', username)
        try:
            result = self._action(page, username, state['account'], stop_event=self._stop)
            status = result.status
            detail = result.detail
            submitted = result.action_submitted
            if status not in _RESULT_STATUSES:
                status, detail = 'unconfirmed', 'İşlem sonucu tanınamadı; kişi listede korundu.'
        except Exception as exc:
            status, detail, submitted = 'unconfirmed', f'İşlem sonucu doğrulanamadı: {exc}', True
        if status == 'unavailable':
            detail += ' Ulaşılamayan hesap listeden çıkarıldı; takibi bırakılmış sayılmadı.'
        with self._lock:
            self._store.finish(username, status, detail=detail, submitted=submitted,
                               target_verified=status == 'unavailable')
        self._publish()
        if status in {'unfollowed', 'already_not_following'}:
            message = ('Takibi bırakıldığı doğrulandı.' if status == 'unfollowed'
                       else 'Zaten takip edilmediği doğrulandı.')
            self._status('idle', f'@{username}: {message} Kalan: {len(self.snapshot()["remaining"])}.', username)
        elif status == 'unavailable':
            self._status('idle', f'@{username}: Ulaşılamadı; listeden çıkarıldı. Kalan: {len(self.snapshot()["remaining"])}.', username)
        else:
            self._status('stopped' if status == 'stopped' else 'attention',
                         f'@{username}: {detail} Kişi kalan listede; otomatik tekrar yapılmaz.', username)
        return status

    def _batch_history(self):
        now = datetime.now(timezone.utc)
        dates = [datetime.fromisoformat(record['at']).astimezone(timezone.utc)
                 for record in self.snapshot()['history'] if record['status'] == 'unfollowed']
        return now, dates

    def _wait_between_unfollows(self, seconds, username, page):
        deadline = time.monotonic() + seconds
        self._status('busy', f'@{username}: sonraki işlem öncesinde uygulama molası.', username)
        last_seconds = None
        while not self._stop.is_set() and not self._closing.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            seconds_left = math.ceil(remaining)
            if seconds_left != last_seconds:
                self._emit('status', state='busy', phase='waiting', username=username,
                           detail=f'@{username}: sıradaki işlem için {seconds_left} sn bekleniyor. Durdur ile kesebilirsiniz.')
                last_seconds = seconds_left
            # Pump Playwright events so restriction responses during a pause are observed.
            page.wait_for_timeout(min(250, remaining * 1000))
        self._status('stopped', 'Otomatik takibi bırakma durduruldu; kalan kişiler korundu.')
        return False

    def _unfollow_batch(self):
        self._require_store()
        state = self.snapshot()
        if not state.get('account'):
            raise ValueError('Önce kendi hesabına ait takipçi ve takip edilen dosyalarını yükle.')
        candidates = tuple(state['remaining'])
        if not candidates:
            self._status('idle', 'Takibi bırakılacak kalan kişi yok.')
            return
        completed = 0
        skipped = 0
        page, restriction = None, []

        def on_response(response):
            host = urlparse(response.url).hostname or ''
            if (host == 'instagram.com' or host.endswith('.instagram.com')) and response.status in {401, 403, 429}:
                restriction[:] = [f'Instagram HTTP {response.status} döndürdü; otomatik sıra durduruldu.']
                self._stop.set()

        try:
            for username in candidates:
                if self._stop.is_set() or self._closing.is_set():
                    self._status('stopped', 'Otomatik takibi bırakma durduruldu; kalan kişiler korundu.')
                    return
                now, recent = self._batch_history()
                if page is None:
                    page = self._open_page()
                    if page is None or self._stop.is_set():
                        self._status('stopped', 'Otomatik sıra başlamadan durduruldu.')
                        return
                    page.on('response', on_response)
                    identity, detail = self._check_identity(page)
                    if identity != state['account']:
                        self._status('attention', detail + ' Otomatik sıra başlatılmadı.')
                        return
                if recent and len(recent) % UNFOLLOW_BATCH_SIZE == 0 and UNFOLLOW_INTERVAL_SECONDS > 0:
                    # Stored timestamps have second precision; keep at least the full interval.
                    due = max(recent) + timedelta(seconds=UNFOLLOW_INTERVAL_SECONDS + 1)
                    seconds = max(0, (due - now).total_seconds())
                    if seconds and not self._wait_between_unfollows(seconds, username, page):
                        return
                self._status('busy', f'Otomatik sıra: @{username} · işlenen {completed + skipped} / {len(candidates)}.', username)
                status = self._unfollow(username)
                if status == 'unavailable':
                    skipped += 1
                    continue
                if status not in {'unfollowed', 'already_not_following'}:
                    # Includes account mismatch, restriction, missing controls and uncertain results.
                    return
                completed += 1
            self._status('idle', f'Otomatik sıra tamamlandı. Doğrulanan: {completed} · Ulaşılamayan: {skipped}; kalan: {len(self.snapshot()["remaining"])}.')
        finally:
            if page is not None:
                try:
                    page.remove_listener('response', on_response)
                except Exception:
                    pass
            if restriction:
                self._status('attention', restriction[0] + ' Kalan kişiler korundu; otomatik tekrar yapılmaz.')

    def _follow(self, callback):
        """Run the original follow task on the same session and worker."""
        page = self._open_page()
        if self._stop.is_set():
            self._status('stopped', 'Takip görevi başlamadan durduruldu.')
            return
        identity, detail = self._check_identity(page)
        expected = self.snapshot().get('account', '')
        if not identity or (expected and identity != expected):
            self._status('attention', detail + ' Takip görevi başlatılmadı.')
            return
        restriction = []

        def on_response(response):
            host = urlparse(response.url).hostname or ''
            if (host == 'instagram.com' or host.endswith('.instagram.com')) and response.status in {401, 403, 429}:
                restriction[:] = [f'Instagram HTTP {response.status} döndürdü; takip görevi durduruldu.']
                self._stop.set()

        page.on('response', on_response)
        try:
            self._status('busy', 'Takip Et sekmesindeki takip listesi işleniyor.')
            outcome = callback(page, self._stop, self._closing)
        finally:
            try:
                page.remove_listener('response', on_response)
            except Exception:
                pass
        if restriction:
            self._status('attention', restriction[0] + ' Kalan takip listesi korundu.')
        elif self._stop.is_set() or self._closing.is_set():
            self._status('stopped', (outcome.get('detail') if isinstance(outcome, dict) else '')
                         or 'Takip görevi durduruldu; kalan takip listesi korundu.')
        elif isinstance(outcome, dict):
            state = {'completed': 'idle', 'ready': 'idle'}.get(outcome.get('state'), outcome.get('state'))
            self._status(state if state in {'idle', 'attention', 'stopped'} else 'attention',
                         outcome.get('detail') or 'Takip görevinin sonucunu Takip Et sekmesinden kontrol edin.')
        else:
            self._status('attention', 'Takip görevinin sonuç bilgisi alınamadı; Takip Et sekmesini kontrol edin.')

    def run_loop(self):
        try:
            while True:
                task = self._queue.get()
                try:
                    if task is None:
                        break
                    command, payload = task
                    if self._stop.is_set() or self._closing.is_set():
                        self._status('stopped', 'Bekleyen işlem iptal edildi; kalan liste korundu.')
                    elif command == 'import':
                        self._import(**payload)
                    elif command in {'open', 'check'}:
                        page = self._open_page()
                        name, detail = self._check_identity(page)
                        expected = self.snapshot().get('account', '')
                        self._status('idle' if name and (not expected or name == expected) else 'attention', detail)
                    elif command == 'unfollow':
                        self._unfollow(**payload)
                    elif command == 'unfollow_batch':
                        self._unfollow_batch()
                    elif command == 'follow':
                        self._follow(**payload)
                except Exception as exc:
                    self._status('attention', str(exc))
                    self._publish()
                finally:
                    self._queue.task_done()
                    with self._lock:
                        self._busy = False
                    # status payloads can arrive before the finally block;
                    # this event is the authoritative release of UI controls.
                    self._emit('busy', busy=False)
        finally:
            if self._session is not None:
                self._session.close()
            with self._lock:
                self._busy = False
