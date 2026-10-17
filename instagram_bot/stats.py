"""Günlük/saatlik sınırlar, istatistik ve geçmiş kayıtları."""
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any
from .paths import BASE_DIR, STATS_FILE, HISTORY_FILE
from .config import DAILY_LIMIT, _scaled
from .storage import atomic_write_text

class DailyLimiter:
    def __init__(self, limit: int = DAILY_LIMIT, file_name: str = "daily_stats.json"):
        self.limit = limit
        # Saatlik limit: günlük limitin 1/4'ü (yani limiti ~4 saatte doldurur).
        # Minimum 5, maksimum sınır yok — DAILY_LIMIT ile otomatik ölçeklenir.
        self.hourly_limit = max(5, math.ceil(limit / 4))
        self.file = BASE_DIR / file_name
        self.today = str(date.today())
        self.count = 0
        self.hourly_count = 0
        self.last_hour = datetime.now().hour
        self._load()

    def _load(self):
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                if data.get("date") == self.today: self.count = data.get("count", 0)
            except Exception: self.count = 0

    def _save(self):
        atomic_write_text(
            self.file,
            json.dumps({"date": self.today, "count": self.count}, ensure_ascii=False),
            encoding="utf-8",
        )

    def check_daily_reset(self):
        """Gün değiştiğinde günlük ve saatlik sayaçları sıfırlar."""
        current_day = str(date.today())
        if current_day != self.today:
            self.today = current_day
            self.count = 0
            self.hourly_count = 0
            self.last_hour = datetime.now().hour
            self._save()
            return True
        return False

    def check_hourly_reset(self):
        """Saat değiştiğinde sayaçları sıfırlar."""
        self.check_daily_reset()
        current_hour = datetime.now().hour
        if current_hour != self.last_hour:
            self.hourly_count = 0
            self.last_hour = current_hour
            return True
        return False

    def daily_limit_reached(self) -> bool:
        """Günlük limit dolduysa True döner."""
        self.check_daily_reset()
        return self.count >= self.limit

    def hourly_limit_reached(self) -> bool:
        """Saatlik limit dolduysa True döner."""
        self.check_hourly_reset()
        return self.hourly_count >= self.hourly_limit

    def seconds_until_next_hour(self) -> int:
        """Bir sonraki saate kaç saniye kaldığını döner."""
        now = datetime.now()
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return max(1, int((next_hour - now).total_seconds()))

    def seconds_until_next_day(self) -> int:
        """Bir sonraki güne kaç saniye kaldığını döner."""
        now = datetime.now()
        next_day = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return max(1, int((next_day - now).total_seconds()))

    def can_follow(self) -> bool:
        self.check_hourly_reset()
        return self.count < self.limit and self.hourly_count < self.hourly_limit

    def mark_follow(self):
        self.count += 1
        self.hourly_count += 1
        self._save()

    def next_delay(self) -> float:
        """
        Takipler arası bekleme süresini DAILY_LIMIT'e göre otomatik ölçekler.
        DAILY_LIMIT ne kadar yüksekse bekleme o kadar kısalır.
        Aşama eşikleri de limite orantılı hesaplanır.
        """
        phase1 = max(5,  math.ceil(self.limit * 0.12))   # ilk ~%12
        phase2 = max(15, math.ceil(self.limit * 0.37))   # ilk ~%37

        if self.count < phase1:
            # Isınma aşaması
            lo, hi = 180, 240
        elif self.count < phase2:
            # Stabilize aşaması
            lo, hi = 240, 360
        else:
            # Temkinli aşama
            lo, hi = 360, 480

        # Minimum gecikme: Isınma=20s, Stabilize=30s, Temkinli=45s
        if self.count < phase1:
            return _scaled(lo, hi, min_lo=20, min_hi=40)
        elif self.count < phase2:
            return _scaled(lo, hi, min_lo=30, min_hi=55)
        else:
            return _scaled(lo, hi, min_lo=45, min_hi=75)

