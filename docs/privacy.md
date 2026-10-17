# Yerel veriler ve gizlilik

İlişki dosyalarının karşılaştırılması yerelde yapılır; bunun için Instagram'a
bağlanılmaz. Tarayıcı işlemleri seçildiğinde uygulama Instagram'a bağlanır.
Bu proje bir sunucu veya analitik/telemetri servisi içermez.

Windows'ta veriler `%LOCALAPPDATA%\InstagramFollowManager` içinde tutulur.
`INSTAGRAMTAKIP_DATA_DIR` ayarlanırsa o klasör kullanılır. Diğer sistemlerde
varsayılan `$XDG_DATA_HOME/InstagramFollowManager` veya
`~/.local/share/InstagramFollowManager` olur; bu sistemlerde destek henüz doğrulanmamıştır.

Klasörde ayarlar, kullanıcı listeleri, işlem geçmişi, loglar ve `ig_profile/`
tarayıcı oturumu bulunabilir. Bunlar şifreli bir kasa değildir. İşletim sistemi
hesabına veya bu klasöre erişimi olan kişilere karşı ayrıca korunmalıdır.

Instagram parolası uygulamada saklanmaz veya bir parola dosyasından okunmaz.
Elle giriş yapılan tarayıcı çerezleri ve oturumu kalıcı profil içinde tutulabilir.
Bu profil, giriş yapmış bir oturum gibi hassas kabul edilmelidir.

Bir sorun bildirirken gerçek kullanıcı adlarını, dosya yollarını, oturum
bilgilerini ve ekran görüntülerindeki kişisel verileri çıkar. Instagram veri
indirmesini veya uygulama veri klasörünü GitHub'a yükleme. Sentetik örneklerle
hatanın nasıl tekrarlandığını göstermek tercih edilir.

Kişisel verileri silmek istersen uygulamayı ve ona ait tarayıcıyı kapatıp veri
klasörünü işletim sistemi üzerinden kaldırabilirsin. Bu işlem yerel işlem
geçmişini ve oturumu kaybettirir; Instagram hesabındaki ilişkileri geri almaz.
