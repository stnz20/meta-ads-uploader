from __future__ import annotations
import logging
import time
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.adcreative import AdCreative
from facebook_business.adobjects.ad import Ad
from facebook_business.adobjects.adset import AdSet
from facebook_business.exceptions import FacebookRequestError
import config
from sheets.reader import AdRow
from meta.assets import is_gdrive_url, download_from_gdrive, upload_asset_to_meta, get_or_upload_gdrive_asset, find_complementary_static_url, UploadedAsset, _upload_image

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Asset vorbereiten
# Google Drive URL → herunterladen → zu Meta hochladen → image_hash
# ---------------------------------------------------------------------------

def _resolve_asset(row: AdRow, dry_run: bool) -> UploadedAsset:
    """
    Löst das Asset auf:
    - image_hash direkt gesetzt → UploadedAsset(image_hash=...)
    - video_id direkt gesetzt   → UploadedAsset(video_id=...)
    - Google Drive URL          → herunterladen + hochladen (Bild oder Video)
    - image_url (direkte URL)   → kein Upload nötig, wird direkt in link_data genutzt
    """
    if row.image_hash:
        return UploadedAsset(image_hash=row.image_hash)

    if row.video_id:
        return UploadedAsset(video_id=row.video_id)

    if row.image_url and is_gdrive_url(row.image_url):
        if dry_run:
            logger.info("[DRY-RUN] Würde Asset von Google Drive hochladen: %s", row.image_url)
            # Thumbnail ebenfalls simulieren
            thumb_hash = "DRY_RUN_THUMB_HASH" if row.thumbnail_url else None
            hash_2 = "DRY_RUN_IMAGE_HASH_2" if row.image_url_2 else None
            return UploadedAsset(image_hash="DRY_RUN_IMAGE_HASH", thumbnail_hash=thumb_hash,
                                 image_hash_2=hash_2)
        logger.info("Lade Asset von Google Drive: %s", row.image_url)
        asset = get_or_upload_gdrive_asset(row.image_url)

        # Thumbnail hochladen falls vorhanden (nur bei Videos nötig)
        if asset.is_video and row.thumbnail_url and not asset.thumbnail_hash:
            logger.info("Lade Thumbnail hoch: %s", row.thumbnail_url)
            if is_gdrive_url(row.thumbnail_url):
                thumb_bytes, thumb_name, _ = download_from_gdrive(row.thumbnail_url)
                asset.thumbnail_hash = _upload_image(thumb_bytes, thumb_name)

        # Zweites Bild für Static Ads (Placement-Customization: 4:5 + 16:9)
        if row.image_url_2 and not asset.is_video:
            logger.info("Lade zweites Bild (Placement-Customization) hoch: %s", row.image_url_2)
            if is_gdrive_url(row.image_url_2):
                asset2 = get_or_upload_gdrive_asset(row.image_url_2)
                asset.image_hash_2 = asset2.image_hash
                asset.aspect_ratio_2 = asset2.aspect_ratio
            else:
                logger.warning("image_url_2 ist keine Google Drive URL — wird ignoriert: %s", row.image_url_2)
        elif (
            not asset.is_video
            and not asset.image_hash_2
            and asset.image_hash
            and row.image_url
            and is_gdrive_url(row.image_url)
        ):
            # Auto-Detect: zweites Format (4x5 ↔ 16x9) im selben Drive-Ordner suchen
            auto_url2 = find_complementary_static_url(row.image_url)
            if auto_url2:
                logger.info("Lade auto-erkanntes zweites Format hoch: %s", auto_url2)
                asset2 = get_or_upload_gdrive_asset(auto_url2)
                asset.image_hash_2 = asset2.image_hash
                asset.aspect_ratio_2 = asset2.aspect_ratio

        return asset

    return UploadedAsset()  # direktes image_url → kein Upload


def _resolve_assets(row: AdRow, dry_run: bool) -> list[UploadedAsset]:
    """3-2-2 DCO: alle row.asset_urls auflösen und als Liste zurückgeben.

    Sequenziell, damit Drive-Rate-Limits eingehalten werden und Logs lesbar bleiben.
    """
    assets: list[UploadedAsset] = []
    for idx, url in enumerate(row.asset_urls, start=1):
        if dry_run:
            logger.info("[DRY-RUN] Würde DCO-Asset %d/%d laden: %s", idx, len(row.asset_urls), url)
            assets.append(UploadedAsset(video_id=f"DRY_RUN_VIDEO_{idx}", thumbnail_hash=f"DRY_RUN_THUMB_{idx}"))
            continue
        if not is_gdrive_url(url):
            raise ValueError(
                f"3-2-2 Asset {idx} ist keine Google-Drive-URL: {url} — "
                "nur Drive-Links werden im DCO-Modus unterstützt."
            )
        logger.info("Lade DCO-Asset %d/%d von Drive: %s", idx, len(row.asset_urls), url)
        assets.append(get_or_upload_gdrive_asset(url))
    return assets


