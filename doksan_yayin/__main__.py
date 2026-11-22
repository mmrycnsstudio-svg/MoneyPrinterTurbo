"""Komut satırı.

  python3 -m doksan_yayin yetkilendir [--yontem cihaz|tarayici] [--istemci-dosyasi YOL]
  python3 -m doksan_yayin kanal
  python3 -m doksan_yayin servis
  python3 -m doksan_yayin durum [URETIM_ID]
  python3 -m doksan_yayin kuyruga-ekle --video V.mp4 [--manifest M.json] [--uretim-id ID]
  python3 -m doksan_yayin mpt-gorevi-ekle GOREV_ID [--kaynak URL ...]
  python3 -m doksan_yayin yeniden-dene URETIM_ID [--yeniden-yukle]
  python3 -m doksan_yayin devam
"""
from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
import time
from pathlib import Path

from . import db
from .ayarlar import yukle
from .servis import Servis, guvenli_id
from .youtube import YetkiHatasi, YouTube, guvenli_yaz, istemci_oku

DURUM_ADI = {
    db.ALINDI: "kalite kontrolü bekliyor", db.DUZELTME: "düzeltme kuyruğunda",
    db.DUZELTILIYOR: "otomatik düzeltiliyor", db.ELLE_DUZELTME: "elle düzeltme gerekiyor",
    db.YUKLENECEK: "yükleme kuyruğunda", db.YUKLENIYOR: "yükleniyor",
    db.ISLENIYOR: "YouTube işliyor", db.YAYINDA: "YAYINDA", db.KISITLI: "kısıtlı (herkese açık değil)",
    db.REDDEDILDI: "reddedildi", db.KOPYA: "kopya (atlandı)",
}


def k_yetkilendir(a, ns) -> int:
    a.dizinleri_hazirla()
    if ns.istemci_dosyasi:
        kaynak = json.loads(Path(ns.istemci_dosyasi).read_text(encoding="utf-8"))
        guvenli_yaz(a.istemci_dosyasi, kaynak)
        print(f"OAuth istemcisi kaydedildi: {a.istemci_dosyasi}")
    elif not a.istemci_dosyasi.exists():
        print("Google Cloud'daki OAuth istemci bilgilerini gir (ekranda gizli kalır, depoya yazılmaz).")
        cid = input("İstemci kimliği (….apps.googleusercontent.com): ").strip()
        sir = getpass.getpass("İstemci gizli anahtarı: ").strip()
        guvenli_yaz(a.istemci_dosyasi, {"installed": {"client_id": cid, "client_secret": sir}})
    istemci_oku(a.istemci_dosyasi)

    yt = YouTube(a.istemci_dosyasi, a.token_dosyasi)
    if ns.yontem == "tarayici":
        yt.tarayici_akisi()
    else:
        yt.cihaz_akisi()

    kanallar = yt.kanallarim()
    if not kanallar:
        print("\nBu Google hesabında YouTube kanalı yok. Doksan Artı kanalının bağlı olduğu "
              "hesabı (veya marka hesabını) seçerek yeniden dene.")
        a.token_dosyasi.unlink(missing_ok=True)
        return 2
    print("\nYetki verilen kanal(lar):")
    for i, k in enumerate(kanallar, 1):
        print(f"  {i}) {k['baslik']}  {k['ozel_url']}  [{k['id']}]")
    secim = 1
    if len(kanallar) > 1:
        secim = int(input("Doksan Artı hangisi? Numara: ").strip() or "1")
    kanal = kanallar[secim - 1]
    onay = input(f"Videolar '{kanal['baslik']}' kanalına yüklenecek. Doğru mu? [e/h]: ").strip().lower()
    if onay not in {"e", "evet", "y", "yes"}:
        a.token_dosyasi.unlink(missing_ok=True)
        print("İptal edildi. Yeniden çalıştır ve Google ekranında doğru kanalı/hesabı seç.")
        return 1
    guvenli_yaz(a.kanal_dosyasi, {**kanal, "kaydedildi": time.strftime("%Y-%m-%d %H:%M:%S")})
    vt = db.Veritabani(a.veritabani)
    vt.genel_yaz("yetki_hatasi", None)
    print(f"\nTAMAM. Kanal kaydedildi: {kanal['baslik']} [{kanal['id']}]")
    print(f"Gizli dosyalar: {a.gizli} (0600, depo dışında)")
    print("Servis çalışıyorsa bekleyen videolar birkaç dakika içinde yüklenmeye başlar.")
    return 0


def k_kanal(a, ns) -> int:
    if not a.kanal_dosyasi.exists():
        print("Kanal seçilmemiş. 'yetkilendir' komutunu çalıştır.")
        return 1
    kanal = json.loads(a.kanal_dosyasi.read_text(encoding="utf-8"))
    yt = YouTube(a.istemci_dosyasi, a.token_dosyasi)
    try:
        dogru = yt.kanal_dogrula(kanal["id"])
        print(f"Bağlı ve doğrulandı: {dogru['baslik']} [{dogru['id']}]")
        return 0
    except YetkiHatasi as e:
        print(f"YETKİ HATASI: {e}")
        return 2


