"""Sidebar, table workspace and persistent status area for the Tk desktop app."""
import customtkinter as ctk

from .follow_panel import LegacyFollowPanel
from .unfollow_worker import UNFOLLOW_INTERVAL_SECONDS, UNFOLLOW_BATCH_SIZE
from . import ui_theme as t


SECTIONS = {
    'Takip Et': ('Takip Et', 'Mevcut takip sıranı ve günlük ilerlemeni yönet.'),
    'Kalanlar': ('Geri takip etmeyenler', 'İçe aktardığın listeden kalan kişiler ve takibi bırakma işlemleri.'),
    'Geçmiş': ('İşlem geçmişi', 'Doğrulanan işlemler, durdurulan denemeler ve açıklamaları.'),
    'Dosyalar': ('Veri yükle', 'Instagram dışa aktarımını seç; dosyaları taşıman gerekmez.'),
}


class SectionStack(ctk.CTkFrame):
    """One content page at a time, without delayed tab cleanup or duplicate navigation."""
    def __init__(self, parent):
        super().__init__(parent, fg_color=t.SURFACE, corner_radius=12, border_width=1, border_color=t.BORDER)
        self._sections = {}
        self._current = ''
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

    def add(self, name):
        frame = ctk.CTkFrame(self, fg_color='transparent')
        self._sections[name] = frame
        if not self._current:
            self.set(name)
        return frame

    def get(self):
        return self._current

    def tab(self, name):
        return self._sections[name]

    def set(self, name):
        target = self._sections[name]
        if self._current:
            self._sections[self._current].grid_remove()
        self._current = name
        target.grid(row=0, column=0, sticky='nsew', padx=8, pady=8)


