"""Kalıcı yayın servisi: alım -> kalite -> düzeltme -> yükleme -> işlenme -> kayıt."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
import re
import signal
import time
from pathlib import Path

from loguru import logger

from . import bildirim, db, duzeltme, kalite, metadata
from .ayarlar import REPO_KOK, Ayarlar
from .youtube import (GeciciHata, KaliciHata, KotaHatasi, OturumGecersiz, YetkiHatasi,
                      YouTube, YouTubeHatasi)

METADATA_NEDENLERI = {"invalidTitle", "invalidDescription", "invalidTags", "invalidCategoryId",
                      "invalidVideoMetadata", "invalidDefaultLanguage"}
KILIT_ACIKLAMASI = (
    "YouTube videoyu işledi ama herkese açık değil. API projesi YouTube denetiminden "
    "geçmediyse YouTube API ile yüklenen videoları otomatik olarak 'özel' kilitler. "
    "Kuyruk, kalan videolar kilitlenmesin diye durduruldu. Denetim onaylanınca: "
    "'python3 -m doksan_yayin devam' ve kilitli olanlar için "
    "'python3 -m doksan_yayin yeniden-dene <id> --yeniden-yukle'.")


def sha256(yol: Path) -> str:
    h = hashlib.sha256()
    with open(yol, "rb") as f:
        for blok in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(blok)
    return h.hexdigest()


def parmak_izi(*yollar: Path) -> str:
    parca = []
    for y in yollar:
        try:
            st = y.stat()
            parca.append(f"{st.st_size}:{st.st_mtime_ns}")
        except FileNotFoundError:
            parca.append("-")
    return "|".join(parca)


def guvenli_id(metin: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "-", metin).strip("-")[:120]


class Servis:
    def __init__(self, ayarlar: Ayarlar, *, yt: YouTube | None = None,
                 mpt: duzeltme.MPTIstemcisi | None = None, saat=time.time):
        self.a = ayarlar
        self.a.dizinleri_hazirla()
        self.db = db.Veritabani(self.a.veritabani)
        self.yt = yt or YouTube(self.a.istemci_dosyasi, self.a.token_dosyasi)
        self.mpt = mpt or duzeltme.MPTIstemcisi(self.a.mpt_api, self.a.mpt_api_anahtari)
        self.saat = saat
        self._kanal_dogrulandi = 0.0
        self._dur = False

    # ------------------------------------------------------------ yardımcılar
    def _ertele(self, uid: str, saniye: float, **alan) -> None:
        self.db.guncelle(uid, sonraki_deneme=self.saat() + saniye, **alan)

    def _bildir(self, anahtar: str, baslik: str, mesaj: str, oncelik: str = "default") -> None:
        gonderilen = self.db.genel_al("son_bildirimler") or {}
        if gonderilen.get(anahtar) == mesaj:
            return
        gonderilen[anahtar] = mesaj
        self.db.genel_yaz("son_bildirimler", gonderilen)
        logger.warning(f"{baslik}: {mesaj}")
        bildirim.gonder(self.a.bildirim_url, baslik, mesaj, oncelik)

    def _sonuc_kaydet(self, satir: dict) -> None:
        kayit = {k: satir.get(k) for k in ("uretim_id", "durum", "youtube_id", "youtube_url",
                                            "yt_gorunurluk", "yt_yukleme_durumu", "son_hata")}
        kayit["baslik"] = (satir.get("metadata") or {}).get("baslik")
        kayit["zaman"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.a.sonuc_kaydi, "a", encoding="utf-8") as f:
            f.write(json.dumps(kayit, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------ alım
    def alim_hazir(self) -> int:
        """hazir/<uretim_id>/{video.mp4, manifest.json, TAMAM} klasörlerini kuyruğa alır."""
        yeni = 0
        for klasor in sorted(p for p in self.a.hazir.iterdir() if p.is_dir()):
            tamam = klasor / "TAMAM"
            if not tamam.exists():
                continue  # üretici henüz bitirmedi
            try:
                manifest = json.loads((klasor / "manifest.json").read_text(encoding="utf-8")) \
                    if (klasor / "manifest.json").exists() else {}
            except ValueError as e:
                manifest = {"kalite": {"durum": "kaldi", "sorunlar": [f"manifest.json okunamadı: {e}"]}}
            uid = guvenli_id(str(manifest.get("uretim_id") or klasor.name))
            video = klasor / str(manifest.get("video") or "video.mp4")
            iz = parmak_izi(video, tamam, klasor / "manifest.json")
            manifest["_parmak_izi"] = iz
            manifest["_klasor"] = str(klasor)
            satir = self.db.getir(uid)
            if satir is None:
                with self.db.islem():
                    self.db.ekle(uid, "hazir", str(video), manifest)
                yeni += 1
                continue
            if (satir.get("manifest") or {}).get("_parmak_izi") == iz:
                continue
            if satir["durum"] in {db.ALINDI, db.DUZELTME, db.ELLE_DUZELTME, db.YUKLENECEK}:
                with self.db.islem():
                    self.db.guncelle(uid, durum=db.ALINDI, video_yolu=str(video), manifest=manifest,
                                     sha256=None, sorunlar=None, sonraki_deneme=0, metadata=None)
                    self.db.olay(uid, "yeniden_birakildi", "düzeltilmiş sürüm kontrole alındı")
                duzeltme.rapor_sil(self.a.duzeltme_kuyrugu, uid)
                yeni += 1
            else:
                with self.db.islem():
                    eski = dict(satir.get("manifest") or {})
                    eski["_parmak_izi"] = iz
                    self.db.guncelle(uid, manifest=eski)
                    self.db.olay(uid, "yok_sayildi",
                                 f"aynı üretim kimliği yeniden bırakıldı ama durum '{satir['durum']}'; "
                                 "ikinci yükleme yapılmadı")
        return yeni

    def mpt_gorevini_al(self, gorev_id: str, ek: dict | None = None, zorla: bool = False) -> list[str]:
        dizin = self.a.mpt_gorev_dizini / gorev_id
        bilgi = duzeltme.mpt_gorev_bilgisi(dizin)
        params = bilgi.get("params") or {}
        konu = str(params.get("video_subject") or "")
        if not zorla:
            if not self.a.mpt_konu_deseni or not re.search(self.a.mpt_konu_deseni, konu):
                return []
        yan = dizin / "doksan.json"  # üretici kaynakları vb. buraya yazabilir
        if yan.exists():
            try:
                ek = {**json.loads(yan.read_text(encoding="utf-8")), **(ek or {})}
            except ValueError:
                pass
        eklenen = []
        for video in sorted(dizin.glob("final-*.mp4")):
            n = video.stem.split("-")[-1]
            uid = f"mpt-{gorev_id}-{n}"
            if self.db.getir(uid):
                continue
            manifest = {"konu": konu, "senaryo": bilgi.get("script") or params.get("video_script") or "",
                        "etiketler": bilgi.get("search_terms") or [], "_mpt_gorev": gorev_id,
                        **(ek or {})}
            with self.db.islem():
                self.db.ekle(uid, "mpt", str(video), manifest)
            eklenen.append(uid)
        return eklenen

    def alim_mpt(self) -> int:
        if not self.a.mpt_konu_deseni or not self.a.mpt_gorev_dizini.exists():
            return 0
        # Otomatik düzeltme için açılan yeniden render görevleri ayrı video sayılmaz.
        duzeltme_gorevleri = {s.get("duzeltme_gorev") for s in self.db.hepsi() if s.get("duzeltme_gorev")}
        toplam = 0
        for dizin in self.a.mpt_gorev_dizini.iterdir():
            if dizin.is_dir() and dizin.name not in duzeltme_gorevleri and any(dizin.glob("final-*.mp4")):
                toplam += len(self.mpt_gorevini_al(dizin.name))
        return toplam

    # ------------------------------------------------------------ kalite
    def _kalite_girdileri(self, satir: dict) -> dict:
        m = satir.get("manifest") or {}
        video = Path(satir["video_yolu"])
        girdiler = {"tamam_isareti": None, "srt": None, "font_yolu": None, "punto": None}
        if satir["kaynak"] == "mpt":
            dizin = video.parent
            params = duzeltme.mpt_gorev_bilgisi(dizin).get("params") or {}
            if params.get("subtitle_enabled", True):
                font = params.get("font_name") or "STHeitiMedium.ttc"
                girdiler.update(srt=dizin / "subtitle.srt",
                                font_yolu=str(REPO_KOK / "resource" / "fonts" / font),
                                punto=int(params.get("font_size") or 60))
        else:
            klasor = Path(m.get("_klasor") or video.parent)
            girdiler["tamam_isareti"] = klasor / "TAMAM"
            if m.get("altyazi"):
                girdiler["srt"] = klasor / m["altyazi"]
            if m.get("font"):
                f = Path(m["font"])
                girdiler["font_yolu"] = str(f if f.is_absolute() else REPO_KOK / "resource" / "fonts" / f)
            if m.get("punto"):
                girdiler["punto"] = int(m["punto"])
        if girdiler["font_yolu"] and not Path(girdiler["font_yolu"]).exists():
            girdiler.update(font_yolu=None, srt=None)
        return girdiler

    def kalite_adimi(self) -> int:
        islenen = 0
        for satir in self.db.durumdakiler(db.ALINDI, zamani_gelmis=True):
            uid = satir["uretim_id"]
            video = Path(satir["video_yolu"])
            manifest = satir.get("manifest") or {}

            if satir["kaynak"] == "mpt" and manifest.get("_mpt_gorev"):
                try:
                    durum = self.mpt.gorev_durumu(manifest["_mpt_gorev"])
                except Exception:
                    durum = None  # API kapalı: dosya kararlılığı kuralı uygulanır
                if durum and durum.get("state") == 4:
                    self._ertele(uid, 30)
                    continue

            sonuc = kalite.kontrol_et(video, manifest, sadece_shorts=self.a.sadece_shorts,
                                      kaynak_zorunlu=self.a.kaynak_zorunlu,
                                      **self._kalite_girdileri(satir))
            if sonuc.karar == kalite.BEKLE:
                self._ertele(uid, 30)
                continue
            islenen += 1

            if sonuc.karar == kalite.GECTI:
                ozet = sha256(video)
                kopyalar = [s for s in self.db.sha_ile_bul(ozet)
                            if s["uretim_id"] != uid and s["durum"] not in {db.KOPYA}]
                if kopyalar:
                    with self.db.islem():
                        self.db.guncelle(uid, durum=db.KOPYA, sha256=ozet,
                                         son_hata=f"aynı dosya: {kopyalar[0]['uretim_id']}")
                        self.db.olay(uid, "kopya", f"aynı dosya {kopyalar[0]['uretim_id']} olarak kayıtlı")
                    continue
                md = metadata.hazirla(uid, manifest, sabit_etiketler=self.a.sabit_etiketler,
                                      llm_kullan=self.a.llm_metadata, dil=self.a.dil)
                with self.db.islem():
                    self.db.guncelle(uid, durum=db.YUKLENECEK, sha256=ozet, boyut=video.stat().st_size,
                                     kalite_raporu=sonuc.rapor(), sorunlar=None, metadata=md,
                                     sonraki_deneme=0, deneme=0, son_hata=None)
                    self.db.olay(uid, "kalite_gecti", md["baslik"])
                duzeltme.rapor_sil(self.a.duzeltme_kuyrugu, uid)
                continue

            # KALDI
            self._duzeltmeye_gonder(satir, sonuc.sorunlar, sonuc.rapor())
        return islenen

    def _duzeltmeye_gonder(self, satir: dict, sorunlar: list[dict], rapor: dict | None = None) -> None:
        uid = satir["uretim_id"]
        ozet_metin = "; ".join(s["aciklama"] for s in sorunlar)
        if (satir["kaynak"] == "mpt" and duzeltme.otomatik_mi(sorunlar)
                and satir["duzeltme_sayisi"] < self.a.otomatik_duzeltme_hakki):
            m = satir.get("manifest") or {}
            dizin = Path(satir["video_yolu"]).parent
            bilgi = duzeltme.mpt_gorev_bilgisi(dizin)
            params = bilgi.get("params")
            if params:
                try:
                    yeni = duzeltme.duzeltilmis_parametreler(params, sorunlar, bilgi.get("script", ""),
                                                             bilgi.get("search_terms"))
                    gorev = self.mpt.render_baslat(yeni)
                    with self.db.islem():
                        self.db.guncelle(uid, durum=db.DUZELTILIYOR, duzeltme_gorev=gorev,
                                         duzeltme_sayisi=satir["duzeltme_sayisi"] + 1,
                                         sorunlar=sorunlar, kalite_raporu=rapor,
                                         sonraki_deneme=self.saat() + 60, manifest=m)
                        self.db.olay(uid, "otomatik_duzeltme", f"yeniden render {gorev}: {ozet_metin}")
                    return
                except Exception as e:
                    logger.warning(f"{uid}: otomatik düzeltme başlatılamadı: {e}")
        hedef = db.DUZELTME if satir["kaynak"] == "hazir" else db.ELLE_DUZELTME
        with self.db.islem():
            self.db.guncelle(uid, durum=hedef, sorunlar=sorunlar, kalite_raporu=rapor, son_hata=ozet_metin)
            self.db.olay(uid, "duzeltme_kuyrugu", ozet_metin)
        duzeltme.rapor_yaz(self.a.duzeltme_kuyrugu, uid, satir, sorunlar)
        if hedef == db.ELLE_DUZELTME:
            self._bildir(f"elle:{uid}", "Doksan Artı: elle düzeltme gerekiyor", f"{uid}: {ozet_metin}")

    def duzeltme_takibi(self) -> None:
        for satir in self.db.durumdakiler(db.DUZELTILIYOR, zamani_gelmis=True):
            uid, gorev = satir["uretim_id"], satir["duzeltme_gorev"]
            try:
                durum = self.mpt.gorev_durumu(gorev)
            except Exception:
                durum = None
            hedef_video = self.a.mpt_gorev_dizini / gorev / "final-1.mp4"
            state = (durum or {}).get("state")
            if state == 4:
                self._ertele(uid, 60)
                continue
            if state == 1 or (durum is None and hedef_video.exists()):
                m = dict(satir.get("manifest") or {})
                m["_mpt_gorev"] = gorev
                with self.db.islem():
                    self.db.guncelle(uid, durum=db.ALINDI, video_yolu=str(hedef_video), manifest=m,
                                     sha256=None, sonraki_deneme=0)
                    self.db.olay(uid, "duzeltme_bitti", gorev)
                continue
            if state == -1 or (durum is None and self.saat() - satir["guncelleme"] > 3 * 3600):
                self._duzeltmeye_gonder(satir, (satir.get("sorunlar") or []) + [
                    {"kod": "duzeltme_render_basarisiz", "aciklama": f"MPT yeniden render başarısız ({gorev})"}])
                continue
            self._ertele(uid, 60)

    # ------------------------------------------------------------ engeller
    def _engel(self) -> str | None:
        yetki = self.db.genel_al("yetki_hatasi")
        if yetki:
            try:
                mtime = self.a.token_dosyasi.stat().st_mtime
            except FileNotFoundError:
                mtime = None
            if mtime and mtime != yetki.get("token_mtime"):
                self.db.genel_yaz("yetki_hatasi", None)  # yeniden yetkilendirilmiş
                self._kanal_dogrulandi = 0
            else:
                return "yetki"
        kota = self.db.genel_al("kota_bekleme")
        if kota and kota.get("bitis", 0) > self.saat():
            return "kota"
        if kota:
            self.db.genel_yaz("kota_bekleme", None)
        if self.db.genel_al("denetim_bekleniyor"):
            return "denetim"
        return None

    def _yetki_hatasi(self, hata: Exception) -> None:
        try:
            mtime = self.a.token_dosyasi.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        self.db.genel_yaz("yetki_hatasi", {"mesaj": str(hata), "zaman": self.saat(), "token_mtime": mtime})
        self._bildir("yetki", "Doksan Artı: YouTube YETKİ HATASI",
                     f"{hata}\nYükleme durdu, videolar kuyrukta bekliyor. Sunucuda: "
                     "docker exec -it doksan-yayin python3 -m doksan_yayin yetkilendir", "high")

    def _kota(self, hata: KotaHatasi) -> None:
        self.db.genel_yaz("kota_bekleme", {"bitis": hata.bekle_bitis, "mesaj": str(hata)})
        self._bildir("kota", "Doksan Artı: YouTube kotası doldu",
                     "Videolar kuyrukta tutuluyor; kota sıfırlanınca (" +
                     time.strftime("%d.%m %H:%M", time.localtime(hata.bekle_bitis)) + ") devam edilecek.")

    def _kanal(self) -> dict:
        if not self.a.kanal_dosyasi.exists():
            raise YetkiHatasi("Kanal seçilmemiş (kanal.json yok). 'yetkilendir' komutunu çalıştır.")
        kanal = json.loads(self.a.kanal_dosyasi.read_text(encoding="utf-8"))
        if self.saat() - self._kanal_dogrulandi > 1800:
            dogru = self.yt.kanal_dogrula(kanal["id"])
            kanal["yuklemeler"] = dogru.get("yuklemeler") or kanal.get("yuklemeler")
            self._kanal_dogrulandi = self.saat()
        return kanal

    # ------------------------------------------------------------ yükleme
    def yukleme_adimi(self) -> bool:
        if self._engel():
            return False
        if (not self.db.genel_al("gorunurluk_dogrulandi")
                and self.db.durumdakiler(db.ISLENIYOR)):
            # Kanalda henüz hiçbir video herkese açık doğrulanmadı: ilki sonuçlanmadan
            # ikincisini gönderme (kilit varsa bütün kuyruk özel kilitlenmesin).
            return False
        adaylar = (self.db.durumdakiler(db.YUKLENIYOR, zamani_gelmis=True)
                   or self.db.durumdakiler(db.YUKLENECEK, zamani_gelmis=True))
        if not adaylar:
            return False
        satir = adaylar[0]
        uid = satir["uretim_id"]
        try:
            self._yukle(satir)
        except YetkiHatasi as e:
            self.db.olay(uid, "yetki_hatasi", str(e))
            self._yetki_hatasi(e)
        except KotaHatasi as e:
            self.db.olay(uid, "kota", str(e))
            self._kota(e)
        except (GeciciHata, OturumGecersiz) as e:
            deneme = satir["deneme"] + 1
            bekle = min(3600, 30 * 2 ** min(deneme, 7)) * random.uniform(0.8, 1.2)
            with self.db.islem():
                self._ertele(uid, bekle, deneme=deneme, son_hata=str(e))
                self.db.olay(uid, "gecici_hata", f"{e} — {int(bekle)} sn sonra yeniden denenecek")
        except KaliciHata as e:
            if e.neden in METADATA_NEDENLERI and not (satir.get("metadata") or {}).get("_yedek"):
                md = metadata.hazirla(uid, {**(satir.get("manifest") or {}), "baslik": None,
                                            "aciklama": None}, sabit_etiketler=self.a.sabit_etiketler,
                                      llm_kullan=False, dil=self.a.dil)
                md["_yedek"] = True
                with self.db.islem():
                    self.db.guncelle(uid, metadata=md, yukleme_url=None, son_hata=str(e))
                    self.db.olay(uid, "metadata_yenilendi", str(e))
            else:
                with self.db.islem():
                    self.db.guncelle(uid, durum=db.ELLE_DUZELTME, son_hata=str(e))
                    self.db.olay(uid, "kalici_hata", str(e))
                duzeltme.rapor_yaz(self.a.duzeltme_kuyrugu, uid, satir,
                                   [{"kod": "youtube_reddetti", "aciklama": str(e)}])
                self._bildir(f"kalici:{uid}", "Doksan Artı: yükleme reddedildi", f"{uid}: {e}")
        return True

    def _govde(self, md: dict) -> dict:
        return {
            "snippet": {"title": md["baslik"], "description": md["aciklama"], "tags": md["etiketler"],
                        "categoryId": self.a.kategori, "defaultLanguage": md.get("dil", "tr"),
                        "defaultAudioLanguage": md.get("dil", "tr")},
            "status": {"privacyStatus": self.a.gorunurluk, "embeddable": True,
                       "selfDeclaredMadeForKids": self.a.cocuklara_yonelik,
                       "containsSyntheticMedia": self.a.yapay_icerik},
        }

    def _yukle(self, satir: dict) -> None:
        uid = satir["uretim_id"]
        video = Path(satir["video_yolu"])
        kanal = self._kanal()

        if not video.exists() or sha256(video) != satir["sha256"]:
            with self.db.islem():
                self.db.guncelle(uid, durum=db.ALINDI, yukleme_url=None, gonderilen_bayt=0, sha256=None)
                self.db.olay(uid, "dosya_degisti", "kalite kontrolünden sonra dosya değişti; yeniden kontrol")
            return
        boyut = video.stat().st_size
        url = satir.get("yukleme_url")
        bitti: dict | None = None
        konum = 0

        if url:  # yarım kalan oturumu sorgula
            try:
                konum, bitti = self.yt.oturum_durumu(url, boyut)
                self.db.olay(uid, "devam", f"oturum kaldığı yerden: {max(konum, 0)}/{boyut} bayt")
            except OturumGecersiz:
                url = None
                self.db.olay(uid, "oturum_sona_erdi", "yeni oturum açılacak")

        if not url and not bitti:
            if satir["oturum_acildi"] and kanal.get("yuklemeler"):
                # Önceki oturum tamamlanmış olabilir: gizli etiketle kanalda ara (çift yükleme önlemi)
                var = self.yt.isaretli_video_bul(kanal["yuklemeler"], metadata.isaret_etiketi(uid))
                if var:
                    self._yuklendi(satir, {"id": var}, "kanalda zaten var (işaret etiketi)")
                    return
            url = self.yt.oturum_ac(self._govde(satir["metadata"]), boyut)
            konum = 0
            with self.db.islem():
                self.db.guncelle(uid, durum=db.YUKLENIYOR, yukleme_url=url, oturum_acildi=1,
                                 gonderilen_bayt=0)
                self.db.olay(uid, "oturum_acildi", f"{boyut} bayt")

        parca = max(1, self.a.parca_mb) * 1024 * 1024  # 256 KB'nin katı
        while bitti is None:
            if self._dur:
                return
            konum, bitti = self.yt.parca_gonder(url, video, konum, boyut, parca)
            if bitti is None:
                self.db.guncelle(uid, gonderilen_bayt=konum)
        self._yuklendi(satir, bitti, "dosya gönderildi")

    def _yuklendi(self, satir: dict, kaynak: dict, not_: str) -> None:
        uid, vid = satir["uretim_id"], kaynak["id"]
        with self.db.islem():
            self.db.guncelle(uid, durum=db.ISLENIYOR, youtube_id=vid,
                             youtube_url=f"https://www.youtube.com/shorts/{vid}",
                             yukleme_url=None, gonderilen_bayt=satir.get("boyut") or 0,
                             yuklenme_zamani=self.saat(), deneme=0, son_hata=None,
                             yt_yukleme_durumu=(kaynak.get("status") or {}).get("uploadStatus"),
                             sonraki_deneme=self.saat() + 45)
            self.db.olay(uid, "yuklendi", f"{vid} — {not_}; işlenme bekleniyor")

    # ------------------------------------------------------------ işlenme
    def islenme_adimi(self) -> None:
        engel = self._engel()
        if engel in {"yetki", "kota"}:
            return
        for satir in self.db.durumdakiler(db.ISLENIYOR, zamani_gelmis=True):
            try:
                self._islenme_kontrol(satir)
            except YetkiHatasi as e:
                self._yetki_hatasi(e)
                return
            except KotaHatasi as e:
                self._kota(e)
                return
            except YouTubeHatasi as e:
                self._ertele(satir["uretim_id"], 300, son_hata=str(e))

    def _islenme_kontrol(self, satir: dict) -> None:
        uid, vid = satir["uretim_id"], satir["youtube_id"]
        gecen = self.saat() - (satir.get("yuklenme_zamani") or self.saat())
        v = self.yt.video_durumu(vid)
        if v is None:
            if gecen > 6 * 3600:
                self._son(satir, db.REDDEDILDI, None, None, "Video YouTube'da bulunamadı (silinmiş olabilir)")
            else:
                self._ertele(uid, 120)
            return
        st = v.get("status") or {}
        yukleme, gorunurluk = st.get("uploadStatus"), st.get("privacyStatus")
        self.db.guncelle(uid, yt_yukleme_durumu=yukleme, yt_gorunurluk=gorunurluk)
        if yukleme in {"rejected", "failed", "deleted"}:
            neden = st.get("rejectionReason") or st.get("failureReason") or yukleme
            self._son(satir, db.REDDEDILDI, yukleme, gorunurluk, f"YouTube: {yukleme} ({neden})")
            return
        if yukleme != "processed":
            ilerleme = (v.get("processingDetails") or {}).get("processingStatus")
            bekle = 60 if gecen < 1800 else 600
            self._ertele(uid, bekle, son_hata=None)
            self.db.olay(uid, "isleniyor", f"uploadStatus={yukleme}, processing={ilerleme}")
            if gecen > 48 * 3600:
                self._bildir(f"uzun:{uid}", "Doksan Artı: işlenme uzun sürüyor", f"{uid} ({vid}) 48 saattir işleniyor")
            return
        if gorunurluk == self.a.gorunurluk:
            self._son(satir, db.YAYINDA, yukleme, gorunurluk, None)
            return
        # işlendi ama hedef görünürlükte değil: bir kez düzeltmeyi dene
        if not (satir.get("manifest") or {}).get("_gorunurluk_denendi"):
            m = dict(satir.get("manifest") or {})
            m["_gorunurluk_denendi"] = True
            self.db.guncelle(uid, manifest=m)
            try:
                self.yt.gorunurluk_ayarla(v, self.a.gorunurluk)
                self.db.olay(uid, "gorunurluk_ayarlandi", f"{gorunurluk} -> {self.a.gorunurluk}")
                self._ertele(uid, 30)
                return
            except (KaliciHata, YetkiHatasi) as e:
                self.db.olay(uid, "gorunurluk_ayarlanamadi", str(e))
        self._son(satir, db.KISITLI, yukleme, gorunurluk,
                  f"Görünürlük '{gorunurluk}' (istenen '{self.a.gorunurluk}'). " + KILIT_ACIKLAMASI)
        if gorunurluk == "private" and self.a.gorunurluk == "public":
            self.db.genel_yaz("denetim_bekleniyor", {"zaman": self.saat(), "video": vid})
            self._bildir("denetim", "Doksan Artı: video ÖZEL kilitlendi", f"{uid} ({vid}). " + KILIT_ACIKLAMASI, "high")

    def _son(self, satir: dict, durum: str, yukleme, gorunurluk, hata) -> None:
        uid = satir["uretim_id"]
        with self.db.islem():
            self.db.guncelle(uid, durum=durum, yt_yukleme_durumu=yukleme, yt_gorunurluk=gorunurluk,
                             son_hata=hata, sonuc_zamani=self.saat())
            self.db.olay(uid, durum, hata or f"{gorunurluk} — {satir.get('youtube_url')}")
        self._sonuc_kaydet(self.db.getir(uid))
        if durum == db.YAYINDA:
            self.db.genel_yaz("gorunurluk_dogrulandi", {"zaman": self.saat(), "video": satir.get("youtube_id")})
            logger.success(f"{uid} yayında: {satir.get('youtube_url')}")

    # ------------------------------------------------------------ durum
    def durum_yaz(self) -> dict:
        kanal = None
        if self.a.kanal_dosyasi.exists():
            try:
                kanal = json.loads(self.a.kanal_dosyasi.read_text(encoding="utf-8"))
            except ValueError:
                pass
        son = [{k: s.get(k) for k in ("uretim_id", "durum", "youtube_url", "son_hata")}
               for s in sorted(self.db.hepsi(), key=lambda s: s["guncelleme"], reverse=True)[:15]]
        d = {
            "zaman": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kanal": kanal and {"id": kanal.get("id"), "baslik": kanal.get("baslik")},
            "sayilar": self.db.sayim(),
            "yetki_hatasi": self.db.genel_al("yetki_hatasi"),
            "kota_bekleme": self.db.genel_al("kota_bekleme"),
            "denetim_bekleniyor": self.db.genel_al("denetim_bekleniyor"),
            "son_videolar": son,
        }
        tmp = self.a.durum_dosyasi.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.a.durum_dosyasi)
        alarmlar = []
        if d["yetki_hatasi"]:
            alarmlar.append("YETKİ HATASI: " + d["yetki_hatasi"]["mesaj"] +
                            "\nÇözüm: docker exec -it doksan-yayin python3 -m doksan_yayin yetkilendir")
        if d["denetim_bekleniyor"]:
            alarmlar.append("YÜKLEME DURDU: " + KILIT_ACIKLAMASI)
        if alarmlar:
            self.a.alarm_dosyasi.write_text("\n\n".join(alarmlar) + "\n", encoding="utf-8")
        else:
            self.a.alarm_dosyasi.unlink(missing_ok=True)
        return d

    # ------------------------------------------------------------ döngü
    def tur(self) -> bool:
        """Bir tur çalışır; iş yapıldıysa True döner."""
        is_var = False
        for ad, adim in (("alim_hazir", self.alim_hazir), ("alim_mpt", self.alim_mpt),
                         ("duzeltme", self.duzeltme_takibi), ("kalite", self.kalite_adimi),
                         ("yukleme", self.yukleme_adimi), ("islenme", self.islenme_adimi)):
            try:
                is_var = bool(adim()) or is_var
            except Exception as e:  # bir adımın hatası servisi durdurmaz
                logger.exception(f"{ad} adımında hata: {e}")
        try:
            self.durum_yaz()
        except Exception as e:
            logger.warning(f"durum yazılamadı: {e}")
        return is_var

    def calis(self) -> None:
        kilit = open(self.a.kok / ".servis.lock", "w")
        try:
            fcntl.flock(kilit, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Başka bir yayın servisi zaten çalışıyor.")

        def durdur(*_):
            logger.info("durdurma sinyali alındı; mevcut parça bitince çıkılacak")
            self._dur = True
        signal.signal(signal.SIGTERM, durdur)
        signal.signal(signal.SIGINT, durdur)

        logger.info(f"Doksan Artı yayın servisi başladı. Kuyruk: {self.a.kok}")
        if self._engel() == "yetki" or not self.a.token_dosyasi.exists():
            logger.warning("YouTube bağlantısı yok/geçersiz: kalite kontrol çalışır, yükleme bekler.")
        while not self._dur:
            is_var = self.tur()
            bekle = 2 if is_var else self.a.dongu_saniye
            for _ in range(int(bekle)):
                if self._dur:
                    break
                time.sleep(1)
        logger.info("servis durdu")
