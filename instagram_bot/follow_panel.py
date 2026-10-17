"""The existing follow engine, displayed as a tab on the shared worker/session."""
import json
import math
import threading
import time
from datetime import datetime

import customtkinter as ctk

from . import browser
from . import config as legacy_config
from .dashboard import DashboardState, reporting_scope
from .paths import BASE_DIR, STATS_FILE
from .stats import DailyHistory, DailyLimiter, Stats
from .storage import get_user_statuses, read_takiplist
from .worker import WorkerMixin
from . import ui_theme as t


class LegacyFollowPanel(WorkerMixin, ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._busy = False
        self._bound_page = None
        self._model_lock = threading.Lock()
        self._after_id = None
        self._settings_window = None
        self._settings_controls = []
        self.stop_event = threading.Event()
        self.shutdown_event = threading.Event()
        self.active_task = None
        self.follow_pending = False
        self._follow_initial_count = 0
        self.follow_safety_stop = False
        self.non_followers_safety_stop = False
        self.bot_ready = False
        self.limiter = self.stats = None
        self.takipler = read_takiplist()
        self.dashboard = DashboardState(remaining=len(self.takipler))
        self._stats_snapshot = self._read_saved_stats()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)
        self._create_widgets()
        self._render_users(tuple(self.takipler))
        self.bind("<Configure>", self._resize)
        self.poll()
        self._after_id = self.after(500, self._poll_loop)

    def _create_widgets(self):
        ctk.CTkLabel(self, text="Takip sıran", font=t.font(20, True), text_color=t.TEXT).grid(
            row=0, column=0, padx=12, pady=(8, 4), sticky="w")
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=1, column=0, padx=8, pady=4, sticky="ew")
        items = (
            ("follow_btn", "Takip Et", self.follow_clicked),
            ("start_btn", "Oturumu başlat", self.start_clicked),
            ("stop_btn", "Durdur", self.stop_clicked),
            ("refresh_btn", "Listeyi yenile", self.refresh),
            ("settings_btn", "Ayarlar", self.open_settings),
            ("history_btn", "Günlük Geçmiş", self.open_history_window),
            ("user_status_btn", "Kullanıcı Durumları", self.open_user_status_window),
        )
        self._idle_controls = []
        for column, (name, label, callback) in enumerate(items):
            controls.grid_columnconfigure(column, weight=1)
            button = t.button(controls, text=label, command=callback, width=80,
                              kind='primary' if name == 'follow_btn' else 'danger' if name == 'stop_btn' else 'secondary', font=t.font(12))
            button.grid(row=0, column=column, padx=3, sticky="ew")
            setattr(self, name, button)
            if name not in {"stop_btn", "history_btn", "user_status_btn"}:
                self._idle_controls.append(button)
        self.status_label = ctk.CTkLabel(self, text="Hazır", anchor="w", justify="left", wraplength=850,
                                        font=ctk.CTkFont(size=14, weight="bold"))
        self.status_label.grid(row=2, column=0, padx=12, pady=(8, 3), sticky="ew")
        self.stats_label = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=850,
                                       text_color=t.MUTED, font=t.font(12))
        self.stats_label.grid(row=3, column=0, padx=12, pady=3, sticky="ew")
        self.list_title = ctk.CTkLabel(self, text="", anchor="w", font=ctk.CTkFont(size=15, weight="bold"))
        self.list_title.grid(row=4, column=0, padx=12, pady=(8, 3), sticky="ew")
        self.listbox = ctk.CTkTextbox(self, wrap="none", fg_color=t.ZEBRA, text_color=t.TEXT, font=t.font(13), border_width=1, border_color=t.BORDER)
        self.listbox.grid(row=5, column=0, padx=12, pady=4, sticky="nsew")
        self.progress_bar = ctk.CTkProgressBar(self, progress_color=t.PRIMARY, fg_color=t.BORDER)
        self.progress_bar.grid(row=6, column=0, padx=12, pady=5, sticky="ew")
        self.progress_bar.set(0)
        self.help_label = ctk.CTkLabel(
            self, text="Takiplist.txt içindeki mevcut sırayı işler. Günlük/saatlik limitler, molalar ve üç turluk yeniden deneme korunur.",
            anchor="w", justify="left", wraplength=850, text_color=t.MUTED, font=t.font(11))
        self.help_label.grid(row=7, column=0, padx=12, pady=(3, 8), sticky="ew")
        self.set_busy(False)

    def _resize(self, event=None):
        if event is not None and event.widget is not self:
            return
        width = max(350, self.winfo_width() - 24)
        for widget in (self.status_label, self.stats_label, self.help_label):
            widget.configure(wraplength=width)

    def _is_busy(self):
        return self._busy or bool(getattr(self.app.controller, "busy", False)) or bool(getattr(self.app, "closing", False))

    def set_busy(self, busy):
        """Called by the host on the Tk main thread for every shared task."""
        self._busy = bool(busy)
        for widget in self._idle_controls:
            widget.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")
        for widget in self._settings_controls:
            if widget.winfo_exists():
                widget.configure(state="disabled" if busy else "normal")

    def call_in_main_thread(self, callback, *args, **kwargs):
        self.app.call_in_main_thread(callback, *args, **kwargs)

    def log(self, message):
        self.app.log(message)

    def start_clicked(self):
        if not self._is_busy():
            self.app._run_command(self.app.controller.open_session, "Ortak Instagram oturumu açılıyor…")

    def stop_clicked(self):
        self.app.controller.stop()

    def follow_clicked(self):
        if self._is_busy():
            return
        self.refresh()
        if not self.takipler:
            self.log("Takiplist.txt boş; takip başlatılmadı.")
            return
        # Only a new, explicit user command releases the legacy safety stop.
        if self.follow_safety_stop:
            self.log("Koruma duruşu Takip Et komutuyla kaldırıldı; hesabınızdaki uyarıları kontrol edin.")
        self.follow_safety_stop = False
        self.non_followers_safety_stop = False
        self.app._run_command(lambda: self.app.controller.follow(self.run_follow), "Takip listesi ortak oturumda işleniyor…")

    def _ensure_bot_ready(self):
        ready = (self._bound_page is not None and browser.page is self._bound_page
                 and not self.stop_event.is_set() and not self.shutdown_event.is_set()
                 and not self._bound_page.is_closed())
        if not ready:
            self._dashboard_task_failed("Ortak Instagram oturumu hazır değil; yeni tarayıcı veya şifreli giriş başlatılmadı.")
        return ready

    @staticmethod
    def _reject_legacy_login(*args, **kwargs):
        raise RuntimeError("Takip akışı yalnız ortak manuel Instagram oturumunu kullanabilir.")

    def run_follow(self, page, stop_event, shutdown_event):
        """Invoked exclusively by the host's existing browser worker thread."""
        previous_page, previous_login = browser.page, browser.run_bot
        previous_binding = (self._bound_page, self.stop_event, self.shutdown_event, self.bot_ready)
        self._bound_page, self.stop_event, self.shutdown_event = page, stop_event, shutdown_event
        self.bot_ready, self.active_task, self.follow_pending = True, "follow", True
        browser.page, browser.run_bot = page, self._reject_legacy_login
        try:
            with reporting_scope(self.dashboard):
                if self.follow_safety_stop:
                    self._show_follow_safety_stop()
                elif stop_event.is_set() or shutdown_event.is_set():
                    self.dashboard_update(state="stopped", detail="Takip başlamadan durduruldu.")
                elif time.time() < browser.rate_limit_lock_until:
                    self._dashboard_task_failed("Instagram güvenlik kilidi etkin; takip başlatılmadı. Kilit bitince Takip Et ile yeniden başlatın.")
                elif self._ensure_bot_ready():
                    limit = int(legacy_config.CONFIG.get("DAILY_LIMIT", 80))
                    if self.limiter is None:
                        self.limiter = DailyLimiter(limit=limit)
                    if self.stats is None:
                        self.stats = Stats(limit)
                    self._follow_initial_count = len(read_takiplist())
                    self.update_progress_bar(0)
                    self.dashboard_update(state="running", detail="Takip başlıyor.", next_action="Durdur ile kesebilirsiniz.")
                    WorkerMixin._run_follow_task(self)
        except Exception as exc:
            self._dashboard_task_failed(f"Takip işlemi durdu: {type(exc).__name__}: {exc}")
            self.log(f"Takip işlemi hata nedeniyle durdu: {exc}")
        finally:
            browser.page, browser.run_bot = previous_page, previous_login
            self._bound_page, self.stop_event, self.shutdown_event, self.bot_ready = previous_binding
            self.active_task, self.follow_pending = None, False
            self.update_stats_panel()
            self.call_in_main_thread(self.poll)
        snapshot = self.dashboard.snapshot()
        state = ("attention" if snapshot["state"] == "attention" else
                 "stopped" if stop_event.is_set() or shutdown_event.is_set() or snapshot["state"] == "stopped" else "idle")
        return {"state": state, "detail": snapshot["detail"]}

    def _read_saved_stats(self):
        """Display persisted numbers without invoking Stats' day migration."""
        def read(path):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}
        stats, daily = read(STATS_FILE), read(BASE_DIR / "daily_stats.json")
        limit = int(legacy_config.CONFIG.get("DAILY_LIMIT", 80))
        return dict(success=stats.get("success", 0), skipped=stats.get("skipped", 0), errors=stats.get("errors", 0),
                    date=stats.get("date", "kayıt yok"), count=daily.get("count", 0), limit=limit,
                    hourly_count=None, hourly_limit=max(5, math.ceil(limit / 4)), avg_delay=None)

    def update_stats_panel(self):
        """Capture model values on the worker; widget changes happen in poll."""
        if self.stats is not None and self.limiter is not None:
            delays = tuple(self.stats.delay_history)
            data = dict(success=self.stats.success, skipped=self.stats.skipped, errors=self.stats.errors,
                        date=self.stats.date, count=self.limiter.count, limit=self.limiter.limit,
                        hourly_count=self.limiter.hourly_count, hourly_limit=self.limiter.hourly_limit,
                        avg_delay=sum(delays) / len(delays) if delays else None)
            with self._model_lock:
                self._stats_snapshot = data
        self.call_in_main_thread(self.poll)

    def update_progress_bar(self, value):
        self.call_in_main_thread(self.progress_bar.set, max(0, min(1, value)))

    def refresh_listbox(self):
        self.call_in_main_thread(self._render_users, tuple(self.takipler))
        if self.active_task == "follow" and self._follow_initial_count:
            self.update_progress_bar((self._follow_initial_count - len(self.takipler)) / self._follow_initial_count)

    def _render_users(self, users):
        self.list_title.configure(text=f"Takiplist.txt · kalan {len(users)} kişi")
        self.listbox.configure(state="normal")
        self.listbox.delete("1.0", "end")
        self.listbox.insert("1.0", "\n".join(users))
        self.listbox.configure(state="disabled")

    def refresh(self):
        if self._is_busy():
            return
        self.takipler = read_takiplist()
        self.dashboard.update(remaining=len(self.takipler))
        self._render_users(tuple(self.takipler))
        if self.stats is None:
            with self._model_lock:
                self._stats_snapshot = self._read_saved_stats()
        self.poll()

    def poll(self):
        """Main-thread rendering, including the legacy wait countdown."""
        if not self.winfo_exists() or getattr(self.app, "closing", False):
            return
        model = self.dashboard.snapshot()
        labels = {"ready": "Hazır", "running": "Takip ediliyor", "waiting": "Molada", "stopping": "Durduruluyor",
                  "stopped": "Durduruldu", "completed": "Tamamlandı", "attention": "Kontrol gerekiyor"}
        wait = model["wait_remaining"]
        suffix = f" · {wait // 60:02}:{wait % 60:02} kaldı" if wait is not None else ""
        detail = f"{labels.get(model['state'], model['state'])}{suffix}\n{model['detail']}"
        if model["next_action"]:
            detail += "\n" + model["next_action"]
        self.status_label.configure(text=detail)
        with self._model_lock:
            data = self._stats_snapshot.copy()
        average = "—" if data["avg_delay"] is None else f"{data['avg_delay']:.0f} sn"
        hourly = "—" if data["hourly_count"] is None else str(data["hourly_count"])
        self.stats_label.configure(text=(f"Günlük: {data['count']} / {data['limit']} · Saatlik: {hourly} / {data['hourly_limit']} · "
                                        f"Başarılı: {data['success']} · Atlanan: {data['skipped']} · Hata: {data['errors']}\n"
                                        f"Ort. gecikme: {average} · İstatistik günü: {data['date']} · Kalan: {model['remaining']}"))

    def _poll_loop(self):
        self._after_id = None
        if self.winfo_exists() and not getattr(self.app, "closing", False):
            self.poll()
            self._after_id = self.after(500, self._poll_loop)

    def destroy(self):
        if self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None
        super().destroy()

    def open_settings(self):
        if self._is_busy():
            return None
        if self._settings_window is not None and self._settings_window.winfo_exists():
            self._settings_window.lift()
            return self._settings_window
        window = self._settings_window = ctk.CTkToplevel(self, fg_color=t.BG)
        window.title("Takip Ayarları")
        window.geometry("470x320")
        window.resizable(False, False)
        ctk.CTkLabel(window, text="Günlük Takip Limiti", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(20, 8))
        self.daily_limit_entry = ctk.CTkEntry(window, width=160, justify="center")
        self.daily_limit_entry.pack(pady=6)
        self.daily_limit_entry.insert(0, str(legacy_config.CONFIG.get("DAILY_LIMIT", 80)))
        ctk.CTkLabel(window, text="1–500 · Gecikmeler ve saatlik limit bu değere göre hesaplanır.",
                     wraplength=420, text_color=t.MUTED).pack(pady=8)
        ctk.CTkLabel(window, text="Tarayıcı ortak oturumda görünür açılır; giriş tarayıcıdan yapılır.\n"
                                    "Kayıtlı HEADLESS ayarı değiştirilmez.",
                     wraplength=420, text_color=t.MUTED).pack(pady=8)
        self.settings_save_btn = ctk.CTkButton(window, text="Kaydet", command=self._save_settings)
        self.settings_save_btn.pack(pady=12)
        self._settings_controls = [self.daily_limit_entry, self.settings_save_btn]
        return window

    def _save_settings(self):
        if self._is_busy():
            return
        try:
            limit = int(self.daily_limit_entry.get().strip())
            if not 1 <= limit <= 500:
                raise ValueError("Günlük limit 1 ile 500 arasında olmalı.")
            updated = dict(legacy_config.CONFIG, DAILY_LIMIT=limit)
            legacy_config.save_config(updated)
        except (ValueError, OSError) as exc:
            self.log(f"Takip ayarları kaydedilemedi: {exc}")
            return
        legacy_config.CONFIG["DAILY_LIMIT"] = limit
        legacy_config.DAILY_LIMIT = limit
        if self.limiter is not None:
            self.limiter.limit = limit
            self.limiter.hourly_limit = max(5, math.ceil(limit / 4))
        if self.stats is not None:
            self.stats.daily_limit = limit
            self.update_stats_panel()
        else:
            self._stats_snapshot.update(limit=limit, hourly_limit=max(5, math.ceil(limit / 4)))
        self.poll()
        self.log(f"Takip ayarları kaydedildi. Günlük limit: {limit}; saatlik limit: {max(5, math.ceil(limit / 4))}.")
        self._settings_window.destroy()
        self._settings_controls = []

    def _information_window(self, title, text):
        window = ctk.CTkToplevel(self, fg_color=t.BG)
        window.title(title)
        window.geometry("740x480")
        window.minsize(580, 350)
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(0, weight=1)
        output = ctk.CTkTextbox(window, wrap="word")
        output.grid(row=0, column=0, padx=15, pady=15, sticky="nsew")
        output.insert("1.0", text)
        output.configure(state="disabled")
        return window

    def open_history_window(self):
        records = DailyHistory().get_all()
        lines = [f"{item.get('date', '')} · Başarılı: {item.get('success', 0)} · "
                 f"Atlanan: {item.get('skipped', 0)} · Hata: {item.get('errors', 0)}"
                 for item in records]
        return self._information_window("Günlük Takip Geçmişi", "\n\n".join(lines) or "Henüz günlük takip kaydı yok.")

    def open_user_status_window(self):
        window = ctk.CTkToplevel(self, fg_color=t.BG)
        window.title("Kullanıcı Durumları")
        window.geometry("780x520")
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(1, weight=1)
        search = ctk.CTkEntry(window, placeholder_text="Kullanıcı adına göre ara")
        search.grid(row=0, column=0, padx=(15, 5), pady=15, sticky="ew")
        output = ctk.CTkTextbox(window, wrap="word")
        output.grid(row=1, column=0, columnspan=2, padx=15, pady=(0, 15), sticky="nsew")

        def refresh():
            try:
                records = get_user_statuses(limit=300, search=search.get())
                statuses = {"pending": "Sırada", "following": "İşleniyor", "followed": "Takip edildi",
                            "requested": "İstek gönderildi", "already_following": "Zaten takipte",
                            "requested_existing": "Önceden istek gönderilmiş", "retryable_error": "Tekrar denenebilir hata",
                            "excluded": "Elendi", "cancelled": "Durduruldu", "unconfirmed": "Sonuç doğrulanamadı"}
                reasons = {"attempt_started": "Takip kontrolü başladı", "profile_timeout": "Profil zamanında yüklenmedi",
                           "profile_relationship_confirmed": "İşlem hedef profil düğmesinde doğrulandı",
                           "profile_following_confirmed": "Hedef profilde takipte olduğu doğrulandı",
                           "profile_request_confirmed": "Hedef profilde bekleyen istek doğrulandı",
                           "verification_timeout": "Tıklama sonucu zamanında doğrulanamadı", "invalid_username": "Geçersiz kullanıcı adı",
                           "profile_control_missing": "Takip düğmesi bulunamadı", "profile_control_ambiguous": "Takip düğmesi belirsiz",
                           "rate_limited": "Instagram kısıtlaması algılandı", "rate_limited_after_action": "Tıklamadan sonra kısıtlama algılandı",
                           "stopped_before_action": "İşlem yapılmadan durduruldu", "stopped_after_action": "Tıklamadan sonra durduruldu"}
                text = "\n\n".join(f"@{row['username']} · {statuses.get(row['status'], row['status'])}\n"
                                     f"Neden: {reasons.get(row['reason'], row['reason'])} · Deneme: {row['attempts']}\n"
                                     f"{row['updated_at']}\n{row['detail']}" for row in records) or "Henüz bu aramaya uygun takip kaydı yok."
            except Exception as exc:
                text = f"Kullanıcı durumları okunamadı: {exc}"
            output.configure(state="normal")
            output.delete("1.0", "end")
            output.insert("1.0", text)
            output.configure(state="disabled")
        ctk.CTkButton(window, text="Ara / Yenile", width=120, command=refresh).grid(row=0, column=1, padx=(5, 15), pady=15)
        search.bind("<Return>", lambda event: refresh())
        refresh()
        return window
