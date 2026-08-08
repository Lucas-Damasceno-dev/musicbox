"""Busca de letras sincronizadas (LRC) na LRCLIB, via stdlib apenas.

`fetch_lrc` consulta a API pública `https://lrclib.net/api/search` (sem chave)
com `urllib` e devolve o LRC com timestamps quando existe `syncedLyrics`, ou o
texto puro como LRC estático quando só há `plainLyrics`. NUNCA levanta: qualquer
erro (rede/timeout/HTTP != 200/JSON inválido/sem match) retorna `None` — a letra
é um extra do download e falha/ausência não podem bloqueá-lo.

Identificadores em inglês; docstrings/comentários em português.
"""

import json
import logging
import urllib.parse
import urllib.request

logger = logging.getLogger("musicbox.lyrics")

# API pública da LRCLIB (busca por artista/faixa/álbum), sem chave.
LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"
_TIMEOUT = 5  # segundos — a busca de letra não pode travar o download


def fetch_lrc(artist: str, title: str, album: str | None = None) -> str | None:
    """Busca a letra de `artist`/`title` na LRCLIB e devolve o conteúdo `.lrc`.

    Escolha: 1º hit com `syncedLyrics` (LRC com timestamps) → devolvido como está;
    senão o 1º hit com `plainLyrics` → devolvido como LRC estático (texto puro);
    nenhum match → `None`. Erros de rede/timeout/HTTP != 200/JSON inválido também
    devolvem `None` (log em debug) — a função nunca levanta.
    """
    params: dict[str, str] = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    url = f"{LRCLIB_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # rede, timeout, HTTP, JSON inválido — nunca levanta
        logger.debug("fetch_lrc(%r, %r): %s", artist, title, exc)
        return None
    if not isinstance(data, list):
        return None
    for hit in data:  # 1º hit com timestamps
        if isinstance(hit, dict) and hit.get("syncedLyrics"):
            return str(hit["syncedLyrics"])
    for hit in data:  # senão 1º com texto puro
        if isinstance(hit, dict) and hit.get("plainLyrics"):
            return str(hit["plainLyrics"])
    return None
