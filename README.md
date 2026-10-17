# Instagram Follow Manager

Instagram veri indirmesindeki takipçi ve takip edilen listelerini yerel olarak
karşılaştıran, takip listelerini yöneten siyah temalı Python masaüstü uygulaması.

**Python kaynak kod projesidir.** EXE veya yükleyici içermez. İlk yayın Windows ve
Python 3.12 hedeflenerek hazırlanmıştır. Arayüz dili Türkçedir.

## Özellikler

- Instagram JSON/HTML dosyalarını, ZIP arşivini veya dışa aktarım klasörünü yükleme.
- Birden fazla `followers_1.json`, `followers_2.json` gibi parçayı birleştirme.
- Geri takip etmeyenleri listeleme; arama, filtreleme, sıralama ve sayfalama.
- Sonuçları TXT/CSV dosyasına kaydetme.
- Mevcut **Takip Et** bölümüyle takip listesini yönetme.
- Kendi hesabınla elle giriş yaptığın görünür tarayıcıda takip bırakma.
- Doğrulanmış işlemleri geçmişe kaydetme ve kalan listeden çıkarma.
- Otomatik sırada her **5 doğrulanmış takip bırakmadan sonra 10 dakika mola**.
- Hata, belirsiz sonuç veya kısıtlama görüldüğünde durma; molada da Durdur desteği.

## Kurulum — Windows / PowerShell

Python **3.12**, Git veya GitHub'dan indirilmiş kaynak kod ve internet bağlantısı gerekir.
Depo klasöründe:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe main.py
```

Sanal ortamı etkinleştirmek zorunlu değildir. Böylece PowerShell yürütme ilkesi
değiştirmeden çalıştırabilirsin. Alternatif giriş: `python -m instagram_bot`.

## İlk kullanım

1. Instagram'dan kendi hesabının **tüm zamanlara ait** takipçi ve takip edilen
   verilerini JSON veya HTML biçiminde indir.
2. Uygulamada **Veri yükle** bölümünden ZIP/klasör seç veya takipçi parçalarının
   tamamıyla `following.json` / `following.html` dosyasını birlikte seç.
3. Kendi Instagram kullanıcı adını doğrula. **Geri takip etmeyenler** bölümünü aç.
4. Yalnız karşılaştırma ve dışa aktarma için Instagram oturumu gerekmez.
5. Tarayıcı işlemi yapacaksan uygulamadaki oturum düğmesiyle tarayıcıyı aç,
   kendi hesabına elle giriş yap ve seçtiğin işlemi başlat.

İndirmede aranacak tipik konum:

```text
connections/followers_and_following/
  followers_1.json
  followers_2.json   # Varsa diğer parçaları da ekle
  following.json
```

**Örnek deneme:** `examples/demo-export/` klasörünü yükle ve hesap adı olarak
`demo_owner` gir. Karşılaştırmada `demo_candidate` kalır. Örnek adlar sentetiktir;
gerçek Instagram hesaplarıyla eşleşmeyeceği garanti edilmez. Örnekleri yalnızca
çevrimdışı karşılaştırmada kullan; bunlarla tarayıcı işlemi başlatma.

## İşlemler ve sonuçlar

**Takip Et** ayrı bir bölümdür; takip listesi ve günlük takip ayarları burada
yönetilir. Takip etme ve takip bırakma aynı iş kuyruğunu kullanır.

Otomatik takip bırakma kalan listenin tamamını sırayla işler; arama filtresi ve
görüntülenen sayfa bu sırayı daraltmaz. Günlük takip bırakma üst sınırı yoktur.
Her beş başarılı işlemden sonra sonraki kişiye geçmeden 10 dakika beklenir.
Bekleme hesabı kayıtlı başarılı işlemlerden yapılır. Yeniden açılışta otomatik
devam edilmez; kullanıcının yeniden başlatması gerekir.

| Sonuç | Kalan liste | Başarılı takip bırakma sayacı |
| --- | --- | --- |
| Takibi bırakıldığı doğrulandı | Çıkarılır | Artar |
| Zaten takip edilmiyor | Çıkarılır | Artmaz |
| Açıkça ulaşılamayan profil | Atlanır, geçmişte ayrı tutulur | Artmaz |
| Hata / doğrulanamayan sonuç / kısıtlama | Korunur, otomatik sıra durur | Artmaz |

Dosyalardaki ilişkiler indirme tarihine aittir. Eski veya eksik dosyalar güncel
ilişki durumunu göstermeyebilir. Ulaşılamayan bir profil, takibinin bırakıldığını
kanıtlamaz. Instagram'ın arayüz değişiklikleri tarayıcı işlemlerini bozabilir.

## Verilerin saklandığı yer

Windows'ta varsayılan veri klasörü:

```text
%LOCALAPPDATA%\InstagramFollowManager
```

Kaynak kod, kişisel veri veya hazır oturum içermez. Uygulama çalışırken listeler,
işlem geçmişi, loglar ve girişten sonra tarayıcı profili bu veri klasöründe oluşur.
Uygulama Instagram parolanı dosyadan okumaz; giriş tarayıcıda elle yapılır.
Tarayıcı profili oturumu koruyabilir; bu klasörü paylaşma veya Git'e ekleme.

Geliştirme için veri konumu değiştirilebilir:

```powershell
$env:INSTAGRAMTAKIP_DATA_DIR = "$PWD\runtime-data"
.\.venv\Scripts\python.exe main.py
```

Kaynak kodun güncellenmesi veri klasörünü temizlemez. Başka bir kurulumun kişisel
verileri otomatik taşınmaz. Ayrıntılar: [gizlilik](docs/privacy.md),
[sorun giderme](docs/troubleshooting.md).

## Geliştirme ve test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.\.venv\Scripts\python.exe tests/gui_smoke.py
```

Testler geçici dosyalar, taklit servisler veya yerel tarayıcı sayfaları kullanır;
gerçek bir Instagram hesabında takip işlemi yapmaz. Playwright testleri için
Chromium kurulmuş olmalıdır. Arayüz testleri masaüstü oturumu gerektirir.
Bazı DOM testleri bekleme senaryoları nedeniyle birkaç dakika sürebilir.

```text
main.py             Uygulama girişi
instagram_bot/      Arayüz, veri okuma, kayıt ve tarayıcı iş akışları
tests/              Çevrimdışı testler
examples/           Sentetik dışa aktarım örnekleri
docs/               Gizlilik ve sorun giderme
```

Katkı için [CONTRIBUTING.md](CONTRIBUTING.md), güvenlik bildirimi için
[SECURITY.md](SECURITY.md) dosyasını incele.

## Proje hakkında

Instagram veya Meta ile resmî bağlantısı yoktur. Mola düzeni Instagram'ın ilan
ettiği güvenli bir limit değildir ve hesabın kısıtlanmayacağını garanti etmez.
Görülen kısıtlamalar aşılmaya çalışılmaz; işlem durur.

Kod [MIT lisansı](LICENSE) ile sunulur. Bağımlılıkların lisansları kendilerine aittir.

---

**English:** A Turkish-language Python desktop app for locally comparing Instagram
export files and managing follow lists. Includes JSON/HTML/ZIP import, TXT/CSV
export, a dark UI, and browser operations through a manually authenticated
session. Source distribution only; Windows/Python 3.12 is the initial target.
The repository includes synthetic examples, not personal lists or browser sessions.
