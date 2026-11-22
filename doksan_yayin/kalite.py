"""Yükleme öncesi kalite kontrolü.

Sonuç üç türlüdür:
  - BEKLE   : render henüz bitmemiş (dosya büyüyor / tamamlanma işareti yok)
  - GECTI   : yüklemeye hazır
  - KALDI   : sorun listesiyle düzeltme kuyruğuna gider

Kontroller: dosya bütünlüğü (tam çözümleme), video/ses akışı, sessiz ya da eksik
ses, Shorts biçimi (dikey, <=180 sn), altyazı/yazı taşması ve Türkçe karakter
desteği, üreticinin kendi kalite raporu (manifest["kalite"]).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

BEKLE, GECTI, KALDI = "bekle", "gecti", "kaldi"

# Sorun kodları (düzeltme mantığı bunlara göre çalışır)
DOSYA_YOK = "dosya_yok"
BOZUK = "bozuk_dosya"
VIDEO_YOK = "video_akisi_yok"
SES_YOK = "ses_yok"
SES_SESSIZ = "ses_sessiz"
SES_EKSIK = "ses_eksik"
SHORTS_DEGIL = "shorts_degil"
YAZI_TASIYOR = "yazi_tasiyor"
FONT_TURKCE = "font_turkce_desteklemiyor"
URETICI_KALITE = "uretici_kalite_kontrolu_basarisiz"
KAYNAK_YOK = "kaynak_baglantisi_yok"

TURKCE_OZEL = set("çÇğĞıİöÖşŞüÜ")
SABIT_SURE_SN = 45  # dosya bu kadar süre değişmezse render bitmiş sayılır


@dataclass
class Sonuc:
    karar: str
    sorunlar: list[dict] = field(default_factory=list)
    bilgi: dict = field(default_factory=dict)

    def sorun(self, kod: str, aciklama: str, **ek) -> None:
        self.sorunlar.append({"kod": kod, "aciklama": aciklama, **ek})

    def rapor(self) -> dict:
        return {"karar": self.karar, "sorunlar": self.sorunlar, "bilgi": self.bilgi}


def _calistir(komut: list[str], zaman_asimi: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(komut, capture_output=True, text=True, timeout=zaman_asimi)


def ffprobe(yol: Path) -> dict:
    p = _calistir(["ffprobe", "-v", "error", "-print_format", "json",
                   "-show_format", "-show_streams", str(yol)], 120)
    if p.returncode != 0:
        raise ValueError(p.stderr.strip() or "ffprobe okuyamadı")
    return json.loads(p.stdout or "{}")


def tam_cozumle(yol: Path) -> str:
    """Tüm kareleri ve sesi çözer; hata metni döner (boşsa sağlam)."""
    p = _calistir(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(yol),
                   "-f", "null", "-"], 1800)
    hata = (p.stderr or "").strip()
    if p.returncode != 0 and not hata:
        hata = f"ffmpeg çıkış kodu {p.returncode}"
    return hata


def ses_seviyesi(yol: Path) -> float | None:
    p = _calistir(["ffmpeg", "-nostdin", "-i", str(yol), "-vn", "-af", "volumedetect",
                   "-f", "null", "-"], 600)
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", p.stderr or "")
    if not m:
        return None
    return float("-inf") if m.group(1) == "-inf" else float(m.group(1))


def render_bitti_mi(yol: Path, tamam_isareti: Path | None) -> bool:
    if tamam_isareti is not None:
        return tamam_isareti.exists()
    try:
        st = yol.stat()
    except FileNotFoundError:
        return False
    return (time.time() - st.st_mtime) >= SABIT_SURE_SN and st.st_size > 0


# ---------------- yazı taşması ----------------

def _srt_metinleri(srt: Path) -> list[str]:
    try:
        icerik = srt.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    metinler = []
    for blok in re.split(r"\n\s*\n", icerik.strip()):
        satirlar = [s for s in blok.splitlines() if s.strip()]
        satirlar = [s for s in satirlar if not s.strip().isdigit() and "-->" not in s]
        if satirlar:
            metinler.append("\n".join(satirlar))
    return metinler


def _font_turkce_destekliyor(font_yolu: str, metin: str) -> bool:
    ornek = "".join(sorted({c for c in metin if c in TURKCE_OZEL}))
    if not ornek:
        return True
    try:
        from fontTools.ttLib import TTFont  # MPT bağımlılığı olabilir
        f = TTFont(font_yolu, fontNumber=0, lazy=True)
        cmap = f.getBestCmap() or {}
        return all(ord(c) in cmap for c in ornek)
    except Exception:
        pass
    try:  # yedek: PIL ile "tofu" karşılaştırması
        from PIL import ImageFont
        font = ImageFont.truetype(font_yolu, 40)
        tofu = font.getmask("￿").getbbox()
        return all(font.getmask(c).getbbox() != tofu for c in ornek)
    except Exception:
        return True


def _sar(metin: str, font_yolu: str, punto: int, max_gen: int) -> tuple[list[str], int, int]:
    """MPT'nin kendi sarma fonksiyonu varsa onu, yoksa sözcük bazlı PIL sarmayı kullanır.

    Döner: (satırlar, en geniş satır px, toplam yükseklik px)
    """
    from PIL import ImageFont
    font = ImageFont.truetype(font_yolu, punto)
    satirlar: list[str]
    try:
        from app.services import video as mpt_video  # type: ignore
        sarili, _ = mpt_video.wrap_text(metin, max_gen, font=font_yolu, fontsize=punto)
        satirlar = sarili.split("\n")
    except Exception:
        satirlar = []
        for paragraf in metin.split("\n"):
            mevcut = ""
            for sozcuk in paragraf.split():
                aday = f"{mevcut} {sozcuk}".strip()
                if font.getlength(aday) <= max_gen or not mevcut:
                    mevcut = aday
                else:
                    satirlar.append(mevcut)
                    mevcut = sozcuk
            satirlar.append(mevcut)
    gen = max((int(font.getlength(s)) for s in satirlar), default=0)
    asc, desc = font.getmetrics()
    return satirlar, gen, (asc + desc) * len(satirlar)


def yazi_kontrol(sonuc: Sonuc, genislik: int, yukseklik: int, manifest: dict,
                 srt: Path | None, font_yolu: str | None, punto: int | None) -> None:
    # 1) Altyazılar (MPT ayarlarıyla aynı genişlik: video genişliğinin %90'ı)
    if srt and srt.exists() and font_yolu and punto:
        max_gen = int(genislik * 0.9) - 2 * int(punto * 0.25)
        tum = ""
        for metin in _srt_metinleri(srt):
            tum += metin
            satirlar, gen, yuk = _sar(metin, font_yolu, punto, max_gen)
            if gen > genislik * 0.94:
                sonuc.sorun(YAZI_TASIYOR, f"Altyazı satırı ekrandan taşıyor ({gen}px > {genislik}px)",
                            metin=metin[:120], tur="altyazi")
                break
            if yuk > yukseklik * 0.38:
                sonuc.sorun(YAZI_TASIYOR, f"Altyazı bloğu çok yüksek ({len(satirlar)} satır)",
                            metin=metin[:120], tur="altyazi")
                break
        if tum and not _font_turkce_destekliyor(font_yolu, tum):
            sonuc.sorun(FONT_TURKCE, f"Altyazı fontu Türkçe karakterleri çizemiyor: {Path(font_yolu).name}")

    # 2) Üreticinin bildirdiği ekran yazıları (başlık kartı, oyuncu adı vb.)
    for y in manifest.get("metinler") or []:
        try:
            fy = y.get("font") or font_yolu
            pt = int(y.get("punto") or punto or 60)
            kutu = int(y.get("kutu_genislik") or genislik * 0.9)
            from PIL import ImageFont
            font = ImageFont.truetype(fy, pt)
            en_genis = max(font.getlength(s) for s in str(y.get("metin", "")).split("\n"))
            if en_genis > kutu:
                sonuc.sorun(YAZI_TASIYOR, f"Ekran yazısı kutusundan taşıyor ({int(en_genis)}px > {kutu}px)",
                            metin=str(y.get("metin"))[:120], tur="ekran_yazisi")
            if not _font_turkce_destekliyor(fy, str(y.get("metin", ""))):
                sonuc.sorun(FONT_TURKCE, f"Ekran yazısı fontu Türkçe karakter çizemiyor: {Path(fy).name}")
        except Exception as e:  # ölçülemeyen yazı yüklemeyi engellemez, rapora düşer
            sonuc.bilgi.setdefault("uyarilar", []).append(f"yazı ölçülemedi: {e}")


# ---------------- ana kontrol ----------------

def kontrol_et(video: Path, manifest: dict, *, tamam_isareti: Path | None = None,
               srt: Path | None = None, font_yolu: str | None = None, punto: int | None = None,
               sadece_shorts: bool = True, kaynak_zorunlu: bool = True) -> Sonuc:
    video = Path(video)
    if not video.exists():
        if tamam_isareti is not None and tamam_isareti.exists():
            s = Sonuc(KALDI)
            s.sorun(DOSYA_YOK, "Tamamlandı işareti var ama video dosyası yok")
            return s
        return Sonuc(BEKLE, bilgi={"neden": "video dosyası henüz yok"})
    if not render_bitti_mi(video, tamam_isareti):
        return Sonuc(BEKLE, bilgi={"neden": "render henüz tamamlanmadı"})

    s = Sonuc(GECTI)
    try:
        bilgi = ffprobe(video)
    except Exception as e:
        s.karar = KALDI
        s.sorun(BOZUK, f"Dosya okunamıyor: {e}")
        return s

    akislar = bilgi.get("streams") or []
    v = next((a for a in akislar if a.get("codec_type") == "video"), None)
    a = next((a for a in akislar if a.get("codec_type") == "audio"), None)
    sure = float((bilgi.get("format") or {}).get("duration") or 0)
    s.bilgi.update(sure=sure, boyut=video.stat().st_size)

    if v is None:
        s.sorun(VIDEO_YOK, "Video akışı yok")
    else:
        gen, yuk = int(v.get("width") or 0), int(v.get("height") or 0)
        s.bilgi.update(genislik=gen, yukseklik=yuk, kodek=v.get("codec_name"))
        if sadece_shorts and (yuk <= gen or sure > 180):
            s.sorun(SHORTS_DEGIL, f"Shorts biçimi değil ({gen}x{yuk}, {sure:.0f} sn)")

    hata = tam_cozumle(video)
    if hata:
        s.sorun(BOZUK, "Dosya çözümlenirken hata: " + hata[:400])

    if a is None:
        s.sorun(SES_YOK, "Ses akışı yok")
    else:
        a_sure = float(a.get("duration") or sure or 0)
        v_sure = float((v or {}).get("duration") or sure or 0)
        if v_sure and a_sure + 1.5 < v_sure:
            s.sorun(SES_EKSIK, f"Ses videodan kısa ({a_sure:.1f} sn / {v_sure:.1f} sn)")
        seviye = ses_seviyesi(video)
        s.bilgi["ses_db"] = seviye
        if seviye is not None and seviye < -50:
            s.sorun(SES_SESSIZ, f"Ses neredeyse sessiz (ortalama {seviye} dB)")

    if v is not None:
        yazi_kontrol(s, int(v.get("width") or 1080), int(v.get("height") or 1920),
                     manifest, srt, font_yolu, punto)

    uretici = manifest.get("kalite") or {}
    if str(uretici.get("durum", "")).lower() in {"kaldi", "basarisiz", "failed", "fail"}:
        for so in uretici.get("sorunlar") or ["üretici kalite kontrolü başarısız"]:
            kod = so.get("kod", URETICI_KALITE) if isinstance(so, dict) else URETICI_KALITE
            ac = so.get("aciklama", str(so)) if isinstance(so, dict) else str(so)
            s.sorun(kod, ac, kaynak="uretici")

    if kaynak_zorunlu and not manifest.get("kaynaklar"):
        s.sorun(KAYNAK_YOK, "Açıklamaya eklenecek kaynak bağlantısı yok (manifest['kaynaklar'])")

    if s.sorunlar:
        s.karar = KALDI
    return s
