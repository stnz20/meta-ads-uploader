from __future__ import annotations
import logging
import os
import time
import requests
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.adset import AdSet
from facebook_business.adobjects.campaign import Campaign
import config
from sheets.reader import AdSetRow

logger = logging.getLogger(__name__)

# Felder die wir vom Referenz-Ad Set zu lesen versuchen.
# In v25.0 sind viele davon deprecated — wir versuchen sie einzeln und
# fallen still zurück, wenn sie nicht mehr lesbar sind.
_READ_FIELDS_REQUIRED = ["targeting"]
_READ_FIELDS_OPTIONAL = [
    "optimization_goal",
    "bid_strategy",
    "bid_amount",
    "destination_type",
    "promoted_object",
    "attribution_spec",
    "billing_event",
]

# Hardcoded Defaults — werden nur als Fallback genutzt, wenn das Referenz-Ad Set
# das Feld nicht liefert (z.B. wegen v25.0 Deprecation).
# Entsprechen einem typischen Standard-Setup für Sales-Kampagnen.
_DEFAULT_BILLING_EVENT = "IMPRESSIONS"
_DEFAULT_OPTIMIZATION_GOAL = "OFFSITE_CONVERSIONS"
_DEFAULT_BID_STRATEGY = "LOWEST_COST_WITHOUT_CAP"
_DEFAULT_DESTINATION_TYPE = "WEBSITE"
_DEFAULT_PROMOTED_OBJECT = {
    "pixel_id": os.getenv("META_PIXEL_ID", ""),  # Pixel-ID aus .env (META_PIXEL_ID)
    "custom_event_type": "PURCHASE",
}

# Cache: source_adset_id → dict — verhindert wiederholte API-Calls
_source_cache: dict[str, dict] = {}


def _read_adset_field(adset_id: str, field: str) -> dict | None:
    """
    Liest ein einzelnes Feld vom Ad Set über die v23.0 REST API.
    Gibt das Response-Dict zurück (ohne id-Key) oder None bei Fehler.
    """
    try:
        resp = requests.get(
            f"https://graph.facebook.com/v23.0/{adset_id}",
            params={
                "access_token": config.META_ACCESS_TOKEN,
                "fields": field,
            },
            timeout=30,
        )
        data = resp.json()
        if "error" in data:
            return None
        # Nur das angefragte Feld zurückgeben
        return {field: data[field]} if field in data else None
    except Exception:
        return None


def _get_source_adset(campaign_id: str, source_adset_id: str | None = None) -> dict:
    """
    Liest Felder vom Referenz-Ad Set (gecacht pro ID).
    Versucht so viele Felder wie möglich zu lesen — fällt aber still zurück,
    wenn ein Feld in v25.0 nicht mehr lesbar ist.
    - source_adset_id gesetzt → dieses Ad Set direkt laden
    - Sonst → erstes Ad Set der Kampagne nehmen
    """
    cache_key = source_adset_id or f"campaign:{campaign_id}"
    if cache_key in _source_cache:
        return _source_cache[cache_key]

    adset_id = source_adset_id
    if not adset_id:
        campaign = Campaign(campaign_id)
        adsets = campaign.get_ad_sets(fields=["id", "name"])
        if not adsets:
            raise ValueError(f"Keine Ad Sets in Kampagne {campaign_id} gefunden")
        adset_id = adsets[0]["id"]
        logger.info("Kein Referenz-Ad Set angegeben — nehme erstes Ad Set: %s", adset_id)

    # Pflichtfelder laden (targeting MUSS lesbar sein)
    data = AdSet(adset_id).api_get(fields=_READ_FIELDS_REQUIRED).export_all_data()

    # Optionale Felder einzeln versuchen
    found_fields = []
    for field in _READ_FIELDS_OPTIONAL:
        result = _read_adset_field(adset_id, field)
        if result:
            data.update(result)
            found_fields.append(field)

    logger.info(
        "Referenz-Ad Set %s geladen — übernommen: targeting, %s",
        adset_id,
        ", ".join(found_fields) if found_fields else "(keine optionalen Felder)",
    )

    _source_cache[cache_key] = data
    return data


