# Sorun giderme

## Chromium bulunamıyor

Sanal ortamın Python'u ile `python -m playwright install chromium` çalıştır.
Paketin kurulu olması tarayıcı ikilisinin kurulu olduğu anlamına gelmez.

## Program zaten açık

Aynı veri klasörünü kullanan bir uygulama zaten çalışıyor olabilir. Önce açık
pencereye dön. Oturum profili veya veri dosyaları kullanılırken silinmemeli.

## Dosyalar yüklenmiyor veya sonuç beklediğim gibi değil

Takipçi dosyalarının bütün numaralı parçalarını ve takip edilen dosyasını ekle.
İndirmenin tüm zamanları kapsadığını ve aynı hesaba ait olduğunu doğrula.
Profildeki güncel sayılarla eski indirmenin aynı olması beklenmez.
ZIP tümüyle diske çıkarılmadan okunur; boyut ve girdi sayısı sınırları vardır.

## Kullanıcı adı veya ilişki düğmesi doğrulanamadı

Uygulamanın açtığı tarayıcıda doğru hesaba giriş yaptığını kontrol et. Normal
Chrome penceresinde açık olan oturum otomatik kullanılmaz. Instagram sayfası
veya düğme yapısı değişmiş olabilir. Kişi kalan listede tutulur; otomatik tekrar
yapılmaz. Hata bildirirken hesabını ve diğer kişileri anonimleştir.

## Bu sayfaya ulaşılamıyor

Doğru hedefte açıkça görülen ulaşılamayan profil atlanır ve geçmişe ayrı kaydedilir.
Bu, takibin bırakıldığı anlamına gelmez. Diğer yükleme ve doğrulama hatalarında
otomatik sıra durur.

## Molada ekran değişmiyor

Her beş doğrulanmış takip bırakmadan sonra 10 dakika beklenir. Geri sayım
biterken sonraki hedefe geçilir. Durdur veya Escape ile bekleme kesilebilir.
Liste bitmişse son beşlinin ardından gereksiz mola verilmez.

## Instagram kısıtlama gösteriyor

İşleme devam etmeye çalışma. Uygulama görülen kısıtlamalarda durur. Mola ayarı
hesabın kısıtlanmayacağının garantisi değildir.

## Kaynak kodu güncelledim, eski kayıtlar görünüyor

Kayıtlar kaynak koddan ayrı veri klasöründe korunur. Yeni kurulum eski çalışma
klasöründeki verileri kendiliğinden taşımaz. Yedek almadan veri dosyalarını
değiştirme veya birbirine karıştırma.
