"""Holt Referenz-Daten (campaign_id, page_id, instagram_actor_id, cta) für ein
BELIEBIGES bestehendes Meta-Ad-Set — auch wenn es nicht im Sheet steht.

Damit lässt sich in Streamlit ein Ad Set aus einer beliebigen Meta-Kampagne als
Referenz wählen. Die Targeting-/Bid-Felder werden weiterhin erst beim Upload via
meta.adset._get_source_adset gelesen — hier geht es nur um die Werte, die schon
beim Schreiben der ad_sets/ads-Zeilen feststehen müssen (v.a. die campaign_id).

Nutzt v23.0 direkt via REST — in v25.0 ist der /ads-Edge auf AdSet-Objekten
deprecated, deshalb gehen wir wie in creative_defaults über den Account-Level.
"""
from __future__ import annotations

import json
import logging

import requests

import config

logger = logging.getLogger(__name__)


def _empty() -> dict:
    return {"campId": "", "cta": "", "srcId": "", "pageId": "", "igId": ""}


# ──────────────────────────────────────────────────────────────────────────────
# Attribution-Check (Testing-Ad-Sets müssen 7-Tage-Klick haben)
# ──────────────────────────────────────────────────────────────────────────────

def _attribution_label(spec: list[dict]) -> str:
    """Wandelt ein attribution_spec in ein kompaktes, lesbares Label um.

    Beispiel: [{"event_type":"CLICK_THROUGH","window_days":7}] → '7d-click'
              [..CLICK_THROUGH 7.., ..VIEW_THROUGH 1..]        → '7d-click + 1d-view'
    """
    if not spec:
        return ""  # nicht gesetzt / nicht ermittelbar
    names = {
        "CLICK_THROUGH": "click",
        "VIEW_THROUGH": "view",
        "ENGAGED_VIDEO_VIEW": "video",
    }
    parts = []
    for entry in spec:
        et = str(entry.get("event_type", ""))
        wd = entry.get("window_days", "")
        parts.append(f"{wd}d-{names.get(et, et.lower())}")
    return " + ".join(parts)


def _is_7day_click(spec: list[dict]) -> bool:
    """True nur wenn das attribution_spec GENAU 7-Tage-Klick ist (ein einziger
    CLICK_THROUGH-Eintrag mit window_days=7, kein View-Through)."""
    if not spec or len(spec) != 1:
        return False
    e = spec[0]
    try:
        return e.get("event_type") == "CLICK_THROUGH" and int(e.get("window_days", 0)) == 7
    except (TypeError, ValueError):
        return False


