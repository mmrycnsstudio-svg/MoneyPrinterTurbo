# MoneyPrinterTurbo + Blender

Bu fork, MoneyPrinterTurbo ve Blender'ı aynı Linux ortamında kurmak için hazırlanmıştır.

## Telefondan Claude Code

1. https://claude.ai/code adresini aç.
2. Depo olarak `mmrycnsstudio-svg/MoneyPrinterTurbo`, dal olarak `main` seç.
3. Yeni, tek depolu bir bulut oturumu başlat. Depodaki SessionStart hook'u kurulumu başlatır. Kurulumun paket kayıt depolarına ve Ubuntu/Debian paket sunucularına internet erişimi gerekir.
4. Claude'a şunu yaz: **`bash scripts/setup-video-tools.sh komutunu çalıştır. Ardından MoneyPrinterTurbo arayüzünü başlat; storage/video-tools/blender-smoke.mp4 dosyasını ve kurulum sonucunu göster.`**

Claude Projects içindeki bulut konuşmalarında ve birden fazla depo kullanan oturumlarda depo hook'ları çalışmaz. Bu durumda aynı komutu Claude'a yazmak yeterlidir. Kurulum tamamlanmadan başarılı olduğunu varsayma; gerçek komut çıktısını kontrol et.

## Kurulum ve kontrol

```bash
bash scripts/setup-video-tools.sh
bash scripts/check-video-tools.sh
bash webui.sh
```

Kurulum, eksikse Blender ve FFmpeg'i `apt-get` ile yükler; bunun için root veya parolasız sudo gerekir. Python 3.11+ gerekir. MoneyPrinterTurbo'nun `uv.lock` dosyasındaki sürümler proje içindeki `.venv` ortamına kurulur. Mevcut `config.toml` değiştirilmez; yoksa örnekten oluşturulur.

Kontrol komutu gerçek bir Blender CPU render'ı üretir, FFmpeg ile bir saniyelik dikey MP4 oluşturur ve boyutunu/kare sayısını doğrular. Sonuçlar:

- `storage/video-tools/blender-smoke.mp4`
- `storage/video-tools/blender-smoke.blend`

Arayüz varsayılan olarak ortam içinde `http://127.0.0.1:8501` adresinde çalışır. Telefonda bu adres telefonun kendisini gösterir; uzaktaki ortamın desteklediği özel önizleme/port yönlendirmesi gerekir. Bu kurulum herkese açık bir adres oluşturmaz.

## Kurulumun kapsamı

GitHub kaynak kodunu ve kurulum dosyalarını saklar. Programlar Claude'un Linux çalışma ortamında çalışır. Yeni bir ortamda kurulum tekrar gerekebilir; bu dosyalar yeniden kurulumu sağlar. Bu, sürekli çalışan bir VPS yayını değildir.

Blender ve MoneyPrinterTurbo aynı ortamda kullanılabilir. Bu değişiklik MoneyPrinterTurbo arayüzüne yeni bir Blender düğmesi veya otomatik 3D üretim hattı eklemez. Blender ile oluşturulan videolar MoneyPrinterTurbo'nun yerel materyal yükleme özelliğiyle kullanılabilir.

Test; dış servis, API anahtarı, seslendirme veya ücretli üretim çağrısı kullanmaz. Asıl video üretiminde seçtiğin metin, ses ve materyal sağlayıcısı ayrıca yapılandırılır. API anahtarlarını ve yerel ayarları herkese açık repoya koyma; `config.toml`, `.env`, `.venv`, `.tools` ve `storage` Git tarafından hariç tutulur.
