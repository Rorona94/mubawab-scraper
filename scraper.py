"""
Scraper mubawab.ma - Commerces à louer
Surveille les annonces sur des quartiers ciblés, filtre selon budget/surface,
et envoie une alerte Telegram/WhatsApp pour chaque nouvelle annonce matchée.

Usage: python scraper.py
Variables d'env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WHATSAPP_PHONE, WHATSAPP_APIKEY, DEBUG_SCRAPER
Critères modifiables sans toucher au code : voir config.json à la racine du repo
"""

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright

# ============ CONFIGURATION ============
CONFIG_FILE = Path(__file__).parent / "config.json"
with open(CONFIG_FILE, encoding="utf-8") as f:
    CONFIG = json.load(f)

QUARTIERS = CONFIG["quartiers"]
BUDGET_MIN = CONFIG["budget_min"]
BUDGET_MAX = CONFIG["budget_max"]
BUDGET_MAX_HARD = CONFIG["budget_max_hard"]
SURFACE_MIN = CONFIG["surface_min"]
SURFACE_MAX = CONFIG["surface_max"]
SURFACE_TOLERANCE = CONFIG["surface_tolerance"]

# Détection extraction / restauration
EXTRACTION_POSITIVE_PATTERNS = [
    r"gaine(?: d['’ ]?extraction)? (?:disponible|existante|installée|prévue)",
    r"gaine d['’ ]?extraction",
    r"avec extraction",
    r"extraction (?:disponible|existante|installée|prévue)",
    r"hotte (?:professionnelle|installée)",
    r"gain[ée]?(?:,| )+id[ée]al restauration",
]
EXTRACTION_NEGATIVE_PATTERNS = [
    r"sans extraction",
    r"pas d['’ ]?extraction",
    r"ne nécessitant pas d['’ ]?extraction",
    r"ne nécessite pas d['’ ]?extraction",
    r"extraction (?:non|pas) (?:possible|autorisée)",
    r"aucune extraction",
]
RESTAURATION_POSITIVE_PATTERNS = [
    r"restauration autorisée",
    r"restauration possible",
    r"id[ée]al(?:e)? (?:pour )?(?:la )?restauration",
    r"restaurant",
    r"snack",
    r"café restaurant",
    r"commerce alimentaire",
]
RESTAURATION_NEGATIVE_PATTERNS = [
    r"sauf restauration",
    r"restauration interdite",
    r"pas de restauration",
    r"restauration non autorisée",
    r"hors restauration",
]

# Filtre anti-résidentiel : exclut appart/villa/studio remontés par des encarts
# sponsorisés/recommandés hors catégorie "locaux commerciaux"
RESIDENTIAL_KEYWORDS = ["appartement", "duplex", "studio", "villa", "riad", "chambre à louer", "chambre a louer"]
COMMERCIAL_KEYWORDS = [
    "local", "commerce", "commercial", "magasin", "bureau", "boutique",
    "restaurant", "snack", "café", "cafe", "showroom", "dépôt", "depot",
    "entrepôt", "entrepot", "rideau", "fonds de commerce"
]

SEEN_FILE = Path(__file__).parent / "seen_listings.json"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# WhatsApp via CallMeBot (gratuit, pas besoin de compte WhatsApp Business)
WHATSAPP_PHONE = os.environ.get("WHATSAPP_PHONE")
WHATSAPP_APIKEY = os.environ.get("WHATSAPP_APIKEY")


# ============ UTILITAIRES ============
def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen_ids):
    SEEN_FILE.write_text(json.dumps(sorted(seen_ids)))


