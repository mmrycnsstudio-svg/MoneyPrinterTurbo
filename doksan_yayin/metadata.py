"""Türkçe başlık, açıklama, kaynak bağlantıları ve etiketler.

Öncelik: üreticinin manifest'te verdiği alanlar -> MPT'nin LLM ile ürettiği
sosyal metin (Türkçe) -> senaryodan sezgisel yedek. Sonuç her zaman YouTube
sınırlarına (başlık 100 karakter, açıklama 5000 bayt, etiketler 500 karakter,
'<' ve '>' yasak) göre temizlenir.
"""
from __future__ import annotations

import re
import unicodedata

from loguru import logger

BASLIK_MAX = 100
ACIKLAMA_MAX_BAYT = 4900
ETIKET_MAX = 480
ISARET_ONEKI = "dzuretim"


def isaret_etiketi(uretim_id: str) -> str:
    """Kopya kontrolünde kendi yüklemelerimizi tanımak için gizli etiket."""
    temiz = re.sub(r"[^0-9a-zA-Z]", "", uretim_id).lower()[:24]
    return f"{ISARET_ONEKI}{temiz}"


def _temizle(metin: str) -> str:
    metin = unicodedata.normalize("NFC", str(metin or ""))
    metin = metin.replace("<", "‹").replace(">", "›")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", metin).strip()


def _kirp(metin: str, n: int) -> str:
    metin = _temizle(metin)
    if len(metin) <= n:
        return metin
    kes = metin[: n - 1]
    if " " in kes[n // 2:]:
        kes = kes[: kes.rfind(" ")]
    return kes.rstrip(" ,.;:-") + "…"


def _bayt_kirp(metin: str, n: int) -> str:
    b = metin.encode("utf-8")
    if len(b) <= n:
        return metin
    return b[:n].decode("utf-8", "ignore").rstrip() + "…"


def _kaynak_satirlari(kaynaklar) -> list[str]:
    satirlar = []
    for k in kaynaklar or []:
        if isinstance(k, dict):
            url, ad = k.get("url", ""), k.get("ad") or k.get("baslik") or ""
        else:
            url, ad = str(k), ""
        url = url.strip()
        if not re.match(r"^https?://", url):
            continue
        satirlar.append(f"• {_temizle(ad)}: {url}" if ad else f"• {url}")
    return satirlar


def _etiketleri_sinirla(etiketler: list[str]) -> list[str]:
    sonuc, gorulen, toplam = [], set(), 0
    for e in etiketler:
        e = _temizle(e).lstrip("#").replace(",", " ").strip()
        e = re.sub(r"\s+", " ", e)[:60]
        if not e or e.lower() in gorulen:
            continue
        maliyet = len(e) + (2 if " " in e else 0) + (1 if sonuc else 0)
        if toplam + maliyet > ETIKET_MAX:
            continue
        sonuc.append(e)
        gorulen.add(e.lower())
        toplam += maliyet
    return sonuc


def _llm_metni(konu: str, senaryo: str) -> dict | None:
    try:
        from app.services import llm  # type: ignore
        sonuc = llm.generate_social_metadata(
            video_subject=konu, video_script=senaryo, language="Türkçe (tr-TR)",
            platform="youtube_shorts")
        if sonuc and (sonuc.get("title") or sonuc.get("caption")):
            return sonuc
    except Exception as e:
        logger.warning(f"LLM ile metin üretilemedi, yedek kullanılacak: {e}")
    return None


def _ilk_cumle(metin: str) -> str:
    m = re.split(r"(?<=[.!?…])\s+", metin.strip(), maxsplit=1)
    return m[0] if m else metin


def hazirla(uretim_id: str, manifest: dict, *, sabit_etiketler: list[str],
            llm_kullan: bool = True, dil: str = "tr") -> dict:
    konu = str(manifest.get("konu") or manifest.get("video_subject") or "").strip()
    senaryo = str(manifest.get("senaryo") or manifest.get("script") or "").strip()

    baslik = manifest.get("baslik")
    aciklama = manifest.get("aciklama")
    hashtagler: list[str] = list(manifest.get("hashtagler") or [])
    kaynak_tur = "manifest"

    if (not baslik or not aciklama) and llm_kullan and (konu or senaryo):
        uretilen = _llm_metni(konu, senaryo)
        if uretilen:
            baslik = baslik or uretilen.get("title")
            aciklama = aciklama or uretilen.get("caption")
            hashtagler = hashtagler or list(uretilen.get("hashtags") or [])
            kaynak_tur = "llm"
    if not baslik:
        baslik = konu or _ilk_cumle(senaryo) or "Doksan Artı"
        kaynak_tur = "yedek"
    if not aciklama:
        aciklama = senaryo[:600] if senaryo else konu

    baslik = _kirp(baslik, BASLIK_MAX)
    if "#shorts" not in baslik.lower() and len(baslik) + 8 <= BASLIK_MAX:
        baslik = f"{baslik} #Shorts"

    hashtagler = [h if str(h).startswith("#") else f"#{h}" for h in hashtagler if str(h).strip()]
    if not any(h.lower() == "#shorts" for h in hashtagler):
        hashtagler.append("#Shorts")
    hashtagler = [re.sub(r"\s+", "", _temizle(h)) for h in hashtagler][:6]

    bolumler = [_temizle(aciklama)]
    kaynaklar = _kaynak_satirlari(manifest.get("kaynaklar"))
    if kaynaklar:
        bolumler.append("Kaynaklar:\n" + "\n".join(kaynaklar))
    bolumler.append(" ".join(hashtagler))
    tam_aciklama = _bayt_kirp("\n\n".join(b for b in bolumler if b), ACIKLAMA_MAX_BAYT)

    etiketler = _etiketleri_sinirla(
        [isaret_etiketi(uretim_id)] + list(manifest.get("etiketler") or [])
        + [h.lstrip("#") for h in hashtagler] + list(sabit_etiketler))

    return {
        "baslik": baslik,
        "aciklama": tam_aciklama,
        "etiketler": etiketler,
        "dil": dil,
        "kaynak_sayisi": len(kaynaklar),
        "uretim_kaynagi": kaynak_tur,
    }