class Stats:
    def __init__(self, daily_limit: int):
        self.daily_limit = daily_limit
        self.delay_history: List[float] = []
        self.max_history = 10
        self.date = str(date.today())
        self.file = STATS_FILE
        self.history = DailyHistory()
        self.success = 0; self.skipped = 0; self.errors = 0
        self._load()
    def _load(self):
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                if data.get("date") == self.date:
                    self.success = data.get("success", 0)
                    self.skipped = data.get("skipped", 0)
                    self.errors = data.get("errors", 0)
                    return

                # Eski sürüm uzun süre açık kaldığında Stats.date yenilenmediği için
                # sayaçlar günler boyunca birikiyordu. Kümülatif aralığı son kayıt
                # gününe kadar günlük değerlere dönüştür; uygulama daha sonra açılsa
                # bile geçmiş veriler doğru şekilde kurtarılsın.
                stored_date = str(data.get("date", ""))
                last_update_date = str(data.get("last_update", "")).split("T", 1)[0]
                if stored_date and last_update_date and stored_date < last_update_date:
                    self.history.migrate_cumulative_range(stored_date, last_update_date)
                    today_record = next(
                        (rec for rec in self.history.records if rec.get("date") == self.date),
                        None,
                    )
                    if today_record:
                        self.success = int(today_record.get("success", 0))
                        self.skipped = int(today_record.get("skipped", 0))
                        self.errors = int(today_record.get("errors", 0))

                # Bayat stats.json dosyasını bugünün şemasıyla hemen yenile.
                self._save()
            except Exception:
                self.success = self.skipped = self.errors = 0
    def _save(self):
        try:
            atomic_write_text(
                self.file,
                json.dumps({"date": self.date, "success": self.success, "skipped": self.skipped, "errors": self.errors, "last_update": datetime.now().isoformat(timespec="seconds")}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.history.record(self.success, self.skipped, self.errors)
        except Exception: pass
    def check_daily_reset(self) -> bool:
        current_date = str(date.today())
        if current_date == self.date:
            return False
        self.date = current_date
        self.success = self.skipped = self.errors = 0
        self.delay_history.clear()
        self._save()
        return True
    def record_result(self, success: bool):
        self.check_daily_reset()
        if success: self.success += 1
        else: self.skipped += 1
        self._save()
    def record_error(self):
        self.check_daily_reset()
        self.errors += 1
        self._save()
    def record_delay(self, d: float):
        if d is None or d <= 0: return
        self.delay_history.append(d)
        if len(self.delay_history) > self.max_history: self.delay_history.pop(0)
    def avg_delay(self):
        self.check_daily_reset()
        if not self.delay_history: return None
        return sum(self.delay_history) / len(self.delay_history)

class DailyHistory:
    """Her günün istatistiğini daily_history.json dosyasına kalıcı olarak kaydeder."""

    MAX_DAYS = 90  # En fazla 90 günlük geçmiş sakla

    def __init__(self):
        self.file = HISTORY_FILE
        self.records: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        return []

    def _save(self):
        try:
            payload = json.dumps(self.records, indent=2, ensure_ascii=False)
            atomic_write_text(self.file, payload)
        except Exception:
            pass

    def record(self, success: int, skipped: int, errors: int):
        """Bugünkü kaydı ekler ya da günceller."""
        today = str(date.today())
        for rec in self.records:
            if rec.get("date") == today:
                rec["success"] = success
                rec["skipped"] = skipped
                rec["errors"] = errors
                rec["last_update"] = datetime.now().strftime("%H:%M:%S")
                self._save()
                return
        # Yeni gün
        self.records.append({
            "date": today,
            "success": success,
            "skipped": skipped,
            "errors": errors,
            "last_update": datetime.now().strftime("%H:%M:%S"),
        })
        # Eski kayıtları temizle
        self.records = sorted(self.records, key=lambda r: r["date"], reverse=True)[:self.MAX_DAYS]
        self._save()

    def get_all(self) -> List[Dict[str, Any]]:
        """Tarihe göre en yeni önce sıralanmış kayıtları döner."""
        return sorted(self.records, key=lambda r: r["date"], reverse=True)

    def migrate_cumulative_range(self, start_date: str, end_date: str) -> bool:
        """Eski sürümün günler boyunca biriktirdiği sayaçları günlük farklara çevirir."""
        selected = sorted(
            (
                rec for rec in self.records
                if start_date <= str(rec.get("date", "")) <= end_date
            ),
            key=lambda rec: rec.get("date", ""),
        )
        if len(selected) < 2:
            return False

        previous_raw = None
        changed = False
        for record in selected:
            current_raw = {
                key: max(0, int(record.get(key, 0) or 0))
                for key in ("success", "skipped", "errors")
            }
            if previous_raw is not None:
                for key, current_value in current_raw.items():
                    previous_value = previous_raw[key]
                    daily_value = (
                        current_value - previous_value
                        if current_value >= previous_value
                        else current_value
                    )
                    if int(record.get(key, 0) or 0) != daily_value:
                        record[key] = daily_value
                        changed = True
            previous_raw = current_raw

        if changed:
            self._save()
        return changed

    def total_follows(self) -> int:
        return sum(r.get("success", 0) for r in self.records)
