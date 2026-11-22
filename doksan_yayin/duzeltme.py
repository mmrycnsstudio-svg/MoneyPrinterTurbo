"""Düzeltme kuyruğu.

- MoneyPrinterTurbo'dan gelen videolarda sorunlar otomatik düzeltilir: aynı
  senaryo ve ayarlarla, soruna göre değiştirilmiş parametrelerle (küçük punto,
  Türkçe destekli font, yeniden ses) MPT API'sinde yeniden render başlatılır.
  Hak dolarsa video 'elle_duzeltme' durumuna geçer.
- Hazır klasörüne bırakılan videolar için sorun raporu
  duzeltme_kuyrugu/<uretim_id>.json dosyasına yazılır; üretici aynı üretim
  kimliğiyle düzeltilmiş videoyu yeniden bıraktığında kontrol tekrarlanır.
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import requests

from . import kalite
from .youtube import guvenli_yaz

TURKCE_FONT = "BeVietnamPro-Bold.ttf"
MIN_PUNTO = 32
OTOMATIK = {kalite.YAZI_TASIYOR, kalite.FONT_TURKCE, kalite.SES_YOK, kalite.SES_SESSIZ,
            kalite.SES_EKSIK, kalite.BOZUK, kalite.VIDEO_YOK, kalite.DOSYA_YOK}


def rapor_yaz(dizin: Path, uretim_id: str, satir: dict, sorunlar: list[dict]) -> Path:
    yol = dizin / f"{uretim_id}.json"
    guvenli_yaz(yol, {
        "uretim_id": uretim_id,
        "video": satir.get("video_yolu"),
        "zaman": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sorunlar": sorunlar,
        "ne_yapmali": ("Videoyu düzelt ve aynı uretim_id ile storage/doksan-arti/hazir/"
                       f"{uretim_id}/ klasörüne yeniden bırak (video.mp4 + manifest.json + TAMAM)."),
    })
    try:
        yol.chmod(0o644)
    except OSError:
        pass
    return yol


def rapor_sil(dizin: Path, uretim_id: str) -> None:
    (dizin / f"{uretim_id}.json").unlink(missing_ok=True)


def otomatik_mi(sorunlar: list[dict]) -> bool:
    return bool(sorunlar) and all(s.get("kod") in OTOMATIK for s in sorunlar)


def duzeltilmis_parametreler(params: dict, sorunlar: list[dict], senaryo: str,
                             terimler) -> dict:
    p = copy.deepcopy(params)
    kodlar = {s.get("kod") for s in sorunlar}
    if kalite.YAZI_TASIYOR in kodlar:
        p["font_size"] = max(MIN_PUNTO, int(int(p.get("font_size") or 60) * 0.82))
    if kalite.FONT_TURKCE in kodlar:
        p["font_name"] = TURKCE_FONT
    if senaryo:
        p["video_script"] = senaryo  # yeni LLM çağrısı / farklı senaryo yok
    if terimler:
        p["video_terms"] = terimler
    p["video_count"] = 1
    return p


class MPTIstemcisi:
    def __init__(self, temel: str, anahtar: str = "", oturum: requests.Session | None = None):
        self.temel = temel.rstrip("/")
        self.http = oturum or requests.Session()
        self.basliklar = {"x-api-key": anahtar} if anahtar else {}

    def render_baslat(self, params: dict) -> str:
        y = self.http.post(f"{self.temel}/videos", json=params, headers=self.basliklar, timeout=60)
        y.raise_for_status()
        veri = y.json().get("data") or {}
        if not veri.get("task_id"):
            raise RuntimeError(f"MPT görev kimliği dönmedi: {y.text[:200]}")
        return veri["task_id"]

    def gorev_durumu(self, gorev_id: str) -> dict | None:
        y = self.http.get(f"{self.temel}/tasks/{gorev_id}", headers=self.basliklar, timeout=30)
        if y.status_code == 404:
            return None
        y.raise_for_status()
        return y.json().get("data") or {}


def mpt_gorev_bilgisi(gorev_dizini: Path) -> dict:
    """MPT görev klasöründen senaryo, parametre ve altyazı bilgilerini okur."""
    bilgi: dict = {}
    sj = gorev_dizini / "script.json"
    if sj.exists():
        try:
            bilgi = json.loads(sj.read_text(encoding="utf-8"))
        except ValueError:
            bilgi = {}
    return bilgi
