"""Kalıcı kuyruk ve kayıt (SQLite, WAL).

Her video için kalıcı üretim kimliği, dosya özeti (SHA-256), YouTube video
kimliği, devam ettirilebilir yükleme oturumu ve gönderilen bayt sayısı tutulur.
Servis yeniden başlatıldığında kuyruktan kaldığı yerden devam eder.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Durumlar
ALINDI = "alindi"                  # kuyruğa girdi, kalite kontrolü bekliyor
DUZELTME = "duzeltme"              # düzeltme kuyruğunda (üretici düzeltecek)
DUZELTILIYOR = "duzeltiliyor"      # otomatik yeniden render sürüyor
ELLE_DUZELTME = "elle_duzeltme"    # otomatik düzeltme hakkı bitti
YUKLENECEK = "yuklenecek"          # kaliteden geçti, yükleme kuyruğunda
YUKLENIYOR = "yukleniyor"          # yükleme oturumu açık (devam ettirilebilir)
ISLENIYOR = "isleniyor"            # dosya gitti, YouTube işliyor
YAYINDA = "yayinda"                # işlendi ve herkese açık doğrulandı
KISITLI = "kisitli"                # işlendi ama herkese açık değil
REDDEDILDI = "reddedildi"          # YouTube reddetti / işleme başarısız
KOPYA = "kopya"                    # aynı dosya zaten yüklenmiş

YUKLEME_SONRASI = {ISLENIYOR, YAYINDA, KISITLI, REDDEDILDI}

SEMA = """
CREATE TABLE IF NOT EXISTS videolar (
    uretim_id        TEXT PRIMARY KEY,
    durum            TEXT NOT NULL,
    kaynak           TEXT NOT NULL,            -- 'hazir' | 'mpt'
    video_yolu       TEXT NOT NULL,
    sha256           TEXT,
    boyut            INTEGER,
    manifest         TEXT NOT NULL DEFAULT '{}',
    metadata         TEXT,                     -- hazırlanan başlık/açıklama/etiket
    kalite_raporu    TEXT,
    sorunlar         TEXT,
    duzeltme_sayisi  INTEGER NOT NULL DEFAULT 0,
    duzeltme_gorev   TEXT,                     -- MPT yeniden render görev kimliği
    yukleme_url      TEXT,                     -- devam ettirilebilir oturum adresi
    gonderilen_bayt  INTEGER NOT NULL DEFAULT 0,
    oturum_acildi    INTEGER NOT NULL DEFAULT 0, -- en az bir oturum açıldı mı
    youtube_id       TEXT,
    youtube_url      TEXT,
    yt_yukleme_durumu TEXT,
    yt_gorunurluk    TEXT,
    deneme           INTEGER NOT NULL DEFAULT 0,
    sonraki_deneme   REAL NOT NULL DEFAULT 0,
    son_hata         TEXT,
    olusturma        REAL NOT NULL,
    guncelleme       REAL NOT NULL,
    yuklenme_zamani  REAL,
    sonuc_zamani     REAL
);
CREATE INDEX IF NOT EXISTS videolar_durum ON videolar(durum, sonraki_deneme);
CREATE INDEX IF NOT EXISTS videolar_sha ON videolar(sha256);
CREATE UNIQUE INDEX IF NOT EXISTS videolar_ytid ON videolar(youtube_id) WHERE youtube_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS olaylar (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    zaman     REAL NOT NULL,
    uretim_id TEXT,
    tur       TEXT NOT NULL,
    mesaj     TEXT
);
CREATE TABLE IF NOT EXISTS genel (
    anahtar TEXT PRIMARY KEY,
    deger   TEXT
);
"""

JSON_ALANLAR = {"manifest", "metadata", "kalite_raporu", "sorunlar"}


class Veritabani:
    def __init__(self, yol: Path):
        self.yol = Path(yol)
        self.yol.parent.mkdir(parents=True, exist_ok=True)
        self._baglanti = sqlite3.connect(str(self.yol), timeout=30, isolation_level=None)
        self._baglanti.row_factory = sqlite3.Row
        self._baglanti.execute("PRAGMA journal_mode=WAL")
        self._baglanti.execute("PRAGMA synchronous=FULL")
        self._baglanti.executescript(SEMA)

    def kapat(self) -> None:
        self._baglanti.close()

    @contextmanager
    def islem(self) -> Iterator[sqlite3.Connection]:
        self._baglanti.execute("BEGIN IMMEDIATE")
        try:
            yield self._baglanti
            self._baglanti.execute("COMMIT")
        except BaseException:
            self._baglanti.execute("ROLLBACK")
            raise

    # --- satır yardımcıları ---
    @staticmethod
    def _coz(satir: sqlite3.Row | None) -> dict | None:
        if satir is None:
            return None
        d = dict(satir)
        for k in JSON_ALANLAR:
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (TypeError, ValueError):
                    pass
        return d

    def getir(self, uretim_id: str) -> dict | None:
        return self._coz(self._baglanti.execute(
            "SELECT * FROM videolar WHERE uretim_id=?", (uretim_id,)).fetchone())

    def sha_ile_bul(self, sha: str) -> list[dict]:
        return [self._coz(r) for r in self._baglanti.execute(
            "SELECT * FROM videolar WHERE sha256=?", (sha,)).fetchall()]

    def durumdakiler(self, *durumlar: str, zamani_gelmis: bool = False) -> list[dict]:
        soru = ",".join("?" * len(durumlar))
        sql = f"SELECT * FROM videolar WHERE durum IN ({soru})"
        params: list[Any] = list(durumlar)
        if zamani_gelmis:
            sql += " AND sonraki_deneme <= ?"
            params.append(time.time())
        sql += " ORDER BY olusturma"
        return [self._coz(r) for r in self._baglanti.execute(sql, params).fetchall()]

    def hepsi(self) -> list[dict]:
        return [self._coz(r) for r in self._baglanti.execute(
            "SELECT * FROM videolar ORDER BY olusturma").fetchall()]

    def ekle(self, uretim_id: str, kaynak: str, video_yolu: str, manifest: dict) -> None:
        simdi = time.time()
        self._baglanti.execute(
            "INSERT INTO videolar (uretim_id, durum, kaynak, video_yolu, manifest, olusturma, guncelleme)"
            " VALUES (?,?,?,?,?,?,?)",
            (uretim_id, ALINDI, kaynak, video_yolu, json.dumps(manifest, ensure_ascii=False), simdi, simdi))
        self.olay(uretim_id, "alindi", video_yolu)

    def guncelle(self, uretim_id: str, **alanlar: Any) -> None:
        if not alanlar:
            return
        alanlar["guncelleme"] = time.time()
        for k in list(alanlar):
            if k in JSON_ALANLAR and alanlar[k] is not None and not isinstance(alanlar[k], str):
                alanlar[k] = json.dumps(alanlar[k], ensure_ascii=False)
        atama = ", ".join(f"{k}=?" for k in alanlar)
        self._baglanti.execute(
            f"UPDATE videolar SET {atama} WHERE uretim_id=?", (*alanlar.values(), uretim_id))

    def olay(self, uretim_id: str | None, tur: str, mesaj: str = "") -> None:
        self._baglanti.execute(
            "INSERT INTO olaylar (zaman, uretim_id, tur, mesaj) VALUES (?,?,?,?)",
            (time.time(), uretim_id, tur, mesaj[:4000]))

    def olaylari_getir(self, uretim_id: str, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self._baglanti.execute(
            "SELECT * FROM olaylar WHERE uretim_id=? ORDER BY id DESC LIMIT ?",
            (uretim_id, limit)).fetchall()]

    # --- genel anahtar/değer (yetki hatası, kota beklemesi vb.) ---
    def genel_al(self, anahtar: str) -> Any:
        r = self._baglanti.execute("SELECT deger FROM genel WHERE anahtar=?", (anahtar,)).fetchone()
        return json.loads(r[0]) if r and r[0] is not None else None

    def genel_yaz(self, anahtar: str, deger: Any) -> None:
        if deger is None:
            self._baglanti.execute("DELETE FROM genel WHERE anahtar=?", (anahtar,))
        else:
            self._baglanti.execute(
                "INSERT INTO genel (anahtar, deger) VALUES (?,?) "
                "ON CONFLICT(anahtar) DO UPDATE SET deger=excluded.deger",
                (anahtar, json.dumps(deger, ensure_ascii=False)))

    def sayim(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self._baglanti.execute(
            "SELECT durum, COUNT(*) FROM videolar GROUP BY durum").fetchall()}
