"""YouTube Data API v3 istemcisi: OAuth, kanal doğrulama, devam ettirilebilir
yükleme ve işlenme/görünürlük kontrolü. Yalnızca `requests` kullanır.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import secrets
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import requests

KAPSAM = "https://www.googleapis.com/auth/youtube"
CIHAZ_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
YETKI_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API = "https://www.googleapis.com/youtube/v3"
YUKLEME_API = "https://www.googleapis.com/upload/youtube/v3/videos"
GERI_DONUS = "http://127.0.0.1:8765/"
ZAMAN_ASIMI = (20, 180)


class YouTubeHatasi(Exception):
    pass


class YetkiHatasi(YouTubeHatasi):
    """Token geçersiz/iptal, yanlış kanal, izin yok: insan müdahalesi gerekir."""


class KotaHatasi(YouTubeHatasi):
    def __init__(self, mesaj: str, bekle_bitis: float):
        super().__init__(mesaj)
        self.bekle_bitis = bekle_bitis


class GeciciHata(YouTubeHatasi):
    """Ağ kesintisi, 5xx, hız sınırı: bekleyip yeniden denenir."""


class OturumGecersiz(YouTubeHatasi):
    """Yükleme oturumu sona ermiş (404/410): yeni oturum gerekir."""


class KaliciHata(YouTubeHatasi):
    def __init__(self, mesaj: str, neden: str = ""):
        super().__init__(mesaj)
        self.neden = neden


# ---------------- gizli dosyalar ----------------

def guvenli_yaz(yol: Path, veri: dict) -> None:
    yol.parent.mkdir(parents=True, exist_ok=True)
    fd, gecici = tempfile.mkstemp(dir=yol.parent, prefix=f".{yol.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(veri, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(gecici, yol)
    except BaseException:
        Path(gecici).unlink(missing_ok=True)
        raise


def istemci_oku(yol: Path) -> dict:
    if not yol.exists():
        raise YetkiHatasi(f"OAuth istemci dosyası yok: {yol}. Önce 'yetkilendir' komutunu çalıştır.")
    veri = json.loads(yol.read_text(encoding="utf-8"))
    ic = veri.get("installed") or veri.get("web") or veri
    if not ic.get("client_id") or not ic.get("client_secret"):
        raise YetkiHatasi("client_secret.json içinde client_id/client_secret yok")
    return {"client_id": ic["client_id"], "client_secret": ic["client_secret"]}


def sonraki_kota_sifirlama(simdi: float | None = None) -> float:
    """YouTube kotası Pasifik saatiyle gece yarısı sıfırlanır (+5 dk pay)."""
    pt = ZoneInfo("America/Los_Angeles")
    an = dt.datetime.fromtimestamp(simdi or time.time(), pt)
    yarin = (an + dt.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return yarin.timestamp()


def _hata_nedeni(yanit: requests.Response) -> tuple[str, str]:
    try:
        govde = yanit.json()
    except ValueError:
        return "", (yanit.text or "")[:300]
    hata = govde.get("error")
    if isinstance(hata, str):  # OAuth uç noktası
        return hata, govde.get("error_description", "")
    hata = hata or {}
    nedenler = hata.get("errors") or [{}]
    return nedenler[0].get("reason", "") or hata.get("status", ""), hata.get("message", "")


def yanit_hatasi(yanit: requests.Response) -> YouTubeHatasi:
    kod = yanit.status_code
    neden, mesaj = _hata_nedeni(yanit)
    metin = f"HTTP {kod} {neden}: {mesaj}".strip()
    if kod in (500, 502, 503, 504) or neden in {"backendError", "internalError"}:
        return GeciciHata(metin)
    if kod == 429 or neden in {"rateLimitExceeded", "userRateLimitExceeded"}:
        return GeciciHata(metin)
    if neden in {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"}:
        return KotaHatasi(metin, sonraki_kota_sifirlama())
    if kod == 401 or neden in {"authError", "invalid_grant", "unauthorized_client", "invalid_client",
                               "insufficientPermissions", "youtubeSignupRequired", "forbidden",
                               "channelClosed", "channelSuspended", "accountClosed"}:
        return YetkiHatasi(metin)
    if kod in (404, 410):
        return OturumGecersiz(metin)
    return KaliciHata(metin, neden)


# ---------------- istemci ----------------

class YouTube:
    def __init__(self, istemci_dosyasi: Path, token_dosyasi: Path,
                 oturum: requests.Session | None = None):
        self.istemci_dosyasi = Path(istemci_dosyasi)
        self.token_dosyasi = Path(token_dosyasi)
        self.http = oturum or requests.Session()

    # ---- OAuth ----
    def _token(self) -> dict:
        if not self.token_dosyasi.exists():
            raise YetkiHatasi("YouTube bağlantısı yok (token.json bulunamadı). 'yetkilendir' komutunu çalıştır.")
        return json.loads(self.token_dosyasi.read_text(encoding="utf-8"))

    def _token_kaydet(self, yanit: dict, eski: dict | None = None) -> dict:
        token = dict(eski or {})
        token["access_token"] = yanit["access_token"]
        token["expires_at"] = time.time() + int(yanit.get("expires_in", 3600)) - 60
        if yanit.get("refresh_token"):
            token["refresh_token"] = yanit["refresh_token"]
        if yanit.get("scope"):
            token["scope"] = yanit["scope"]
        guvenli_yaz(self.token_dosyasi, token)
        return token

    def erisim_tokeni(self, zorla_yenile: bool = False) -> str:
        token = self._token()
        if not zorla_yenile and token.get("access_token") and token.get("expires_at", 0) > time.time():
            return token["access_token"]
        if not token.get("refresh_token"):
            raise YetkiHatasi("Yenileme tokenı yok. 'yetkilendir' komutunu yeniden çalıştır.")
        ist = istemci_oku(self.istemci_dosyasi)
        try:
            y = self.http.post(TOKEN_URL, data={
                "client_id": ist["client_id"], "client_secret": ist["client_secret"],
                "refresh_token": token["refresh_token"], "grant_type": "refresh_token"},
                timeout=ZAMAN_ASIMI)
        except requests.RequestException as e:
            raise GeciciHata(f"Token yenilenemedi (ağ): {e}") from e
        if y.status_code != 200:
            hata = yanit_hatasi(y)
            if isinstance(hata, (KaliciHata, OturumGecersiz)) or y.status_code in (400, 401):
                raise YetkiHatasi(
                    f"YouTube yetkisi geçersiz veya iptal edilmiş ({hata}). "
                    "'yetkilendir' komutuyla yeniden bağlan.")
            raise hata
        return self._token_kaydet(y.json(), token)["access_token"]

    def cihaz_akisi(self, yaz: Callable[[str], None] = print) -> dict:
        """Telefondan yetkilendirme: google.com/device adresine kod girilir."""
        ist = istemci_oku(self.istemci_dosyasi)
        y = self.http.post(CIHAZ_URL, data={"client_id": ist["client_id"], "scope": KAPSAM},
                           timeout=ZAMAN_ASIMI)
        if y.status_code != 200:
            neden, mesaj = _hata_nedeni(y)
            raise YetkiHatasi(
                f"Cihaz kodu alınamadı ({neden}: {mesaj}). OAuth istemcisinin türü "
                "'TV ve Sınırlı Giriş Cihazları' olmalı; masaüstü istemcin varsa "
                "'--yontem tarayici' kullan.")
        v = y.json()
        yaz("")
        yaz("=" * 56)
        yaz(f"  1) Telefonda aç:  {v.get('verification_url') or v.get('verification_uri')}")
        yaz(f"  2) Bu kodu gir:   {v['user_code']}")
        yaz("  3) Doksan Artı kanalının bağlı olduğu Google hesabını seç,")
        yaz("     kanal listesi çıkarsa Doksan Artı kanalını seç ve izin ver.")
        yaz("=" * 56)
        yaz("Onay bekleniyor...")
        aralik = int(v.get("interval", 5))
        bitis = time.time() + int(v.get("expires_in", 1800))
        while time.time() < bitis:
            time.sleep(aralik)
            t = self.http.post(TOKEN_URL, data={
                "client_id": ist["client_id"], "client_secret": ist["client_secret"],
                "device_code": v["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, timeout=ZAMAN_ASIMI)
            if t.status_code == 200:
                return self._token_kaydet(t.json())
            neden, _ = _hata_nedeni(t)
            if neden == "authorization_pending":
                continue
            if neden == "slow_down":
                aralik += 5
                continue
            raise YetkiHatasi(f"Yetkilendirme tamamlanmadı: {neden}")
        raise YetkiHatasi("Kodun süresi doldu; 'yetkilendir' komutunu yeniden çalıştır.")

    def tarayici_akisi(self, oku: Callable[[str], str] = input,
                       yaz: Callable[[str], None] = print) -> dict:
        """Masaüstü istemcisiyle: izin sonrası açılamayan 127.0.0.1 adresi kopyalanıp yapıştırılır."""
        ist = istemci_oku(self.istemci_dosyasi)
        dogrulayici = secrets.token_urlsafe(64)
        meydan = base64.urlsafe_b64encode(hashlib.sha256(dogrulayici.encode()).digest()).rstrip(b"=").decode()
        durum = secrets.token_urlsafe(16)
        adres = YETKI_URL + "?" + urllib.parse.urlencode({
            "client_id": ist["client_id"], "redirect_uri": GERI_DONUS, "response_type": "code",
            "scope": KAPSAM, "access_type": "offline", "prompt": "consent select_account",
            "code_challenge": meydan, "code_challenge_method": "S256", "state": durum})
        yaz("\n1) Bu adresi telefonda aç ve Doksan Artı kanalını seçip izin ver:\n")
        yaz(adres)
        yaz("\n2) Sonunda 'siteye ulaşılamıyor' sayfası açılacak. Adres çubuğundaki")
        yaz("   http://127.0.0.1:8765/?... ile başlayan TÜM adresi kopyala ve buraya yapıştır.\n")
        donus = oku("Adres: ").strip()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(donus).query)
        if q.get("state", [""])[0] != durum:
            raise YetkiHatasi("Adres bu yetkilendirmeye ait değil (state uyuşmuyor).")
        if "code" not in q:
            raise YetkiHatasi(f"İzin verilmedi: {q.get('error', ['bilinmiyor'])[0]}")
        t = self.http.post(TOKEN_URL, data={
            "client_id": ist["client_id"], "client_secret": ist["client_secret"],
            "code": q["code"][0], "code_verifier": dogrulayici, "redirect_uri": GERI_DONUS,
            "grant_type": "authorization_code"}, timeout=ZAMAN_ASIMI)
        if t.status_code != 200:
            raise YetkiHatasi(f"Kod tokena çevrilemedi: {_hata_nedeni(t)}")
        return self._token_kaydet(t.json())

    # ---- API çağrıları ----
    def _istek(self, yontem: str, url: str, *, tekrar: bool = True, **kw) -> requests.Response:
        ozgun = dict(kw.pop("headers", {}) or {})
        basliklar = {**ozgun, "Authorization": f"Bearer {self.erisim_tokeni()}"}
        try:
            y = self.http.request(yontem, url, headers=basliklar, timeout=ZAMAN_ASIMI, **kw)
        except requests.RequestException as e:
            raise GeciciHata(f"Ağ hatası: {e}") from e
        if y.status_code == 401 and tekrar:
            self.erisim_tokeni(zorla_yenile=True)
            return self._istek(yontem, url, tekrar=False, headers=ozgun, **kw)
        return y

    def _json(self, yontem: str, url: str, **kw) -> dict:
        y = self._istek(yontem, url, **kw)
        if y.status_code >= 400:
            raise yanit_hatasi(y)
        return y.json() if y.content else {}

    def kanallarim(self) -> list[dict]:
        v = self._json("GET", f"{API}/channels", params={"part": "snippet,contentDetails", "mine": "true"})
        return [{"id": k["id"], "baslik": k["snippet"].get("title", ""),
                 "ozel_url": k["snippet"].get("customUrl", ""),
                 "yuklemeler": k.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")}
                for k in v.get("items", [])]

    def kanal_dogrula(self, beklenen_id: str) -> dict:
        kanallar = self.kanallarim()
        for k in kanallar:
            if k["id"] == beklenen_id:
                return k
        bulunan = ", ".join(f"{k['baslik']} ({k['id']})" for k in kanallar) or "kanal yok"
        raise YetkiHatasi(
            f"Token kayıtlı kanala ait değil. Beklenen {beklenen_id}, bulunan: {bulunan}. "
            "Yanlış hesapla yükleme yapılmadı; 'yetkilendir' ile doğru kanalı seç.")

    def isaretli_video_bul(self, yuklemeler_listesi: str, isaret: str, sayfa: int = 2) -> str | None:
        """Kanalın son yüklemelerinde gizli etiketle işaretli videoyu arar (kopya önleme)."""
        token = None
        for _ in range(sayfa):
            p = {"part": "contentDetails", "playlistId": yuklemeler_listesi, "maxResults": 50}
            if token:
                p["pageToken"] = token
            v = self._json("GET", f"{API}/playlistItems", params=p)
            idler = [i["contentDetails"]["videoId"] for i in v.get("items", [])]
            if idler:
                d = self._json("GET", f"{API}/videos", params={"part": "snippet", "id": ",".join(idler)})
                for vid in d.get("items", []):
                    if isaret in (vid.get("snippet", {}).get("tags") or []):
                        return vid["id"]
            token = v.get("nextPageToken")
            if not token:
                break
        return None

    # ---- devam ettirilebilir yükleme ----
    def oturum_ac(self, govde: dict, boyut: int) -> str:
        y = self._istek("POST", YUKLEME_API, params={"uploadType": "resumable", "part": "snippet,status"},
                        headers={"Content-Type": "application/json; charset=UTF-8",
                                 "X-Upload-Content-Length": str(boyut),
                                 "X-Upload-Content-Type": "video/mp4"},
                        data=json.dumps(govde, ensure_ascii=False).encode("utf-8"))
        if y.status_code >= 400:
            raise yanit_hatasi(y)
        konum = y.headers.get("Location")
        if not konum:
            raise GeciciHata("Yükleme oturumu adresi dönmedi")
        return konum

    def oturum_durumu(self, url: str, boyut: int) -> tuple[int, dict | None]:
        """(sunucudaki bayt, tamamlandıysa video kaynağı)"""
        y = self._istek("PUT", url, headers={"Content-Length": "0", "Content-Range": f"bytes */{boyut}"})
        return self._parca_yaniti(y)

    @staticmethod
    def _parca_yaniti(y: requests.Response) -> tuple[int, dict | None]:
        if y.status_code in (200, 201):
            return -1, y.json()
        if y.status_code == 308:
            aralik = y.headers.get("Range")
            return (int(aralik.split("-")[-1]) + 1 if aralik else 0), None
        raise yanit_hatasi(y)

    def parca_gonder(self, url: str, dosya: Path, baslangic: int, boyut: int,
                     parca: int) -> tuple[int, dict | None]:
        with open(dosya, "rb") as f:
            f.seek(baslangic)
            veri = f.read(parca)
        son = baslangic + len(veri) - 1
        y = self._istek("PUT", url, data=veri, headers={
            "Content-Length": str(len(veri)), "Content-Type": "video/mp4",
            "Content-Range": f"bytes {baslangic}-{son}/{boyut}"})
        return self._parca_yaniti(y)

    def video_durumu(self, video_id: str) -> dict | None:
        v = self._json("GET", f"{API}/videos", params={
            "part": "status,processingDetails,snippet", "id": video_id})
        items = v.get("items") or []
        return items[0] if items else None

    def gorunurluk_ayarla(self, video: dict, gorunurluk: str) -> dict:
        durum = dict(video.get("status") or {})
        durum["privacyStatus"] = gorunurluk
        izinli = {"privacyStatus", "embeddable", "license", "publicStatsViewable",
                  "selfDeclaredMadeForKids", "containsSyntheticMedia"}
        return self._json("PUT", f"{API}/videos", params={"part": "status"},
                          headers={"Content-Type": "application/json"},
                          data=json.dumps({"id": video["id"],
                                           "status": {k: v for k, v in durum.items() if k in izinli}}))