# ---------------------------------------------------------------------------
# Creative-Erstellung: zwei Pfade
#   1. Einzel-Variation  → object_story_spec  (einfach, image_url möglich)
#   2. Multi-Variation   → asset_feed_spec    (braucht image_hash oder video_id)
# ---------------------------------------------------------------------------

def _build_single_spec(row: AdRow, page_id: str, asset: UploadedAsset,
                       allow_placement_customization: bool = True) -> dict:
    """object_story_spec für genau eine Text-Variation.

    Bei zwei Static-Formaten wird stattdessen ein asset_feed_spec mit
    Placement-Customization gebaut — außer allow_placement_customization ist
    False (Fallback, wenn Meta die Rules für das Ziel-Ad-Set ablehnt).
    """
    if not row.bodies:
        raise ValueError(
            f"Ad '{row.ad_name}': Kein Body-Text vorhanden — bitte im Sheet eintragen "
            "oder ein gültiges Referenz-Ad Set mit body-Text setzen."
        )
    if not row.headlines:
        raise ValueError(
            f"Ad '{row.ad_name}': Kein Headline-Text vorhanden — bitte im Sheet eintragen "
            "oder ein gültiges Referenz-Ad Set mit headline-Text setzen."
        )
    body = row.bodies[0]
    headline = row.headlines[0]

    # object_story_spec Basis — mit optionalem Instagram-Konto
    story_spec_base: dict = {"page_id": page_id}
    if row.instagram_actor_id:
        story_spec_base["instagram_actor_id"] = row.instagram_actor_id

    if asset.is_video:
        video_data: dict = {
            "video_id": asset.video_id,
            "message": body,
            "title": headline,
            "call_to_action": {
                "type": row.cta,
                "value": {"link": row.destination_url},
            },
        }
        if row.descriptions:
            video_data["link_description"] = row.descriptions[0]
        if asset.thumbnail_hash:
            video_data["image_hash"] = asset.thumbnail_hash
        elif row.thumbnail_url and not is_gdrive_url(row.thumbnail_url):
            video_data["image_url"] = row.thumbnail_url
        return {"object_story_spec": {**story_spec_base, "video_data": video_data}}

    link_data: dict = {
        "message": body,
        "link": row.destination_url,
        "name": headline,
        "call_to_action": {"type": row.cta, "value": {"link": row.destination_url}},
    }
    if row.descriptions:
        link_data["description"] = row.descriptions[0]
    if row.display_link:
        link_data["caption"] = row.display_link
    if asset.image_hash:
        link_data["image_hash"] = asset.image_hash
    elif row.image_url:
        link_data["picture"] = row.image_url

    # Static Ads mit zwei Formaten → Placement-Customization über asset_feed_spec.
    # Muss VOR dem object_story_spec-Ergebnis geprüft werden: Meta kennt auf
    # AdCreative kein Top-Level-Feld "asset_customization_rules" und verwarf es
    # früher stillschweigend — die Ads liefen dann mit nur einem Format aus.
    if allow_placement_customization:
        placement_spec = _build_placement_customized_spec(row, story_spec_base, asset)
        if placement_spec:
            return placement_spec

    return {"object_story_spec": {**story_spec_base, "link_data": link_data}}


# Placements für das breitere Bild (Feed & Co.) bzw. das hochformatige Bild.
# Alles Vertikale gehört zu _TALL, der Rest zu _WIDE — überschneidungsfrei und
# zusammen vollständig: Meta lehnt asset_customization_rules ab, wenn Rules sich
# überlappen oder ein im Ad Set aktives Placement in keiner Rule vorkommt.
_WIDE_PLACEMENTS = {
    "publisher_platforms": ["facebook", "instagram", "audience_network", "messenger"],
    "facebook_positions": ["feed", "video_feeds", "marketplace", "search",
                           "instream_video", "right_hand_column", "profile_feed",
                           "biz_disco_feed"],
    "instagram_positions": ["stream", "explore", "explore_home", "profile_feed",
                            "ig_search", "shop"],
    "audience_network_positions": ["classic", "rewarded_video"],
    "messenger_positions": ["messenger_home"],
}
_TALL_PLACEMENTS = {
    "publisher_platforms": ["facebook", "instagram", "messenger"],
    "facebook_positions": ["story", "facebook_reels"],
    "instagram_positions": ["story", "reels", "profile_reels"],
    "messenger_positions": ["story"],
}

