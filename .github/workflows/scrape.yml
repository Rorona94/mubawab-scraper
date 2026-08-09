results = []
    page.goto(url, timeout=30000)
    page.wait_for_timeout(3000)  # laisse le JS charger le contenu

    # Cookies / popup éventuels
    try:
        page.click("text=Accepter", timeout=3000)
    except Exception:
        pass

    cards = page.query_selector_all("div.listingBox, li.listingBox, div[class*='listing']")
    print(f"  → {len(cards)} annonces trouvées sur la page ({quartier})")