def _clean_number(value):
    """Convertit '28 350', '28.350' ou '28,350' en entier quand il s'agit d'un montant."""
    if value is None:
        return None
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def parse_price(text):
    """Extrait un prix en DH/MAD. Priorité aux formulations de loyer/prix."""
    if not text:
        return None

    normalized = " ".join(text.replace("\xa0", " ").split())

    priority_patterns = [
        r"(?:loyer|prix|location)\s*(?:mensuel(?:le)?\s*)?(?::|-)?\s*(\d[\d\s.,]{2,})\s*(?:dh|dhs|mad)",
        r"(\d[\d\s.,]{2,})\s*(?:dh|dhs|mad)\s*(?:ttc|ht|htva|htsc)?\s*(?:/|par)?\s*(?:mois)?",
    ]
    for pattern in priority_patterns:
        match = re.search(pattern, normalized, flags=re.I)
        if match:
            value = _clean_number(match.group(1))
            if value and 1000 <= value <= 1000000:
                return value
    return None


def parse_surface(text):
    """Extrait une surface en m² ; accepte 97 m², 97m2, 97.0 m²."""
    if not text:
        return None
    normalized = text.replace("\xa0", " ")
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*m(?:²|2)\b", normalized, flags=re.I)
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    return int(round(value))


def detect_activity_status(text):
    """Détecte les mentions positives/négatives d'extraction et de restauration."""
    t = " ".join((text or "").lower().split())

    extraction_negative = any(re.search(p, t, flags=re.I) for p in EXTRACTION_NEGATIVE_PATTERNS)
    restauration_negative = any(re.search(p, t, flags=re.I) for p in RESTAURATION_NEGATIVE_PATTERNS)

    extraction_positive = (
        not extraction_negative
        and any(re.search(p, t, flags=re.I) for p in EXTRACTION_POSITIVE_PATTERNS)
    )
    restauration_positive = (
        not restauration_negative
        and any(re.search(p, t, flags=re.I) for p in RESTAURATION_POSITIVE_PATTERNS)
    )

    if extraction_negative:
        extraction_status = "non"
    elif extraction_positive:
        extraction_status = "oui"
    else:
        extraction_status = "inconnue"

    return {
        "gaine_extraction": extraction_positive,
        "extraction_interdite": extraction_negative,
        "extraction_status": extraction_status,
        "restauration_mentionnee": restauration_positive,
        "restauration_interdite": restauration_negative,
    }


def extract_listing_id(url):
    """Utilise l'identifiant Mubawab /a/1234567 pour éviter les doublons."""
    if not url:
        return None
    match = re.search(r"/a/(\d+)", url)
    return match.group(1) if match else url


def extract_card_image(card):
    """Récupère la meilleure image/miniature visible dans une carte de résultats Mubawab."""
    try:
        imgs = card.query_selector_all("img")
        for img in imgs:
            for attr in ("src", "data-src", "data-original", "data-lazy", "data-srcset"):
                value = img.get_attribute(attr)
                if not value:
                    continue
                value = value.split(",")[0].strip().split(" ")[0]
                if "mubawab-media.com" in value and "/ad/" in value:
                    return value
        for img in imgs:
            value = img.get_attribute("src") or img.get_attribute("data-src")
            if value and "mubawab-media.com" in value and "logo" not in value.lower():
                return value
    except Exception:
        pass
    return None


def extract_quick_features(card_text):
    """Extrait les caractéristiques visibles directement sur la carte de résultats."""
    if not card_text:
        return []
    known = [
        "Garage", "Ascenseur", "Concierge", "Chambre rangement", "Climatisation",
        "Chauffage central", "Sécurité", "Double vitrage", "Porte blindée",
        "Cuisine équipée", "Terrasse", "Jardin"
    ]
    found = []
    low = card_text.lower()
    for item in known:
        if item.lower() in low:
            found.append(item)

    bath = re.search(r"(\d+)\s+salle(?:s)? de bain", low)
    if bath:
        found.insert(0, f"{bath.group(1)} salle(s) de bain")

    return found[:10]


