"""Exercise the import/unfollow desktop GUI using temporary data, fully offline."""
import csv
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def wait_for(app, predicate, detail):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        app.update()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(detail)


def check_import_and_exports(app, folder):
    app.show_section("Dosyalar")
    app.update()
    assert app.account_entry.winfo_viewable(), "Import controls must be available with the offline lists"
    fixture = folder / "export-fixture"
    fixture.mkdir()

    def entry(username):
        return {"title": "", "string_list_data": [{"value": username,
                "href": f"https://www.instagram.com/{username}/", "timestamp": 1}]}

    (fixture / "followers_1.json").write_text(json.dumps([entry("alpha")]), encoding="utf-8")
    (fixture / "followers_2.json").write_text(json.dumps([entry("delta")]), encoding="utf-8")
    (fixture / "following.json").write_text(json.dumps({"relationships_following": [
        entry("alpha"), entry("beta"), entry("charlie")]}), encoding="utf-8")
    app.account_var.set("smoke_account")
    with patch("instagram_bot.gui.filedialog.askdirectory", return_value=str(fixture)):
        app.import_folder_btn.invoke()
    wait_for(app, lambda: app.snapshot_data.get("remaining") == ["beta", "charlie"],
             "Import should combine every followers_N file and calculate the difference")
    wait_for(app, lambda: not app._is_busy(), "Local import should finish")
    app.show_section('Kalanlar')
    app.update()
    assert not app.account_entry.winfo_viewable(), "Import form belongs on its own page"
    assert app.nav_buttons['Kalanlar'].cget('fg_color') != app.nav_buttons['Dosyalar'].cget('fg_color')
    assert app.counters["followers"].cget("text") == "2"
    assert app.counters["following"].cget("text") == "3"
    assert app.counters["mutual"].cget("text") == "1"
    assert app.counters["remaining"].cget("text") == "2"
    assert app.batch_btn.cget("text") == "Otomatik takibi bırak"
    assert app.batch_btn.winfo_ismapped()
    assert not hasattr(app, "check_session_btn"), "Account checks should be automatic, not a replacement for the action"
    assert app.account_var.get() == "smoke_account"
    assert set(app.remaining_rows) == {"beta", "charlie"}
    assert "followers_1.json" in app.source_label.cget("text")
    assert "followers_2.json" in app.source_label.cget("text")
    assert not (folder / "idsifre.txt").exists()
    assert (folder / "geri_takip_verileri.json").exists()

    for extension in ("txt", "csv"):
        destination = folder / ("remaining." + extension)
        with patch("instagram_bot.gui.filedialog.asksaveasfilename", return_value=str(destination)):
            app._export_remaining(extension)
        if extension == "txt":
            saved = destination.read_text(encoding="utf-8")
            assert "@beta" in saved and "@charlie" in saved and "@alpha" not in saved
        else:
            with destination.open(encoding="utf-8", newline="") as stream:
                assert list(csv.reader(stream)) == [
                    ["username", "profile_url"],
                    ["beta", "https://www.instagram.com/beta/"],
                    ["charlie", "https://www.instagram.com/charlie/"],
                ]