# Unterhalb dieser relativen Abweichung gelten zwei Bilder als gleiches Format —
# Placement-Customization brächte dann nichts. Wichtig, weil der Auto-Detect in
# find_complementary_static_url() nur nach Dateinamen sucht und das gefundene
# Geschwister auch eine zweite Variante desselben Formats sein kann.
_RATIO_MIN_DELTA = 0.05


def _build_placement_customized_spec(row: AdRow, story_spec_base: dict,
                                     asset: UploadedAsset) -> dict | None:
    """asset_feed_spec mit Placement-Customization für Static Ads mit zwei Formaten.

    Das hochformatigere Bild (kleineres Breite/Höhe-Verhältnis) geht in Stories +
    Reels, das breitere in Feed & Co. Die Zuordnung basiert auf dem gemessenen
    Seitenverhältnis, nicht auf der Reihenfolge oder dem Dateinamen.

    Gibt None zurück, wenn kein zweites Bild da ist oder beide Bilder dasselbe
    Format haben — dann baut der Aufrufer das normale object_story_spec-Creative.
    """
    if not (asset.image_hash and asset.image_hash_2):
        return None

    r1, r2 = asset.aspect_ratio, asset.aspect_ratio_2
    if r1 is not None and r2 is not None:
        if abs(r1 - r2) / max(r1, r2) < _RATIO_MIN_DELTA:
            logger.info(
                "Ad '%s': beide Bilder haben praktisch dasselbe Seitenverhältnis "
                "(%.3f / %.3f) — keine Placement-Customization.", row.ad_name, r1, r2)
            return None
        # Kleineres Verhältnis = höher/schmaler → vertikale Placements
        tall_hash, wide_hash = (
            (asset.image_hash, asset.image_hash_2) if r1 < r2
            else (asset.image_hash_2, asset.image_hash)
        )
        logger.info("Ad '%s': Placement-Customization — hochformat %.3f → Stories/Reels, "
                    "quer %.3f → Feed.", row.ad_name, min(r1, r2), max(r1, r2))
    else:
        # Alter Cache-Eintrag ohne gemessenes Ratio → bisherige Konvention beibehalten
        tall_hash, wide_hash = asset.image_hash_2, asset.image_hash
        logger.warning(
            "Ad '%s': Seitenverhältnisse unbekannt (Cache ohne Maße) — nutze die "
            "Reihenfolge: image_url_2 → Stories/Reels.", row.ad_name)

    feed: dict = {
        "images": [
            {"hash": wide_hash, "adlabels": [{"name": "placement_wide"}]},
            {"hash": tall_hash, "adlabels": [{"name": "placement_tall"}]},
        ],
        "bodies": [{"text": row.bodies[0]}],
        "titles": [{"text": row.headlines[0]}],
        "link_urls": [{"website_url": row.destination_url}],
        "call_to_action_types": [row.cta],
        "ad_formats": ["SINGLE_IMAGE"],
        "asset_customization_rules": [
            {
                "customization_spec": _TALL_PLACEMENTS,
                "image_label": {"name": "placement_tall"},
            },
            {
                "customization_spec": _WIDE_PLACEMENTS,
                "image_label": {"name": "placement_wide"},
            },
        ],
    }
    if row.descriptions:
        feed["descriptions"] = [{"text": row.descriptions[0]}]
    if row.display_link:
        feed["link_urls"][0]["display_url"] = row.display_link

    return {"object_story_spec": story_spec_base, "asset_feed_spec": feed}


