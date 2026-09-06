from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Optional

from facebook_business.adobjects.adset import AdSet
from facebook_business.adobjects.ad import Ad
from facebook_business.adobjects.adcreative import AdCreative

logger = logging.getLogger(__name__)


@dataclass
class CreativeDefaults:
    """Werte aus dem ersten Ad eines Referenz-Ad Sets."""
    bodies: list[str] = field(default_factory=list)
    headlines: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    cta: Optional[str] = None
    destination_url: Optional[str] = None
    display_link: Optional[str] = None
    image_hash: Optional[str] = None
    video_id: Optional[str] = None
    instagram_actor_id: Optional[str] = None
    url_tags: Optional[str] = None  # URL-Parameter (UTM-Tracking) — Top-Level-Feld am Creative


def _extract_from_object_story_spec(spec: dict) -> CreativeDefaults:
    d = CreativeDefaults()
    d.instagram_actor_id = spec.get("instagram_actor_id") or None

    link_data = spec.get("link_data", {})
    video_data = spec.get("video_data", {})

    if link_data:
        if link_data.get("message"):
            d.bodies = [link_data["message"]]
        if link_data.get("name"):
            d.headlines = [link_data["name"]]
        if link_data.get("description"):
            d.descriptions = [link_data["description"]]
        if link_data.get("link"):
            d.destination_url = link_data["link"]
        if link_data.get("caption"):
            d.display_link = link_data["caption"]
        if link_data.get("image_hash"):
            d.image_hash = link_data["image_hash"]
        cta = link_data.get("call_to_action", {})
        if cta.get("type"):
            d.cta = cta["type"]

    if video_data:
        if video_data.get("message"):
            d.bodies = [video_data["message"]]
        if video_data.get("title"):
            d.headlines = [video_data["title"]]
        if video_data.get("link_description"):
            d.descriptions = [video_data["link_description"]]
        if video_data.get("video_id"):
            d.video_id = video_data["video_id"]
        cta = video_data.get("call_to_action", {})
        if cta.get("type"):
            d.cta = cta["type"]
        if cta.get("value", {}).get("link"):
            d.destination_url = cta["value"]["link"]

    return d


def _extract_from_asset_feed_spec(feed: dict) -> CreativeDefaults:
    d = CreativeDefaults()
    d.bodies = [b["text"] for b in feed.get("bodies", []) if b.get("text")]
    d.headlines = [t["text"] for t in feed.get("titles", []) if t.get("text")]
    d.descriptions = [desc["text"] for desc in feed.get("descriptions", []) if desc.get("text")]

    cta_types = feed.get("call_to_action_types", [])
    if cta_types:
        d.cta = cta_types[0]

    link_urls = feed.get("link_urls", [])
    if link_urls:
        d.destination_url = link_urls[0].get("website_url")

    images = feed.get("images", [])
    if images:
        d.image_hash = images[0].get("hash")

    videos = feed.get("videos", [])
    if videos:
        d.video_id = videos[0].get("video_id")

    return d