def _is_cbo_campaign(campaign_id: str) -> bool:
    """Prüft ob die Kampagne CBO (Campaign Budget Optimization) verwendet."""
    campaign = Campaign(campaign_id)
    data = campaign.api_get(fields=[
        Campaign.Field.daily_budget,
        Campaign.Field.lifetime_budget,
    ]).export_all_data()
    has_campaign_budget = bool(data.get("daily_budget") or data.get("lifetime_budget"))
    if has_campaign_budget:
        logger.info("Kampagne %s verwendet CBO — kein Ad-Set-Budget wird gesetzt.", campaign_id)
    return has_campaign_budget


def create_adset(account: AdAccount, row: AdSetRow, dry_run: bool = False) -> str:
    source = _get_source_adset(row.source_campaign_id, row.source_adset_id)

    targeting = source.get("targeting", {})
    if row.targeting_override:
        targeting.update(row.targeting_override)

    # Meta-Pflicht: explore_home erfordert explore in instagram_positions
    ig_pos = targeting.get("instagram_positions", [])
    if "explore_home" in ig_pos and "explore" not in ig_pos:
        ig_pos = list(ig_pos) + ["explore"]
        targeting["instagram_positions"] = ig_pos
        logger.info("instagram_positions: 'explore' automatisch ergänzt (erforderlich für explore_home)")

    # Werte aus Referenz-Ad Set übernehmen, mit Fallback auf Mammaly-Defaults.
    # So funktioniert das System weiterhin "kopiere vom Referenz-Ad Set" wo möglich.
    params: dict = {
        AdSet.Field.name: row.ad_set_name,
        AdSet.Field.campaign_id: row.source_campaign_id,
        AdSet.Field.start_time: row.start_time,
        AdSet.Field.targeting: targeting,
        AdSet.Field.status: AdSet.Status.paused,  # immer paused — nie direkt aktivieren
        AdSet.Field.billing_event:
            source.get("billing_event") or _DEFAULT_BILLING_EVENT,
        AdSet.Field.optimization_goal:
            source.get("optimization_goal") or _DEFAULT_OPTIMIZATION_GOAL,
        AdSet.Field.bid_strategy:
            source.get("bid_strategy") or _DEFAULT_BID_STRATEGY,
        AdSet.Field.destination_type:
            source.get("destination_type") or _DEFAULT_DESTINATION_TYPE,
        AdSet.Field.promoted_object:
            source.get("promoted_object") or _DEFAULT_PROMOTED_OBJECT,
    }

    # bid_amount: aus Sheet hat Vorrang, sonst aus Referenz-Ad Set
    bid_amount = row.bid_amount or source.get("bid_amount")
    if bid_amount:
        params[AdSet.Field.bid_amount] = bid_amount

    # attribution_spec: optional, nur setzen wenn vorhanden
    if source.get("attribution_spec"):
        params[AdSet.Field.attribution_spec] = source["attribution_spec"]

    # 3-2-2: Dynamic Creative beim Ad Set aktivieren — nicht änderbar nach Create
    if row.dco_mode:
        params[AdSet.Field.is_dynamic_creative] = True
        logger.info("Ad Set '%s' wird mit is_dynamic_creative=True angelegt (3-2-2-Modus).", row.ad_set_name)

    if row.end_time:
        params[AdSet.Field.end_time] = row.end_time

    # Budget nur setzen wenn kein CBO — bei CBO wirft Meta sonst einen Fehler
    is_cbo = not dry_run and _is_cbo_campaign(row.source_campaign_id)
    if is_cbo:
        logger.info("CBO-Kampagne: daily_budget aus Sheet wird ignoriert.")
    else:
        if not row.daily_budget:
            raise ValueError(
                f"ABO-Kampagne erfordert daily_budget > 0 — bitte im Sheet eintragen "
                f"(Kampagne: {row.source_campaign_id})"
            )
        if row.daily_budget < 100:
            raise ValueError(
                f"daily_budget zu gering: {row.daily_budget} Cent (€{row.daily_budget/100:.2f}) — "
                f"Minimum ist 100 Cent (€1,00). Werte im Sheet sind in Cent, z.B. €25/Tag = 2500"
            )
        params[AdSet.Field.daily_budget] = row.daily_budget

    if dry_run:
        return "DRY_RUN_ADSET_ID"

    time.sleep(config.API_CALL_DELAY)
    adset = account.create_ad_set(fields=[AdSet.Field.id], params=params)
    return adset["id"]
