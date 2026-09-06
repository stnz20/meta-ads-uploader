"""Naming convention logic and reference data for the Meta Ads Uploader."""
from __future__ import annotations
import os
import re
from datetime import datetime, timezone
from typing import Optional

# Agency abbreviation → our product code
PRODUCT_ABBREVIATIONS: dict[str, str] = {
    "NP": "NatureProtect",
    "LB": "LuckyBelly",
    "AH": "ActiveHips",
    "SL": "ScratchLess",
    "FS": "FreshSmile",
    "MV": "MultiVital",
    "RT": "RelaxTime",
    "SH": "ShinyHair",
    "ES": "EasySqueezy",
    "FF": "FireFighter",
    "FY": "ForeverYoung",
    "IP": "ImmuPush",
    "PP": "PuppyPower",
    "PG": "PureGenius",
    "PW": "PerfectWeight",
    "LL": "LongLife",
    "UH": "UroHero",
    "HT": "HappyTummy",
    "SP": "SynProbio",
    "SGL": "SynGrünlipp",
    "SS": "SynSeealge",
    "SK": "SynKollagen",
    "SVB": "SynVitaminB",
    "ST": "SynTeufels",
    "TLB": "Topping-LuckyBelly",
    "TFS": "Topping-FreshSmile",
    "TAH": "Topping-ActiveHips",
    "TSH": "Topping-ShinyHair",
    "TRT": "Topping-RelaxTime",
}

# Externe Advertiser-Advertorials (TL / Ben / Wuffes / Petlab). NICHT mehr der Default —
# aufbewahrt als Referenz/optionales Override. Default ist jetzt die mammaly-PDP (s. LP_DEFAULTS unten).
LP_ADVERTORIALS: dict[str, tuple[str, str]] = {
    "NatureProtect":      ("https://seiten.hunde-spiegel.de/nie-wieder-zecken-im-fell", "Adv-HS-Ben-NP-1"),
    "LuckyBelly":         ("https://hundegesundheit.online/pages/magen-darm-kur-hund-komplettschutz", "Meta-Offer-TL-LB"),
    "ActiveHips":         ("https://www.mammaly.de/pages/lp-stopp-den-schmerz", "Meta-Offer-Wuffes-AH"),
    "ScratchLess":        ("https://seiten.hunde-spiegel.de/nie-wieder-pfoten-blutig-lecken", "Adv-HS-Ben-SL-1"),
    "FreshSmile":         ("https://seiten.hunde-spiegel.de/wie-zahnstein-ohne-narkose-verschwindet", "Adv-HS-Ben-FS-1"),
    "MultiVital":         ("https://hundegesundheit.online/pages/natuerlicher-juckreizschutz-hunde-mv", "Meta-Offer-TL-MV"),
    "RelaxTime":          ("https://www.mammaly.de/pages/lp-relax-time-mobile", "Meta-Offer-Wuffes-RT"),
    "ShinyHair":          ("https://www.mammaly.de/products/shiny-hair-2", "mm-PDP-SH"),
    "HappyTummy":         ("https://hundegesundheit.online/pages/magen-darm-kur-hund", "Meta-Offer-TL-HT"),
    "SynProbio":          ("https://www.mammaly.de/products/probiotika-2", "mm-PDP-Probio"),
    "SynSeealge":         ("https://www.mammaly.de/products/seealge-2", "mm-PDP-Seealge"),
    "SynKollagen":        ("https://www.mammaly.de/products/kollagen-2", "mm-PDP-Kollagen"),
    "Topping-FreshSmile": ("https://www.mammaly.de/pages/lp-fresh-smile-topping-plc", "Meta-Offer-Petlab-FST"),
    "Topping-ShinyHair":  ("https://www.mammaly.de/products/shiny-hair-2", "mm-PDP-SH"),
}

