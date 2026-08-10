"""
Scraper mubawab.ma - Commerces à louer
Surveille les annonces sur des quartiers ciblés, filtre selon budget/surface,
et envoie une alerte Telegram pour chaque nouvelle annonce matchée.

Usage: python scraper.py
Variables d'env requises: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import os
import re
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ============ CONFIGURATION ============
# Quartiers ciblés -> slug utilisé dans l'URL mubawab
# ⚠️ Slugs "gauthier", "maarif", "ain-chock", "racine" confirmés existants sur mubawab.
# Les slugs "casa-anfa", "casablanca-finance-city", "bourgogne", "tan-tan", "yacoub-el-mansour"
# sont des suppositions à vérifier (navigue sur mubawab.ma pour confirmer l'URL exacte
# de chaque quartier et corrige ici si besoin — ex: certains quartiers n'ont pas de page
# dédiée et remontent dans la recherche globale Casablanca).
QUARTIERS = {
    "gauthier": "https://www.mubawab.ma/fr/sd/casablanca/gauthier/locaux-a-louer",
    "maarif": "https://www.mubawab.ma/fr/sd/casablanca/ma%C3%A2rif/locaux-a-louer",
    "maarif_extension": "https://www.mubawab.ma/fr/sd/casablanca/ma%C3%A2rif-extension/locaux-a-louer",
    "racine": "https://www.mubawab.ma/fr/sd/casablanca/racine/locaux-a-louer",
    "ain_chock": "https://www.mubawab.ma/fr/sd/casablanca/ain-chock/locaux-a-louer",
    "cfc": "https://www.mubawab.ma/fr/sd/casablanca/casablanca-finance-city/locaux-a-louer",
    "bourgogne_ouest": "https://www.mubawab.ma/fr/sd/casablanca/bourgogne-ouest/locaux-a-louer",
    "bourgogne_est": "https://www.mubawab.ma/fr/sd/casablanca/bourgogne-est/locaux-a-louer",
}

# Critères de filtrage
BUDGET_MIN = 0
BUDGET_MAX = 25000       # en DH/mois — loyer idéal. Au-delà = "à considérer si emplacement fort"
BUDGET_MAX_HARD = 40000  # au-delà de ce seuil, on ignore même les emplacements exceptionnels
SURFACE_MIN = 80
SURFACE_MAX = 120        # en m² — tolérance gérée via SURFACE_TOLERANCE ci-dessous
SURFACE_TOLERANCE = 25   # m² de marge acceptée hors fourchette (signalé comme "hors critère strict")

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

SEEN_FILE = Path(__file__).parent / "seen_listings.json"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# WhatsApp via CallMeBot (gratuit, pas besoin de compte WhatsApp Business)
# Voir README pour l'activation (envoyer un message à un numéro pour obtenir l'apikey)
WHATSAPP_PHONE = os.environ.get("WHATSAPP_PHONE")     # ton numéro, format international sans +, ex: 212612345678
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
                # data-srcset peut contenir plusieurs URLs
                value = value.split(",")[0].strip().split(" ")[0]
                if "mubawab-media.com" in value and "/ad/" in value:
                    return value
        # fallback: image Mubawab même si le chemin ne contient pas /ad/
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


def send_whatsapp_alert(listing, evaluation):
    if not WHATSAPP_PHONE or not WHATSAPP_APIKEY:
        print("⚠️ WhatsApp non configuré, alerte ignorée.")
        return

    tag = "MATCH IDEAL" if evaluation["strict_match"] else "A CONSIDERER"
    reasons_txt = " | ".join(evaluation["reasons"]) if evaluation["reasons"] else ""

    message = (
        f"{tag} - {listing['quartier'].replace('_', ' ').capitalize()}\n"
        f"Prix: {listing['prix'] or 'N/A'} DH | Surface: {listing['surface'] or 'N/A'} m2\n"
        f"{listing['titre']}\n"
        f"{reasons_txt}\n"
        f"{listing['lien']}"
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
            # Cherche de préférence un vrai lien d'annonce /a/
            link_el = card.query_selector("a[href*='/a/']") or card.query_selector("a")
            lien = link_el.get_attribute("href") if link_el else None
            if not lien:
                continue
            if not lien.startswith("http"):
                lien = "https://www.mubawab.ma" + lien

            # Ignore les programmes immobiliers /p/ et les doublons
            if "/a/" not in lien or lien in seen_urls:
                continue
            seen_urls.add(lien)

            titre_el = card.query_selector("h2, h3, .listingTit, [class*='title']")
            titre = titre_el.inner_text().strip() if titre_el else "Sans titre"

            desc_el = card.query_selector("[class*='description'], [class*='desc'], p")
            description = desc_el.inner_text().strip() if desc_el else ""

            # Texte complet du bloc : utile quand Mubawab déplace prix/surface dans son HTML
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
            residential_keywords = ["appartement", "duplex", "studio", "villa", "riad", "chambre à louer", "chambre a louer"]
            commercial_keywords = [
                "local", "commerce", "commercial", "magasin", "bureau", "boutique",
                "restaurant", "snack", "café", "cafe", "showroom", "dépôt", "depot",
                "entrepôt", "entrepot", "rideau", "fonds de commerce"
            ]
            type_text = sale_text
            if any(k in type_text for k in residential_keywords) and not any(k in type_text for k in commercial_keywords):
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
            time.sleep(2)  # politesse entre requêtes

        browser.close()

    # Sauvegarde de toutes les annonces pour la webapp / page de listing
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
