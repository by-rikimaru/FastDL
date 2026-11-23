# FastDL

IDM benzeri, açık kaynak bir Windows indirme yöneticisi. Çok bağlantılı indirir, kaldığı yerden devam eder, hızı sınırlayabilir ve inen RAR/7z/ZIP arşivlerini otomatik çıkartır. Tarayıcı eklentisiyle **fuckingfast** dosya sayfalarındaki indirme linklerini yakalar.

## Özellikler

- **Çok bağlantılı indirme:** dosya başına 1–32 bağlantı, kaldığı yerden devam, bağlantı koparsa yeniden deneme
- **Kuyruk:** Sırada / Bekliyor / Duraklatıldı / Durduruldu durumları, "aynı anda" sınırı, Kuyruğu Başlat/Durdur
- **Hız sınırlayıcı:** dosya başına KB/s sınırı, açılışta otomatik açma seçeneği
- **Arşiv çıkartma:** çok parçalı bir arşivin tüm parçaları inince WinRAR ya da 7-Zip ile çıkartır; şifreliyse şifre sorar ve doğru şifreleri hatırlar
- **Tarayıcı eklentisi (Chrome/Edge):** sayfadaki tüm fuckingfast linklerini tek tıkla gönderir; süresi dolan linki tekrar gönderince kaldığı yerden devam eder
- **IDM tarzı arayüz:** menüler, araç çubuğu, kategoriler, sağ tık menüsü, Ctrl+A
- **Otomatik güncelleme:** açılışta yeni sürümü denetler, tek tıkla günceller

## Kurulum

1. [Releases](https://github.com/by-rikimaru/FastDL/releases/latest) sayfasından `FastDL-Setup-x.y.z.exe` dosyasını indirip çalıştırın.
   - Yönetici izni gerekmez.
   - Bilgisayarda Python yoksa kurulum, içine gömülü Python 3.12'yi kendisi kurar.
   - Kurulum dosyası imzalı olmadığı için Windows SmartScreen uyarı verebilir: **Ek bilgi → Yine de çalıştır**.
2. Tarayıcı eklentisi:
   - Chrome'da `chrome://extensions` (Edge'de `edge://extensions`) adresini açın.
   - **Geliştirici modu**nu açın.
   - **Paketlenmemiş öğe yükle** deyip kurulum klasöründeki `extension` klasörünü seçin. Başlat menüsündeki "FastDL Tarayıcı Eklentisi (klasör)" kısayolu bu klasörü açar.

Arşiv çıkartmak için [WinRAR](https://www.win-rar.com/) ya da [7-Zip](https://www.7-zip.org/) kurulu olmalıdır.

## Kullanım

- **Bir fuckingfast dosya sayfasında:** eklenti simgesi → **Bu dosyayı / sayfadaki tüm fuckingfast linklerini gönder**
- **Çok parçalı oyunlarda:** linklerin listelendiği sayfada sağ tık → **Sayfadaki tüm fuckingfast linklerini FastDL'e gönder**
- **Doğrudan link:** FastDL'de **URL Ekle** (Ctrl+N)

İndirme listesi ve ayarlar `%APPDATA%\FastDL` klasöründe tutulur. Güncelleme ya da yeniden kurulum bunlara dokunmaz.

## Kaynaktan çalıştırma

Python 3.9+ (tkinter dahil) yeterlidir, başka paket gerekmez:

```bash
pythonw fastdl.pyw
```

## Yeni sürüm yayınlama

1. `fastdl.pyw` içindeki `VERSION` değerini artırın.
2. Commit edip etiketi gönderin:

```bash
git tag v1.0.1
```

```bash
git push origin v1.0.1
```

GitHub Actions kurulumu derler ve Release olarak yayınlar. Kurulu FastDL'ler açılışta yeni sürümü görüp güncellemeyi önerir.

## Lisans

[MIT](LICENSE). Yalnızca indirme hakkınız olan dosyalar için kullanın.