def _build_asset_feed_spec(row: AdRow, page_id: str, assets: list[UploadedAsset]) -> dict:
    """
    3-2-2 Dynamic Creative: asset_feed_spec mit mehreren Assets + Bodies + Headlines.

    Unterstützt entweder reine Video- ODER reine Bild-Sets (Meta erlaubt kein Mix
    pro asset_feed_spec). Format wird automatisch aus den Assets erkannt.
    """
    if not row.bodies:
        raise ValueError(
            f"Ad '{row.ad_name}': Kein Body-Text vorhanden — bitte im Sheet eintragen "
            "oder ein gültiges Referenz-Ad Set mit body-Text setzen."
        )
    if not row.headlines:
        raise ValueError(
            f"Ad '{row.ad_name}': Kein Headline-Text vorhanden — bitte im Sheet eintragen "
            "oder ein gültiges Referenz-Ad Set mit headline-Text setzen."
        )

    # Meta-Limit: max 5 bodies + 5 titles.
    bodies = row.bodies[:5]
    headlines = row.headlines[:5]

    video_count = sum(1 for a in assets if a.is_video)
    image_count = sum(1 for a in assets if a.image_hash and not a.is_video)
    if video_count and image_count:
        raise ValueError(
            f"Ad '{row.ad_name}': 3-2-2 erlaubt nicht Video + Bild gemischt "
            f"(gefunden: {video_count} Videos, {image_count} Bilder). "
            "Bitte pro Ad Set nur einen Asset-Typ verwenden."
        )
    if not (video_count or image_count):
        raise ValueError(
            f"Ad '{row.ad_name}': Keine validen Assets — weder Video noch Bild aufgelöst."
        )

    is_video = video_count > 0
    feed: dict = {
        "bodies": [{"text": t} for t in bodies],
        "titles": [{"text": t} for t in headlines],
        "call_to_action_types": [row.cta],
        "link_urls": [{"website_url": row.destination_url}],
        "ad_formats": ["SINGLE_VIDEO" if is_video else "SINGLE_IMAGE"],
    }

    if is_video:
        video_entries: list[dict] = []
        for a in assets:
            entry = {"video_id": a.video_id}
            if a.thumbnail_hash:
                entry["thumbnail_hash"] = a.thumbnail_hash
            video_entries.append(entry)
        feed["videos"] = video_entries
    else:
        feed["images"] = [{"hash": a.image_hash} for a in assets]

    story_spec_base: dict = {"page_id": page_id}
    if row.instagram_actor_id:
        story_spec_base["instagram_actor_id"] = row.instagram_actor_id

    return {
        "object_story_spec": story_spec_base,
        "asset_feed_spec": feed,
    }


def _create_creative(account: AdAccount, row: AdRow, page_id: str,
                     dry_run: bool, asset: "UploadedAsset | None" = None,
                     force_single: bool = False,
                     allow_placement_customization: bool = True) -> tuple[str, "UploadedAsset"]:
    """
    Gibt (creative_id, asset) zurück.
    asset wird mitgegeben damit bei force_single kein zweiter Upload passiert.

    3-2-2 DCO-Pfad: wenn row.is_dco und nicht force_single → asset_feed_spec.
    Sonst klassisch object_story_spec (1 Asset, 1 Body, 1 Headline).
    """
    if dry_run and not row.image_url and not row.image_hash and not row.video_id:
        return "DRY_RUN_CREATIVE_ID", UploadedAsset()

    use_dco = row.is_dco and not force_single

    if use_dco:
        # 3-2-2: alle Asset-URLs auflösen
        assets = _resolve_assets(row, dry_run)
        if dry_run:
            logger.info(
                "[DRY-RUN] Creative '%s': dco, %d assets, %d bodies, %d headlines, cta=%s",
                row.ad_name, len(assets), min(5, len(row.bodies)), min(5, len(row.headlines)), row.cta,
            )
            return "DRY_RUN_CREATIVE_ID", assets[0] if assets else UploadedAsset()

        params = _build_asset_feed_spec(row, page_id, assets)
        # Für Rückwärtskompatibilität — primary asset als asset zurückgeben
        first_asset = assets[0] if assets else UploadedAsset()
    else:
        # Asset nur einmal auflösen
        if asset is None:
            asset = _resolve_asset(row, dry_run)

        if dry_run:
            mode = "single (forced)" if force_single else ("multi" if row.is_multi_variation else "single")
            asset_info = f"video={asset.video_id}" if asset.is_video else f"hash={asset.image_hash}" if asset.image_hash else f"url={row.image_url}"
            logger.info("[DRY-RUN] Creative '%s': %s, asset=%s, %d bodies, %d headlines",
                        row.ad_name, mode, asset_info, len(row.bodies), len(row.headlines))
            return "DRY_RUN_CREATIVE_ID", asset

        # Immer single spec — ein Ad Set kann mehrere Ads enthalten (unterschiedliche Videos)
        params = _build_single_spec(row, page_id, asset, allow_placement_customization)
        first_asset = asset

    params[AdCreative.Field.name] = f"Creative — {row.ad_name}"

    # URL-Parameter (UTM-Tracking) — wird an jede Link-URL angehängt.
    # Stammt aus dem Sheet oder (Fallback) aus dem Referenz-Ad Set, damit das
    # Tracking auf den neuen Ads immer erhalten bleibt.
    if row.url_tags:
        params[AdCreative.Field.url_tags] = row.url_tags
        logger.info("Creative '%s': url_tags gesetzt (%s)", row.ad_name, row.url_tags)

    # Advertiser für Identity-Bereich (optional)
    if row.advertiser_id:
        params["advertiser_id"] = row.advertiser_id

    # Advantage+ Creative Enhancements deaktivieren — nur von Meta v25 akzeptierte Keys
    params["degrees_of_freedom_spec"] = {
        "creative_features_spec": {
            "IG_VIDEO_NATIVE_SUBTITLE":      {"enroll_status": "OPT_OUT"},
            "IMAGE_ANIMATION":               {"enroll_status": "OPT_OUT"},
            "PRODUCT_METADATA_AUTOMATION":   {"enroll_status": "OPT_OUT"},
            "PROFILE_CARD":                  {"enroll_status": "OPT_OUT"},
            "STANDARD_ENHANCEMENTS_CATALOG": {"enroll_status": "OPT_OUT"},
            "TEXT_OVERLAY_TRANSLATION":      {"enroll_status": "OPT_OUT"},
        }
    }

    time.sleep(config.API_CALL_DELAY)
    creative = account.create_ad_creative(fields=[AdCreative.Field.id], params=params)
    return creative["id"], first_asset