# Product code → (LP URL, LP tag). Kanonische mammaly-PDP pro Produkt.
# Pattern: mammaly.de PDP `/products/{kebab-name}-2` + tag `mm-PDP-{abbr}`.
# All URLs verified as live (2026-07-28). Syn-line uses German slugs, not the kebab pattern.
LP_FALLBACKS: dict[str, tuple[str, str]] = {
    "NatureProtect": ("https://www.mammaly.de/products/nature-protect-2", "mm-PDP-NP"),
    "LuckyBelly":    ("https://www.mammaly.de/products/lucky-belly-2", "mm-PDP-LB"),
    "ActiveHips":    ("https://www.mammaly.de/products/active-hips-2", "mm-PDP-AH"),
    "ScratchLess":   ("https://www.mammaly.de/products/scratch-less-2", "mm-PDP-SL"),
    "FreshSmile":    ("https://www.mammaly.de/products/fresh-smile-2", "mm-PDP-FS"),
    "MultiVital":    ("https://www.mammaly.de/products/multi-vital-2", "mm-PDP-MV"),
    "RelaxTime":     ("https://www.mammaly.de/products/relax-time-2", "mm-PDP-RT"),
    "ShinyHair":     ("https://www.mammaly.de/products/shiny-hair-2", "mm-PDP-SH"),
    "EasySqueezy":   ("https://www.mammaly.de/products/easy-squeezy-2", "mm-PDP-ES"),
    "FireFighter":   ("https://www.mammaly.de/products/fire-fighter-2", "mm-PDP-FF"),
    "ForeverYoung":  ("https://www.mammaly.de/products/forever-young-2", "mm-PDP-FY"),
    "ImmuPush":      ("https://www.mammaly.de/products/immu-push-2", "mm-PDP-IP"),
    "PuppyPower":    ("https://www.mammaly.de/products/puppy-power-2", "mm-PDP-PP"),
    "PureGenius":    ("https://www.mammaly.de/products/pure-genius-2", "mm-PDP-PG"),
    "PerfectWeight": ("https://www.mammaly.de/products/perfect-weight-2", "mm-PDP-PW"),
    "LongLife":      ("https://www.mammaly.de/products/long-life-2", "mm-PDP-LL"),
    "UroHero":       ("https://www.mammaly.de/products/uro-hero-2", "mm-PDP-UH"),
    "HappyTummy":    ("https://www.mammaly.de/products/happy-tummy-2", "mm-PDP-HT"),
    # Syn-line: German product slugs (not derivable from the code name)
    "SynProbio":     ("https://www.mammaly.de/products/probiotika-2", "mm-PDP-Probio"),
    "SynSeealge":    ("https://www.mammaly.de/products/seealge-2", "mm-PDP-Seealge"),
    "SynKollagen":   ("https://www.mammaly.de/products/kollagen-2", "mm-PDP-Kollagen"),
    "SynGrünlipp":   ("https://www.mammaly.de/products/grunlippmuschel-2", "mm-PDP-Gruenlipp"),
    "SynVitaminB":   ("https://www.mammaly.de/products/vitamin-b-2", "mm-PDP-VitaminB"),
    "SynTeufels":    ("https://www.mammaly.de/products/teufelskralle-2", "mm-PDP-Teufels"),
    # Topping variants have no own PDP → fall back to the base product's PDP
    "Topping-LuckyBelly": ("https://www.mammaly.de/products/lucky-belly-2", "mm-PDP-LB"),
    "Topping-FreshSmile": ("https://www.mammaly.de/products/fresh-smile-2", "mm-PDP-FS"),
    "Topping-ActiveHips": ("https://www.mammaly.de/products/active-hips-2", "mm-PDP-AH"),
    "Topping-ShinyHair":  ("https://www.mammaly.de/products/shiny-hair-2", "mm-PDP-SH"),
    "Topping-RelaxTime":  ("https://www.mammaly.de/products/relax-time-2", "mm-PDP-RT"),
}

# Default-LP pro Produkt = die Produktdetailseite (Entscheidung vom 2026-07-28).
# Ad-Naming/Batch/Upload schlagen die LP hier nach → standardmäßig PDPs statt Advertorials.
# Für externe Advertorials stattdessen LP_ADVERTORIALS verwenden (manuelles Override).
LP_DEFAULTS: dict[str, tuple[str, str]] = dict(LP_FALLBACKS)


def fallback_lp(product_code: str) -> Optional[tuple[str, str]]:
    """Returns (LP URL, LP tag) for a product code, or None if unknown."""
    return LP_FALLBACKS.get(product_code)


# Reverse-Map LP-ID → LP-URL (aus allen bekannten LP-Quellen). Erlaubt, zu einer
# (auch in der Tabelle editierten) LP-ID die passende URL aufzulösen.
_LP_URL_BY_ID: dict[str, str] = {}
for _url, _lp_id in list(LP_DEFAULTS.values()) + list(LP_ADVERTORIALS.values()):
    if _lp_id and _lp_id not in _LP_URL_BY_ID:
        _LP_URL_BY_ID[_lp_id] = _url


def lp_url_for_id(lp_id: str) -> Optional[str]:
    """LP-URL zu einer LP-ID, oder None wenn unbekannt."""
    return _LP_URL_BY_ID.get((lp_id or "").strip())


def resolve_lp(product: str, lp_id: str) -> tuple[str, str]:
    """Konsistentes (LP-URL, LP-ID)-Paar für den finalen Zustand einer Zeile.

    Vorrang: passt die (ggf. editierte) LP-ID zu einer bekannten URL → diese nehmen.
    Sonst: Produkt-Default aus LP_DEFAULTS. Sonst: gegebene Werte unverändert.
    """
    url = lp_url_for_id(lp_id)
    if url:
        return url, lp_id.strip()
    dflt = LP_DEFAULTS.get(product)
    if dflt:
        return dflt
    return "", (lp_id or "").strip()