def create_shell(app):
    app.configure(fg_color=t.BG)
    app.grid_columnconfigure(0, weight=0, minsize=220)
    app.grid_columnconfigure(1, weight=1)
    app.grid_rowconfigure(0, weight=1)
    sidebar = app.sidebar = ctk.CTkFrame(app, width=220, fg_color=t.SURFACE, corner_radius=0)
    sidebar.grid(row=0, column=0, sticky='nsew')
    sidebar.grid_columnconfigure(0, weight=1)
    sidebar.grid_rowconfigure(3, weight=1)
    brand = ctk.CTkFrame(sidebar, fg_color='transparent')
    brand.grid(row=0, column=0, padx=20, pady=(28, 28), sticky='ew')
    ctk.CTkLabel(brand, text='IG', width=40, height=40, corner_radius=10,
                 fg_color=t.PRIMARY, text_color='white', font=t.font(17, True)).pack(side='left')
    ctk.CTkLabel(brand, text='Takip\nYönetimi', justify='left', text_color=t.TEXT,
                 font=t.font(17, True)).pack(side='left', padx=10)
    ctk.CTkLabel(sidebar, text='ÇALIŞMA ALANI', anchor='w', text_color=t.MUTED,
                 font=t.font(11, True)).grid(row=1, column=0, padx=24, sticky='ew')
    navigation = ctk.CTkFrame(sidebar, fg_color='transparent')
    navigation.grid(row=2, column=0, padx=12, pady=10, sticky='ew')
    navigation.grid_columnconfigure(0, weight=1)
    app.nav_buttons = {}
    for index, (name, (title, _)) in enumerate(SECTIONS.items()):
        control = t.button(navigation, f'{index + 1:02}   {title}', lambda section=name: app.show_section(section),
                           kind='nav', width=196, height=44)
        control.grid(row=index, column=0, pady=3, sticky='ew')
        app.nav_buttons[name] = control
    tools = ctk.CTkFrame(sidebar, fg_color='transparent')
    tools.grid(row=4, column=0, padx=16, pady=(16, 24), sticky='ew')
    tools.grid_columnconfigure(0, weight=1)
    app.session_label = ctk.CTkLabel(tools, text='Oturum doğrulanmadı\nGiriş tarayıcıda yapılır.',
                                    text_color=t.MUTED, font=t.font(12), anchor='w', justify='left', wraplength=184)
    app.session_label.grid(row=0, column=0, pady=(0, 12), sticky='ew')
    app.session_btn = t.button(tools, 'Oturumu aç',
        lambda: app._run_command(app.controller.open_session, 'Tarayıcı oturumu açılıyor…'), width=188)
    app.session_btn.grid(row=1, column=0, pady=4, sticky='ew')
    app.settings_btn = t.button(tools, 'Takip ayarları', lambda: app.follow_panel.open_settings(), width=188)
    app.settings_btn.grid(row=2, column=0, pady=4, sticky='ew')
    app.log_toggle_btn = t.button(tools, 'İşlem günlüğünü göster', app._toggle_log, width=188)
    app.log_toggle_btn.grid(row=3, column=0, pady=4, sticky='ew')

    body = app.body = ctk.CTkFrame(app, fg_color='transparent')
    body.grid(row=0, column=1, padx=24, pady=24, sticky='nsew')
    body.grid_columnconfigure(0, weight=1)
    body.grid_rowconfigure(3, weight=1)
    header = ctk.CTkFrame(body, fg_color='transparent')
    header.grid(row=0, column=0, pady=(0, 20), sticky='ew')
    header.grid_columnconfigure(0, weight=1)
    app.header_title = ctk.CTkLabel(header, text='', font=t.font(29, True), text_color=t.TEXT, anchor='w')
    app.header_title.grid(row=0, column=0, sticky='ew')
    app.header_description = ctk.CTkLabel(header, text='', text_color=t.MUTED, font=t.font(12), anchor='w', justify='left')
    app.header_description.grid(row=1, column=0, pady=(4, 0), sticky='ew')
    app.batch_btn = t.button(header, 'Otomatik takibi bırak', app._start_batch_clicked, kind='primary', width=180, height=40)
    app.batch_btn.grid(row=0, column=1, rowspan=2, padx=(16, 0))

    cards = app.summary_cards = ctk.CTkFrame(body, fg_color='transparent')
    cards.grid(row=1, column=0, pady=(0, 16), sticky='ew')
    app.counters = {}
    for index, (key, title) in enumerate((('followers', 'Takipçi'), ('following', 'Takip edilen'),
                                         ('mutual', 'Karşılıklı'), ('remaining', 'Kalan kişi'))):
        cards.grid_columnconfigure(index, weight=1, uniform='counts')
        card = ctk.CTkFrame(cards, fg_color=t.SURFACE, corner_radius=10, border_width=1, border_color=t.BORDER)
        card.grid(row=0, column=index, padx=(0 if index == 0 else 6, 0 if index == 3 else 6), sticky='ew')
        ctk.CTkLabel(card, text=title, text_color=t.MUTED, font=t.font(12), anchor='w').pack(fill='x', padx=16, pady=(10, 0))
        app.counters[key] = ctk.CTkLabel(card, text='0', text_color=t.ACCENT if key == 'remaining' else t.TEXT,
                                       font=t.font(27, True), anchor='w')
        app.counters[key].pack(fill='x', padx=16)
        ctk.CTkLabel(card, text='İşlemler sonrası' if key == 'remaining' else 'İçe aktarılan veri',
                     text_color=t.MUTED, font=t.font(11), anchor='w').pack(fill='x', padx=16, pady=(0, 8))

    toolbar = app.list_toolbar = ctk.CTkFrame(body, fg_color='transparent')
    toolbar.grid(row=2, column=0, pady=(0, 12), sticky='ew')
    toolbar.grid_columnconfigure(0, weight=1)
    app.search_entry = ctk.CTkEntry(toolbar, textvariable=app.search_var, placeholder_text='Kullanıcı adı veya geçmişte ara…',
                                   height=36, fg_color=t.SURFACE, border_color=t.BORDER, text_color=t.TEXT, font=t.font())
    app.search_entry.grid(row=0, column=0, padx=(0, 10), sticky='ew')
    app.filter_var = ctk.StringVar(value='Tümü')
    app.filter_menu = ctk.CTkOptionMenu(toolbar, values=['Tümü', 'İşlem görmeyenler', 'Kontrol gerekenler'],
        variable=app.filter_var, command=lambda value: app._search_changed(), width=160, height=36,
        fg_color=t.SURFACE, button_color=t.BORDER, button_hover_color=t.SELECTED, text_color=t.TEXT,
        dropdown_fg_color=t.SURFACE, dropdown_text_color=t.TEXT, dropdown_hover_color=t.SELECTED, font=t.font(12))
    app.filter_menu.grid(row=0, column=1, padx=(0, 10))
    app.export_txt_btn = t.button(toolbar, 'TXT', lambda: app._export_remaining('txt'), width=64)
    app.export_txt_btn.grid(row=0, column=2, padx=(0, 6))
    app.export_csv_btn = t.button(toolbar, 'CSV', lambda: app._export_remaining('csv'), width=64)
    app.export_csv_btn.grid(row=0, column=3)

    app.tabs = SectionStack(body)
    app.tabs.grid(row=3, column=0, sticky='nsew')
    for name in SECTIONS:
        tab = app.tabs.add(name)
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
    follow_tab, remaining_tab, history_tab, import_tab = [app.tabs.tab(name) for name in SECTIONS]
    follow_tab.grid_rowconfigure(0, weight=1)
    app.follow_panel = LegacyFollowPanel(follow_tab, app)
    app.follow_panel.grid(row=0, column=0, rowspan=2, sticky='nsew')
    app.follow_btn = app.follow_panel.follow_btn

    headings = ctk.CTkFrame(remaining_tab, fg_color=t.ZEBRA, corner_radius=6)
    headings.grid(row=0, column=0, padx=4, pady=(4, 0), sticky='ew')
    headings.grid_columnconfigure(0, weight=1)
    app.sort_btn = t.button(headings, 'Kullanıcı adı ↑', app._toggle_sort, kind='nav', height=32)
    app.sort_btn.grid(row=0, column=0, padx=12, sticky='w')
    ctk.CTkLabel(headings, text='DURUM', width=135, font=t.font(11, True), text_color=t.MUTED).grid(row=0, column=1)
    ctk.CTkLabel(headings, text='İŞLEM', width=125, font=t.font(11, True), text_color=t.MUTED).grid(row=0, column=2, padx=(0, 20))
    app.remaining_frame = ctk.CTkScrollableFrame(remaining_tab, fg_color=t.SURFACE, corner_radius=0)
    app.remaining_frame.grid(row=1, column=0, sticky='nsew')
    app.remaining_frame.grid_columnconfigure(0, weight=1)
    paging = ctk.CTkFrame(remaining_tab, fg_color='transparent')
    paging.grid(row=2, column=0, padx=12, pady=12, sticky='ew')
    paging.grid_columnconfigure(1, weight=1)
    app.prev_btn = t.button(paging, 'Önceki', lambda: app._change_page('remaining', -1), width=88)
    app.prev_btn.grid(row=0, column=0)
    app.page_label = ctk.CTkLabel(paging, text='', text_color=t.MUTED, font=t.font(12))
    app.page_label.grid(row=0, column=1)
    app.next_btn = t.button(paging, 'Sonraki', lambda: app._change_page('remaining', 1), width=88)
    app.next_btn.grid(row=0, column=2)
    history_tab.grid_rowconfigure(0, weight=1)
    history_tab.grid_rowconfigure(1, weight=0)
    app.history_text = ctk.CTkTextbox(history_tab, wrap='word', state='disabled', fg_color=t.SURFACE,
                                     text_color=t.TEXT, font=t.font(13), spacing3=8)
    app.history_text.grid(row=0, column=0, padx=12, pady=12, sticky='nsew')
    paging = ctk.CTkFrame(history_tab, fg_color='transparent')
    paging.grid(row=1, column=0, padx=12, pady=12, sticky='ew')
    paging.grid_columnconfigure(1, weight=1)
    app.history_prev_btn = t.button(paging, 'Önceki', lambda: app._change_page('history', -1), width=88)
    app.history_prev_btn.grid(row=0, column=0)
    app.history_page_label = ctk.CTkLabel(paging, text='', text_color=t.MUTED, font=t.font(12))
    app.history_page_label.grid(row=0, column=1)
    app.history_next_btn = t.button(paging, 'Sonraki', lambda: app._change_page('history', 1), width=88)
    app.history_next_btn.grid(row=0, column=2)
    _import_page(app, import_tab)

    footer = ctk.CTkFrame(body, fg_color=t.SURFACE, corner_radius=10, border_width=1, border_color=t.BORDER)
    footer.grid(row=4, column=0, pady=(16, 0), sticky='ew')
    footer.grid_columnconfigure(0, weight=1)
    app.status_label = ctk.CTkLabel(footer, text='Hazır', anchor='w', text_color=t.SUCCESS, font=t.font(13, True))
    app.status_label.grid(row=0, column=0, padx=16, pady=(8, 0), sticky='ew')
    app.status_detail = ctk.CTkLabel(footer, text='Bir bölüm seçerek başlayabilirsin.', anchor='w', justify='left',
                                     wraplength=750, text_color=t.MUTED, font=t.font(12))
    app.status_detail.grid(row=1, column=0, padx=16, pady=(0, 10), sticky='ew')
    app.stop_btn = t.button(footer, 'Durdur', app._stop_clicked, kind='danger', width=96)
    app.stop_btn.grid(row=0, column=1, rowspan=2, padx=16, pady=12)
    app.batch_help_base = (f'{UNFOLLOW_BATCH_SIZE} kişi peş peşe · ardından {UNFOLLOW_INTERVAL_SECONDS // 60} dk mola · Liste bitene kadar devam eder · '
        'Ulaşılamayanlar atlanır; diğer hatalarda durur. Blok yememe garantisi yoktur.')
    app.batch_help = ctk.CTkLabel(footer, text=app.batch_help_base,
        anchor='w', justify='left', text_color=t.MUTED, font=t.font(11), wraplength=850)
    app.batch_help.grid(row=2, column=0, columnspan=2, padx=16, pady=(0, 10), sticky='ew')
    app.log_text = ctk.CTkTextbox(body, height=120, wrap='word', state='disabled', fg_color=t.SURFACE,
                                 border_width=1, border_color=t.BORDER, text_color=t.MUTED, font=t.font(12))
    app.log_text.grid(row=5, column=0, pady=(12, 0), sticky='ew')
    app.log_text.grid_remove()
    app.show_section('Takip Et')