def check_events_and_rows(app):
    original = app.snapshot_data.copy()
    many = dict(original, remaining=[f"person_{index:03}" for index in range(125)], candidate_count=125)
    app.events.put({"type": "snapshot", "data": many})
    wait_for(app, lambda: app.counters["remaining"].cget("text") == "125", "Snapshot must update total")
    assert 1 <= len(app.remaining_rows) <= 100, "Render a bounded page, not thousands of Tk widgets"
    first_page = set(app.remaining_rows)
    app.next_btn.invoke()
    assert set(app.remaining_rows) != first_page
    app.search_var.set("person_124")
    app.update()
    assert set(app.remaining_rows) == {"person_124"}
    assert app.counters["remaining"].cget("text") == "125", "Searching must not change the remaining total"
    app.search_var.set("")
    app._render_snapshot(original)

    app.events.put({"type": "status", "state": "busy", "detail": "@beta işleniyor", "username": "beta"})
    wait_for(app, lambda: app.account_entry.cget("state") == "disabled", "Account must lock while busy")
    assert app.import_folder_btn.cget("state") == "disabled"
    assert app.import_files_btn.cget("state") == "disabled"
    assert app.stop_btn.cget("state") == "normal"
    assert all(row["button"].cget("state") == "disabled" for row in app.remaining_rows.values())
    assert app.export_txt_btn.cget("state") == "disabled"
    assert app.follow_btn.cget("state") == "disabled", "Follow and unfollow must share the operation lock"
    assert app.batch_btn.cget("state") == "disabled"
    app._handle_event({'type': 'status', 'state': 'busy', 'phase': 'waiting', 'username': 'beta', 'detail': 'Mola'})
    assert app.status_label.cget('text') == 'Molada'
    assert app.remaining_rows['beta']['button'].cget('text') == 'Bekleniyor…'
    assert app.counters['remaining'].cget('text') == '2'

    app.events.put({"type": "status", "state": "attention", "detail": "Olası kısıtlama; kişi kalanlarda tutuldu."})
    wait_for(app, lambda: "kişi kalanlarda" in app.status_detail.cget("text"), "Restriction explanation must show")
    assert app.counters["remaining"].cget("text") == "2"
    assert set(app.remaining_rows) == {"beta", "charlie"}

    updated = dict(original, remaining=["charlie"], history=[{
        "username": "beta", "status": "unfollowed", "detail": "Takibi bırakma doğrulandı.",
        "at": "2026-10-16T12:00:00+03:00", "submitted": True,
    }])
    app.events.put({"type": "snapshot", "data": updated})
    app.events.put({"type": "status", "state": "idle", "detail": "Takibi bırakma doğrulandı."})
    wait_for(app, lambda: app.counters["remaining"].cget("text") == "1", "Only successful snapshot removes a row")
    assert set(app.remaining_rows) == {"charlie"}
    app.show_section("Geçmiş")
    app.update()
    history = app.history_text.get("1.0", "end")
    assert "beta" in history and "2026-10-16" in history and "doğrulandı" in history
    assert app.history_text._textbox.cget("state") == "disabled"
    app.show_section("Kalanlar")
    app._render_snapshot(original)

    # A per-person command goes only to the external-action boundary.
    requested = []
    with patch.object(app.controller, "unfollow", side_effect=lambda username: requested.append(username) or True):
        app.remaining_rows["beta"]["button"].invoke()
    assert requested == ["beta"]
    assert app.counters["remaining"].cget("text") == "2", "Clicking alone must not claim success"
    app._handle_event({"type": "status", "state": "idle", "detail": "Hazır"})
    app._handle_event({"type": "session", "username": "smoke_account", "detail": "Oturum doğrulandı."})
    assert "smoke_account" in app.session_label.cget("text")
    batches = []
    with patch.object(app.controller, "unfollow_batch", side_effect=lambda: batches.append(True) or True):
        app.batch_btn.invoke()
    assert batches == [True], "Automatic start should dispatch once through the shared worker"
    assert app.counters["remaining"].cget("text") == "2", "Batch start alone is not success"
    app._handle_event({"type": "status", "state": "idle", "detail": "Hazır"})


