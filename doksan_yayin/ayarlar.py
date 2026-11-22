"""Ortam değişkenlerinden okunan ayarlar.

Gizli bilgiler (OAuth istemcisi, yenileme tokenı, kanal kimliği) depo dışında,
DOKSAN_GIZLI_DIZIN altında 0600 izinle tutulur. Depo herkese açık olduğu için
bu dizin hiçbir zaman depo içinde olmamalıdır.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_KOK = Path(__file__).resolve().parent.parent


def _bool(ad: str, varsayilan: bool) -> bool:
    deger = os.environ.get(ad)
    if deger is None or deger.strip() == "":
        return varsayilan
    return deger.strip().lower() in {"1", "true", "evet", "yes", "on"}


def _int(ad: str, varsayilan: int) -> int:
    try:
        return int(os.environ.get(ad, "") or varsayilan)
    except ValueError:
        return varsayilan


@dataclass
class Ayarlar:
    # Çalışma dizini (kuyruk, veritabanı, durum dosyaları). storage/ git'e girmez.
    kok: Path = field(default_factory=lambda: Path(
        os.environ.get("DOKSAN_KOK", str(REPO_KOK / "storage" / "doksan-arti"))))
    # Gizli dosyalar: depo DIŞINDA.
    gizli: Path = field(default_factory=lambda: Path(
        os.environ.get("DOKSAN_GIZLI_DIZIN", "/opt/mrycns/secrets/doksan-arti")))
    gorunurluk: str = field(default_factory=lambda: os.environ.get("DOKSAN_GORUNURLUK", "public"))
    kategori: str = field(default_factory=lambda: os.environ.get("DOKSAN_KATEGORI", "17"))  # Spor
    dil: str = field(default_factory=lambda: os.environ.get("DOKSAN_DIL", "tr"))
    cocuklara_yonelik: bool = field(default_factory=lambda: _bool("DOKSAN_COCUKLARA_YONELIK", False))
    yapay_icerik: bool = field(default_factory=lambda: _bool("DOKSAN_YAPAY_ICERIK", False))
    kaynak_zorunlu: bool = field(default_factory=lambda: _bool("DOKSAN_KAYNAK_ZORUNLU", True))
    sadece_shorts: bool = field(default_factory=lambda: _bool("DOKSAN_SADECE_SHORTS", True))
    llm_metadata: bool = field(default_factory=lambda: _bool("DOKSAN_LLM_METADATA", True))
    sabit_etiketler: list[str] = field(default_factory=lambda: [
        e.strip() for e in os.environ.get(
            "DOKSAN_SABIT_ETIKETLER", "Doksan Artı,futbol,Süper Lig,Shorts").split(",") if e.strip()])
    # MoneyPrinterTurbo API (compose ağı içinde "api" servisi).
    mpt_api: str = field(default_factory=lambda: os.environ.get("DOKSAN_MPT_API", "http://api:8080/api/v1"))
    mpt_api_anahtari: str = field(default_factory=lambda: os.environ.get("DOKSAN_MPT_API_ANAHTARI", ""))
    # Boş: MPT görevleri otomatik alınmaz. Örn. "(?i)doksan|fenerbah|galatasaray|beşiktaş|trabzon"
    mpt_konu_deseni: str = field(default_factory=lambda: os.environ.get("DOKSAN_MPT_KONU_DESENI", ""))
    mpt_gorev_dizini: Path = field(default_factory=lambda: Path(
        os.environ.get("DOKSAN_MPT_GOREV_DIZINI", str(REPO_KOK / "storage" / "tasks"))))
    otomatik_duzeltme_hakki: int = field(default_factory=lambda: _int("DOKSAN_DUZELTME_HAKKI", 2))
    dongu_saniye: int = field(default_factory=lambda: _int("DOKSAN_DONGU_SANIYE", 30))
    parca_mb: int = field(default_factory=lambda: _int("DOKSAN_PARCA_MB", 8))
    bildirim_url: str = field(default_factory=lambda: os.environ.get("DOKSAN_BILDIRIM_URL", ""))

    # --- türetilmiş yollar ---
    @property
    def hazir(self) -> Path:  # üretici final videoyu buraya bırakır
        return self.kok / "hazir"

    @property
    def duzeltme_kuyrugu(self) -> Path:
        return self.kok / "duzeltme_kuyrugu"

    @property
    def veritabani(self) -> Path:
        return self.kok / "yayin.db"

    @property
    def durum_dosyasi(self) -> Path:
        return self.kok / "durum.json"

    @property
    def alarm_dosyasi(self) -> Path:
        return self.kok / "ALARM.txt"

    @property
    def sonuc_kaydi(self) -> Path:
        return self.kok / "sonuclar.jsonl"

    @property
    def istemci_dosyasi(self) -> Path:
        return self.gizli / "client_secret.json"

    @property
    def token_dosyasi(self) -> Path:
        return self.gizli / "token.json"

    @property
    def kanal_dosyasi(self) -> Path:
        return self.gizli / "kanal.json"

    def dizinleri_hazirla(self) -> None:
        for d in (self.kok, self.hazir, self.duzeltme_kuyrugu):
            d.mkdir(parents=True, exist_ok=True)
        self.gizli.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.gizli, 0o700)
        except OSError:
            pass
        _depo_icinde_mi(self.gizli)


def _depo_icinde_mi(yol: Path) -> None:
    """Gizli dizin depo içindeyse ve git tarafından yok sayılmıyorsa dur."""
    try:
        yol.resolve().relative_to(REPO_KOK)
    except ValueError:
        return  # depo dışında: doğru
    goreli = yol.resolve().relative_to(REPO_KOK).as_posix()
    if not (goreli.startswith("storage/") or goreli.startswith(".secrets")):
        raise RuntimeError(
            f"Gizli dizin depo içinde ve git'e girebilir: {yol}. "
            "DOKSAN_GIZLI_DIZIN'i depo dışına ayarla (örn. /opt/mrycns/secrets/doksan-arti).")


def yukle() -> Ayarlar:
    return Ayarlar()