def extract_property_details(text):
    """Extrait type de bien / état / étage / année / sol depuis le texte libre de l'annonce."""
    t = " ".join((text or "").split())
    low = t.lower()
    details = {}

    type_match = re.search(
        r"\b(local commercial|magasin|dépôt|depot|entrepôt|entrepot|bureau|showroom|boutique|fonds de commerce)\b",
        low,
    )
    if type_match:
        details["type_bien"] = type_match.group(1).capitalize()

    etat_match = re.search(
        r"\b(bon état|à rénover|a renover|neuf|habitable|nouveau|refait à neuf)\b", low
    )
    if etat_match:
        details["etat"] = etat_match.group(1).capitalize()

    etage_match = re.search(r"\b(rez[- ]de[- ]chauss\w*|\d+\s*(?:er|ère|ème|eme)\s*étage)\b", low)
    if etage_match:
        details["etage"] = etage_match.group(1).replace("ere", "re").capitalize()

    annee_match = re.search(r"\b(moins d'un an|\d+\s*ans?)\b", low)
    if annee_match:
        details["annee"] = annee_match.group(1).capitalize()

    sol_match = re.search(r"(?:type de sol\s*:?\s*|sol en\s*)(marbre|carrelage|parquet|béton|beton|granit|céramique|ceramique)", low)
    if sol_match:
        details["type_sol"] = sol_match.group(1).capitalize()

    bath_match = re.search(r"(\d+)\s+salle(?:s)? de bain", low)
    if bath_match:
        details["salle_bain"] = bath_match.group(1)

    return details


def send_whatsapp_alert(listing, evaluation):
    if not WHATSAPP_PHONE or not WHATSAPP_APIKEY:
        print("⚠️ WhatsApp non configuré, alerte ignorée.")
        return

    tag = "MATCH IDEAL" if evaluation["strict_match"] else "A CONSIDERER"
    reasons_txt = " | ".join(evaluation["reasons"]) if evaluation["reasons"] else "Aucune"

    quartier_txt = listing["quartier"].replace("_", " ").capitalize()
    prix_txt = f"{listing['prix']} DH" if listing.get("prix") else "Non precise"
    surface_txt = f"{listing['surface']} m2" if listing.get("surface") else "Non precise"

    description = (listing.get("description") or "").strip()
    if len(description) > 400:
        description = description[:400].rsplit(" ", 1)[0] + "..."
    if not description:
        description = "Non precisee"

    # Caractéristiques générales — toujours affichées, avec "Non precise" pour les champs manquants
    caracteristiques_lines = (
        f"Type de bien: {listing.get('type_bien') or 'Non precise'}\n"
        f"Etat: {listing.get('etat') or 'Non precise'}\n"
        f"Etage: {listing.get('etage') or 'Non precise'}\n"
        f"Anciennete: {listing.get('annee') or 'Non precise'}\n"
        f"Type de sol: {listing.get('type_sol') or 'Non precise'}\n"
        f"Salle(s) de bain: {listing.get('salle_bain') or 'Non precise'}\n"
        f"Equipements: {', '.join(listing.get('caracteristiques', [])) or 'Non precise'}"
    )

    message = (
        f"{tag}\n\n"
        f"📍 Localisation: {quartier_txt}\n"
        f"💰 Prix: {prix_txt}\n"
        f"📐 Surface: {surface_txt}\n\n"
        f"📝 Description:\n{listing['titre']}\n{description}\n\n"
        f"🏗️ Caracteristiques generales:\n{caracteristiques_lines}\n\n"
        f"⚠️ Points a verifier: {reasons_txt}\n\n"
        f"🔗 Voir l'annonce:\n{listing['lien']}"
    )

    url = "https://api.callmebot.com/whatsapp.php"
    params = {
        "phone": WHATSAPP_PHONE,
        "text": message,
        "apikey": WHATSAPP_APIKEY,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"❌ Erreur envoi WhatsApp : {e}")