def get_existing_ad_names(adset_id: str) -> dict[str, str]:
    """Liefert {ad_name: ad_id} aller nicht gelöschten Ads eines Ad Sets.

    Doppel-Upload-Schutz: bevor eine Ad angelegt wird, prüft der Uploader hiermit,
    ob im Ziel-Ad-Set schon eine gleichnamige Ad existiert (z.B. weil ein früherer
    Lauf abgebrochen ist, bevor er DONE ins Sheet schreiben konnte). DELETED-Ads
    zählen nicht als vorhanden — die dürfen neu angelegt werden.
    """
    result: dict[str, str] = {}
    ads = AdSet(adset_id).get_ads(
        fields=[Ad.Field.name, Ad.Field.id, Ad.Field.effective_status],
        params={"limit": 500},
    )
    for ad in ads:
        if ad.get(Ad.Field.effective_status, "") == "DELETED":
            continue
        name = ad.get(Ad.Field.name, "")
        if name and name not in result:
            result[name] = ad["id"]
    return result


def create_ad(account: AdAccount, row: AdRow, adset_id: str, page_id: str, dry_run: bool = False) -> str:
    creative_id, asset = _create_creative(account, row, page_id, dry_run)

    if dry_run:
        return "DRY_RUN_AD_ID"

    def _post_ad(cid: str) -> dict:
        time.sleep(config.API_CALL_DELAY)
        return account.create_ad(
            fields=[Ad.Field.id],
            params={
                Ad.Field.name: row.ad_name,
                Ad.Field.adset_id: adset_id,
                Ad.Field.creative: {"creative_id": cid},
                Ad.Field.status: Ad.Status.paused,
            },
        )

    # Wurde Placement-Customization genutzt, kann Meta die Ad ablehnen, falls das
    # Ziel-Ad-Set ein Placement nutzt, das keine Rule abdeckt.
    had_customization = bool(asset.image_hash and asset.image_hash_2)

    try:
        ad = _post_ad(creative_id)
    except FacebookRequestError as e:
        subcode = e.api_error_subcode()
        if subcode == 1487860:
            raise ValueError(
                f"Ad Set ist archiviert — bitte im Meta Ads Manager reaktivieren "
                f"(Ad Set ID: {adset_id}). Danach nochmal starten."
            )
        if subcode == 1885998:
            # Ad Set hat kein Dynamic Creative (DCO)
            # → Creative neu als Einzel-Variation erstellen und nochmal versuchen
            logger.warning(
                "Ad Set hat kein Dynamic Creative — Fallback auf erste Variation für '%s'. "
                "Tipp: Ad Set neu erstellen damit alle Textvariationen genutzt werden.",
                row.ad_name,
            )
            creative_id, _ = _create_creative(account, row, page_id, dry_run,
                                              asset=asset, force_single=True)
            ad = _post_ad(creative_id)
        elif had_customization:
            # Placement-Customization abgelehnt → ohne sie wiederholen.
            # Eine Ad mit einem Format ist besser als gar keine Ad.
            logger.warning(
                "Ad '%s' mit Placement-Customization abgelehnt (%s) — Fallback auf "
                "ein einzelnes Format.",
                row.ad_name, config.mask_token(str(e.api_error_message())),
            )
            creative_id, _ = _create_creative(account, row, page_id, dry_run, asset=asset,
                                              allow_placement_customization=False)
            ad = _post_ad(creative_id)
        else:
            raise
    return ad["id"]