def check_follow_panel(app):
    app.show_section("Takip Et")
    app.update()
    assert app.follow_btn.winfo_ismapped(), (
        "The restored follow action must be visible", app.tabs.get(),
        {title: (app.tabs.tab(title).winfo_ismapped(), app.tabs.tab(title).winfo_manager())
         for title in ("Takip Et", "Kalanlar", "Geçmiş")},
    )
    settled = []
    app.after(150, lambda: settled.append(True))
    wait_for(app, lambda: settled, "Tab layout should settle")
    assert app.follow_btn.winfo_ismapped(), "An older deferred tab update must not hide the active follow panel"
    assert not app.batch_btn.winfo_ismapped(), "Unfollow start should not crowd the restored follow tab"
    assert app.follow_panel.settings_btn.winfo_ismapped(), "Follow settings must remain accessible"
    assert app.follow_panel.history_btn.winfo_ismapped(), "Follow history must remain accessible"
    callbacks = []
    with patch.object(app.controller, "follow", side_effect=lambda callback: callbacks.append(callback) or True):
        app.follow_btn.invoke()
    assert len(callbacks) == 1 and callable(callbacks[0]), "Follow must dispatch a real operation through the shared controller"
    assert app.counters["remaining"].cget("text") == "2", "Follow controls must not alter the offline candidate list"
    app._handle_event({"type": "status", "state": "idle", "detail": "Hazır"})
    app._handle_event({"type": "busy", "busy": False})


def check_sidebar_and_filters(app):
    from instagram_bot import ui_theme
    import customtkinter as ctk
    assert app.cget('fg_color') == ui_theme.BG
    assert ctk.get_appearance_mode().casefold() == 'dark'
    original = app.snapshot_data.copy()
    app._render_snapshot(dict(original, remaining=['alpha', 'beta', 'charlie'], last_results={
        'beta': {'status': 'unconfirmed', 'detail': 'Sonuç doğrulanamadı.'}}))
    app.show_section('Kalanlar')
    app.filter_menu.set('Kontrol gerekenler')
    app._search_changed()
    assert set(app.remaining_rows) == {'beta'}
    assert app.counters['remaining'].cget('text') == '3', 'A view filter must not delete pending people'
    details = []
    with patch.object(app.follow_panel, '_information_window', side_effect=lambda *args: details.append(args)):
        app.remaining_rows['beta']['badge'].invoke()
    assert details == [('@beta', 'Sonuç doğrulanamadı.')]
    app.filter_menu.set('İşlem görmeyenler')
    app._search_changed()
    assert set(app.remaining_rows) == {'alpha', 'charlie'}
    app._clear_filters()
    app.sort_btn.invoke()
    assert list(app.remaining_rows) == ['charlie', 'beta', 'alpha']
    app.sort_btn.invoke()
    assert list(app.remaining_rows) == ['alpha', 'beta', 'charlie']
    app.search_var.set('no_match')
    app.update()
    assert app.remaining_rows == {}
    assert app.counters['remaining'].cget('text') == '3'
    app._clear_filters()
    app._render_snapshot(original)
    app.log_toggle_btn.invoke()
    app.update()
    assert app.log_text.winfo_viewable()
    app.log_toggle_btn.invoke()
    app.update()
    assert not app.log_text.winfo_viewable()
    for geometry in ('1100x700', '1366x768', '1920x1080'):
        app.geometry(geometry)
        for section, controls in (
            ('Takip Et', (app.follow_btn, app.follow_panel.settings_btn, app.follow_panel.listbox)),
            ('Kalanlar', (app.batch_btn, app.search_entry, app.next_btn)),
            ('Geçmiş', (app.history_text, app.history_next_btn)),
            ('Dosyalar', (app.account_entry, app.import_files_btn)),
        ):
            app.nav_buttons[section].invoke()
            app.update()
            assert app.tabs.get() == section
            assert sum(app.tabs.tab(name).winfo_viewable() for name in app.nav_buttons) == 1
            for control in (*controls, app.stop_btn, app.status_detail, app.session_btn):
                assert control.winfo_viewable(), (geometry, section, control)
                x = control.winfo_rootx() - app.winfo_rootx()
                y = control.winfo_rooty() - app.winfo_rooty()
                assert 0 <= x and x + control.winfo_width() <= app.winfo_width(), (geometry, section, 'horizontal', x, control.winfo_width())
                assert 0 <= y and y + control.winfo_height() <= app.winfo_height(), (geometry, section, 'vertical', y, control.winfo_height())
    app.show_section('Takip Et')