def _import_page(app, tab):
    tab.grid_rowconfigure(0, weight=1)
    tab.grid_rowconfigure(1, weight=0)
    panel = app.import_panel = ctk.CTkScrollableFrame(tab, fg_color=t.SURFACE, corner_radius=0)
    panel.grid(row=0, column=0, padx=16, pady=16, sticky='nsew')
    panel.grid_columnconfigure(0, weight=1)
    ctk.CTkLabel(panel, text='Takipçi ve takip edilen dosyaları', text_color=t.TEXT, font=t.font(20, True),
                 anchor='w').grid(row=0, column=0, columnspan=2, pady=(0, 12), sticky='ew')
    app.import_help = ctk.CTkLabel(panel, text='1. Instagram dışa aktarımında tarih aralığını Tüm zamanlar seç.\n'
        '2. following.json / .html ile tüm followers_*.json / .html parçalarını birlikte seç.\n'
        '3. ZIP arşivini veya çıkardığın klasörü de doğrudan açabilirsin.',
        text_color=t.MUTED, font=t.font(13), justify='left', anchor='w', wraplength=750)
    app.import_help.grid(row=1, column=0, columnspan=2, pady=(0, 24), sticky='ew')
    ctk.CTkLabel(panel, text='Dosyaların ait olduğu hesap', text_color=t.TEXT, font=t.font(13, True),
                 anchor='w').grid(row=2, column=0, columnspan=2, sticky='ew')
    app.account_entry = ctk.CTkEntry(panel, textvariable=app.account_var, placeholder_text='kullanici_adi · şifre gerekmez',
                                     height=40, fg_color=t.SURFACE, border_color=t.BORDER, text_color=t.TEXT, font=t.font())
    app.account_entry.grid(row=3, column=0, columnspan=2, pady=(6, 16), sticky='ew')
    buttons = ctk.CTkFrame(panel, fg_color='transparent')
    buttons.grid(row=4, column=0, columnspan=2, sticky='w')
    app.import_files_btn = t.button(buttons, 'ZIP / dosyaları seç', app._select_files, kind='primary', width=180, height=40)
    app.import_files_btn.pack(side='left', padx=(0, 12))
    app.import_folder_btn = t.button(buttons, 'Klasör seç', app._select_folder, width=125, height=40)
    app.import_folder_btn.pack(side='left')
    app.source_label = ctk.CTkLabel(panel, text='', text_color=t.MUTED, font=t.font(12), anchor='w', justify='left', wraplength=750)
    app.source_label.grid(row=5, column=0, columnspan=2, pady=(24, 8), sticky='ew')
    t.button(panel, 'Kalan kişileri görüntüle →', lambda: app.show_section('Kalanlar'), width=220).grid(row=6, column=0, pady=12, sticky='w')
