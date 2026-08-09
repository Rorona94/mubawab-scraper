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
    "gauthier": "https://www.mubawab.ma/fr/sd/casablanca/gauthier/commerces-a-louer",
    "maarif": "https://www.mubawab.ma/fr/sd/casablanca/ma%C3%A2rif/commerces-a-louer",
    "ain_chock": "https://www.mubawab.ma/fr/sd/casablanca/ain-chock/commerces-a-louer",
    "racine": "https://www.mubawab.ma/fr/sd/casablanca/racine/commerces-a-louer",
    "casa_anfa": "https://www.mubawab.ma/fr/sd/casablanca/casa-anfa/commerces-a-louer",
    "cfc": "https://www.mubawab.ma/fr/sd/casablanca/casablanca-finance-city/commerces-a-louer",
    "bourgogne": "https://www.mubawab.ma/fr/sd/casablanca/bourgogne/commerces-a-louer",
    "yacoub_el_mansour": "https://www.mubawab.ma/fr/sd/casablanca/yacoub-el-mansour/commerces-a-louer",
}

# Critères de filtrage
BUDGET_MIN = 0
BUDGET_MAX = 25000       # en DH/mois — loyer idéal. Au-delà = "à considérer si emplacement fort"
BUDGET_MAX_HARD = 40000  # au-delà de ce seuil, on ignore même les emplacements exceptionnels
SURFACE_MIN = 80
SURFACE_MAX = 120        # en m² — tolérance gérée via SURFACE_TOLERANCE ci-dessous
SURFACE_TOLERANCE = 25   # m² de marge acceptée hors fourchette (signalé comme "hors critère strict")

# Mots-clés indiquant la présence (ou l'absence) d'une gaine d'extraction / autorisation restauration
EXTRACTION_KEYWORDS = ["gaine", "extraction", "hotte", "conduit"]
RESTAURATION_KEYWORDS = ["restauration autorisée", "restaurant", "usage commerce alimentaire"]

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


def parse_price(text):
    """Extrait un nombre en DH depuis un texte type '12 500 DH'."""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def parse_surface(text):
    """Extrait un nombre en m² depuis un texte type '85 m²'."""
    if not text:
        return None
    match = re.search(r"(\d+)\s*m", text)
    return int(match.group(1)) if match else None


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
    """
    Scrape une page de résultats mubawab pour un quartier donné.
    NOTE: les sélecteurs CSS ci-dessous sont basés sur la structure standard
    des annonces mubawab. À vérifier/ajuster après un premier run réel
    (structure du site pouvant changer).
    """
    results = []
    page.goto(url, timeout=30000)
    page.wait_for_timeout(3000)  # laisse le JS charger le contenu

    # Cookies / popup éventuels
    for label in ["Accepter", "Accepter tout", "J'accepte", "OK", "Tout accepter"]:
        try:
            page.click(f"text={label}", timeout=2000)
            break
        except Exception:
            continue

    page.wait_for_timeout(1500)

    # Debug GitHub Actions : capture PNG + HTML réellement reçus
    if os.environ.get("DEBUG_SCRAPER") == "1":
        debug_dir = Path(__file__).parent / "debug"
        debug_dir.mkdir(exist_ok=True)
        page.screenshot(path=str(debug_dir / f"{quartier}.png"), full_page=True)
        (debug_dir / f"{quartier}.html").write_text(
            page.content(),
            encoding="utf-8"
        )

    cards = page.query_selector_all("div.listingBox, li.listingBox, div[class*='listing']")
    print(f"  → {len(cards)} annonces trouvées sur la page ({quartier})")

    for card in cards:
        try:
            link_el = card.query_selector("a")
            lien = link_el.get_attribute("href") if link_el else None
            if lien and not lien.startswith("http"):
                lien = "https://www.mubawab.ma" + lien

            titre_el = card.query_selector("h2, h3, .listingTit")
            titre = titre_el.inner_text().strip() if titre_el else "Sans titre"

            prix_el = card.query_selector("[class*='price'], .priceTag")
            prix = parse_price(prix_el.inner_text()) if prix_el else None

            surface_el = card.query_selector("[class*='surface'], .adDetailFeature")
            surface = parse_surface(surface_el.inner_text()) if surface_el else None

            desc_el = card.query_selector("[class*='description'], p")
            description = desc_el.inner_text().strip() if desc_el else ""

            listing_id = re.sub(r"\D", "", lien.split("/")[-1]) if lien else titre

            if not lien:
                continue

            texte_complet = f"{titre} {description}".lower()
            has_extraction = any(kw in texte_complet for kw in EXTRACTION_KEYWORDS)
            has_restauration_mention = any(kw in texte_complet for kw in RESTAURATION_KEYWORDS)

            results.append({
                "id": listing_id,
                "quartier": quartier,
                "titre": titre,
                "description": description,
                "prix": prix,
                "surface": surface,
                "lien": lien,
                "gaine_extraction": has_extraction,
                "restauration_mentionnee": has_restauration_mention,
            })
        except Exception as e:
            print(f"  ⚠️ Erreur parsing annonce : {e}")
            continue

    return results


def evaluate_listing(listing):
    """
    Retourne None si l'annonce est hors critères (à ignorer),
    ou un dict {strict_match, reasons} si elle mérite une alerte.
    - strict_match=True  -> coche toutes les cases idéales
    - strict_match=False -> hors fourchette idéale mais assez proche pour être signalée
      (ex: loyer > 25000 DH ou surface hors 80-120 m² avec tolérance)
    """
    prix, surface = listing["prix"], listing["surface"]
    reasons = []
    strict_match = True

    if prix is not None:
        if prix > BUDGET_MAX_HARD:
            return None  # trop cher, même en cas d'opportunité exceptionnelle
        if prix > BUDGET_MAX:
            strict_match = False
            reasons.append(f"💰 loyer {prix} DH > {BUDGET_MAX} DH (à valider si emplacement très fort)")

    if surface is not None:
        if surface < SURFACE_MIN - SURFACE_TOLERANCE or surface > SURFACE_MAX + SURFACE_TOLERANCE:
            return None  # trop loin de la cible, on ignore
        if surface < SURFACE_MIN or surface > SURFACE_MAX:
            strict_match = False
            reasons.append(f"📐 surface {surface} m² hors fourchette 80-120 m² (tolérance)")

    if listing["gaine_extraction"]:
        reasons.append("✅ gaine d'extraction mentionnée")
    elif listing["restauration_mentionnee"]:
        reasons.append("⚠️ restauration mentionnée, extraction à confirmer")

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