def run_isolated():
    folder = Path(os.environ["INSTAGRAMTAKIP_DATA_DIR"])
    sys.path.insert(0, str(ROOT))
    with patch.object(socket.socket, "connect", side_effect=AssertionError("GUI smoke must stay offline")):
        from instagram_bot.gui import InstaGUI

        (folder / "Takiplist.txt").write_text("follow_target\n", encoding="utf-8")
        app = InstaGUI()
        try:
            assert hasattr(app, "controller"), "New import controller and interface are missing"
            assert hasattr(app, "follow_panel"), "The original follow controls must be restored beside the new workflows"
            assert app.follow_btn is app.follow_panel.follow_btn
            assert app.tabs.get() == "Takip Et", "The follow controls should be easy to find when the app opens"
            app.update()
            assert not app.account_entry.winfo_viewable(), "Offline import controls must not crowd the follow panel"
            assert app.session_btn.winfo_ismapped() and app.stop_btn.winfo_ismapped()
            assert all(app.tabs.tab(title) is not None for title in ("Takip Et", "Kalanlar", "Geçmiş"))
            assert not hasattr(app, "non_followers_scan_btn"), "Live list scanning must leave the interface"
            assert app.account_var.get() == ""
            assert app.counters["remaining"].cget("text") == "0"
            app.worker_thread = threading.Thread(target=app.worker_loop, daemon=True)
            app.worker_thread.start()
            check_import_and_exports(app, folder)
            check_events_and_rows(app)
            check_follow_panel(app)
            check_sidebar_and_filters(app)
            for geometry in ("1180x780", "980x680"):
                app.geometry(geometry)
                app.show_section("Takip Et")
                app.update()
                assert app.follow_btn.winfo_ismapped()
                assert app.tabs.winfo_height() >= 250, "The original follow controls need room at minimum size"
                app.show_section("Kalanlar")
                app.update()
                assert app.tabs.winfo_height() >= 150, "List area must remain usable at minimum size"
                assert not app.account_entry.winfo_viewable()
                assert app.status_detail.winfo_ismapped()
            app.events.put({"type": "unknown"})
            app.events.put({"type": "log", "detail": "Smoke: olay kuyruğu çalışıyor."})
            wait_for(app, lambda: "olay kuyruğu" in app.log_text.get("1.0", "end"), "Event processing must survive unknown events")
            recovered = []

            def broken_callback():
                raise RuntimeError("Smoke: örnek callback hatası")

            app.call_in_main_thread(broken_callback)
            app.call_in_main_thread(recovered.append, "sağlıklı güncelleme")
            wait_for(app, lambda: recovered == ["sağlıklı güncelleme"], "A failed callback must not block the next update")
            wait_for(app, lambda: "örnek callback hatası" in app.log_text.get("1.0", "end"), "Callback failures must be visible in the log")
            print("GUI_SMOKE_OK: offline fixture import, all follower parts, counts, source, pagination/search, "
                  "busy locks, remaining/history snapshots, one-person and batch dispatch, TXT/CSV, restored follow dispatch, "
                  "black theme, sidebar, filters/sort/detail, responsive bounds, callback recovery")
        finally:
            app.on_closing()
            deadline = time.monotonic() + 5
            while app.worker_thread is not None and app.worker_thread.is_alive() and time.monotonic() < deadline:
                app.update()
                time.sleep(0.01)
            try:
                app.update()
                app.destroy()
            except Exception:
                pass


def main():
    with TemporaryDirectory(prefix="instagram-unfollow-gui-") as folder:
        env = os.environ.copy()
        env.update(INSTAGRAMTAKIP_DATA_DIR=folder, PYTHONUTF8="1")
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--isolated"],
                                cwd=ROOT, env=env, timeout=30, check=False)
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    run_isolated() if sys.argv[1:] == ["--isolated"] else main()
