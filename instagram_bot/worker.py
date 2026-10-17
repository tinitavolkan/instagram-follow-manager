"""Kuyruk, takip turları, iptal ve sonuçların kaydı."""
import math
import json
import queue
import random
import time
from datetime import datetime, timedelta
from . import browser
from .config import CONFIG
from .dashboard import reported_wait, reporting_scope, wait_description
from .following import FollowResult, follow_user
from .collection import CollectionTasks
from .logging_setup import APP_LOG
from .paths import BASE_DIR
from .storage import read_takiplist, write_to_karalist, remove_from_takiplist, record_user_status, atomic_write_text
from .timing import stoppable_wait

UNCONFIRMED_FOLLOW_STOP_THRESHOLD = 3
FOLLOW_SAFETY_MESSAGE = "Olası işlem kısıtlaması — hesabı kontrol et"
# Temporarily disabled for manual testing; a positive value restores pacing.
NON_FOLLOWERS_COOLDOWN = 0

class WorkerMixin(CollectionTasks):
    def _scan_blocks_other_task(self):
        if getattr(self, "non_followers_pending", False) or self.active_task == "non_followers":
            self.log("Geri takip taraması çalışıyor veya sırada. Yeni işlem için taramanın durmasını bekleyin.")
            return True
        return False

    def _non_followers_cooldown_remaining(self):
        """Read persistent pacing only when the scan cooldown is enabled."""
        if NON_FOLLOWERS_COOLDOWN <= 0:
            return 0
        path = BASE_DIR / "geri_takip_tarama.json"
        if not path.exists():
            return 0
        data = json.loads(path.read_text(encoding="utf-8"))
        stamp = data.get("last_attempt_at") if isinstance(data, dict) else None
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 0:
            raise ValueError("Tarama geçmişinde geçerli bir zaman kaydı yok.")
        return max(0, stamp + NON_FOLLOWERS_COOLDOWN - time.time())

    def _non_followers_can_start(self):
        # A manual read-only recheck may recover from the scanner's own stop
        # when scan-start checks allow it. It never clears follow protection.
        if (getattr(self, "follow_safety_stop", False)
                and not getattr(self, "non_followers_safety_stop", False)
                and self._follow_safety_blocks_task()):
            return False
        if time.time() < browser.rate_limit_lock_until:
            self._dashboard_task_failed("Instagram güvenlik kilidi etkin; geri takip taraması başlatılmadı.")
            self.log("Güvenlik kilidi etkin. Tarama otomatik olarak yeniden denenmeyecek.")
            return False
        try:
            remaining = self._non_followers_cooldown_remaining()
        except (OSError, ValueError, TypeError) as exc:
            self._dashboard_task_failed("Tarama geçmişi okunamadı; sık taramayı önleyen kontrol doğrulanamadı.")
            self.log(f"Geri takip taraması başlatılmadı: {exc}")
            return False
        if remaining:
            minutes = math.ceil(remaining / 60)
            self.log(f"Yeni geri takip taraması için {minutes} dakika bekleyin. Otomatik tekrar yapılmayacak.")
            self.dashboard_update(state="attention", detail=f"Tarama aralığı: {minutes} dakika sonra yeniden başlatabilirsiniz.",
                                  next_action="Hazır olduğunuzda Geri Takip Taraması penceresinden tekrar başlatın.")
            return False
        return True

    def _refresh_scan_window(self):
        refresh = getattr(self, "_refresh_non_followers_window", None)
        if refresh is not None:
            self.call_in_main_thread(refresh)

    def _publish_scan_result(self, result):
        self.non_followers_result = result
        self.call_in_main_thread(self._show_non_followers_result, result)

    def _scan_did_not_run(self, status, detail):
        from .non_followers import NonFollowersResult
        self._publish_scan_result(NonFollowersResult(
            status=status, detail=detail,
            checked_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        ))

    def non_followers_clicked(self):
        """One explicit, idle-only scan; never resume another stopped task."""
        if (getattr(self, "non_followers_pending", False) or self.active_task is not None
                or not self.task_queue.empty() or self.follow_pending):
            self.log("Önce çalışan veya sıradaki işlemin bitmesini bekleyin; tarama sıraya eklenmedi.")
            return
        if self.shutdown_event.is_set() or not self._non_followers_can_start():
            return
        self.non_followers_pending = True
        self.non_followers_result = None
        self.stop_event.clear()
        self._dashboard_task_queued("Geri takip taraması")
        self.task_queue.put("non_followers")
        self._refresh_scan_window()

    def _run_non_followers_task(self):
        if self.stop_event.is_set() or self.shutdown_event.is_set():
            self._scan_did_not_run("stopped", "Tarama başlamadan durduruldu; listeler okunmadı.")
            return
        if not self._non_followers_can_start():
            self._scan_did_not_run("incomplete", "Tarama başlatma kontrolü nedeniyle listeler okunmadı. Ana ekrandaki açıklamayı kontrol edin.")
            return
        # If pacing is enabled, reserve even failed attempts before browser access.
        if NON_FOLLOWERS_COOLDOWN > 0:
            atomic_write_text(BASE_DIR / "geri_takip_tarama.json", json.dumps({"last_attempt_at": time.time()}))
        if not self._ensure_bot_ready():
            self._scan_did_not_run("error", "Tarayıcı oturumu başlatılamadı. Giriş ve bağlantıyı kontrol edin.")
            self._dashboard_task_failed("Tarayıcı oturumu başlatılamadı; geri takip listeleri okunmadı.")
            return
        if self.stop_event.is_set() or self.shutdown_event.is_set():
            self._scan_did_not_run("stopped", "Tarama oturum hazırlanırken durduruldu; listeler okunmadı.")
            return
        from .non_followers import scan_non_followers
        self.dashboard_update(state="running", detail="Kendi hesabınızın takipçi ve takip edilen listeleri okunuyor.",
                              next_action="Durdur ile taramayı kesebilirsiniz. Listeler tamamlanınca karşılaştırılacak.")
        result = scan_non_followers(
            browser.page, self.stop_event, log_cb=self.log,
            progress_cb=lambda detail: self.dashboard_update(state="running", detail=detail),
        )
        self._publish_scan_result(result)
        if result.status == "completed":
            self.dashboard_update(state="completed", detail=f"Tarama tamamlandı: {len(result.not_following_back)} kişi geri takip etmiyor.",
                                  next_action="Geri Takip Taraması penceresinden sonuçları görüntüleyebilir veya kaydedebilirsiniz.")
        elif result.status == "restricted":
            self.non_followers_safety_stop = True
            self.follow_safety_stop = True
            self.stop_event.set()
            self.log(f"Tarama durdu: {result.detail}. Otomatik yeniden deneme yapılmayacak.")
            self._show_follow_safety_stop()
        elif result.status == "stopped":
            self.stop_event.set()
            self.dashboard_update(state="stopped", detail="Geri takip taraması durduruldu; karşılaştırma tamamlanmadı.",
                                  next_action="Eksik listelerden geri takip sonucu üretilmedi.")
        else:
            self.log(f"Geri takip karşılaştırması tamamlanamadı: {result.detail}")
            self._dashboard_task_failed(result.detail)

    def _show_follow_safety_stop(self):
        next_action = ("Tarama otomatik tekrarlanmaz. Hesabı kontrol ettikten sonra Geri Takip Taraması ile yeniden okuyabilirsiniz."
                       if getattr(self, "non_followers_safety_stop", False)
                       else "Otomatik denemeler durdu. Hesabı kontrol ettikten sonra Takip Et ile yeniden başlatabilirsin.")
        self.dashboard_update(
            state="attention", detail=FOLLOW_SAFETY_MESSAGE,
            next_action=next_action,
            remaining=len(getattr(self, "takipler", ())),
        )

    def _follow_safety_blocks_task(self):
        if not getattr(self, "follow_safety_stop", False):
            return False
        self._show_follow_safety_stop()
        self.log(f"🛑 {FOLLOW_SAFETY_MESSAGE}. Koruma duruşu etkin; yeni işlem başlatılmadı.")
        return True

    def dashboard_update(self, **fields):
        """Tkinter'a dokunmadan ana ekranın gözlem modelini güncelle."""
        dashboard = getattr(self, "dashboard", None)
        if dashboard is not None:
            if fields.get("state") == "running" and self.stop_event.is_set():
                return
            dashboard.update(**fields)

    def _dashboard_task_failed(self, detail):
        self.dashboard_update(state="attention", detail=detail,
                              next_action="Ayrıntıları işlem günlüğünden kontrol edin; hazır olduğunuzda yeniden başlatın.")

    def _dashboard_task_queued(self, title):
        # Yeni düğme tıklaması, çalışan görevin veya molanın görünümünü gizlemesin.
        if self.active_task is None and self.task_queue.empty():
            self.dashboard_update(state="ready", detail=f"{title} sıraya alındı.",
                                  next_action="Görev başlatılınca durum burada güncellenecek.")

    def _dashboard_task_finished(self, task):
        if task != "non_followers" and getattr(self, "follow_safety_stop", False):
            self._show_follow_safety_stop()
            return
        dashboard = getattr(self, "dashboard", None)
        if dashboard is None or dashboard.snapshot()["state"] == "attention":
            return
        remaining = len(getattr(self, "takipler", ()))
        if self.stop_event.is_set() or self.shutdown_event.is_set():
            detail = ("Geri takip taraması durduruldu; karşılaştırma tamamlanmadı." if task == "non_followers"
                      else "İşlem durduruldu; kalan kullanıcılar korundu.")
            self.dashboard_update(state="stopped", detail=detail,
                                  next_action="Devam etmek için ilgili işlemi yeniden başlatın.", remaining=remaining)
        elif task == "login":
            self.dashboard_update(state="ready", detail="Tarayıcı oturumu hazır.",
                                  next_action="Takip veya toplama işlemini başlatabilirsiniz.", remaining=remaining)
        elif task == "follow" and remaining:
            self._dashboard_task_failed(f"Otomatik denemeler bitti; {remaining} kullanıcı listede kaldı.")
        elif dashboard.snapshot()["state"] != "completed":
            detail = "Takip tamamlandı; işlenecek kullanıcı kalmadı." if task == "follow" else "Toplama işlemi tamamlandı."
            self.dashboard_update(state="completed", detail=detail,
                                  next_action="Sonuçları kontrol edebilir veya yeni bir işlem başlatabilirsiniz.", remaining=remaining)

    def _ensure_bot_ready(self) -> bool:
        """Worker thread içinde oturumu açar; istenen görevi kaybetmeden devam eder."""
        if self.bot_ready and browser.page is not None:
            return True
        try:
            self.dashboard_update(state="running", detail="Tarayıcı oturumu hazırlanıyor.",
                                  next_action="Oturum hazır olduğunda seçilen işleme devam edilecek.")
            self.log("Bot oturumu hazırlanıyor…")
            browser.run_bot(self.log)
            self.bot_ready = True
            if self.start_time is None:
                self.start_time = time.time()
            self.log("✅ Bot oturumu hazır.")
            return True
        except Exception as e:
            self.bot_ready = False
            self.log(f"❌ Oturum başlatılamadı: {e}")
            self.stats.record_error()
            self.update_stats_panel()
            self._dashboard_task_failed("Tarayıcı oturumu başlatılamadı; bağlantı ve oturumu kontrol edin.")
            return False

    def start_clicked(self):
        if self._scan_blocks_other_task():
            return
        if self._follow_safety_blocks_task():
            return
        self.stop_event.clear()
        self._dashboard_task_queued("Oturum açma")
        self.task_queue.put("login")

    def stop_clicked(self):
        self.stop_event.set()
        if self._follow_safety_blocks_task():
            return
        if self.active_task is not None or not self.task_queue.empty():
            self.dashboard_update(state="stopping", detail="Durdurma isteği alındı; mevcut işlemin bitmesi bekleniyor.",
                                  next_action="İşlem durunca burada Durduruldu yazacak; kalan kullanıcılar korunacak.")
        else:
            self.dashboard_update(state="stopped", detail="Program durduruldu.",
                                  next_action="Devam etmek için ilgili işlemi yeniden başlatın.")
        self.log("⏹️ Durdur komutu alındı.")

    def follow_clicked(self):
        if self._scan_blocks_other_task():
            return
        if self.active_task == "follow" or self.follow_pending:
            if self.stop_event.is_set():
                self.log("ℹ️ Önceki takip görevi durduruluyor; durduktan sonra Takip Et ile yeniden başlatabilirsiniz.")
            else:
                self.log("ℹ️ Takip görevi zaten çalışıyor veya sırada; mola sonrası otomatik devam edecek.")
            return
        if getattr(self, "follow_safety_stop", False):
            self.log("ℹ️ Koruma duruşu, kullanıcının Takip Et komutuyla kaldırıldı. Bu, Instagram kısıtlamasının kalktığını doğrulamaz.")
        self.follow_safety_stop = False
        self.non_followers_safety_stop = False
        self.stop_event.clear()
        self._dashboard_task_queued("Takip işlemi")
        self.follow_pending = True
        self.task_queue.put("follow")

    def trigger_daily_limit_button(self):
        """Yeni gün başladığında Takip Et görevini bir kere yeniden başlatır."""
        if self._follow_safety_blocks_task():
            return
        try:
            self.follow_btn.invoke()
        except Exception:
            self.follow_clicked()

    def topla_clicked(self):
        if self._scan_blocks_other_task():
            return
        if self._follow_safety_blocks_task():
            return
        self.stop_event.clear()
        self._dashboard_task_queued("Beğenilerden toplama")
        self.task_queue.put("topla")

    def topla2_clicked(self):
        if self._scan_blocks_other_task():
            return
        if self._follow_safety_blocks_task():
            return
        self.stop_event.clear()
        self._dashboard_task_queued("Kullanıcı gönderilerinden toplama")
        self.task_queue.put("topla2")

    def gonderiden_topla_clicked(self):
        if self._scan_blocks_other_task():
            return
        if self._follow_safety_blocks_task():
            return
        self.stop_event.clear()
        self._dashboard_task_queued("Gönderilerden toplama")
        filter_state = "açık" if CONFIG.get("GONDERIDEN_EXCLUDE_FOLLOWING", True) else "kapalı"
        self.log(f"⚙ Gönderilerden Topla takip filtresi: {filter_state}.")
        self.task_queue.put("gonderiden_topla")

    def _wait_with_status(self, seconds: float, reason: str) -> bool:
        """Uzun molalarda kalan süreyi göster; Durdur/kapat ile beklemeyi kes."""
        seconds = max(0, seconds)
        deadline = time.monotonic() + seconds
        finish_at = datetime.now() + timedelta(seconds=seconds)
        self.log(f"⏸️ {reason}. Planlanan bitiş: {finish_at:%H:%M:%S}.")
        with reported_wait(seconds, reason, stop_event=self.stop_event,
                           dashboard=getattr(self, "dashboard", None)):
            while True:
                if self.stop_event.is_set() or self.shutdown_event.is_set():
                    self.log("⏹️ Mola durduruldu; otomatik yeniden deneme iptal edildi.")
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.log(f"✅ {reason} bitti. İşleme devam ediliyor.")
                    return False
                if stoppable_wait(self.stop_event, min(60, remaining)):
                    self.log("⏹️ Mola durduruldu; otomatik yeniden deneme iptal edildi.")
                    return True
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self.log(f"⏳ {reason}: {math.ceil(remaining)} saniye kaldı.")

    def _wait_briefly(self, seconds, reason):
        with reported_wait(seconds, reason, stop_event=self.stop_event,
                           dashboard=getattr(self, "dashboard", None)):
            return stoppable_wait(self.stop_event, seconds)

    def _follow_candidates(self):
        """Kalan kullanıcıları sınırlı turlarla yeniden dene; sessizce bitirme."""
        max_rounds = 3
        for round_no in range(1, max_rounds + 1):
            if self.stop_event.is_set() or self.shutdown_event.is_set():
                self.log("⏹️ Takip işlemi durduruldu; kalan kullanıcılar listede korundu.")
                return
            if not self.takipler:
                self.log("✅ Takip işlemi tamamlandı. Listede işlenecek kullanıcı kalmadı.")
                return
            if round_no > 1:
                self.log(
                    f"🔄 Önceki tur bitti; {len(self.takipler)} kullanıcı listede kaldı. "
                    f"{round_no}/{max_rounds}. tur otomatik başlayacak."
                )
                if self._wait_with_status(180 * (round_no - 1), "Yeniden deneme molası"):
                    return
            candidates = list(dict.fromkeys(self.takipler))
            random.shuffle(candidates)
            self.dashboard_update(state="running", detail=f"Takip turu {round_no}/{max_rounds} işleniyor.",
                                  next_action="Listedeki kullanıcılar sırayla kontrol edilecek.", remaining=len(self.takipler))
            self.log(f"▶ Takip turu {round_no}/{max_rounds}: {len(candidates)} kullanıcı.")
            for index, user in enumerate(candidates, start=1):
                if self.stop_event.is_set() or self.shutdown_event.is_set():
                    self.log("⏹️ Takip işlemi durduruldu; kalan kullanıcılar listede korundu.")
                    return
                if user in self.takipler:
                    yield round_no, index, len(candidates), user

        if self.stop_event.is_set() or self.shutdown_event.is_set():
            self.log("⏹️ Takip işlemi durduruldu; kalan kullanıcılar listede korundu.")
        elif self.takipler:
            self._dashboard_task_failed(f"{max_rounds} takip turu bitti; {len(self.takipler)} kullanıcı listede korundu.")
            self.log(
                f"⚠️ {max_rounds} takip turu tamamlandı. {len(self.takipler)} kullanıcı "
                "hâlâ işlenemedi ve Takiplist.txt içinde korundu. Otomatik denemeler "
                "durduruldu; oturumu kontrol ettikten sonra Takip Et ile yeniden başlatabilirsiniz."
            )
        else:
            self.log("✅ Takip işlemi tamamlandı. Listede işlenecek kullanıcı kalmadı.")

    def _run_follow_task(self):
        if self._follow_safety_blocks_task():
            return
        if not self._ensure_bot_ready():
            return
        self.takipler = read_takiplist()
        self.dashboard_update(state="running", detail="Takip listesi işleniyor.",
                              next_action="Listedeki kullanıcılar sırayla kontrol edilecek.", remaining=len(self.takipler))
        self.refresh_listbox()
        self.log("Takip listesi işleniyor…")
        private_follow_count = 0
        consecutive_skips = 0
        consecutive_unconfirmed_actions = 0
        for round_no, index, total, user in self._follow_candidates():
            if self.stop_event.is_set():
                break
            if time.time() < browser.rate_limit_lock_until:
                remaining = max(1, browser.rate_limit_lock_until - time.time())
                if self._wait_with_status(remaining, "Global güvenlik kilidi"):
                    break
            if self.limiter.daily_limit_reached():
                wait_sec = self.limiter.seconds_until_next_day()
                self.log(f"🛑 Günlük limit doldu. Yeni güne kadar {math.ceil(wait_sec / 60)} dakika bekleniyor.")
                with wait_description("Günlük takip limitine ulaşıldı.", "Yeni gün başlayınca takibe devam edilecek."):
                    if browser.perform_safe_wait(wait_sec + 5, self.log, return_to_insta=True, stop_event=self.stop_event):
                        break
                self.limiter.check_daily_reset()
                self.log("✅ Yeni gün başladı, günlük limit sıfırlandı. Takibe devam ediliyor.")
            if self.limiter.hourly_limit_reached():
                wait_sec = self.limiter.seconds_until_next_hour()
                self.log(f"⏳ Saatlik limit doldu. {math.ceil(wait_sec / 60)} dakika bekleniyor.")
                with wait_description("Saatlik takip limitine ulaşıldı.", "Yeni saat başlayınca takibe devam edilecek."):
                    if browser.perform_safe_wait(wait_sec + 5, self.log, return_to_insta=True, stop_event=self.stop_event):
                        break
                self.limiter.check_hourly_reset()
                self.log("✅ Saat değişti, takip işlemine devam ediliyor.")
            if user not in self.takipler or self.stop_event.is_set():
                continue
            if random.random() < 0.05:
                self.log(f"🤔 {user} üzerinde düşünülüyor...")
                if self._wait_briefly(random.uniform(15, 30), "Profil kontrolü öncesi kısa bekleme."):
                    break

            record_user_status(user, "following", "attempt_started", f"Tur {round_no}/3", increment_attempt=True)
            self.dashboard_update(state="running", detail=f"@{user} kontrol ediliyor · Tur {round_no}/3 · {index}/{total}",
                                  next_action="Sonuç doğrulanınca listedeki sonraki kullanıcıya geçilecek.", remaining=len(self.takipler))
            self.log(f"[Tur {round_no}, {index}/{total}] {user} takip ediliyor…")
            try:
                result = follow_user(user, self.log, stop_event=self.stop_event)
            except Exception as exc:
                APP_LOG.exception("Takip işlemi hatası: %s", user)
                self.stats.record_error()
                result = FollowResult("retryable_error", "unexpected_error", str(exc))
            self._record_follow_result(user, result)

            # Onaylanmış sonucu kaydettikten sonra dur; son başarılı işlem kaybolmasın.
            if self.stop_event.is_set() or self.shutdown_event.is_set() or result.status == "cancelled":
                self.log("⏹️ Takip işlemi durduruldu; son kullanıcının durumu kaydedildi.")
                break
            # Yalnız gerçekten gönderilmiş yeni takip işlemlerini karşılaştır.
            # Önceden var olan ilişkiler ve tıklama öncesi hatalar kısıtlamanın
            # kalktığını kanıtlamaz; sayacı ne artırır ne de sıfırlarlar.
            if result.status in {"followed", "requested"}:
                consecutive_unconfirmed_actions = 0
            elif result.status == "unconfirmed" and result.action_submitted:
                consecutive_unconfirmed_actions += 1
                self.log(
                    f"⚠️ Doğrulanamayan yeni takip işlemi: "
                    f"{consecutive_unconfirmed_actions}/{UNCONFIRMED_FOLLOW_STOP_THRESHOLD}."
                )
                if consecutive_unconfirmed_actions >= UNCONFIRMED_FOLLOW_STOP_THRESHOLD:
                    self.follow_safety_stop = True
                    self.stop_event.set()
                    self._show_follow_safety_stop()
                    self.log(
                        f"🛑 {FOLLOW_SAFETY_MESSAGE}. Art arda "
                        f"{consecutive_unconfirmed_actions} takip tıklamasının sonucu doğrulanamadı. "
                        "Takip görevi sonlandırıldı; kalan kişiler listede korundu. "
                        "Bu bir kesin engel tespiti değildir. Otomatik yeniden deneme yapılmayacak."
                    )
                    break
            if result.status == "followed":
                private_follow_count = consecutive_skips = 0
                delay = self.limiter.next_delay()
                self.stats.record_delay(delay)
                self.log(f"⏳ Bekleniyor: {int(delay)} saniye")
                browser.perform_safe_wait(delay, self.log, return_to_insta=False, stop_event=self.stop_event)
            elif result.status == "requested":
                private_follow_count += 1
                consecutive_skips = 0
                self.log(f"⚠️ Private İstek Sayısı: {private_follow_count}/5")
                if private_follow_count >= 5:
                    if self._wait_with_status(30 * 60, "Çok fazla private hesap; 30 dakika mola"):
                        break
                    private_follow_count = 0
                else:
                    delay = self.limiter.next_delay()
                    self.log(f"⏳ Private bekleme: {int(delay)} saniye")
                    browser.perform_safe_wait(delay, self.log, return_to_insta=False, stop_event=self.stop_event)
            elif result.status in {"already_following", "requested_existing"}:
                consecutive_skips = 0
                if result.status == "already_following":
                    private_follow_count = 0
                delay = random.uniform(60, 120) if result.status == "already_following" else random.uniform(2, 6)
                browser.perform_safe_wait(delay, self.log, return_to_insta=False, stop_event=self.stop_event)
            else:
                consecutive_skips += 1
                self.log(f"⚡ Skip (Toplam Skip: {consecutive_skips}) — {result.reason}: {result.detail}")
                if consecutive_skips >= 5:
                    if self._wait_with_status(180, "Çok fazla skip (5), 3 dk mola"):
                        break
                    consecutive_skips = 0
                elif self._wait_briefly(random.uniform(5, 10), "Sonraki kullanıcı öncesi kısa bekleme."):
                    break
            self.update_stats_panel()
        self._dashboard_task_finished("follow")

    def _record_follow_result(self, user, result: FollowResult):
        """Sonucu önce kalıcı kaydet; yalnız kesin terminal durumda listeden çıkar."""
        detail = result.detail
        if result.action_submitted:
            detail = (detail + " | Takip düğmesine basıldı.").strip()
        record_user_status(user, result.status, result.reason, detail)
        if result.status in {"followed", "requested"}:
            dashboard = getattr(self, "dashboard", None)
            if dashboard is not None:
                dashboard.mark_success(user)
        self.log(f"📝 {user}: {result.status} — {result.reason}. {result.detail}")
        terminal = {"followed", "requested", "already_following", "requested_existing", "excluded"}
        if result.status in terminal:
            write_to_karalist(user)
            remove_from_takiplist(user)
            self.takipler = read_takiplist()
            self.refresh_listbox()
        self.dashboard_update(remaining=len(self.takipler))
        if result.status in {"followed", "requested"}:
            self.stats.record_result(True)
            self.limiter.mark_follow()
        elif result.status != "cancelled":
            self.stats.record_result(False)
        self.update_stats_panel()

    def worker_loop(self):
        actions = {
            "login": self._ensure_bot_ready,
            "follow": self._run_follow_task,
            "topla": self.collect_likes,
            "topla2": self.collect_user_posts,
            "gonderiden_topla": self.collect_post_links,
            "non_followers": self._run_non_followers_task,
        }
        while True:
            try:
                task = self.task_queue.get(timeout=0.5)
            except queue.Empty:
                if self.shutdown_event.is_set():
                    browser.close_bot()
                    break
                continue
            self.active_task = task
            try:
                if task == "shutdown":
                    browser.close_bot()
                    break
                if self.shutdown_event.is_set():
                    continue
                # Daha önce sıraya giren işler koruma duruşunu aşamaz.
                if task != "non_followers" and self._follow_safety_blocks_task():
                    continue
                titles = {"login": "Oturum hazırlanıyor.", "follow": "Takip görevi başlıyor.",
                          "topla": "Beğenilerden kullanıcı toplanıyor.", "topla2": "Kullanıcının gönderileri işleniyor.",
                          "gonderiden_topla": "Gönderilerden kullanıcı toplanıyor.",
                          "non_followers": "Geri takip taraması başlıyor."}
                self.dashboard_update(state="running", detail=titles.get(task, "Görev çalışıyor."),
                                      next_action="Görevin ilerlemesi burada gösterilecek.")
                with reporting_scope(getattr(self, "dashboard", None)):
                    if time.time() < browser.rate_limit_lock_until:
                        if task == "non_followers":
                            self._dashboard_task_failed("Instagram güvenlik kilidi etkin; tarama başlatılmadı.")
                            continue
                        remaining = max(1, browser.rate_limit_lock_until - time.time())
                        self.log(f"🔒 Global güvenlik kilidi aktif. {remaining/60:.1f} dk bekleniyor.")
                        if self._wait_briefly(remaining, "Global güvenlik kilidinin bitmesi bekleniyor."):
                            continue
                    actions[task]()
            except Exception as exc:
                self.stop_event.set()
                APP_LOG.exception("Görev hatası: %s", task)
                self.log(
                    f"❌ {task} işlemi hata nedeniyle durduruldu: {type(exc).__name__}: {exc}. "
                    "Ayrıntı: instagramtakip.log. Sorun giderildikten sonra yeniden başlatabilirsiniz."
                )
                self._dashboard_task_failed(f"İşlem hata nedeniyle durdu: {type(exc).__name__}: {exc}")
                if task == "non_followers":
                    self._scan_did_not_run("error", f"Tarama hata nedeniyle durdu: {type(exc).__name__}: {exc}")
                try:
                    self.stats.record_error()
                    self.update_stats_panel()
                except Exception:
                    APP_LOG.exception("Hata istatistiği güncellenemedi")
            finally:
                if task != "shutdown":
                    self._dashboard_task_finished(task)
                self.active_task = None
                if task == "follow":
                    self.follow_pending = False
                if task == "non_followers":
                    self.non_followers_pending = False
                    self._refresh_scan_window()
                self.task_queue.task_done()