# Duration in seconds → Format suffix (number only)
_DURATION_BREAKPOINTS = [30, 60, 90, 120, 180, 240, 360]


def duration_to_suffix(seconds: int) -> str:
    for bp in _DURATION_BREAKPOINTS:
        if seconds <= bp:
            return str(bp)
    return str(seconds)


def build_ad_name(meta_id: str, fmt: str, product: str, description: str,
                   creator: str, version: str) -> str:
    return f"{meta_id}_{fmt}_{product}_{description}_{creator}_{version}"


def build_adset_name(meta_id: str, fmt: str, product: str, description: str,
                      creator: str, lp_id: str, date: Optional[str] = None) -> str:
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{date}_{meta_id}_{fmt}_{product}_{description}_{creator}_{lp_id}"


def normalize_adset_names(proposals: list[dict]) -> None:
    """Groups proposals by concept (fmt, product, description, creator, lp_id).
    Within each group, all versions get the adset_name of V1 (lowest MetaAd ID).
    Mutates proposals in-place."""
    from collections import defaultdict

    groups: dict = defaultdict(list)
    for p in proposals:
        key = (p["fmt"], p["product"], p["description"], p["creator"], p["lp_id"])
        groups[key].append(p)

    for group in groups.values():
        if len(group) <= 1:
            continue
        group.sort(key=lambda p: int(p["meta_id"].split("-")[1]))
        v1_adset = group[0]["adset_name"]
        for p in group[1:]:
            p["adset_name"] = v1_adset


_FORMAT_KEYWORDS = {"VSL", "UGC", "VID", "STA"}
_SKIP_WORDS = {
    # Füllwörter / Branding
    "mammaly", "metaad", "ad", "ads", "by", "von", "for", "fur", "für", "mit", "und",
    "and", "the", "creative", "video", "clip", "asset",
    # Produktions-/Export-Rauschen
    "final", "finale", "export", "render", "master", "draft", "cut",
    "neu", "new", "copy", "kopie", "rev", "korrektur", "feedback",
    # Sprache / Markt
    "de", "ger", "german", "deutsch", "dach", "eng", "en",
}

# Creator-/Agenturnamen, die in Rohdateinamen auftauchen können, aber kein
# Konzept beschreiben. Wird ergänzt um den im UI angegebenen Creator.
# Konfigurierbar über KNOWN_CREATORS in .env (kommagetrennt, z. B. "agentur-a,agentur-b").
KNOWN_CREATORS = {
    c.strip().lower() for c in os.getenv("KNOWN_CREATORS", "").split(",") if c.strip()
}

# Funnel-Stage-Phrasen (kein Konzept) — werden vor der Tokenisierung entfernt.
_FUNNEL_STAGE_RE = re.compile(
    r"\b(?:(?:problem|product|solution|most)[\s_-]*)*(?:un)?aware(?:ness)?\b"
    r"|\b(?:prospecting|retargeting|tof|mof|bof|cold|warm)\b",
    re.IGNORECASE,
)

# Technisches Rauschen auf Token-Ebene: Auflösungen, Seitenverhältnisse, Codecs.
_TECH_TOKEN_RE = re.compile(
    r"^(?:\d+x\d+|\d+:\d+|\d{3,4}p|[24]k|hd|fullhd|h264|h265|hevc|prores)$",
    re.IGNORECASE,
)


def _spaced_name_regex(code: str) -> re.Pattern:
    """Regex, der einen Produkt-Code auch in getrennter Schreibweise findet:
    'LuckyBelly' → matcht 'LuckyBelly', 'Lucky Belly', 'lucky-belly', 'Lucky_Belly'."""
    words = re.split(r"[-_\s]+", re.sub(r"(?<=[a-zäöü])(?=[A-ZÄÖÜ])", " ", code))
    return re.compile(r"\b" + r"[\s_-]*".join(map(re.escape, words)) + r"\b", re.IGNORECASE)


# Produkt-Vollnamen (längste zuerst, damit 'Topping-LuckyBelly' vor 'LuckyBelly' greift)
_PRODUCT_NAME_REGEXES: list[tuple[str, re.Pattern]] = [
    (code, _spaced_name_regex(code))
    for code in sorted(set(PRODUCT_ABBREVIATIONS.values()), key=len, reverse=True)
]


