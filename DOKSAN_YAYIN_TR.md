# Doksan Artı: Otomatik YouTube Yayını

Akış: **üretim → kalite kontrol → (düzeltme) → yükleme kuyruğu → YouTube'a yükleme → işlenme ve görünürlük kontrolü → kayıt**

Servis `doksan-yayin` adlı Docker konteynerinde sürekli çalışır. Sohbetin açık olmasına gerek yoktur. Sunucu ya da konteyner yeniden başlarsa iş kuyruktan devam eder.

---

## 1. İlk kurulum (yalnızca bir kez)

### A) Google Cloud tarafı (telefondan console.cloud.google.com)
1. Yeni bir proje oluştur (örn. `doksan-arti-yayin`).
2. **APIs & Services → Library → "YouTube Data API v3" → Enable**.
3. **Google Auth Platform → Branding**: uygulama adı ve e-posta adresini gir.
4. **Audience**: *External* seç ve **Publish app** düğmesiyle durumu **In production** yap.
   Durum *Testing* kalırsa yenileme tokenı 7 günde bir geçersizleşir ve yükleme durur.
   "Doğrulanmamış uygulama" uyarısı çıkarsa **Gelişmiş → devam et** de. Uygulamayı yalnızca sen kullandığın için bu bir sorun oluşturmaz.
5. **Clients → Create client → "TVs and Limited Input devices"**. Çıkan **Client ID** ve **Client secret** değerlerini kopyala.

### B) Sunucu tarafı
```bash
cd /opt/mrycns/MoneyPrinterTurbo
git pull
sudo mkdir -p /opt/mrycns/secrets/doksan-arti && sudo chmod 700 /opt/mrycns/secrets/doksan-arti
docker compose up -d --build doksan-yayin
docker exec -it doksan-yayin python3 -m doksan_yayin yetkilendir
```
`yetkilendir` komutu şunları yapar:
1. Client ID ve secret değerlerini ister. Bunlar depoya değil `/opt/mrycns/secrets/doksan-arti/` klasörüne 0600 izinle kaydedilir.
2. Ekrana bir **kod** yazar. Telefonda **google.com/device** adresini aç, kodu gir, Doksan Artı kanalının bağlı olduğu hesabı ya da marka hesabını seç ve izin ver.
3. Bağlanan kanalın adını gösterir ve **"Doğru mu? [e/h]"** diye sorar. `e` dersen kanal kimliği kaydedilir. Sistem her yüklemeden önce bu kimliği kontrol eder ve başka bir kanala yükleme yapmaz.

> Cihaz kodu yöntemi hata verirse: Masaüstü türünde bir istemci oluştur ve `yetkilendir --yontem tarayici` komutunu çalıştır. İzin verdikten sonra açılan "siteye ulaşılamıyor" sayfasının adresini kopyalayıp terminale yapıştır.

### C) Herkese açık yayın için YouTube denetimi (ÖNEMLİ)
YouTube, **denetimden geçmemiş API projeleriyle yüklenen videoları otomatik olarak "özel" kilitler.** Denetim formu: https://support.google.com/youtube/contact/yt_api_form

Denetim tamamlanana kadar sistem şöyle davranır:
- İlk videoyu yükler ve sonucunu bekler. Henüz hiçbir video herkese açık doğrulanmadıysa ikinci videoyu göndermez.
- Video özel kilitlenirse durumu `kisitli` olarak kaydeder, **kuyruğu durdurur**, bildirim gönderir ve `ALARM.txt` dosyasına yazar. Kalan videolar kuyrukta güvenle bekler.
- Denetim onaylandığında şu komutları çalıştır:
  ```bash
  docker exec doksan-yayin python3 -m doksan_yayin devam
  # kilitlenen videoyu YouTube Studio'dan sildikten sonra:
  docker exec doksan-yayin python3 -m doksan_yayin yeniden-dene <uretim_id> --yeniden-yukle
  ```

---

## 2. Üretim hattı videoları nasıl teslim eder

**Yol 1: Hazır klasörü (önerilen)**
`storage/doksan-arti/hazir/<uretim_id>/` klasörüne şu dosyalar konur:
- `video.mp4`: final render
- `manifest.json`: aşağıdaki örneğe bakın
- `TAMAM`: **en son** yazılır. Bu dosya yoksa video kuyruğa alınmaz, yani render bitmeden yükleme başlamaz.

Ya da tek komutla:
```bash
python3 -m doksan_yayin kuyruga-ekle --video final.mp4 --manifest manifest.json --uretim-id da-2026-11-22-fb-01
```

`manifest.json` örneği:
```json
{
  "uretim_id": "da-2026-11-22-fb-01",
  "konu": "Fenerbahçe 2000-01: şampiyonluğu getiren dönüm noktası",
  "senaryo": "Seslendirme metninin tamamı…",
  "kaynaklar": [{"ad": "TFF", "url": "https://www.tff.org/"}, "https://tr.wikipedia.org/wiki/…"],
  "etiketler": ["Fenerbahçe", "Süper Lig tarihi"],
  "altyazi": "altyazi.srt", "font": "BeVietnamPro-Bold.ttf", "punto": 60,
  "metinler": [{"metin": "2000-01 ŞAMPİYONLUK", "font": "BeVietnamPro-Bold.ttf", "punto": 110}],
  "kalite": {"durum": "gecti", "sorunlar": []}
}
```
- `baslik` ve `aciklama` verilmezse sistem Türkçe başlık, açıklama ve etiketleri konuya ve senaryoya göre kendisi hazırlar. Kaynaklar açıklamaya **Kaynaklar:** başlığıyla eklenir.
- `kalite` alanı mevcut kalite kontrolünün sonucudur. `"durum": "kaldi"` ise video yüklenmez ve düzeltme kuyruğuna gider.

