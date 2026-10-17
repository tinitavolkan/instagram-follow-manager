# Katkıda bulunma

Küçük ve tek bir soruna odaklanan pull request'ler gönder. Hata bildirirken
beklenen davranışı, gerçekleşen davranışı, Python/Windows sürümünü ve sentetik
verilerle tekrar oluşturma adımlarını yaz.

- Gerçek dışa aktarım, parola, çerez, tarayıcı profili veya kişisel log ekleme.
- Testler gerçek hesapta takip veya takip bırakma işlemi yapmamalı.
- Arayüzde siyah temayı ve mevcut Takip Et bölümünü koru.
- Kullanıcı durdurduğunda veya doğrulama belirsiz olduğunda otomatik tekrar ekleme.
- Kurulum ve test komutları README'de bulunur. Arayüz değişikliklerinde
  `tests/gui_smoke.py` kontrolünü de çalıştır.

Kod yapısı: `export_lists.py` dosya okuma; `unfollow_store.py` atomik kayıt;
`unfollow_worker.py` seri iş kuyruğu; `unfollowing.py` profil işlemi;
`gui.py`, `ui_shell.py`, `ui_theme.py` arayüz; `follow_panel.py` takip bölümüdür.

Eski takip iş akışının ortak kullandığı tarama ve depolama yardımcıları hâlâ
pakettedir. Kullanılmıyor gibi görünen bir modülü kaldırmadan önce çağrılarını
ve testlerini incele.