def parse_agency_filename(filename: str, creator: Optional[str] = None) -> dict:
    """
    Parses agency filename to extract product, description, version, and format type.

    Handles formats like:
      001_NP_ProductAware_ConceptName_V1.ext
      mammaly Ad 16 (NP) DoDont - VSL - Problem Solution Aware V1.ext

    Entfernt aus der Concept-Beschreibung alles, was kein Konzept beschreibt:
    Produkt-Vollnamen, Creator-Namen, Funnel-Stages, Export-/Technik-Rauschen.
    """
    stem = re.sub(r"(\.(mp4|mov|jpg|jpeg|png|gif|avi|mkv|webm))+$", "", filename, flags=re.IGNORECASE)

    # Unterstriche → Leerzeichen, sonst greifen \b-Wortgrenzen der Regexe unten
    # nicht (Unterstrich zählt als Wortzeichen: '_Name_' hätte keine Grenze).
    stem = re.sub(r"_+", " ", stem)

    product = None

    # Produkt-Vollnamen erkennen → Produkt setzen und aus dem Stem entfernen,
    # damit sie nicht in der Description landen (auch 'Lucky Belly' getrennt).
    for code, pattern in _PRODUCT_NAME_REGEXES:
        m = pattern.search(stem)
        if m:
            if product is None:
                product = code
            stem = pattern.sub(" ", stem)

    # Creator-Namen entfernen (bekannte + der im UI angegebene)
    creator_names = set(KNOWN_CREATORS)
    if creator and creator.strip():
        creator_names.add(creator.strip().lower())
    for name in creator_names:
        stem = re.sub(rf"\b{re.escape(name)}\b", " ", stem, flags=re.IGNORECASE)

    # Funnel-Stages ('Problem Solution Aware', 'Retargeting' …) entfernen
    stem = _FUNNEL_STAGE_RE.sub(" ", stem)

    # Datums-Stempel (20260812, 12.08.2026, 12-08-26) entfernen
    stem = re.sub(r"\b\d{4}[.\-/]?\d{2}[.\-/]?\d{2}\b|\b\d{2}[.\-/]\d{2}[.\-/]\d{2,4}\b", " ", stem)

    # Extract concept number (e.g. "Ad 30" / "Ad30" → "30") to prepend to description
    concept_num_match = re.search(r'\bAd\s*(\d+)\b', stem, re.IGNORECASE)
    concept_num = concept_num_match.group(1) if concept_num_match else None
    if concept_num_match:
        # Aus dem Stem entfernen, damit 'Ad17' nicht zusätzlich als Concept-Wort landet
        stem = stem[:concept_num_match.start()] + " " + stem[concept_num_match.end():]

    # Split on strong separators " - " first to isolate concept section
    # e.g. "mammaly Ad 16 (NP) DoDont - VSL - Problem Solution V1"
    # → ["mammaly Ad 16 (NP) DoDont", "VSL", "Problem Solution V1"]
    sections = re.split(r"\s+-\s+", stem)

    version = None
    fmt_type = None
    concept_parts = []  # only from the concept section, not funnel stage

    for section_idx, section in enumerate(sections):
        parts = re.split(r"[\s_-]+", section)

        for part in parts:
            clean = re.sub(r"[()[\]]", "", part).strip()
            # Strip leading hyphens (e.g. "-V2" → "V2")
            clean = clean.lstrip("-")
            # Strip leading number+colon prefix (e.g. "30:Durchfall" → "Durchfall")
            clean = re.sub(r"^\d+:", "", clean)
            upper = clean.upper()

            if not clean:
                continue

            # Pure number → skip
            if re.match(r"^\d+$", clean):
                continue

            # Version: V1, V2, ...
            if re.match(r"^[Vv]\d+$", clean):
                version = clean.upper()
                continue

            # Format type keyword — marks this section as format, not concept
            if upper in _FORMAT_KEYWORDS:
                fmt_type = upper
                continue

            # Product abbreviation
            if upper in PRODUCT_ABBREVIATIONS and product is None:
                product = PRODUCT_ABBREVIATIONS[upper]
                continue

            # Skip noise words
            if clean.lower() in _SKIP_WORDS:
                continue

            # Technisches Rauschen (Auflösung, Seitenverhältnis, Codec)
            if _TECH_TOKEN_RE.match(clean):
                continue

            # Only collect concept words from the section that contains the product
            # (or the first non-format section). Funnel stage words come later → skip.
            if section_idx == 0 or (product and section_idx < 2 and fmt_type is None):
                if len(clean) > 1:
                    concept_parts.append(clean)

    # Description: optional "Ad{N}" prefix + CamelCase joined concept parts
    concept_str = "".join(p[0].upper() + p[1:] for p in concept_parts) if concept_parts else None
    if concept_num and concept_str:
        description = f"Ad{concept_num}_{concept_str}"
    elif concept_num:
        description = f"Ad{concept_num}"
    else:
        description = concept_str

    return {
        "product": product,
        "description": description,
        "version": version or "V1",
        "fmt_type": fmt_type,
    }


def next_meta_id(current_max: int) -> str:
    return f"MetaAd-{current_max + 1}"
