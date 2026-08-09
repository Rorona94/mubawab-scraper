# Scraper mubawab.ma — Locaux commerciaux Casablanca

Surveille automatiquement les annonces de commerces à louer sur Gauthier et Maârif,
et envoie une alerte Telegram pour chaque nouvelle annonce. Gratuit, tourne sur GitHub Actions.

## Mise en place (10-15 min)

### 1. Créer le bot Telegram (optionnel, si tu veux les deux canaux)
1. Ouvre Telegram, cherche **@BotFather**
2. Envoie `/newbot`, suis les instructions, donne un nom à ton bot
3. Récupère le **token** qu'il te donne (ressemble à `123456:ABC-DEF...`)
4. Envoie un message à ton nouveau bot (n'importe quoi)
5. Va sur `https://api.telegram.org/bot<TON_TOKEN>/getUpdates` dans ton navigateur
6. Récupère ton **chat_id** dans le JSON retourné (`"chat":{"id": XXXXXXXX}`)

### 1bis. Activer les notifications WhatsApp (gratuit, via CallMeBot)
1. Ajoute ce contact à tes contacts WhatsApp : **+34 644 59 71 67**
2. Envoie-lui ce message exact : `I allow callmebot to send me messages`
3. Tu reçois une réponse avec ton **apikey** (un nombre)
4. Note aussi ton numéro de téléphone au format international sans le `+` (ex: `212612345678`)

### 2. Créer le repo GitHub
1. Crée un nouveau repo GitHub (peut être privé)
2. Pousse tous les fichiers de ce dossier dedans
3. Va dans **Settings > Secrets and variables > Actions**, ajoute :
   - `TELEGRAM_BOT_TOKEN` = ton token (si utilisé)
   - `TELEGRAM_CHAT_ID` = ton chat_id (si utilisé)
   - `WHATSAPP_PHONE` = ton numéro (ex: `212612345678`)
   - `WHATSAPP_APIKEY` = ton apikey CallMeBot

### 3. Activer GitHub Pages (pour la liste web)
1. **Settings > Pages**
2. Source : branche `main`, dossier `/ (root)`
3. Ta liste sera visible sur `https://<ton-user>.github.io/<repo>/`

### 4. Lancer un premier test manuel
1. Onglet **Actions** du repo
2. Sélectionne le workflow "Scrape mubawab"
3. Clique **Run workflow**

## ⚠️ Important — à vérifier au premier run

Le script utilise des sélecteurs CSS basés sur la structure standard de mubawab.ma,
mais je n'ai pas pu tester en conditions réelles (pas d'accès au site depuis mon environnement).

Si le premier run remonte 0 annonce ou des données vides :
1. Ouvre `scraper.py`
2. Regarde la fonction `scrape_quartier`
3. Il faudra probablement ajuster les sélecteurs `card.query_selector(...)` en inspectant
   le HTML réel de la page (clic droit > Inspecter sur mubawab.ma)

Dis-moi ce que tu observes et je corrige les sélecteurs avec toi.

## Filtres sur la webapp (index.html)
Une fois ouverte, la page affiche toutes les annonces scrapées avec des filtres 100% modifiables :
- prix min/max, surface min/max (champs libres)
- quartiers (clic pour activer/désactiver, plusieurs possibles)
- extraction (toutes / avec gaine confirmée / restauration mentionnée)

Ces filtres sont côté navigateur uniquement — ils n'affectent pas ce que le scraper récupère
(le scraper remonte tout ce qu'il trouve dans les quartiers ciblés, la webapp te permet ensuite
de trier comme tu veux, à volonté, sans toucher au code).

## Modifier les critères d'alerte (WhatsApp/Telegram)
Dans `scraper.py`, en haut du fichier :
- `QUARTIERS` — ajouter/retirer des quartiers (Aïn Chock, Californie, Corniche...)
- `BUDGET_MIN` / `BUDGET_MAX` — fourchette de loyer en DH
- `SURFACE_MIN` / `SURFACE_MAX` — fourchette de surface en m²

## Fréquence de scraping
Dans `.github/workflows/scrape.yml`, la ligne `cron: "0 */3 * * *"` = toutes les 3h.
Modifiable librement (attention : trop fréquent peut se faire bloquer par le site).
