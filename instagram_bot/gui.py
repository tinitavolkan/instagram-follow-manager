"""Local export comparison, original follow controls and a paced unfollow queue."""
import csv
from datetime import datetime
import io
from pathlib import Path
import queue
from tkinter import filedialog

import customtkinter as ctk

from .paths import BASE_DIR
from .logging_setup import APP_LOG
from .storage import atomic_write_text
from .unfollow_worker import UnfollowController
from .ui_shell import create_shell, SECTIONS
from . import ui_theme as t


PAGE_SIZE = 50


class InstaGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("Instagram Takip Yönetimi")
        self.geometry("1280x820")
        self.minsize(1100, 700)
        self.worker_thread = None
        self.closing = False
        self.gui_queue = self.events = queue.Queue()
        self._busy = False
        self._active_username = ""
        self._waiting = False
        self._controls_state = None
        self._pages = {"remaining": 0, "history": 0}
        self.remaining_rows = {}
        self.log_history = []
        self.snapshot_data = {}
        self._sort_ascending = True
        self._log_visible = False
        self.controller = UnfollowController(
            data_dir=BASE_DIR, event_cb=lambda event: self.call_in_main_thread(self._handle_event, event),
        )
        self.account_var = ctk.StringVar()
        self.search_var = ctk.StringVar()
        self._create_widgets()
        self._render_snapshot(self.controller.snapshot())
        self.search_var.trace_add("write", self._search_changed)
        self.after(50, self.process_gui_queue)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.bind("<Configure>", self._resize_text)
        self.bind('<Control-f>', self._focus_search)
        self.bind('<Escape>', lambda event: self._stop_clicked() if self._is_busy() else None)

    def _create_widgets(self):
        create_shell(self)

    def worker_loop(self):
        self.controller.run_loop()

    def show_section(self, name):
        self.tabs.set(name)
        self._tab_changed()

    def _tab_changed(self):
        section = self.tabs.get()
        title, description = SECTIONS[section]
        self.header_title.configure(text=title)
        self.header_description.configure(text=description)
        for name, control in self.nav_buttons.items():
            control.configure(fg_color=t.SELECTED if name == section else t.SURFACE,
                              text_color=t.ACCENT if name == section else t.MUTED)
        for widget, visible in ((self.summary_cards, section in {'Kalanlar', 'Geçmiş'}),
                                (self.list_toolbar, section in {'Kalanlar', 'Geçmiş'}),
                                (self.batch_btn, section == 'Kalanlar'),
                                (self.batch_help, section == 'Kalanlar'),
                                (self.filter_menu, section == 'Kalanlar'),
                                (self.export_txt_btn, section == 'Kalanlar'),
                                (self.export_csv_btn, section == 'Kalanlar')):
            widget.grid() if visible else widget.grid_remove()

    def _toggle_log(self):
        self._log_visible = not self._log_visible
        self.log_text.grid() if self._log_visible else self.log_text.grid_remove()
        self.log_toggle_btn.configure(text='İşlem günlüğünü gizle' if self._log_visible else 'İşlem günlüğünü göster')

    def _focus_search(self, event=None):
        if self.tabs.get() not in {'Kalanlar', 'Geçmiş'}:
            self.show_section('Kalanlar')
        self.search_entry.focus_set()
        return 'break'

    def _toggle_sort(self):
        self._sort_ascending = not self._sort_ascending
        self.sort_btn.configure(text='Kullanıcı adı ↑' if self._sort_ascending else 'Kullanıcı adı ↓')
        self._search_changed()

    def _clear_filters(self):
        self.filter_var.set('Tümü')
        self.search_var.set('')
        self._search_changed()

    def _show_person_detail(self, username):
        result = self.snapshot_data.get('last_results', {}).get(username, {})
        detail = result.get('detail', '') if isinstance(result, dict) else str(result)
        self.follow_panel._information_window('@' + username, detail or 'Bu kişi henüz işlenmedi. Sonuç doğrulanınca listeden çıkarılır.')

    def _is_busy(self):
        return self._busy or self.controller.busy

    def _select_folder(self):
        if self._is_busy():
            return
        selected = filedialog.askdirectory(parent=self, title="Instagram dışa aktarımının bulunduğu klasör")
        if selected:
            self._request_import([selected])

    def _select_files(self):
        if self._is_busy():
            return
        selected = filedialog.askopenfilenames(
            parent=self, title="following ve tüm followers dosyalarını veya ZIP arşivini seçin",
            filetypes=[("Instagram dışa aktarımı", "*.json *.html *.htm *.zip"), ("Tüm dosyalar", "*.*")],
        )
        if selected:
            self._request_import(list(selected))

    def _request_import(self, paths):
        self._run_command(lambda: self.controller.import_paths(paths, account=self.account_var.get().strip().lstrip("@")),
                          "Dışa aktarım dosyaları okunuyor…")

    def _run_command(self, command, detail):
        if self.closing or self._is_busy():
            return
        try:
            if command():
                self._handle_event({"type": "status", "state": "busy", "detail": detail})
        except Exception as exc:
            self._handle_event({"type": "status", "state": "attention", "detail": str(exc)})
            self._append_log(str(exc))

    def _unfollow_clicked(self, username):
        self._run_command(lambda: self.controller.unfollow(username), f"@{username} için takibi bırakma kontrol ediliyor…")

    def _start_batch_clicked(self):
        if self._is_busy() or not self.snapshot_data.get("remaining"):
            return
        self.show_section("Kalanlar")
        self._run_command(self.controller.unfollow_batch, "Otomatik takibi bırakma hazırlanıyor; açık hesap doğrulanacak…")

    def _stop_clicked(self):
        self.controller.stop()
        self.status_detail.configure(text="Durdurma isteği alındı. Sonucu doğrulanmayan kişiler kalanlarda tutulur.")

    def call_in_main_thread(self, func, *args, **kwargs):
        self.gui_queue.put((func, args, kwargs))

    def process_gui_queue(self):
        """Run worker events and ordinary GUI callbacks on the Tk main thread."""
        try:
            for _ in range(100):
                try:
                    payload = self.gui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if isinstance(payload, dict):
                        self._handle_event(payload)
                    else:
                        func, args, kwargs = payload
                        func(*args, **kwargs)
                except Exception as exc:
                    APP_LOG.exception("Arayüz güncellemesi başarısız")
                    self.log(f"Arayüz güncelleme hatası: {exc}")
        finally:
            if not self.closing:
                self.after(50, self.process_gui_queue)

    def log(self, detail):
        APP_LOG.info(detail)
        self.call_in_main_thread(self._append_log, detail)

    def _handle_event(self, event):
        kind = event.get("type")
        if kind == "snapshot":
            self._render_snapshot(event["data"])
        elif kind == "busy":
            self._busy = bool(event.get("busy"))
            if not self._busy:
                self._waiting = False
            self._update_controls(force=True)
        elif kind == "status":
            state = event.get("state", "idle")
            self._busy = state == "busy"
            self._active_username = event.get("username", "") if self._busy else ""
            self._waiting = self._busy and event.get('phase') == 'waiting'
            title, color = {"idle": ("Hazır", t.SUCCESS), "busy": ("İşlem sürüyor", t.ACCENT),
                            "attention": ("Kontrol gerekiyor", t.DANGER), "stopped": ("Durduruldu", t.WARNING)}.get(state, ("Hazır", t.SUCCESS))
            if self._waiting:
                title = 'Molada'
            self.status_label.configure(text=title, text_color=color)
            detail = str(event.get('detail', ''))
            self.status_detail.configure(text=detail[:237] + '…' if len(detail) > 240 else detail)
            self._update_controls(force=True)
        elif kind == "session":
            username = event.get("username", "")
            self.session_label.configure(text=(f'@{username}\nİşlem öncesinde doğrulanır.' if username
                                              else 'Oturum doğrulanmadı\nTarayıcıdan giriş yap.'))
        elif kind == "log":
            self._append_log(event.get("detail", ""))

    def _render_snapshot(self, data):
        self.snapshot_data = dict(data)
        if data.get("account"):
            self.account_var.set(data["account"])
        for key, field in (("followers", "followers_count"), ("following", "following_count"), ("mutual", "mutual_count")):
            self.counters[key].configure(text=str(data.get(field, 0)))
        self.counters["remaining"].configure(text=str(len(data.get("remaining", []))))
        sources = [Path(path).name for path in data.get("sources", [])]
        source_text = ", ".join(sources)
        if len(source_text) > 240:
            source_text = source_text[:237] + "…"
        text = f"Kaynak ({len(sources)} dosya): {source_text}" if sources else "Kaynak: Henüz içe aktarılmadı."
        if data.get("imported_at"):
            text += f" · Yüklendi: {data['imported_at']}"
        warnings = " ".join(str(warning) for warning in data.get("warnings", []))
        if warnings:
            text += "\nUyarı: " + warnings[:260]
        self.source_label.configure(text=text)
        self.batch_help.configure(text=self.batch_help_base)
        self._render_lists()
        self._update_controls(force=True)

    def _search_changed(self, *_):
        self._pages = {"remaining": 0, "history": 0}
        self._render_lists()

    def _change_page(self, key, step):
        self._pages[key] = max(0, self._pages[key] + step)
        self._render_lists()

    def _page(self, key, values, label, previous, following):
        total_pages = max(1, (len(values) + PAGE_SIZE - 1) // PAGE_SIZE)
        index = self._pages[key] = min(self._pages[key], total_pages - 1)
        label.configure(text=f"{len(values)} kayıt · Sayfa {index + 1} / {total_pages}")
        previous.configure(state="normal" if index else "disabled")
        following.configure(state="normal" if index + 1 < total_pages else "disabled")
        return values[index * PAGE_SIZE:(index + 1) * PAGE_SIZE]

    def _render_lists(self):
        query = self.search_var.get().strip().casefold().lstrip("@")
        results = self.snapshot_data.get("last_results", {})
        remaining = [name for name in self.snapshot_data.get("remaining", []) if query in name.casefold()]
        selected_filter = self.filter_var.get()
        if selected_filter == 'İşlem görmeyenler':
            remaining = [name for name in remaining if name not in results]
        elif selected_filter == 'Kontrol gerekenler':
            remaining = [name for name in remaining if name in results]
        remaining.sort(reverse=not self._sort_ascending)
        page = self._page("remaining", remaining, self.page_label, self.prev_btn, self.next_btn)
        for widget in self.remaining_frame.winfo_children():
            widget.destroy()
        self.remaining_rows = {}
        if not page:
            filtered = bool(query or selected_filter != 'Tümü')
            loaded = bool(self.snapshot_data.get('imported_at'))
            text = ('Filtreye uygun kişi bulunamadı' if filtered else 'Kalan kişi yok' if loaded else 'Önce Instagram verilerini yükle')
            ctk.CTkLabel(self.remaining_frame, text=text, text_color=t.TEXT, font=t.font(18, True)).grid(row=0, column=0, pady=(48, 8))
            help_text = ('Aramayı veya durum filtresini temizleyebilirsin.' if filtered else
                         'Tamamlanan işlemleri geçmişte görebilirsin.' if loaded else 'ZIP arşivini veya takipçi ve takip edilen dosyalarını seç.')
            ctk.CTkLabel(self.remaining_frame, text=help_text, text_color=t.MUTED, font=t.font(13)).grid(row=1, column=0, pady=(0, 16))
            action, command = (('Filtreleri temizle', self._clear_filters) if filtered else
                               ('Geçmişi görüntüle', lambda: self.show_section('Geçmiş')) if loaded else
                               ('Veri yükle', lambda: self.show_section('Dosyalar')))
            t.button(self.remaining_frame, action, command, kind='primary', width=180).grid(row=2, column=0, pady=8)
        for index, username in enumerate(page):
            row = ctk.CTkFrame(self.remaining_frame, fg_color=t.SURFACE if index % 2 else t.ZEBRA, corner_radius=4)
            row.grid(row=index, column=0, padx=4, pady=1, sticky="ew")
            row.grid_columnconfigure(0, weight=1)
            name_btn = t.button(row, '@' + username, lambda name=username: self._show_person_detail(name),
                                kind='nav', fg_color='transparent', width=120, height=46, font=t.font(13, True), text_color=t.TEXT)
            name_btn.grid(row=0, column=0, padx=12, sticky='ew')
            result = results.get(username, {})
            detail = result.get("detail", "") if isinstance(result, dict) else str(result)
            badge = t.button(row, 'Kontrol gerekli' if detail else 'Bekliyor', lambda name=username: self._show_person_detail(name),
                             width=125, height=28, fg_color=t.WARNING_BG if detail else t.SELECTED,
                             text_color=t.WARNING if detail else t.MUTED, border_width=0, font=t.font(11))
            badge.grid(row=0, column=1, padx=6)
            button = t.button(row, 'Takibi bırak', lambda name=username: self._unfollow_clicked(name), kind='danger',
                              width=116, height=32, state='disabled' if self._is_busy() else 'normal')
            button.grid(row=0, column=2, padx=(6, 12), pady=7)
            self.remaining_rows[username] = {'frame': row, 'button': button, 'badge': badge, 'name': name_btn}
        self.remaining_frame._parent_canvas.yview_moveto(0)
        status_titles = {"unfollowed": "Takibi bırakıldı", "success": "Takibi bırakıldı", "not_following": "Zaten takip edilmiyor",
                         "already_not_following": "Zaten takip edilmiyor",
                         "unavailable": "Ulaşılamadı — listeden çıkarıldı",
                         "restricted": "Kısıtlama", "error": "Hata", "stopped": "Durduruldu", "unconfirmed": "Doğrulanamadı"}
        history = [record for record in self.snapshot_data.get("history", [])
                   if query in (' '.join(str(record.get(key, '')) for key in ('username', 'status', 'detail', 'at'))
                                + ' ' + status_titles.get(record.get('status'), '')).casefold()]
        history_page = self._page("history", history, self.history_page_label, self.history_prev_btn, self.history_next_btn)
        lines = []
        for record in history_page:
            status = record.get("status", "")
            lines.append(f"{record.get('at', '')}  ·  @{record.get('username', '')}  ·  {status_titles.get(status, status)}\n{record.get('detail', '')}")
        self.history_text.configure(state="normal")
        self.history_text.delete("1.0", "end")
        self.history_text.insert("1.0", "\n\n".join(lines) or "Henüz işlem geçmişi yok.")
        self.history_text.configure(state="disabled")

    def _update_controls(self, force=False):
        busy = self._is_busy()
        loaded = bool(self.snapshot_data.get("sources") or self.snapshot_data.get("imported_at"))
        signature = (busy, loaded, self._active_username)
        if not force and signature == self._controls_state:
            return
        self._controls_state = signature
        self.follow_panel.set_busy(busy)
        state = "disabled" if busy else "normal"
        for widget in (self.account_entry, self.import_folder_btn, self.import_files_btn, self.session_btn, self.settings_btn):
            widget.configure(state=state)
        self.batch_btn.configure(state="normal" if self.snapshot_data.get("remaining") and not busy else "disabled")
        self.stop_btn.configure(state="normal" if busy else "disabled")
        for widget in (self.export_txt_btn, self.export_csv_btn):
            widget.configure(state="normal" if loaded and not busy else "disabled")
        for username, row in self.remaining_rows.items():
            active = busy and username == self._active_username
            row["button"].configure(state=state, text=('Bekleniyor…' if self._waiting else 'İşleniyor…') if active else 'Takibi bırak')
            result = self.snapshot_data.get('last_results', {}).get(username)
            row['badge'].configure(text=('Molada' if self._waiting else 'İşleniyor') if active else 'Kontrol gerekli' if result else 'Bekliyor')

    def _export_remaining(self, extension):
        if self._is_busy() or extension not in ("txt", "csv") or not self.snapshot_data.get("imported_at"):
            return
        snapshot = self.snapshot_data
        filename = filedialog.asksaveasfilename(parent=self, title="Kalan kişileri kaydet", defaultextension="." + extension,
                    initialfile=f"geri_takip_etmeyenler_{snapshot.get('account', 'hesap')}.{extension}",
                    filetypes=[("CSV dosyası" if extension == "csv" else "Metin dosyası", "*." + extension)])
        if not filename or self._is_busy() or snapshot is not self.snapshot_data:
            return
        remaining = snapshot.get("remaining", [])
        if extension == "csv":
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerow(("username", "profile_url"))
            writer.writerows((username, f"https://www.instagram.com/{username}/") for username in remaining)
            content = stream.getvalue()
        else:
            content = (f"Geri takip etmeyenler · Kalan kişiler\nHesap: @{snapshot.get('account', '')}\n"
                       f"İçe aktarım: {snapshot.get('imported_at', '')}\nKalan: {len(remaining)} kişi\n\n"
                       + "".join(f"@{username}\thttps://www.instagram.com/{username}/\n" for username in remaining))
        try:
            atomic_write_text(filename, content)
            self._append_log(f"Kalan kişiler kaydedildi: {filename}")
        except (OSError, UnicodeError) as exc:
            self._handle_event({"type": "status", "state": "attention", "detail": f"Dosya kaydedilemedi: {exc}"})

    def _append_log(self, detail):
        self.log_history.append(f"[{datetime.now():%d.%m.%Y %H:%M:%S}] {detail}")
        self.log_history = self.log_history[-100:]
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", "\n".join(self.log_history))
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _resize_text(self, event):
        if event.widget is self:
            width = max(500, self.body.winfo_width() - 64)
            for label in (self.import_help, self.source_label, self.batch_help):
                label.configure(wraplength=width)
            self.status_detail.configure(wraplength=max(340, width - 130))
            self.header_description.configure(wraplength=max(300, width - 200))

    def on_closing(self):
        if self.closing:
            return
        self.closing = True
        self.controller.shutdown()
        self._wait_for_worker_shutdown()

    def _wait_for_worker_shutdown(self):
        if self.worker_thread is not None and self.worker_thread.is_alive():
            self.after(100, self._wait_for_worker_shutdown)
        else:
            self.destroy()