def k_durum(a, ns) -> int:
    s = Servis(a)
    if ns.uretim_id:
        satir = s.db.getir(ns.uretim_id)
        if not satir:
            print("Bulunamadı.")
            return 1
        print(json.dumps({k: v for k, v in satir.items() if k != "manifest"}, ensure_ascii=False, indent=2))
        print("\nOlaylar:")
        for o in s.db.olaylari_getir(ns.uretim_id):
            print(f"  {time.strftime('%d.%m %H:%M:%S', time.localtime(o['zaman']))}  {o['tur']}: {o['mesaj']}")
        return 0
    d = s.durum_yaz()
    print(f"Kanal: {(d['kanal'] or {}).get('baslik', 'SEÇİLMEDİ')}")
    for durum, sayi in sorted(d["sayilar"].items()):
        print(f"  {DURUM_ADI.get(durum, durum):32} {sayi}")
    for anahtar, ad in (("yetki_hatasi", "YETKİ HATASI"), ("kota_bekleme", "KOTA BEKLEMESİ"),
                        ("denetim_bekleniyor", "DENETİM BEKLENİYOR")):
        if d[anahtar]:
            print(f"\n!! {ad}: {json.dumps(d[anahtar], ensure_ascii=False)}")
    print("\nSon videolar:")
    for v in d["son_videolar"]:
        print(f"  {v['uretim_id']:40} {DURUM_ADI.get(v['durum'], v['durum']):28} {v['youtube_url'] or ''}"
              f"{('  — ' + v['son_hata'][:90]) if v['son_hata'] else ''}")
    return 0


def k_kuyruga_ekle(a, ns) -> int:
    a.dizinleri_hazirla()
    manifest = json.loads(Path(ns.manifest).read_text(encoding="utf-8")) if ns.manifest else {}
    uid = guvenli_id(ns.uretim_id or manifest.get("uretim_id") or Path(ns.video).stem)
    manifest["uretim_id"] = uid
    hedef = a.hazir / uid
    hedef.mkdir(parents=True, exist_ok=True)
    (hedef / "TAMAM").unlink(missing_ok=True)
    gecici = hedef / ".video.mp4.part"
    shutil.copyfile(ns.video, gecici)
    gecici.replace(hedef / "video.mp4")
    manifest["video"] = "video.mp4"
    if ns.altyazi:
        shutil.copyfile(ns.altyazi, hedef / "altyazi.srt")
        manifest["altyazi"] = "altyazi.srt"
    (hedef / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (hedef / "TAMAM").write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
    print(f"Kuyruğa bırakıldı: {uid}  ({hedef})")
    return 0


def k_mpt(a, ns) -> int:
    s = Servis(a)
    ek = {"kaynaklar": ns.kaynak} if ns.kaynak else None
    eklenen = s.mpt_gorevini_al(ns.gorev_id, ek, zorla=True)
    print("Eklendi: " + (", ".join(eklenen) or "yok (video yok ya da zaten kuyrukta)"))
    return 0


def k_yeniden(a, ns) -> int:
    s = Servis(a)
    satir = s.db.getir(ns.uretim_id)
    if not satir:
        print("Bulunamadı.")
        return 1
    if satir["youtube_id"] and not ns.yeniden_yukle:
        print(f"Bu video zaten YouTube'da ({satir['youtube_url']}). İkinci kez yüklemek için "
              "önce eskisini YouTube Studio'dan sil, sonra --yeniden-yukle ekle.")
        return 1
    with s.db.islem():
        s.db.guncelle(ns.uretim_id, durum=db.ALINDI, sonraki_deneme=0, deneme=0, son_hata=None,
                      duzeltme_sayisi=0, sha256=None, yukleme_url=None, gonderilen_bayt=0,
                      **({"youtube_id": None, "youtube_url": None, "oturum_acildi": 0,
                          "yt_gorunurluk": None, "yt_yukleme_durumu": None} if ns.yeniden_yukle else {}))
        s.db.olay(ns.uretim_id, "elle_yeniden", "yeniden yükleme" if ns.yeniden_yukle else "yeniden kontrol")
    print("Kuyruğa geri alındı.")
    return 0


def k_devam(a, ns) -> int:
    s = Servis(a)
    for k in ("yetki_hatasi", "kota_bekleme", "denetim_bekleniyor"):
        s.db.genel_yaz(k, None)
    print("Engeller temizlendi; servis bir sonraki turda devam eder.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="doksan_yayin", description="Doksan Artı otomatik YouTube yayını")
    alt = p.add_subparsers(dest="komut", required=True)
    y = alt.add_parser("yetkilendir", help="YouTube kanalını bağla (ilk kurulum)")
    y.add_argument("--yontem", choices=["cihaz", "tarayici"], default="cihaz")
    y.add_argument("--istemci-dosyasi")
    alt.add_parser("kanal", help="Bağlı kanalı doğrula")
    alt.add_parser("servis", help="Kalıcı servis döngüsü")
    d = alt.add_parser("durum", help="Kuyruk ve sonuçlar")
    d.add_argument("uretim_id", nargs="?")
    k = alt.add_parser("kuyruga-ekle", help="Final videoyu kuyruğa bırak")
    k.add_argument("--video", required=True)
    k.add_argument("--manifest")
    k.add_argument("--altyazi")
    k.add_argument("--uretim-id")
    m = alt.add_parser("mpt-gorevi-ekle", help="MoneyPrinterTurbo görevini kuyruğa al")
    m.add_argument("gorev_id")
    m.add_argument("--kaynak", action="append")
    r = alt.add_parser("yeniden-dene", help="Videoyu kuyruğa geri al")
    r.add_argument("uretim_id")
    r.add_argument("--yeniden-yukle", action="store_true")
    alt.add_parser("devam", help="Yetki/kota/denetim engellerini temizle")
    ns = p.parse_args(argv)
    a = yukle()
    komutlar = {"yetkilendir": k_yetkilendir, "kanal": k_kanal, "durum": k_durum,
                "kuyruga-ekle": k_kuyruga_ekle, "mpt-gorevi-ekle": k_mpt,
                "yeniden-dene": k_yeniden, "devam": k_devam,
                "servis": lambda a, ns: Servis(a).calis() or 0}
    try:
        return komutlar[ns.komut](a, ns)
    except YetkiHatasi as e:
        print(f"YETKİ HATASI: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