def fetch_creative_defaults(source_adset_id: str) -> CreativeDefaults | None:
    """
    Liest Body, Headline, Description, CTA und URL aus dem ersten Ad
    des Referenz-Ad Sets aus und gibt sie als Defaults zurück.

    Nutzt v23.0 direkt via REST — in v25.0 ist der /ads-Edge auf
    AdSet-Objekten deprecated.
    """
    import requests
    import config

    token = config.META_ACCESS_TOKEN

    try:
        # 1. Ads via Account-Level mit Filter (in v25.0 ist /adset_id/ads deprecated)
        import json
        ads_resp = requests.get(
            f"https://graph.facebook.com/v23.0/{config.META_AD_ACCOUNT_ID}/ads",
            params={
                "access_token": token,
                "fields": "creative",
                "filtering": json.dumps([{
                    "field": "adset.id",
                    "operator": "IN",
                    "value": [str(source_adset_id)],
                }]),
                "limit": 1,
            },
            timeout=30,
        )
        ads_data = ads_resp.json()
        if "error" in ads_data:
            logger.warning("Konnte Ads von Ad Set %s nicht laden: %s",
                           source_adset_id, ads_data["error"].get("message"))
            return None

        ads_list = ads_data.get("data", [])
        if not ads_list:
            logger.warning("Referenz-Ad Set %s hat keine Ads — keine Defaults.", source_adset_id)
            return None

        creative_ref = ads_list[0].get("creative", {})
        creative_id = creative_ref.get("id") if isinstance(creative_ref, dict) else str(creative_ref)
        if not creative_id:
            logger.warning("Erstes Ad in %s hat keine creative-ID.", source_adset_id)
            return None

        # 2. Creative-Details via v23.0 REST
        cr_resp = requests.get(
            f"https://graph.facebook.com/v23.0/{creative_id}",
            params={
                "access_token": token,
                "fields": "object_story_spec,asset_feed_spec,url_tags",
            },
            timeout=30,
        )
        data = cr_resp.json()
        if "error" in data:
            logger.warning("Konnte Creative %s nicht laden: %s",
                           creative_id, data["error"].get("message"))
            return None

        if data.get("asset_feed_spec"):
            defaults = _extract_from_asset_feed_spec(data["asset_feed_spec"])
        elif data.get("object_story_spec"):
            defaults = _extract_from_object_story_spec(data["object_story_spec"])
        else:
            logger.warning("Creative %s hat kein erkennbares Format.", creative_id)
            return None

        # URL-Parameter (url_tags) liegen Top-Level am Creative — immer übernehmen,
        # damit das Tracking (UTM-Parameter) auf den neuen Ads erhalten bleibt.
        url_tags = data.get("url_tags")
        if url_tags:
            defaults.url_tags = url_tags

        logger.info(
            "Creative-Defaults aus Referenz-Ad Set %s: %d bodies, %d headlines, cta=%s, url_tags=%s",
            source_adset_id, len(defaults.bodies), len(defaults.headlines), defaults.cta,
            defaults.url_tags or "—",
        )
        return defaults

    except Exception as e:
        logger.warning("Konnte Creative-Defaults nicht laden (Ad Set %s): %s", source_adset_id, e)
        return None


def apply_defaults(row: "AdRow", defaults: CreativeDefaults) -> "AdRow":  # type: ignore[name-defined]
    """
    Füllt leere Felder in AdRow mit Werten aus dem Referenz-Creative auf.
    Sheet-Werte haben immer Vorrang — nur wirklich leere Felder werden ersetzt.
    """
    from copy import copy
    r = copy(row)

    if not r.bodies and defaults.bodies:
        r.bodies = defaults.bodies
        logger.info("  bodies aus Referenz: %s", r.bodies)
    if not r.headlines and defaults.headlines:
        r.headlines = defaults.headlines
        logger.info("  headlines aus Referenz: %s", r.headlines)
    if not r.descriptions and defaults.descriptions:
        r.descriptions = defaults.descriptions
    if not r.cta and defaults.cta:
        r.cta = defaults.cta
    if not r.destination_url and defaults.destination_url:
        r.destination_url = defaults.destination_url
    if not r.display_link and defaults.display_link:
        r.display_link = defaults.display_link
    if not r.instagram_actor_id and defaults.instagram_actor_id:
        r.instagram_actor_id = defaults.instagram_actor_id
    if not r.url_tags and defaults.url_tags:
        r.url_tags = defaults.url_tags
        logger.info("  url_tags aus Referenz: %s", r.url_tags)
    if not r.image_hash and not r.image_url and not r.video_id:
        if defaults.image_hash:
            r.image_hash = defaults.image_hash
        elif defaults.video_id:
            r.video_id = defaults.video_id

    return r
