"""Telefona bildirim (isteğe bağlı) ve sunucuda ALARM.txt.

DOKSAN_BILDIRIM_URL bir ntfy konusu olabilir (örn. https://ntfy.sh/gizli-konu-adi);
ntfy uygulamasında aynı konuya abone olunca bildirim telefona düşer.
"""
from __future__ import annotations

import requests
from loguru import logger


def gonder(url: str, baslik: str, mesaj: str, oncelik: str = "default") -> None:
    if not url:
        return
    try:
        # Türkçe karakterler başlıkta bozulmasın diye başlık sorgu parametresiyle gider.
        requests.post(url, data=mesaj.encode("utf-8"), timeout=15,
                      params={"title": baslik, "priority": oncelik, "tags": "tv"})
    except requests.RequestException as e:
        logger.warning(f"bildirim gönderilemedi: {e}")
