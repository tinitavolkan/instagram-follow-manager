"""Beğenenler penceresi ve üç kullanıcı toplama iş akışı."""
import random
import re
import time
from typing import Callable, List
from . import browser
from .config import CONFIG
from .paths import TAKIPLIST_PATH, KARALIST_PATH, KULLANICI_PATH, IG_URL
from .storage import (
    read_takiplist, read_all_post_links, append_to_takiplist,
    save_takiplist, record_user_status,
)

def open_likers_dialog(page, log_cb: Callable[[str], None] | None = None, timeout_ms: int = 20000) -> bool:
    """
    Beğenenler (likers) penceresini açmayı dener.

    Instagram web UI sürekli değişebildiği için:
      - /liked_by linki (a[href*="/liked_by/"])
      - like sayısı butonu (span/div[role="button"] gibi; örn "3,6 B")
    varyasyonlarını sırayla dener.

    Bu sürüm DEBUG için ekstra log basar:
      - aday selector sayıları
      - yakalanan adayların ilk text'leri
      - tıklama hataları
    """
    like_count_re = re.compile(r"^\s*[\d.,]+\s*(B|K|M|MN|Mn|m|k|b)?\s*$", re.UNICODE)

    def _log(msg: str):
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass

    def _dump_candidates(tag: str = ""):
        """DOM'da gerçekten ne var görmek için kısa bir özet basar."""
        try:
            url = page.url
        except Exception:
            url = "?"
        try:
            dlg_n = page.locator("div[role='dialog']").count()
        except Exception:
            dlg_n = -1
        try:
            art_n = page.locator("article").count()
        except Exception:
            art_n = -1

        _log(f"🧩 DEBUG[{tag}] url={url} dialogs={dlg_n} articles={art_n}")

        probes = [
            ("a_liked_by", "a[href*='/liked_by/']"),
            ("span_role_btn_tab0", "span[role='button'][tabindex='0']"),
            ("span_role_btn_any", "span[role='button']"),
            ("div_role_btn", "div[role='button']"),
            ("article_span_tab0", "article span[role='button'][tabindex='0']"),
            ("dlg_span_tab0", "div[role='dialog'] span[role='button'][tabindex='0']"),
        ]
        for name, sel in probes:
            try:
                c = page.locator(sel).count()
                _log(f"   • {name}: {c}")
            except Exception as e:
                _log(f"   • {name}: err {e}")

        # Sayısal görünen rol=button'ların örnek textlerini al
        try:
            samples = page.evaluate(r"""
                () => {
                  const out = [];
                  const els = Array.from(document.querySelectorAll('span[role="button"],div[role="button"],a[role="button"]'));
                  for (const e of els) {
                    const t = (e.innerText || '').replace(/\u00a0/g,' ').trim();
                    if (!t) continue;
                    if (!/[0-9]/.test(t)) continue;
                    if (!/^[0-9.,\s]+([BKMbkm]|Mn|MN)?$/.test(t)) continue;
                    out.push({t, tag: e.tagName.toLowerCase(), cls: (e.className||'').toString().slice(0,80)});
                    if (out.length >= 12) break;
                  }
                  return out;
                }
            """)
            if samples:
                _log("   • numeric role=button samples:")
                for s in samples:
                    _log(f"     - <{s.get('tag')}> '{s.get('t')}' cls='{s.get('cls')}'")
            else:
                _log("   • numeric role=button samples: (yok)")
        except Exception as e:
            _log(f"   • sample evaluate err: {e}")

    def _robust_click(loc, label: str):
        try:
            if loc.count() == 0:
                _log(f"   ⤷ {label}: count=0")
                return False
        except Exception:
            pass

        # görünür mü / text nedir?
        try:
            txt = loc.inner_text(timeout=1500).replace("\u00a0", " ").strip()
            _log(f"🔎 Aday ({label}) text='{txt[:80]}'")
        except Exception as e:
            _log(f"🔎 Aday ({label}) text alınamadı: {e}")

        # scroll
        try:
            loc.scroll_into_view_if_needed(timeout=timeout_ms)
        except Exception as e:
            _log(f"   ⤷ {label}: scroll err: {e}")

        # normal click
        try:
            loc.click(timeout=timeout_ms)
            _log(f"   ✅ {label}: click OK")
            return True
        except Exception as e:
            _log(f"   ⚠ {label}: normal click err: {e}")

        # force click
        try:
            loc.click(timeout=timeout_ms, force=True)
            _log(f"   ✅ {label}: force click OK")
            return True
        except Exception as e:
            _log(f"   ⚠ {label}: force click err: {e}")

        # mouse click center
        try:
            box = loc.bounding_box()
            if box:
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                page.mouse.move(x, y)
                page.mouse.click(x, y)
                _log(f"   ✅ {label}: mouse center click OK")
                return True
        except Exception as e:
            _log(f"   ⚠ {label}: mouse click err: {e}")

        # JS dispatch
        try:
            el = loc.element_handle(timeout=2000)
            page.evaluate(
                """(e)=>{ if(!e) return false;
                    try{ e.scrollIntoView({block:'center', inline:'center'});}catch(_){}
                    try{ e.dispatchEvent(new MouseEvent('mouseover',{bubbles:true,cancelable:true,view:window})); }catch(_){}
                    try{ e.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true,view:window})); }catch(_){}
                    try{ e.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,cancelable:true,view:window})); }catch(_){}
                    try{ e.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window})); }catch(_){}
                    try{ e.click(); }catch(_){}
                    return true;
                }""",
                el
            )
            _log(f"   ✅ {label}: JS dispatch denendi")
            return True
        except Exception as e:
            _log(f"   ❌ {label}: JS dispatch err: {e}")

        return False

    def _wait_likers_ready(before_dialogs: int) -> bool:
        # 1) dialog sayısı arttıysa tamam
        try:
            page.wait_for_function(
                "(n) => document.querySelectorAll('div[role=\"dialog\"]').length > n",
                arg=before_dialogs,
                timeout=min(timeout_ms, 8000)
            )
            return True
        except Exception:
            pass

        # 2) aynı dialog içinde içerik değişmiş olabilir: son dialog'da profil linkleri geldi mi?
        try:
            page.wait_for_function(
                """() => {
                    const ds = Array.from(document.querySelectorAll('div[role="dialog"]'));
                    if (!ds.length) return false;
                    const d = ds[ds.length-1];
                    const links = d.querySelectorAll('a[href^="/"]').length;
                    // Likers listesinde genelde en az birkaç profil linki olur
                    return links >= 3;
                }""",
                timeout=min(timeout_ms, 8000)
            )
            return True
        except Exception:
            return False

    # --- Başlangıç probe ---
    _dump_candidates("before")

    try:
        before_dialogs = page.locator("div[role='dialog']").count()
    except Exception:
        before_dialogs = 0

    # Adaylar: önce net olanlar
    candidates = [
        ("liked_by_link", page.locator("a[href*='/liked_by/']").first),
        # Post zaten modal'da ise önce son dialog içini dene
        ("like_span_in_last_dialog", page.locator("div[role='dialog'] span[role='button'][tabindex='0']").filter(has_text=like_count_re).last),
        ("like_span_in_article", page.locator("article span[role='button'][tabindex='0']").filter(has_text=like_count_re).first),
        ("like_span_global", page.locator("span[role='button'][tabindex='0']").filter(has_text=like_count_re).first),
        # tabindex yoksa
        ("like_span_anytab", page.locator("span[role='button']").filter(has_text=like_count_re).first),
        ("like_div_rolebtn", page.locator("div[role='button']").filter(has_text=like_count_re).first),
    ]

    for name, loc in candidates:
        clicked = _robust_click(loc, name)
        if not clicked:
            continue

        if _wait_likers_ready(before_dialogs):
            _log("✅ Beğenenler penceresi açıldı (detected).")
            return True

        _log(f"⚠ Tıklandı ama beğenenler listesi tespit edilemedi: {name}")
        _dump_candidates(f"after_{name}")
        time.sleep(random.uniform(0.5, 1.0))

    # Son çare: DOM tarayıp 'beğendi/likes' satırındaki sayısal butonu JS ile tıklamayı dene
    try:
        _log("🧪 Son çare JS tarama ile like sayısı butonu aranıyor...")
        clicked_text = page.evaluate(r"""
            () => {
              const numRe = /^\s*[0-9.,]+\s*(B|K|M|MN|Mn|m|k|b)?\s*$/;
              const hintRe = /(beğen|liked|likes)/i;
              const nodes = Array.from(document.querySelectorAll('span[role="button"],div[role="button"],a[role="button"]'));
              // önce hint içeren satırlarda ara
              const pick = (onlyHint) => {
                for (const n of nodes) {
                  const t = (n.innerText || '').replace(/\u00a0/g,' ').trim();
                  if (!t || !numRe.test(t)) continue;
                  const parentText = (n.parentElement ? (n.parentElement.innerText||'') : '').replace(/\u00a0/g,' ');
                  if (onlyHint && !hintRe.test(parentText)) continue;
                  try { n.scrollIntoView({block:'center', inline:'center'}); } catch(e) {}
                  try { n.click(); } catch(e) {}
                  return t;
                }
                return null;
              };
              return pick(true) || pick(false);
            }
        """)
        if clicked_text:
            _log(f"🔎 JS tarama tıkladı: '{clicked_text}'")
            if _wait_likers_ready(before_dialogs):
                _log("✅ Beğenenler penceresi açıldı (JS tarama).")
                return True
        else:
            _log("⚠ JS tarama ile uygun like sayısı butonu bulunamadı.")
    except Exception as e:
        _log(f"⚠ JS tarama hata: {e}")

    _dump_candidates("final")
    return False

