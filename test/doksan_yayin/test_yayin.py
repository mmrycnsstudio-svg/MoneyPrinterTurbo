"""Doksan Artı yayın hattı uçtan uca testleri (sahte YouTube + gerçek ffmpeg videoları)."""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import requests

from doksan_yayin import db, kalite, metadata
from doksan_yayin.ayarlar import Ayarlar
from doksan_yayin.servis import Servis
from doksan_yayin.youtube import YouTube, guvenli_yaz

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg yok")


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


@pytest.fixture(scope="module")
def videolar(tmp_path_factory):
    d = tmp_path_factory.mktemp("v")
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=540x960:rate=25:duration=3",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "iyi.mp4"))
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=540x960:rate=25:duration=3",
           "-f", "lavfi", "-i", "sine=frequency=660:duration=3",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "iyi2.mp4"))
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=540x960:rate=25:duration=3",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "sessiz_akis.mp4"))
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=540x960:rate=25:duration=3",
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "3",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "sessiz.mp4"))
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=960x540:rate=25:duration=3",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "yatay.mp4"))
    veri = (d / "iyi.mp4").read_bytes()
    (d / "bozuk.mp4").write_bytes(veri[: len(veri) // 2])
    return d


# ------------------------------------------------------------ sahte YouTube
class Yanit:
    def __init__(self, kod, govde=None, basliklar=None):
        self.status_code = kod
        self._govde = govde
        self.headers = basliklar or {}
        self.content = b"" if govde is None else json.dumps(govde).encode()
        self.text = self.content.decode()

    def json(self):
        if self._govde is None:
            raise ValueError("boş")
        return self._govde


class SahteYouTube:
    """requests.Session yerine geçer; YouTube'un devam ettirilebilir yükleme protokolünü taklit eder."""

    def __init__(self):
        self.oturumlar = {}       # url -> {"boyut", "alinan": bytearray, "govde"}
        self.videolar = {}        # id -> kaynak
        self.kanal = {"id": "UCdoksan", "title": "Doksan Artı"}
        self.yuklemeler_listesi = "UUdoksan"
        self.kopma_parcasi = None  # n. parçada bağlantı kopsun
        self.parca_sayaci = 0
        self.kota_doldu = False
        self.yetki_iptal = False
        self.islenme_sorgu = 0
        self.kilitle = False
        self.oturum_acma = 0

    def post(self, url, data=None, **kw):
        return self.request("POST", url, data=data, **kw)

    def _hata(self, kod, neden):
        return Yanit(kod, {"error": {"errors": [{"reason": neden}], "message": neden}})

    def request(self, yontem, url, headers=None, params=None, data=None, json=None, timeout=None):
        headers = headers or {}
        params = params or {}
        if url.startswith("https://oauth2.googleapis.com/token"):
            if self.yetki_iptal:
                return Yanit(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            return Yanit(200, {"access_token": "erisim", "expires_in": 3600})
        if self.yetki_iptal:
            return self._hata(401, "authError")
        if url.endswith("/channels"):
            return Yanit(200, {"items": [{"id": self.kanal["id"], "snippet": {"title": self.kanal["title"]},
                                          "contentDetails": {"relatedPlaylists": {"uploads": self.yuklemeler_listesi}}}]})
        if url.startswith("https://www.googleapis.com/upload/youtube/v3/videos") and yontem == "POST":
            if self.kota_doldu:
                return self._hata(403, "quotaExceeded")
            self.oturum_acma += 1
            ou = f"https://upload.example/session/{len(self.oturumlar) + 1}"
            self.oturumlar[ou] = {"boyut": int(headers["X-Upload-Content-Length"]),
                                  "alinan": bytearray(), "govde": __import__("json").loads(data)}
            return Yanit(200, None, {"Location": ou})
        if url in self.oturumlar and yontem == "PUT":
            o = self.oturumlar[url]
            aralik = headers["Content-Range"]
            if aralik.startswith("bytes */"):
                if o.get("video"):
                    return Yanit(200, o["video"])
                return Yanit(308, None, {"Range": f"bytes=0-{len(o['alinan']) - 1}"} if o["alinan"] else {})
            self.parca_sayaci += 1
            if self.kopma_parcasi and self.parca_sayaci == self.kopma_parcasi:
                raise requests.ConnectionError("bağlantı koptu")
            bas = int(aralik.split()[1].split("-")[0])
            assert bas == len(o["alinan"]), "parça yanlış konumdan gönderildi"
            o["alinan"].extend(data)
            if len(o["alinan"]) >= o["boyut"]:
                vid = f"vid{len(self.videolar) + 1}"
                kaynak = {"id": vid, "snippet": o["govde"]["snippet"],
                          "status": {"uploadStatus": "uploaded",
                                     "privacyStatus": "private" if self.kilitle else o["govde"]["status"]["privacyStatus"]}}
                self.videolar[vid] = kaynak
                o["video"] = kaynak
                return Yanit(201, kaynak)
            return Yanit(308, None, {"Range": f"bytes=0-{len(o['alinan']) - 1}"})
        if url.endswith("/videos") and yontem == "GET":
            idler = params["id"].split(",")
            items = []
            for i in idler:
                if i in self.videolar:
                    v = self.videolar[i]
                    if params.get("part", "").startswith("status"):
                        self.islenme_sorgu += 1
                        if self.islenme_sorgu >= 2:
                            v["status"]["uploadStatus"] = "processed"
                    items.append(v)
            return Yanit(200, {"items": items})
        if url.endswith("/videos") and yontem == "PUT":
            if self.kilitle:
                return self._hata(403, "forbidden")
            g = __import__("json").loads(data)
            self.videolar[g["id"]]["status"].update(g["status"])
            return Yanit(200, self.videolar[g["id"]])
        if url.endswith("/playlistItems"):
            return Yanit(200, {"items": [{"contentDetails": {"videoId": v}} for v in self.videolar]})
        raise AssertionError(f"beklenmeyen istek {yontem} {url}")


class SahteMPT:
    def gorev_durumu(self, _):
        return None

    def render_baslat(self, params):
        raise RuntimeError("test")


# ------------------------------------------------------------ kurulum
@pytest.fixture
def ortam(tmp_path, monkeypatch):
    a = Ayarlar()
    a.kok = tmp_path / "kok"
    a.gizli = tmp_path / "gizli"
    a.mpt_gorev_dizini = tmp_path / "tasks"
    a.llm_metadata = False
    a.parca_mb = 1
    a.dizinleri_hazirla()
    guvenli_yaz(a.istemci_dosyasi, {"installed": {"client_id": "id", "client_secret": "sir"}})
    guvenli_yaz(a.token_dosyasi, {"refresh_token": "yenile"})
    guvenli_yaz(a.kanal_dosyasi, {"id": "UCdoksan", "baslik": "Doksan Artı", "yuklemeler": "UUdoksan"})
    monkeypatch.setattr(kalite, "SABIT_SURE_SN", 0)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    sahte = SahteYouTube()
    s = Servis(a, yt=YouTube(a.istemci_dosyasi, a.token_dosyasi, oturum=sahte), mpt=SahteMPT())
    return a, s, sahte


def birak(a, uid, video, **manifest):
    d = a.hazir / uid
    d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(video, d / "video.mp4")
    manifest.setdefault("konu", "Fenerbahçe'nin 2000-01 şampiyonluğu")
    manifest.setdefault("senaryo", "O sezon her şey değişti. Kadıköy'de unutulmaz bir hikâye.")
    manifest.setdefault("kaynaklar", [{"ad": "TFF", "url": "https://www.tff.org/"}])
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (d / "TAMAM").write_text("ok")


def calistir(s, tur=12):
    for _ in range(tur):
        s.tur()
        for satir in s.db.hepsi():  # bekleme sürelerini atla
            s.db.guncelle(satir["uretim_id"], sonraki_deneme=0)


# ------------------------------------------------------------ testler
def test_uctan_uca_yayinda_ve_kayit(ortam, videolar):
    a, s, yt = ortam
    birak(a, "da-001", videolar / "iyi.mp4")
    calistir(s)
    satir = s.db.getir("da-001")
    assert satir["durum"] == db.YAYINDA
    assert satir["youtube_id"] == "vid1" and satir["yt_gorunurluk"] == "public"
    assert satir["youtube_url"].endswith("/shorts/vid1")
    govde = next(iter(yt.oturumlar.values()))["govde"]
    assert "Kaynaklar:" in govde["snippet"]["description"] and "https://www.tff.org/" in govde["snippet"]["description"]
    assert metadata.isaret_etiketi("da-001") in govde["snippet"]["tags"]
    assert govde["status"]["privacyStatus"] == "public"
    kayit = [json.loads(l) for l in a.sonuc_kaydi.read_text().splitlines()]
    assert kayit[-1]["durum"] == db.YAYINDA and kayit[-1]["youtube_url"]


def test_render_bitmeden_yuklenmez(ortam, videolar):
    a, s, yt = ortam
    d = a.hazir / "da-002"
    d.mkdir(parents=True)
    shutil.copyfile(videolar / "iyi.mp4", d / "video.mp4")  # TAMAM yok
    calistir(s, 3)
    assert s.db.getir("da-002") is None and yt.oturum_acma == 0


@pytest.mark.parametrize("dosya,kod", [
    ("sessiz_akis.mp4", kalite.SES_YOK), ("sessiz.mp4", kalite.SES_SESSIZ),
    ("bozuk.mp4", kalite.BOZUK), ("yatay.mp4", kalite.SHORTS_DEGIL)])
def test_kalite_hatasi_duzeltme_kuyruguna(ortam, videolar, dosya, kod):
    a, s, yt = ortam
    birak(a, "da-x", videolar / dosya)
    calistir(s, 3)
    satir = s.db.getir("da-x")
    assert satir["durum"] == db.DUZELTME
    assert kod in {so["kod"] for so in satir["sorunlar"]}
    assert (a.duzeltme_kuyrugu / "da-x.json").exists()
    assert yt.oturum_acma == 0
    # düzeltilmiş sürüm aynı kimlikle bırakılınca yeniden kontrol edilip yüklenir
    birak(a, "da-x", videolar / "iyi.mp4")
    calistir(s)
    assert s.db.getir("da-x")["durum"] == db.YAYINDA
    assert not (a.duzeltme_kuyrugu / "da-x.json").exists()


def test_uretici_kalite_raporu_ve_kaynak_zorunlu(ortam, videolar):
    a, s, _ = ortam
    birak(a, "da-u", videolar / "iyi.mp4", kalite={"durum": "kaldi", "sorunlar": ["yazı taşıyor"]})
    birak(a, "da-k", videolar / "iyi.mp4", kaynaklar=[])
    calistir(s, 3)
    assert s.db.getir("da-u")["durum"] == db.DUZELTME
    assert {x["kod"] for x in s.db.getir("da-k")["sorunlar"]} == {kalite.KAYNAK_YOK}


def test_ayni_dosya_iki_kez_yuklenmez(ortam, videolar):
    a, s, yt = ortam
    birak(a, "da-a", videolar / "iyi.mp4")
    calistir(s)
    birak(a, "da-b", videolar / "iyi.mp4")  # farklı kimlik, aynı dosya
    birak(a, "da-a", videolar / "iyi2.mp4")  # aynı kimlik, zaten yayında
    calistir(s)
    assert s.db.getir("da-b")["durum"] == db.KOPYA
    assert s.db.getir("da-a")["youtube_id"] == "vid1"
    assert len(yt.videolar) == 1


def test_baglanti_kopunca_kaldigi_yerden_devam(ortam, videolar):
    a, s, yt = ortam
    yt.kopma_parcasi = 2  # ilk parça gider, ikincide bağlantı kopar
    big = videolar / "buyuk.mp4"
    # Gürültülü kareler sıkışmaz: birden çok 1 MB parça oluşsun
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=540x960:rate=25:duration=6",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-vf", "noise=alls=80:allf=t",
           "-crf", "18",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(big))
    assert big.stat().st_size > 1.5 * 1024 * 1024
    birak(a, "da-d", big)
    calistir(s)
    satir = s.db.getir("da-d")
    assert satir["durum"] == db.YAYINDA
    assert yt.oturum_acma == 1, "yeni oturum açılmamalı, eski oturumdan devam edilmeli"
    assert bytes(next(iter(yt.oturumlar.values()))["alinan"]) == big.read_bytes()
    olaylar = [o["tur"] for o in s.db.olaylari_getir("da-d")]
    assert "gecici_hata" in olaylar and "devam" in olaylar


def test_servis_yeniden_baslayinca_devam_eder(ortam, videolar):
    a, s, yt = ortam
    yt.kopma_parcasi = 1
    birak(a, "da-r", videolar / "iyi.mp4")
    calistir(s, 1)  # ilk turda bağlantı koptu
    assert s.db.getir("da-r")["durum"] == db.YUKLENIYOR
    s.db.kapat()
    yeni = Servis(a, yt=YouTube(a.istemci_dosyasi, a.token_dosyasi, oturum=yt), mpt=SahteMPT())
    calistir(yeni)
    assert yeni.db.getir("da-r")["durum"] == db.YAYINDA and yt.oturum_acma == 1


def test_oturum_kaybolursa_isaretle_kopya_bulunur(ortam, videolar):
    a, s, yt = ortam
    birak(a, "da-m", videolar / "iyi.mp4")
    calistir(s)
    # Senaryo: yükleme bitti ama kayıt yazılmadan çöktük; oturum da sona ermiş.
    s.db.guncelle("da-m", durum=db.YUKLENIYOR, youtube_id=None, yukleme_url="https://upload.example/yok")
    yt.oturumlar.pop("https://upload.example/yok", None)
    orijinal = yt.request

    def sona_ermis(yontem, url, **kw):
        if url == "https://upload.example/yok":
            return Yanit(404, {"error": {"errors": [{"reason": "notFound"}]}})
        return orijinal(yontem, url, **kw)
    yt.request = sona_ermis
    calistir(s)
    satir = s.db.getir("da-m")
    assert satir["youtube_id"] == "vid1" and len(yt.videolar) == 1 and yt.oturum_acma == 1


def test_kota_dolunca_kuyrukta_bekler(ortam, videolar):
    a, s, yt = ortam
    yt.kota_doldu = True
    birak(a, "da-q", videolar / "iyi.mp4")
    calistir(s, 3)
    assert s.db.getir("da-q")["durum"] == db.YUKLENECEK
    assert s.db.genel_al("kota_bekleme")["bitis"] > time.time()
    yt.kota_doldu = False
    calistir(s, 3)
    assert s.db.getir("da-q")["durum"] == db.YUKLENECEK  # kota sıfırlanana kadar denemez
    s.db.genel_yaz("kota_bekleme", {"bitis": time.time() - 1})
    calistir(s)
    assert s.db.getir("da-q")["durum"] == db.YAYINDA


def test_yetki_hatasi_acikca_bildirilir(ortam, videolar):
    a, s, yt = ortam
    yt.yetki_iptal = True
    birak(a, "da-y", videolar / "iyi.mp4")
    calistir(s, 3)
    assert s.db.getir("da-y")["durum"] == db.YUKLENECEK
    assert "yetki" in s.db.genel_al("yetki_hatasi")["mesaj"].lower()
    assert "YETKİ HATASI" in a.alarm_dosyasi.read_text()
    # yeniden yetkilendirme token dosyasını değiştirir -> servis kendiliğinden devam eder
    yt.yetki_iptal = False
    time.sleep(0.01)
    guvenli_yaz(a.token_dosyasi, {"refresh_token": "yeni"})
    calistir(s)
    assert s.db.getir("da-y")["durum"] == db.YAYINDA
    assert not a.alarm_dosyasi.exists()


def test_yanlis_kanala_yuklenmez(ortam, videolar):
    a, s, yt = ortam
    yt.kanal = {"id": "UCbaska", "title": "Başka Kanal"}
    birak(a, "da-w", videolar / "iyi.mp4")
    calistir(s, 3)
    assert yt.oturum_acma == 0
    assert "kayıtlı kanala ait değil" in s.db.genel_al("yetki_hatasi")["mesaj"]


def test_ozel_kilit_tespit_edilir_ve_kuyruk_durur(ortam, videolar):
    a, s, yt = ortam
    yt.kilitle = True
    birak(a, "da-l1", videolar / "iyi.mp4")
    birak(a, "da-l2", videolar / "iyi2.mp4")
    calistir(s)
    assert s.db.getir("da-l1")["durum"] == db.KISITLI
    assert s.db.getir("da-l1")["yt_gorunurluk"] == "private"
    assert s.db.getir("da-l2")["durum"] == db.YUKLENECEK  # kilitlenmesin diye beklemede
    assert s.db.genel_al("denetim_bekleniyor")
    assert len(yt.videolar) == 1


def test_metadata_sinirlari():
    md = metadata.hazirla("x" * 30, {"baslik": "<Fenerbahçe> " + "çok uzun " * 30,
                                     "aciklama": "a" * 6000, "etiketler": [f"etiket {i}" for i in range(200)],
                                     "kaynaklar": ["https://example.com/a", "javascript:alert(1)"]},
                          sabit_etiketler=["Doksan Artı"], llm_kullan=False)
    assert len(md["baslik"]) <= 100 and "<" not in md["baslik"]
    assert len(md["aciklama"].encode()) <= 5000
    toplam = sum(len(e) + (2 if " " in e else 0) for e in md["etiketler"]) + len(md["etiketler"]) - 1
    assert toplam <= 500
    assert "javascript" not in md["aciklama"] and md["kaynak_sayisi"] == 1


def test_turkce_desteklemeyen_font_yakalanir(tmp_path):
    from doksan_yayin.ayarlar import REPO_KOK
    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nŞampiyonluk için gerekenler: İsmail, Uğur\n", encoding="utf-8")
    s = kalite.Sonuc(kalite.GECTI)
    kalite.yazi_kontrol(s, 1080, 1920, {}, srt, str(REPO_KOK / "resource/fonts/UTM Kabel KT.ttf"), 60)
    assert kalite.FONT_TURKCE in {x["kod"] for x in s.sorunlar}
    s2 = kalite.Sonuc(kalite.GECTI)
    kalite.yazi_kontrol(s2, 1080, 1920, {"metinler": [{"metin": "FENERBAHÇE ŞAMPİYONLUK YOLUNDA HER ŞEY",
                                                         "font": str(REPO_KOK / "resource/fonts/BeVietnamPro-Bold.ttf"),
                                                         "punto": 140}]},
                        None, None, None)
    assert kalite.YAZI_TASIYOR in {x["kod"] for x in s2.sorunlar}


def test_mpt_gorevi_otomatik_duzeltilip_yayinlanir(ortam, videolar):
    a, s, yt = ortam
    a.mpt_konu_deseni = "(?i)fenerbah"
    gorev = a.mpt_gorev_dizini / "t1"
    gorev.mkdir(parents=True)
    shutil.copyfile(videolar / "sessiz_akis.mp4", gorev / "final-1.mp4")
    params = {"video_subject": "Fenerbahçe şampiyonluk", "font_name": "STHeitiMedium.ttc",
              "font_size": 60, "subtitle_enabled": False}
    (gorev / "script.json").write_text(json.dumps({"script": "Senaryo.", "search_terms": ["futbol"],
                                                   "params": params}, ensure_ascii=False))
    (gorev / "doksan.json").write_text(json.dumps({"kaynaklar": ["https://www.tff.org/"]}))
    baslatilan = []

    class MPT:
        def gorev_durumu(self, g):
            return {"state": 1} if g == "t2" else None

        def render_baslat(self, p):
            baslatilan.append(p)
            d = a.mpt_gorev_dizini / "t2"
            d.mkdir()
            shutil.copyfile(videolar / "iyi.mp4", d / "final-1.mp4")
            (d / "script.json").write_text(json.dumps({"script": "Senaryo.", "params": p}))
            return "t2"
    s.mpt = MPT()
    calistir(s)
    satir = s.db.getir("mpt-t1-1")
    assert baslatilan and baslatilan[0]["video_script"] == "Senaryo."
    assert satir["durum"] == db.YAYINDA and satir["duzeltme_sayisi"] == 1
    assert s.db.getir("mpt-t2-1") is None, "düzeltme görevi ayrı video sayılmamalı"
