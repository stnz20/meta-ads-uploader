"""
Frame.io-Share-Client: Assets hinter einem (anonymen) Share-Link auflisten
und in Original-Qualität streamen.

Bewusst ohne Abhängigkeit zu config/Meta-SDK — nur requests.

Die offizielle V4-REST-API kann Share-Inhalte nicht auflisten. Die Share-Viewer-
Web-App nutzt stattdessen die **GraphQL-API** (api.frame.io/graphql). Der Ablauf
(per Browser-DevTools verifiziert am 15.08.2026):
  1. GetShareCollectionAssets(shareId, page[, folderId]) → Asset-Node-IDs
  2. GetAssetsForViewer(assetIds)                        → Name, Größe, Original-URL
Auth pro Request:
  - authorization: Bearer <share-access-token>   (vom Nutzer aus DevTools; ~8 h gültig)
  - x-frameio-share-authentication: base64(shareId)
  - apollographql-client-name: web-app
Die Original-Download-URL ist eine signierte S3-URL (assets.frame.io) und lädt
OHNE Auth (~24 h gültig) — perfekt für den serverseitigen Download.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_GRAPHQL_URL = "https://api.frame.io/graphql"
_RETRY_HTTP_CODES = {408, 429, 500, 502, 503, 504}
_NETWORK_ERRORS = (requests.ConnectionError, requests.Timeout)
_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
_TIMEOUT = 60
_DOWNLOAD_TIMEOUT = 300
_PAGE_SIZE = 200
_ASSET_BATCH = 50


class FrameioShareError(Exception):
    """Fehler mit deutscher, direkt anzeigbarer Meldung für die UI."""


@dataclass
class ShareRef:
    kind: str  # "v4" (next.frame.io/share/…)
    share_id: str
    url: str


@dataclass
class FrameioAsset:
    id: str
    name: str
    size_bytes: int = 0
    media_type: str = "other"  # "video" | "image" | "other"
    path: str = ""  # Ordner-Breadcrumb für die Anzeige
    download_url: str = ""  # signierte S3-URL — ~24 h gültig
    raw: dict = field(default_factory=dict)


# ──────────────────────────── URL-Parsing ────────────────────────────

_V4_RE = re.compile(r"next\.frame\.io/share/([0-9a-fA-F-]{36})")


def parse_share_url(url: str) -> ShareRef:
    """Erkennt V4-Shares (next.frame.io) und f.io-Shortlinks."""
    url = (url or "").strip()
    if not url:
        raise FrameioShareError("Bitte einen Frame.io-Link einfügen.")

    if "f.io/" in url:
        url = _follow_short_link(url)

    m = _V4_RE.search(url)
    if m:
        return ShareRef(kind="v4", share_id=m.group(1).lower(), url=url)
    raise FrameioShareError(
        "Link nicht erkannt — erwartet wird ein Frame.io-Share-Link wie "
        "https://next.frame.io/share/…"
    )


def _follow_short_link(url: str) -> str:
    """f.io-Shortlinks auflösen (Redirect folgen, nur Header laden)."""
    try:
        resp = _retry(
            lambda: requests.head(url, allow_redirects=True, timeout=_TIMEOUT,
                                  headers={"User-Agent": _UA}),
            label="shortlink",
        )
        return resp.url
    except Exception as e:
        raise FrameioShareError(f"Shortlink konnte nicht aufgelöst werden: {e}") from e


# ──────────────────────────── Retry (Muster aus meta/assets.py) ────────────────────────────

def _retry(fn, retries: int = 4, delay: int = 5, label: str = ""):
    """Wiederholt fn() bei Netzwerkfehlern/retryable HTTP-Status mit Backoff."""
    for attempt in range(retries):
        try:
            result = fn()
        except _NETWORK_ERRORS as e:
            if attempt < retries - 1:
                wait = delay * (2 ** attempt)
                logger.warning("Frame.io Netzwerkfehler%s: %s — warte %ds (%d/%d)",
                               f" ({label})" if label else "", e, wait, attempt + 1, retries)
                time.sleep(wait)
                continue
            raise
        if isinstance(result, requests.Response) and result.status_code in _RETRY_HTTP_CODES:
            if attempt < retries - 1:
                wait = delay * (2 ** attempt)
                logger.warning("Frame.io HTTP %d%s — warte %ds (%d/%d)",
                               result.status_code, f" ({label})" if label else "",
                               wait, attempt + 1, retries)
                time.sleep(wait)
                continue
        return result


# ──────────────────────────── GraphQL ────────────────────────────

def _share_auth_header(share_id: str) -> str:
    """Der x-frameio-share-authentication-Header ist base64(shareId)."""
    return base64.b64encode(share_id.encode()).decode()


def _gql(share_id: str, token: str, operation: str, query: str, variables: dict) -> dict:
    """Ein GraphQL-Call gegen die Share-API. Wirft FrameioShareError bei Fehlern."""
    headers = {
        "User-Agent": _UA,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://next.frame.io",
        "Referer": "https://next.frame.io/",
        "apollographql-client-name": "web-app",
        "authorization": f"Bearer {token}",
        "x-frameio-share-authentication": _share_auth_header(share_id),
        "x-gql-op": operation,
    }
    payload = {"operationName": operation, "variables": variables, "query": query}
    resp = _retry(
        lambda: requests.post(_GRAPHQL_URL, json=payload, headers=headers, timeout=_TIMEOUT),
        label=operation,
    )
    if resp.status_code == 503:
        raise FrameioShareError(
            "Frame.io ist gerade nicht erreichbar (Wartungsmodus / 503). "
            "Bitte später erneut versuchen."
        )
    try:
        data = resp.json()
    except ValueError:
        raise FrameioShareError(f"Unerwartete Antwort von Frame.io (HTTP {resp.status_code}).")

    if data.get("errors"):
        msg = "; ".join(e.get("message", "?") for e in data["errors"])
        code = data["errors"][0].get("extensions", {}).get("code", "")
        if code == "PERMISSION_DENIED":
            raise FrameioShareError(
                "Zugriff verweigert — der Frame.io-Zugangs-Token ist abgelaufen oder "
                "gehört zu einem anderen Share. Bitte den Token neu aus dem Browser holen."
            )
        raise FrameioShareError(f"Frame.io-Fehler: {msg}")
    return data.get("data") or {}


_Q_COLLECTION = (
    "query GetShareCollectionAssets($shareId: ID!, $folderId: ID, "
    "$assetType: ChildAssetTypeInput, $page: PageInput!) { "
    "share(shareId: $shareId) { id ... on Share { "
    "collectionAssets(page: $page, assetType: $assetType, folderId: $folderId) { "
    "nodes { id index } totalCount pageInfo { hasNextPage } } } } }"
)

_Q_ASSETS = (
    "query GetAssetsForViewer($assetIds: [ID!]!) { assets(assetIds: $assetIds) { "
    "id name __typename "
    "... on FolderAsset { itemCount } "
    "... on VideoAsset { media { filesize original { downloadUrl filesizeInBytes } } } "
    "... on ImageAsset { media { filesize original { downloadUrl filesizeInBytes } } } "
    "... on AudioAsset { media { filesize original { downloadUrl filesizeInBytes } } } "
    "... on DocumentAsset { media { original { downloadUrl filesizeInBytes } } } } }"
)


def _classify(typename: str, name: str) -> str:
    t = (typename or "").lower()
    if "video" in t:
        return "video"
    if "image" in t:
        return "image"
    n = name.lower()
    if n.endswith((".mp4", ".mov", ".m4v", ".webm")):
        return "video"
    if n.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
        return "image"
    return "other"


def list_share_assets(ref: ShareRef, token: str, passcode: Optional[str] = None) -> list[FrameioAsset]:
    """Alle Dateien im Share, Ordner rekursiv flach.

    `token` = der Bearer-Share-Access-Token aus dem Browser (DevTools).
    """
    token = (token or "").strip()
    if not token:
        raise FrameioShareError(
            "Frame.io-Zugangs-Token fehlt — bitte aus dem Browser kopieren "
            "(siehe Anleitung auf der Seite)."
        )
    assets: list[FrameioAsset] = []
    _collect_folder(ref.share_id, token, folder_id=None, path="", out=assets)
    return assets


def _list_node_ids(share_id: str, token: str, folder_id: Optional[str],
                   asset_type: Optional[str]) -> list[str]:
    """Node-IDs einer Ebene (paginiert). asset_type=None → Dateien, "FOLDER" → Ordner."""
    ids: list[str] = []
    offset = 0
    while True:
        variables = {
            "shareId": share_id,
            "folderId": folder_id,
            "page": {"first": _PAGE_SIZE, "mode": "OFFSET", "afterOffset": offset},
        }
        if asset_type:
            variables["assetType"] = asset_type
        data = _gql(share_id, token, "GetShareCollectionAssets", _Q_COLLECTION, variables)
        coll = ((data.get("share") or {}).get("collectionAssets") or {})
        nodes = coll.get("nodes") or []
        ids.extend(n["id"] for n in nodes if n.get("id"))
        if not coll.get("pageInfo", {}).get("hasNextPage"):
            break
        offset += len(nodes) or _PAGE_SIZE
    return ids


def _collect_folder(share_id: str, token: str, folder_id: Optional[str],
                    path: str, out: list[FrameioAsset]) -> None:
    # Dateien und Ordner werden getrennt abgefragt: ohne assetType liefert die
    # API nur Dateien, mit assetType="FOLDER" nur (Unter-)Ordner.
    file_ids = _list_node_ids(share_id, token, folder_id, asset_type=None)
    folder_ids = _list_node_ids(share_id, token, folder_id, asset_type="FOLDER")

    # Dateien in Batches auflösen (Name, Größe, signierte Original-URL)
    for i in range(0, len(file_ids), _ASSET_BATCH):
        batch = file_ids[i:i + _ASSET_BATCH]
        data = _gql(share_id, token, "GetAssetsForViewer", _Q_ASSETS, {"assetIds": batch})
        for asset in data.get("assets") or []:
            typename = asset.get("__typename", "")
            name = asset.get("name", "")
            if typename == "FolderAsset":
                # Sollte hier nicht auftauchen (nur Dateien angefragt) — sicherheitshalber
                folder_ids.append(asset["id"])
                continue
            media = asset.get("media") or {}
            original = media.get("original") or {}
            out.append(FrameioAsset(
                id=asset.get("id", ""),
                name=name,
                size_bytes=int(original.get("filesizeInBytes")
                               or media.get("filesize") or 0),
                media_type=_classify(typename, name),
                path=path.rstrip("/"),
                download_url=original.get("downloadUrl") or "",
                raw=asset,
            ))

    # Unterordner: Namen für den Breadcrumb holen, dann rekursiv absteigen
    names: dict[str, str] = {}
    for i in range(0, len(folder_ids), _ASSET_BATCH):
        batch = folder_ids[i:i + _ASSET_BATCH]
        data = _gql(share_id, token, "GetAssetsForViewer", _Q_ASSETS, {"assetIds": batch})
        for asset in data.get("assets") or []:
            names[asset.get("id", "")] = asset.get("name", "")
    for fid in folder_ids:
        sub = names.get(fid, "")
        _collect_folder(share_id, token, fid, path=f"{path}{sub}/", out=out)


def resolve_download_url(ref: ShareRef, asset: FrameioAsset, token: str,
                         passcode: Optional[str] = None) -> str:
    """Signierte Download-URL — bei Bedarf frisch auflösen (S3-URLs laufen ab)."""
    if asset.download_url:
        return asset.download_url
    data = _gql(ref.share_id, token, "GetAssetsForViewer", _Q_ASSETS, {"assetIds": [asset.id]})
    for a in data.get("assets") or []:
        url = ((a.get("media") or {}).get("original") or {}).get("downloadUrl")
        if a.get("id") == asset.id and url:
            return url
    raise FrameioShareError(
        f"Keine Download-URL für „{asset.name}“ — ist der Download im Share erlaubt?"
    )


# ──────────────────────────── Download ────────────────────────────

def download_asset(url: str, dest_path: str,
                   progress_cb: Optional[Callable[[int, int], None]] = None) -> str:
    """Streamt eine Datei nach dest_path (8-MB-Chunks, .part + atomic replace).

    Die signierte S3-URL braucht KEINE Auth-Header.
    """
    part_path = dest_path + ".part"
    resp = _retry(
        lambda: requests.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT,
                             headers={"User-Agent": _UA}),
        label="download",
    )
    if resp.status_code >= 400:
        raise FrameioShareError(
            f"Download fehlgeschlagen (HTTP {resp.status_code}) — signierte URL evtl. abgelaufen."
        )
    total = int(resp.headers.get("Content-Length") or 0)
    done = 0
    try:
        with open(part_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if not chunk:
                    continue
                fh.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)
        os.replace(part_path, dest_path)
    finally:
        if os.path.exists(part_path):
            os.remove(part_path)
    return dest_path