def send_telegram_alert(listing, evaluation):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram non configuré, alerte ignorée.")
        return

    tag = "🎯 MATCH IDÉAL" if evaluation["strict_match"] else "🔎 À CONSIDÉRER"
    reasons_txt = "\n".join(evaluation["reasons"]) if evaluation["reasons"] else ""

    message = (
        f"{tag} — *{listing['quartier'].replace('_', ' ').capitalize()}*\n\n"
        f"💰 Prix : {listing['prix'] or 'N/A'} DH\n"
        f"📐 Surface : {listing['surface'] or 'N/A'} m²\n"
        f"📍 {listing['titre']}\n"
        f"{reasons_txt}\n\n"
        f"🔗 {listing['lien']}"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"❌ Erreur envoi Telegram : {e}")


# ============ SCRAPING ============
def scrape_quartier(page, quartier, url):
    """Scrape la page Mubawab d'un quartier et normalise prix/surface/extraction."""
    results = []
    page.goto(url, timeout=30000, wait_until="domcontentloaded")
    page.wait_for_timeout(3500)

    for label in ["Accepter", "Accepter tout", "J'accepte", "OK", "Tout accepter"]:
        try:
            page.click(f"text={label}", timeout=1500)
            break
        except Exception:
            continue

    page.wait_for_timeout(1000)

    if os.environ.get("DEBUG_SCRAPER") == "1":
        debug_dir = Path(__file__).parent / "debug"
        debug_dir.mkdir(exist_ok=True)
        page.screenshot(path=str(debug_dir / f"{quartier}.png"), full_page=True)
        (debug_dir / f"{quartier}.html").write_text(page.content(), encoding="utf-8")

    cards = page.query_selector_all(
        "div.listingBox, li.listingBox, div[class*='listing'], "
        "div[class*='adCard'], article"
    )
    print(f"  → {len(cards)} blocs trouvés sur la page ({quartier})")

    seen_urls = set()

    for card in cards:
        try:
            anchors = card.query_selector_all("a")
            lien = None
            for a_el in anchors:
                href = a_el.get_attribute("href")
                if href and re.search(r"/a/\d+/", href):
                    lien = href
                    break
            if not lien:
                continue
            lien = urljoin(page.url, lien)

            if lien in seen_urls:
                continue
            seen_urls.add(lien)

            titre_el = card.query_selector("h2, h3, .listingTit, [class*='title']")
            titre = titre_el.inner_text().strip() if titre_el else "Sans titre"

            desc_el = card.query_selector("[class*='description'], [class*='desc'], p")
            description = desc_el.inner_text().strip() if desc_el else ""

            try:
                card_text = card.inner_text().strip()
            except Exception:
                card_text = f"{titre} {description}"

            image = extract_card_image(card)
            caracteristiques = extract_quick_features(card_text)

            # Exclut les ventes qui apparaissent parfois dans les encarts sponsorisés
            sale_text = f"{titre} {description}".lower()
            if re.search(r"\b(?:à vendre|a vendre|vente)\b", sale_text) and not re.search(r"\b(?:à louer|a louer|location)\b", sale_text):
                continue

            # Exclut le résidentiel (appart/villa/studio) qui remonte parfois via des encarts
            # sponsorisés/recommandés hors catégorie "locaux commerciaux"
            if any(k in sale_text for k in RESIDENTIAL_KEYWORDS) and not any(k in sale_text for k in COMMERCIAL_KEYWORDS):
                continue

            prix = None
            prix_el = card.query_selector("[class*='price'], .priceTag")
            if prix_el:
                prix = parse_price(prix_el.inner_text())
            if prix is None:
                prix = parse_price(card_text)
            if prix is None:
                prix = parse_price(f"{titre} {description}")

            surface = None
            surface_el = card.query_selector("[class*='surface'], .adDetailFeature, [class*='feature']")
            if surface_el:
                surface = parse_surface(surface_el.inner_text())
            if surface is None:
                surface = parse_surface(titre)
            if surface is None:
                surface = parse_surface(description)
            if surface is None:
                surface = parse_surface(card_text)

            activity = detect_activity_status(f"{titre} {description}")
            details = extract_property_details(f"{titre} {description} {card_text}")

            results.append({
                "id": extract_listing_id(lien),
                "ville": "Casablanca",
                "quartier": quartier,
                "titre": titre,
                "description": description,
                "prix": prix,
                "surface": surface,
                "image": image,
                "caracteristiques": caracteristiques,
                "lien": lien,
                **activity,
                **details,
            })

        except Exception as e:
            print(f"  ⚠️ Erreur parsing annonce : {e}")
            continue

    print(f"  → {len(results)} annonces exploitables conservées ({quartier})")
    return results