def fetch_attribution_status(adset_id: str) -> dict:
    """Liest das attribution_spec eines Ad Sets und klassifiziert es.

    Returns: {
        "ok": bool,             # konnte gelesen werden
        "is_7day_click": bool,  # genau 7-Tage-Klick?
        "label": str,           # lesbares Label, z.B. '7d-click' / '7d-click + 1d-view'
        "adset_id": str,
    }
    """
    adset_id = str(adset_id or "").strip()
    if not adset_id:
        return {"ok": False, "is_7day_click": False, "label": "", "adset_id": ""}

    try:
        resp = requests.get(
            f"https://graph.facebook.com/v23.0/{adset_id}",
            params={"access_token": config.META_ACCESS_TOKEN,
                    "fields": "attribution_spec,name"},
            timeout=30,
        )
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logger.warning("Attribution für Ad Set %s nicht ladbar: %s", adset_id, e)
        return {"ok": False, "is_7day_click": False, "label": "", "adset_id": adset_id}

    if "error" in data:
        logger.warning("Attribution für Ad Set %s nicht lesbar: %s",
                       adset_id, data["error"].get("message"))
        return {"ok": False, "is_7day_click": False, "label": "", "adset_id": adset_id}

    spec = data.get("attribution_spec") or []
    return {
        "ok": True,
        "is_7day_click": _is_7day_click(spec),
        "label": _attribution_label(spec),
        "adset_id": adset_id,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Performance: Käufe je Ad-Set (für "best-performende Referenz je Produkt")
# ──────────────────────────────────────────────────────────────────────────────

# Reihenfolge = Präferenz beim Auslesen des Purchase-Werts aus `actions`.
_PURCHASE_ACTION_TYPES = (
    "purchase",
    "offsite_conversion.fb_pixel_purchase",
    "omni_purchase",
)


def _extract_purchases(actions: list[dict]) -> int:
    """Zieht die Anzahl Käufe aus dem Insights-`actions`-Array (best matching type)."""
    by_type = {str(a.get("action_type", "")): a.get("value", 0) for a in (actions or [])}
    for at in _PURCHASE_ACTION_TYPES:
        if at in by_type:
            try:
                return int(float(by_type[at]))
            except (TypeError, ValueError):
                return 0
    return 0


def fetch_adset_purchases(campaign_id: str, *, date_preset: str = "last_7d") -> dict[str, int]:
    """Käufe je Ad-Set innerhalb einer Kampagne im Zeitfenster (Meta Insights).

    Returns {adset_id: purchases}. Leeres Dict bei Fehler oder keinen Daten — der
    Aufrufer fällt dann auf die bisherige Vorschlagslogik zurück.
    date_preset z. B. 'last_7d' | 'last_14d' | 'last_30d' | 'maximum'.
    """
    campaign_id = str(campaign_id or "").strip()
    if not campaign_id:
        return {}
    out: dict[str, int] = {}
    url = f"https://graph.facebook.com/v23.0/{campaign_id}/insights"
    params: dict = {
        "access_token": config.META_ACCESS_TOKEN,
        "level": "adset",
        "date_preset": date_preset,
        "fields": "adset_id,adset_name,actions",
        "limit": 200,
    }
    try:
        while True:
            resp = requests.get(url, params=params, timeout=60)
            data = resp.json()
            if "error" in data:
                logger.warning("Insights für Kampagne %s nicht lesbar: %s",
                               campaign_id, data["error"].get("message"))
                return out
            for row in data.get("data", []):
                aid = str(row.get("adset_id", "")).strip()
                if aid:
                    out[aid] = _extract_purchases(row.get("actions"))
            nxt = (data.get("paging", {}) or {}).get("next")
            if not nxt:
                break
            url, params = nxt, {}  # 'next' enthält bereits alle Query-Parameter
    except requests.exceptions.RequestException as e:
        logger.warning("Insights für Kampagne %s nicht ladbar: %s", campaign_id, e)
    return out


def _fetch_campaign_id(adset_id: str, token: str) -> str:
    resp = requests.get(
        f"https://graph.facebook.com/v23.0/{adset_id}",
        params={"access_token": token, "fields": "campaign_id,name"},
        timeout=30,
    )
    data = resp.json()
    if "error" in data:
        logger.warning("Referenz-Ad-Set %s nicht lesbar: %s",
                       adset_id, data["error"].get("message"))
        return ""
    return str(data.get("campaign_id", "")).strip()


def _fetch_creative_meta(adset_id: str, token: str) -> dict:
    """Liefert {pageId, igId, cta} aus dem ersten Ad des Ad Sets (best effort)."""
    out = {"pageId": "", "igId": "", "cta": ""}
    try:
        ads_resp = requests.get(
            f"https://graph.facebook.com/v23.0/{config.META_AD_ACCOUNT_ID}/ads",
            params={
                "access_token": token,
                "fields": "creative",
                "filtering": json.dumps([{
                    "field": "adset.id",
                    "operator": "IN",
                    "value": [str(adset_id)],
                }]),
                "limit": 1,
            },
            timeout=30,
        )
        ads_data = ads_resp.json()
        ads_list = ads_data.get("data", []) if "error" not in ads_data else []
        if not ads_list:
            return out

        creative_ref = ads_list[0].get("creative", {})
        creative_id = creative_ref.get("id") if isinstance(creative_ref, dict) else str(creative_ref)
        if not creative_id:
            return out

        cr_resp = requests.get(
            f"https://graph.facebook.com/v23.0/{creative_id}",
            params={
                "access_token": token,
                # object_story_spec/asset_feed_spec sind bei bereits veröffentlichten
                # Creatives oft leer — dann liefern die Top-Level-Felder die Werte.
                "fields": ("object_story_spec,asset_feed_spec,call_to_action_type,"
                           "object_story_id,effective_object_story_id,instagram_actor_id"),
            },
            timeout=30,
        )
        data = cr_resp.json()
        if "error" in data:
            return out

        spec = data.get("object_story_spec", {}) or {}
        feed = data.get("asset_feed_spec", {}) or {}

        # Page ID: object_story_spec → sonst Präfix aus object_story_id ({page_id}_{story_id})
        out["pageId"] = str(spec.get("page_id", "") or "").strip()
        if not out["pageId"]:
            story_id = str(data.get("object_story_id") or data.get("effective_object_story_id") or "")
            if "_" in story_id:
                out["pageId"] = story_id.split("_", 1)[0].strip()

        # Instagram Actor ID: object_story_spec → sonst Top-Level
        out["igId"] = str(spec.get("instagram_actor_id", "") or data.get("instagram_actor_id", "") or "").strip()

        # CTA: object_story_spec (link_data/video_data) → asset_feed_spec → Top-Level
        link_data = spec.get("link_data", {}) or {}
        video_data = spec.get("video_data", {}) or {}
        cta = (link_data.get("call_to_action") or video_data.get("call_to_action") or {})
        out["cta"] = str(cta.get("type", "") or "").strip()
        if not out["cta"]:
            cta_types = feed.get("call_to_action_types", [])
            if cta_types:
                out["cta"] = str(cta_types[0] or "").strip()
        if not out["cta"]:
            out["cta"] = str(data.get("call_to_action_type", "") or "").strip()
    except Exception as e:
        logger.warning("Creative-Meta für Ad Set %s nicht ladbar: %s", adset_id, e)
    return out


def fetch_adset_reference(adset_id: str) -> dict:
    """Holt Referenz-Werte für ein beliebiges Meta-Ad-Set direkt von der Graph-API.

    Returns: {"campId", "cta", "srcId", "pageId", "igId"} — alle Felder als String.
    campId ist leer, wenn das Ad Set nicht lesbar ist (dann kann es nicht als
    Referenz dienen). pageId/igId/cta sind best effort und werden ggf. beim Upload
    über creative_defaults nachgefüllt.
    """
    adset_id = str(adset_id or "").strip()
    if not adset_id:
        return _empty()

    token = config.META_ACCESS_TOKEN
    camp_id = _fetch_campaign_id(adset_id, token)
    if not camp_id:
        return {**_empty(), "srcId": adset_id}

    creative_meta = _fetch_creative_meta(adset_id, token)
    logger.info(
        "Referenz von Meta geladen — Ad Set %s → Kampagne %s (page=%s, ig=%s, cta=%s)",
        adset_id, camp_id, creative_meta["pageId"] or "—",
        creative_meta["igId"] or "—", creative_meta["cta"] or "—",
    )
    return {
        "campId": camp_id,
        "srcId": adset_id,
        "pageId": creative_meta["pageId"],
        "igId": creative_meta["igId"],
        "cta": creative_meta["cta"],
    }