**Yol 2: MoneyPrinterTurbo görevleri**
`DOKSAN_MPT_KONU_DESENI` ortam değişkeni ayarlanırsa (örn. `(?i)doksan|fenerbah|galatasaray|beşiktaş|trabzon`) konusu bu desene uyan MPT görevlerinin `final-*.mp4` dosyaları kendiliğinden kuyruğa alınır. Kaynak bağlantıları görev klasöründeki `doksan.json` dosyasına yazılır: `{"kaynaklar": ["https://…"]}`. Tek bir görevi elle eklemek için:
`python3 -m doksan_yayin mpt-gorevi-ekle <gorev_id> --kaynak https://…`

---

## 3. Kalite kontrolü ve düzeltme
Her video yüklenmeden önce şu kontrollerden geçer:
- Dosyanın tamamı çözümlenir; bozuk dosya yakalanır.
- Ses akışının varlığına, sesin sessiz olup olmadığına ve videodan kısa olup olmadığına bakılır.
- Videonun dikey olması ve 180 saniyeyi aşmaması gerekir.
- Altyazı ve ekran yazılarının taşıp taşmadığı ölçülür.
- Fontun Türkçe karakterleri (ğ, ş, ı, İ…) çizip çizemediği kontrol edilir.
- Mevcut kalite kontrolünün raporu dikkate alınır.
- Kaynak bağlantısı yoksa video yüklenmez.

Kalite kontrolünden geçemeyen videolar için:
- **MPT videoları otomatik düzeltilir.** Aynı senaryoyla, soruna göre ayarlanmış parametrelerle (daha küçük punto, Türkçe destekli font, yeniden ses) yeniden render alınır. Bunun için en fazla 2 hak vardır. Haklar biterse durum `elle_duzeltme` olur ve bildirim gönderilir.
- **Hazır klasörü videolarında** sorun raporu `storage/doksan-arti/duzeltme_kuyrugu/<id>.json` dosyasına yazılır. Düzeltilmiş video aynı `uretim_id` ile yeniden bırakılınca kontrol baştan yapılır.

## 4. Çift yükleme koruması ve hata yönetimi
- Her videonun kalıcı üretim kimliği, SHA-256 dosya özeti ve YouTube video kimliği saklanır. Aynı dosya ya da zaten yüklenmiş bir kimlik ikinci kez gönderilmez.
- Yükleme **devam ettirilebilir** yöntemle ve 8 MB'lık parçalarla yapılır. Bağlantı koparsa kaldığı bayttan devam eder. Oturum kaybolursa önce kanalda gizli işaret etiketiyle arama yapılır, video zaten yüklenmişse tekrar yüklenmez.
- **Geçici hatalarda** (ağ kesintisi, 5xx yanıtları, hız sınırı) süre giderek uzatılarak yeniden denenir.
- **Kota dolarsa** videolar kuyrukta kalır. Kota Pasifik saatiyle gece yarısı (Türkiye saatiyle sabah 10–11 civarı) sıfırlanınca devam edilir.
- **Yetki hatasında** (token iptal edilmiş ya da yanlış kanal) yükleme durur. `ALARM.txt` dosyasına yazılır ve bildirim gönderilir. `yetkilendir` komutu tekrar çalıştırılınca servis kendiliğinden devam eder.
- Dosyanın gönderilmesi başarı sayılmaz. YouTube'un videoyu **işlediği** (`processed`) ve görünürlüğünün gerçekten `public` olduğu doğrulanır. Ancak bundan sonra durum **YAYINDA** olur.

## 5. Durum ve kayıtlar
```bash
docker exec doksan-yayin python3 -m doksan_yayin durum              # özet ve son videolar
docker exec doksan-yayin python3 -m doksan_yayin durum <uretim_id>  # tek video ve olay geçmişi
docker logs -f doksan-yayin
```
- `storage/doksan-arti/sonuclar.jsonl`: her video için bağlantı ve sonuç
- `storage/doksan-arti/durum.json`: anlık özet
- `storage/doksan-arti/ALARM.txt`: yalnızca müdahale gerektiğinde oluşur

**Telefona bildirim (isteğe bağlı):** ntfy uygulamasını kur ve tahmin edilmesi zor bir konuya abone ol. Ardından sunucudaki `.env` dosyasına `DOKSAN_BILDIRIM_URL=https://ntfy.sh/<konu-adin>` satırını ekle ve `docker compose up -d doksan-yayin` komutunu çalıştır.

## 6. Ayarlar (`.env` ya da ortam değişkeni)
| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `DOKSAN_GORUNURLUK` | `public` | Hedef görünürlük |
| `DOKSAN_MPT_KONU_DESENI` | boş | MPT görevlerini otomatik alma deseni |
| `DOKSAN_KAYNAK_ZORUNLU` | `true` | Kaynak bağlantısı yoksa yükleme yapılmaz |
| `DOKSAN_BILDIRIM_URL` | boş | ntfy konusu |
| `DOKSAN_GIZLI_HOST` | `/opt/mrycns/secrets/doksan-arti` | Gizli dosyaların sunucudaki yeri |

**Güvenlik:** Depo herkese açık. `client_secret.json`, `token.json` ve `kanal.json` dosyaları depo dışında tutulur ve `.gitignore` tarafından da engellenir. Gizli dizin yanlışlıkla depo içine ayarlanırsa servis çalışmayı reddeder.