def evaluate_listing(listing):
    """
    Retourne None si l'annonce est clairement hors critères.
    Sinon retourne strict_match + raisons pour l'alerte.
    """
    prix, surface = listing["prix"], listing["surface"]
    reasons = []
    strict_match = True

    if prix is None:
        strict_match = False
        reasons.append("💰 prix à vérifier")
    else:
        if prix < BUDGET_MIN:
            return None
        if prix > BUDGET_MAX_HARD:
            return None
        if prix > BUDGET_MAX:
            strict_match = False
            reasons.append(f"💰 loyer {prix} DH > {BUDGET_MAX} DH")

    if surface is None:
        strict_match = False
        reasons.append("📐 surface à vérifier")
    else:
        if surface < SURFACE_MIN - SURFACE_TOLERANCE or surface > SURFACE_MAX + SURFACE_TOLERANCE:
            return None
        if surface < SURFACE_MIN or surface > SURFACE_MAX:
            strict_match = False
            reasons.append(f"📐 surface {surface} m² hors cible 80-120 m² (tolérance)")

    if listing.get("extraction_interdite"):
        strict_match = False
        reasons.append("❌ extraction explicitement absente/interdite")
    elif listing.get("gaine_extraction"):
        reasons.append("✅ gaine/extraction mentionnée")
    else:
        strict_match = False
        reasons.append("❓ extraction à confirmer")

    if listing.get("restauration_interdite"):
        strict_match = False
        reasons.append("❌ restauration explicitement interdite")
    elif listing.get("restauration_mentionnee"):
        reasons.append("✅ restauration mentionnée/possible")

    # Fast food = besoin de vitrine/passage piéton -> exclut les étages (sauf rez-de-chaussée)
    etage = (listing.get("etage") or "").lower()
    if etage:
        is_rdc = "rez" in etage
        is_upper_floor = re.search(r"\d", etage) is not None
        if is_upper_floor and not is_rdc:
            return None  # en étage, inutilisable pour un commerce avec vitrine
        if is_rdc:
            reasons.append("✅ rez-de-chaussée")
    else:
        strict_match = False
        reasons.append("❓ étage à confirmer (vitrine/passage requis pour fast food)")

    return {"strict_match": strict_match, "reasons": reasons}


def main():
    seen = load_seen()
    all_listings = []
    new_matches = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ))

        for quartier, url in QUARTIERS.items():
            print(f"Scraping {quartier}...")
            listings = scrape_quartier(page, quartier, url)
            all_listings.extend(listings)
            time.sleep(2)

        browser.close()

    Path(__file__).parent.joinpath("listings.json").write_text(
        json.dumps(all_listings, ensure_ascii=False, indent=2)
    )

    for listing in all_listings:
        if listing["id"] in seen:
            continue
        seen.add(listing["id"])
        evaluation = evaluate_listing(listing)
        if evaluation is not None:
            new_matches.append(listing)
            send_whatsapp_alert(listing, evaluation)
            send_telegram_alert(listing, evaluation)

    save_seen(seen)
    print(f"✅ Terminé. {len(new_matches)} nouvelle(s) annonce(s) matchée(s) sur {len(all_listings)} au total.")


if __name__ == "__main__":
    main()