FOLLOWING_RELATIONSHIP_LABELS = {
    "takiptesin",
    "following",
    "istek gönderildi",
    "requested",
}

def get_existing_relationship_label(user_link) -> str | None:
    """Beğenen satırındaki takip/istek durumunu güvenli biçimde döndürür."""
    try:
        # Instagram sınıf adlarını sık değiştirdiği için sabit CSS sınıfı yerine,
        # kullanıcı bağlantısına en yakın ve düğme içeren satırı buluyoruz.
        row = user_link.locator("xpath=ancestor::div[.//button][1]")
        if row.count() == 0:
            return None
        buttons = row.locator("button")
        for index in range(min(buttons.count(), 8)):
            label = " ".join((buttons.nth(index).inner_text() or "").split())
            if label.casefold().replace("\u0307", "") in FOLLOWING_RELATIONSHIP_LABELS:
                return label
    except Exception:
        pass
    return None

class CollectionTasks:
    def _collection_begin(self, name):
        self._collection_issue_count = 0
        self._collection_issue_reasons = []
        self.dashboard_update(
            state="running", detail=f"{name}: kullanıcılar toplanıyor.",
            next_action="Toplanan kullanıcılar takip listesine kaydedilecek.",
        )

    def _collection_issue(self, reason):
        """Kısmi taramayı ana ekranda eksiksiz tamamlandı olarak göstermeyin."""
        self._collection_issue_count += 1
        if reason not in self._collection_issue_reasons and len(self._collection_issue_reasons) < 3:
            self._collection_issue_reasons.append(reason)

    def _collection_finish(self, name):
        if self.stop_event.is_set():
            self.dashboard_update(
                state="stopped", detail=f"{name} durduruldu. Kaydedilen kullanıcılar listede.",
                next_action="Devam etmek için ilgili toplama düğmesini kullanabilirsin.",
                remaining=len(self.takipler),
            )
        elif self._collection_issue_count:
            reasons = " / ".join(self._collection_issue_reasons)
            self._dashboard_task_failed(f"{name}: {reasons} ({self._collection_issue_count} uyarı).")
        else:
            self.dashboard_update(
                state="completed", detail=f"{name} taraması bitti. Liste güncellendi.",
                next_action="İstersen Takip Et ile listedeki kullanıcıları işlemeye başlayabilirsin.",
                remaining=len(self.takipler),
            )

    def _save_collected_users(self, users, source):
        """Tüm toplama yollarında aynı atomik birleştirme ve durum kaydını kullan."""
        blocked = {name.casefold() for name in read_takiplist(KARALIST_PATH)}
        existing = read_takiplist()
        existing_keys = {name.casefold() for name in existing}
        accepted = list(dict.fromkeys(name for name in users if name.casefold() not in blocked))
        combined = append_to_takiplist(accepted)
        self.takipler = save_takiplist(name for name in combined if name.casefold() not in blocked)
        self.dashboard_update(remaining=len(self.takipler))
        self.refresh_listbox()
        new_users = [name for name in self.takipler if name.casefold() not in existing_keys]
        for name in new_users:
            record_user_status(name, "pending", "collected", source)
        self.log(f"💾 Eklenen: {len(new_users)} | Güncel liste: {len(self.takipler)} kullanıcı")
        return len(new_users)

    def collect_likes(self):
        if not self._ensure_bot_ready(): return
        self._collection_begin("Beğenilerden Topla")
        if browser.page is not None and browser.is_rate_limited(browser.page): self.log("🚫 Rate limit algılandı (TOPLA) – cooldown."); browser.cooldown_wait(self.log, stop_event=self.stop_event)
        self.log("🔍 'Beğeniler' penceresinden toplanıyor...")
        yeni_kullanicilar: List[str] = []
        try:
            dlg_count = browser.page.locator("div[role='dialog']").count()
            if dlg_count == 0:
                self.log("ℹ Beğenenler penceresi açık değil – açmayı deniyorum...")
                opened = open_likers_dialog(browser.page, self.log, timeout_ms=12000)
                dlg_count = browser.page.locator("div[role='dialog']").count()
                if not opened or dlg_count == 0:
                    self.log("❌ Beğenenler penceresi açılamadı / bulunamadı – önce post üzerindeki beğeni sayısına tıklanabiliyor mu kontrol et.")
                    raise RuntimeError("beğenenler dialog yok")
            dialog = browser.page.get_by_role("dialog").nth(max(0, dlg_count - 1))
            MAX_TOPLA = 3000; last_count = -1; stable_rounds = 0; iteration_count = 0
            try: box = dialog.bounding_box()
            except Exception: box = None
            if box: browser.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 80)
            while True:
                if self.stop_event.is_set():
                    self.log("⏹️ Toplama işlemi durduruldu.")
                    break
                iteration_count += 1
                if iteration_count > 150:
                    self.log("⚠ Scroll limiti aşıldı, toplama durduruluyor.")
                    self._collection_issue("Tarama sınırına ulaşıldı; tüm listenin sonuna gelindiği doğrulanamadı")
                    break

                scroll_box = dialog.locator("div[style*='overflow']")
                items = scroll_box.locator("a[href^='/'][href*='/']:not([href*='explore'])")
                count = items.count()
                if count >= MAX_TOPLA:
                    self._collection_issue("3.000 kişilik tarama sınırına ulaşıldı")
                    break
                if count == last_count: stable_rounds += 1
                else: stable_rounds = 0
                if stable_rounds >= 5: break
                last_count = count
                browser.page.mouse.wheel(0, 400)
                time.sleep(random.uniform(2.8, 4.4))
            user_items = dialog.locator("a[href^='/' i]:not([href*='/explore/'])")
            if user_items.count() == 0:
                a_total = dialog.locator("a").count()
                a_user_candidate = dialog.locator("a[href^='/' i]").count()
                dialogs_total = browser.page.locator("div[role='dialog']").count()
                self.log(f"🧩 DEBUG(TOPLA) dialog_a_total={a_total} a_user_candidate={a_user_candidate} dialogs_total={dialogs_total}")
                try:
                    a_all = dialog.locator("a[href^='/']")
                    n = min(8, a_all.count())
                    for si in range(n):
                        href = a_all.nth(si).get_attribute('href')
                        txt = (a_all.nth(si).inner_text() or '').strip().replace('\n',' ')[:120]
                        self.log(f"  - link#{si+1}: href={href} text='{txt}'")
                except Exception as e:
                    self.log(f"🧩 DEBUG(TOPLA) sample_err: {e}")
            count = min(user_items.count(), MAX_TOPLA)
            self.log(f"📥 Toplam {count} kullanıcı bulundu.")
            for i in range(count):
                try:
                    a = user_items.nth(i); href = a.get_attribute("href")
                    if not href or not (href.startswith("/") and href.count("/") == 2): continue
                    username = href.strip("/")
                    if (not username or username == "accounts" or username in yeni_kullanicilar): continue
                    parent = a.locator("xpath=ancestor::div[contains(@class, '_aacl')]")
                    if not parent: continue
                    btn = parent.locator("button")
                    if btn and btn.is_visible():
                        btn_text = btn.inner_text().strip()
                        if btn_text in ["Takiptesin", "Following", "İstek Gönderildi", "Requested"]:
                            self.log(f"🔕 {username} – '{btn_text}' olduğu için atlandı."); continue
                    yeni_kullanicilar.append(username)
                    if (i + 1) % 20 == 0: self.log(f"… {i + 1} kullanıcı işlendi.")
                except Exception as e:
                    self.log(f"⚠ Kullanıcı alınamadı: {e}"); self.stats.record_error()
                    self._collection_issue("Bazı kullanıcılar okunamadı")
            self.log(f"✅ {len(yeni_kullanicilar)} yeni aday bulundu.")
            eklenen = self._save_collected_users(yeni_kullanicilar, "Beğenilerden Topla")
            self.log(f"🎯 TOPLA işlemi tamamlandı – {eklenen} yeni kullanıcı eklendi.")
        except Exception as e:
            self.log(f"❌ Toplama sırasında hata: {e}"); self.stats.record_error()
            self._collection_issue(f"Toplama hatası: {e}")
        finally:
            self.update_stats_panel()
            self._collection_finish("Beğenilerden Topla")

    def collect_user_posts(self):
        if not self._ensure_bot_ready(): return
        self._collection_begin("Kullanıcı gönderilerinden toplama")
        if browser.page is not None and browser.is_rate_limited(browser.page): self.log("🚫 Rate limit algılandı (TOPLA 2) – cooldown."); browser.cooldown_wait(self.log, stop_event=self.stop_event)
        try:
            if not KULLANICI_PATH.exists():
                self.log("⚠ kullanıcı.txt bulunamadı!")
                self._collection_issue("kullanıcı.txt bulunamadı")
                return
            raw_users = [line.strip() for line in KULLANICI_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
            if not raw_users:
                self.log("⚠ kullanıcı.txt boş!")
                self._collection_issue("kullanıcı.txt boş")
                return
            usernames = []
            for line in raw_users:
                if "/" in line: nick, lim = line.rsplit("/", 1); lim = int(lim) if lim.isdigit() else 4
                else: nick, lim = line, 4
                usernames.append((nick.strip(), lim))
            limit_takip = 1000
            self.log(f"👥 Toplam {len(usernames)} kullanıcı kuyruğa alındı.")
            karalist_set = set()
            if KARALIST_PATH.exists(): karalist_set = set(line.strip() for line in KARALIST_PATH.read_text(encoding="utf-8").splitlines() if line.strip())
            for user_index, (username, max_post) in enumerate(usernames):
                if self.stop_event.is_set():
                    self.log("⏹️ TOPLA 2 durduruldu.")
                    break
                if len(self.takipler) >= limit_takip: self.log("🎯 Takip hedefi doldu – döngü durduruldu."); break
                if browser.page is not None and browser.is_rate_limited(browser.page): self.log("🚫 Rate limit algılandı (TOPLA 2 loop) – cooldown."); browser.cooldown_wait(self.log, stop_event=self.stop_event)
                self.log(f"👤 [{user_index+1}/{len(usernames)}] Profil: {username} | max_post = {max_post}")
                self.dashboard_update(detail=f"Kullanıcı gönderileri: @{username} ({user_index+1}/{len(usernames)})")
                profile_url = f"{IG_URL}/{username}"; browser.page.goto(profile_url, timeout=60_000, wait_until="domcontentloaded")
                browser.page.wait_for_selector("header", timeout=25_000); time.sleep(random.uniform(3, 4))
                self.log(f"✅ {username} profili açıldı.")
                self.log("⏳ Grid yükleniyor...")
                for _ in range(4): browser.page.mouse.wheel(0, 3000); time.sleep(random.uniform(1, 1.5))
                posts = browser.page.locator("a[href*='/p/']"); total_posts = posts.count()
                if total_posts == 0: self.log("🚫 Bu profilde gönderi yok."); continue
                self.log(f"📸 Post bulundu: {total_posts} → {max_post} taranacak.")
                for post_index in range(min(total_posts, max_post)):
                    if self.stop_event.is_set(): break
                    if len(self.takipler) >= limit_takip: self.log("🎯 Takip hedefi doldu – post döngüsü durduruldu."); break
                    if browser.page is not None and browser.is_rate_limited(browser.page): self.log("🚫 Rate limit algılandı (post loop) – cooldown."); browser.cooldown_wait(self.log, stop_event=self.stop_event)
                    posts = browser.page.locator("a[href*='/p/']"); post = posts.nth(post_index)
                    self.log(f"➡ {post_index+1}. gönderi açılıyor..."); post.scroll_into_view_if_needed()
                    time.sleep(random.uniform(0.7, 1.3)); post.click(force=True); self.log("🖼 Gönderi açıldı.")
                    time.sleep(random.uniform(1.2, 2.0)); self.log("❤️ Beğenenler açılıyor...")
                    ok = open_likers_dialog(browser.page, self.log, timeout_ms=20000)
                    if not ok:
                        self.log("⚠ Beğenenler penceresi açılamadı, bu gönderi SKIP edildi.")
                        self._collection_issue("Bazı gönderilerin beğenenler penceresi açılamadı")
                        try: browser.page.keyboard.press("Escape")
                        except Exception: pass
                        continue
                    dlg_count = browser.page.locator("div[role='dialog']").count(); dialog = browser.page.get_by_role("dialog").nth(max(0, dlg_count - 1)); dialog.locator("a[href^='/']").first.wait_for(state="visible", timeout=20000)
                    self.log("🔍 Kullanıcılar toplanıyor..."); time.sleep(random.uniform(1.5, 2.3))
                    post_buffer = []; seen_users = set(self.takipler); stable_rounds = 0; scroll_iter = 0
                    while True:
                        if self.stop_event.is_set(): break
                        scroll_iter += 1
                        if scroll_iter > 50:
                            self._collection_issue("Tarama sınırına ulaşıldı; bazı gönderiler kısmen tarandı")
                            break
                        items = dialog.locator("a[href^='/']:not([href*='/explore/'])")

                        for i in range(items.count()):
                            try:
                                a = items.nth(i)
                                href = a.get_attribute("href")
                                if not href or not (href.startswith("/") and href.count("/") == 2): continue
                                uname = href.strip("/")
                                if uname in karalist_set or uname in seen_users or uname == "accounts": continue

                                try:
                                    parent = a.locator("xpath=ancestor::div[contains(@class,'_aacl')]")
                                    btn = parent.locator("button")
                                    if btn.is_visible():
                                        bt = btn.inner_text().strip()
                                        if bt in ["Takiptesin", "Following", "İstek Gönderildi", "Requested"]:
                                            seen_users.add(uname)
                                            continue
                                except Exception: pass

                                post_buffer.append(uname)
                                seen_users.add(uname)
                            except Exception as e:
                                self.log(f"⚠ Kullanıcı işleme hatası: {e}")
                                self._collection_issue("Bazı kullanıcılar okunamadı")
                                continue

                        self.log(f"➕ Turda yeni kullanıcılar (Post toplam: {len(post_buffer)})")
                        at_end = browser.page.evaluate("""(() => { const d = document.querySelector("div[role='dialog']"); if (!d) return false; const s = d.querySelector("div[style*='overflow']"); if (!s) return false; return Math.abs(s.scrollTop + s.clientHeight - s.scrollHeight) < 5; })();""")
                        if at_end: break
                        try: box = dialog.bounding_box(); browser.page.mouse.move(box["x"] + box["width"]/2, box["y"] + box["height"]/2, steps=12); scroll_amount = random.randint(900, 1400); browser.page.mouse.wheel(0, scroll_amount)
                        except: pass
                        time.sleep(random.uniform(0.45, 0.75))
                    self.log("🔙 Gönderi kapatılıyor..."); browser.page.keyboard.press("Escape"); time.sleep(0.6); browser.page.keyboard.press("Escape"); time.sleep(random.uniform(1.2, 2.0))
                    self._save_collected_users(post_buffer, f"Kullanıcı gönderileri: {username}")
                    if len(self.takipler) >= limit_takip: self.log("🎯 Takip hedefi doldu – tüm işlem durduruldu."); break
                    if random.random() < 0.3: self.log("😴 Kullanıcı geçiş molası..."); time.sleep(random.uniform(6, 12))
            self.log("✅ TOPLA 2 – TÜM İŞLEMLER TAMAMLANDI")
        except Exception as e:
            self.log(f"❌ TOPLA 2 hata: {e}"); self.stats.record_error()
            self._collection_issue(f"Toplama hatası: {e}")
        finally:
            self.update_progress_bar(0); self.update_stats_panel()
            self._collection_finish("Kullanıcı gönderilerinden toplama")

    def collect_post_links(self):
        if not self._ensure_bot_ready(): return
        self._collection_begin("Gönderilerden Topla")
        self.log("📌 GÖNDERİDEN TOPLA başlatıldı...")
        try:
            exclude_following = bool(CONFIG.get("GONDERIDEN_EXCLUDE_FOLLOWING", True))
            if exclude_following:
                self.log("🔕 Zaten takip edilenler ve bekleyen istekler listeye eklenmeyecek.")
            links = read_all_post_links()
            if not links:
                self.log("⚠ gönderiler.txt boş — işlem durduruldu.")
                self._collection_issue("gönderiler.txt boş veya bulunamadı")
                return
            total_links = len(links)
            karalist_set = set()
            if KARALIST_PATH.exists():
                karalist_set = set(line.strip() for line in KARALIST_PATH.read_text(encoding="utf-8").splitlines() if line.strip())
            for i, url in enumerate(links):
                if self.stop_event.is_set():
                    self.log("⏹️ Gönderilerden toplama durduruldu.")
                    break
                progress = (i + 1) / total_links; self.update_progress_bar(progress)
                self.log(f"➡ [{i+1}/{total_links}] Gönderi işleniyor: {url}")
                self.dashboard_update(detail=f"Gönderilerden Topla: {i+1}/{total_links}. gönderi taranıyor.")
                if browser.page is None:
                    self.log("⚠ Bot oturumu kapandı — işlem durduruldu.")
                    self._collection_issue("Tarayıcı oturumu kapandı")
                    break
                self.log("🌐 Gönderiye gidiliyor...")
                try: browser.page.goto(url, timeout=60000, wait_until="domcontentloaded"); time.sleep(3)
                except Exception as e:
                    self.log(f"❌ Gönderi açılamadı: {e}")
                    self._collection_issue("Bazı gönderiler açılamadı")
                    continue
                self.log("✅ Gönderi açıldı."); self.log("❤️ Beğenenler penceresi açılıyor...")
                try: browser.page.wait_for_selector("article", timeout=15000)
                except Exception: pass
                ok = open_likers_dialog(browser.page, self.log, timeout_ms=20000)
                if not ok:
                    self.log("⚠ Beğenenler penceresi açılamadı, bu gönderi SKIP edildi.")
                    self._collection_issue("Bazı gönderilerin beğenenler penceresi açılamadı")
                    continue
                self.log("✅ Beğenenler penceresi açık.")
                dlg_count = browser.page.locator("div[role='dialog']").count(); dialog = browser.page.get_by_role("dialog").nth(max(0, dlg_count - 1)); scroll_box = dialog.locator("div[style*='overflow']"); post_buffer = []; seen_users = set(self.takipler); filtered_following = set()
                no_change_rounds = 0; max_no_change = 6; scroll_step = 0; prev_seen_on_screen = []
                while True:
                    if self.stop_event.is_set(): break
                    scroll_step += 1
                    if scroll_step > 100:
                        self.log("⚠ Maksimum scroll adımına ulaşıldı.")
                        self._collection_issue("Tarama sınırına ulaşıldı; bazı gönderiler kısmen tarandı")
                        break
                    yeni_sayisi = 0; current_seen_on_screen = []; items = scroll_box.locator("span a[href^='/'][href*='/']"); count = items.count()

                    for i in range(count):
                        try:
                            a = items.nth(i)
                            href = a.get_attribute("href")
                            if not href or not (href.startswith("/") and href.count("/") == 2): continue
                            username = href.strip("/")
                            if not username or username == "accounts": continue
                            current_seen_on_screen.append(username)
                            if username in karalist_set or username in seen_users: continue
                            if exclude_following:
                                relationship_label = get_existing_relationship_label(a)
                                if relationship_label:
                                    seen_users.add(username)
                                    if username not in filtered_following:
                                        filtered_following.add(username)
                                        self.log(
                                            f"🔕 {username} – '{relationship_label}' olduğu için listeye eklenmedi."
                                        )
                                    continue
                            post_buffer.append(username)
                            seen_users.add(username)
                            yeni_sayisi += 1
                        except Exception as e:
                            self.log(f"⚠ Kullanıcı işleme hatası: {e}")
                            self._collection_issue("Bazı kullanıcılar okunamadı")
                            continue

                    if current_seen_on_screen == prev_seen_on_screen:
                        no_change_rounds += 1
                        self.log(f"⚠ Ekran değişmedi ({no_change_rounds}/{max_no_change})")
                    else:
                        no_change_rounds = 0
                    prev_seen_on_screen = current_seen_on_screen.copy()
                    if no_change_rounds >= max_no_change:
                        self.log("⏬ Popup dibe ulaştı — ekran değişimi durdu.")
                        break
                    mode = scroll_step % 3
                    if mode == 0: scroll_amount = random.randint(600, 900)
                    elif mode == 1: scroll_amount = random.randint(1100, 1600)
                    else: scroll_amount = random.randint(1800, 2600)
                    try:
                        box = scroll_box.bounding_box()
                        browser.page.mouse.move(box["x"] + box["width"]/2, box["y"] + box["height"]/2, steps=6)
                        browser.page.mouse.wheel(0, scroll_amount)
                    except: pass
                    time.sleep(random.uniform(0.45, 0.65))
                browser.page.keyboard.press("Escape"); time.sleep(1)
                self._save_collected_users(post_buffer, f"Gönderilerden Topla: {url}")
                if exclude_following:
                    self.log(f"🔕 Bu gönderide takip filtresiyle atlanan: {len(filtered_following)}");
                self.log(f"📊 Güncel liste: {len(self.takipler)} kullanıcı");
                time.sleep(random.uniform(3, 6))
            self.update_progress_bar(1.0)
            self.log("🎉 TÜM GÖNDERİLERDEN TOPLAMA TAMAMLANDI.")
        except Exception as e:
            self.log(f"❌ GÖNDERİDEN TOPLA hata: {e}")
            self._collection_issue(f"Toplama hatası: {e}")
        finally:
            self.update_progress_bar(0); self.update_stats_panel()
            self._collection_finish("Gönderilerden Topla")
