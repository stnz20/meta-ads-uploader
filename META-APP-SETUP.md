# Meta App Setup — Schritt-für-Schritt

> **Wichtigster Hinweis vorab:** Die App muss im **Live-Modus** sein.
> Im Entwickler-Modus können KEINE echten Ads erstellt werden.
> Das ist der häufigste Fehler.

---

## 1. Meta Developer App erstellen

1. Gehe zu [developers.facebook.com](https://developers.facebook.com)
2. Klicke oben rechts auf **Meine Apps → App erstellen**
3. Wähle als App-Typ: **Business**
4. Gib einen App-Namen ein (z.B. „Ads Uploader Internal")
5. Verknüpfe die App mit deinem **Business Manager**

---

## 2. Use Case hinzufügen

1. In deiner App → **Use Cases** (linke Sidebar)
2. Klicke auf **Add use case**
3. Wähle: **Create and manage ads**
4. Bestätige mit **Next**

---

## 3. App veröffentlichen (Live-Modus) ⚠️

1. In deiner App → oben rechts siehst du den Toggle **In Entwicklung**
2. Klicke darauf und wechsle zu **Live**
3. Du wirst nach einer **Datenschutz-URL** gefragt — ein öffentliches Google Doc reicht aus
4. Bestätige den Wechsel

**Ohne Live-Modus funktionieren API-Calls zum Erstellen von Ads nicht.**

---

## 4. System User Token erstellen

Normale User-Tokens laufen nach 60 Tagen ab. Für Automatisierungen brauchst du einen **System User Token** ohne Ablaufdatum.

1. Gehe zu [business.facebook.com](https://business.facebook.com)
2. **Einstellungen** → **Benutzer** → **Systembenutzer**
3. Klicke auf **Systembenutzer hinzufügen**
   - Name: z.B. „Ads Uploader Bot"
   - Rolle: **Admin**
4. Klicke auf den neuen Systembenutzer → **Assets hinzufügen**
   - Füge dein Ad Account hinzu (Rolle: Advertiser)
   - Füge deine Facebook Page hinzu (Rolle: Advertiser)
5. Klicke auf **Token generieren**
   - Wähle deine App aus
   - Aktiviere diese Berechtigungen:
     - `ads_management`
     - `ads_read`
     - `business_management`
     - `pages_show_list`
6. Kopiere den Token → in `.env` als `META_ACCESS_TOKEN` eintragen

---

## 5. Ad Account ID und Page ID finden

**Ad Account ID:**
- [business.facebook.com](https://business.facebook.com) → Einstellungen → Ad Accounts
- Format: `act_123456789` — das `act_`-Präfix ist Pflicht in `.env`

**Page ID:**
- Gehe zu deiner Facebook Page
- **Über diese Seite** → ganz unten: „Seiten-ID"
- Alternativ: [facebook.com/your-page-name/about](https://facebook.com)

---

## 6. Google Service Account einrichten

1. Gehe zur [Google Cloud Console](https://console.cloud.google.com)
2. Projekt erstellen (oder bestehendes wählen)
3. **APIs & Dienste → Bibliothek**
   - Aktiviere **Google Sheets API**
   - Aktiviere **Google Drive API**
4. **APIs & Dienste → Anmeldedaten → Dienstkonto erstellen**
   - Name: z.B. „Ads Uploader"
   - Rolle: kein Projekt-Zugriff nötig (leer lassen)
5. Dienstkonto öffnen → **Schlüssel** → **Schlüssel hinzufügen → JSON**
   - Die heruntergeladene Datei → umbenennen in `credentials.json` → ins Projektverzeichnis legen
   - `credentials.json` ist in `.gitignore` — NIEMALS committen!
6. Die E-Mail-Adresse des Dienstkontos (endet auf `@...iam.gserviceaccount.com`) kopieren
7. Google Sheet öffnen → **Teilen** → diese E-Mail-Adresse als **Bearbeiter** hinzufügen

---

## 7. Google Sheet vorbereiten

Erstelle ein Sheet mit diesen drei Tabs:

### Tab `ad_sets`
| source_campaign_id | ad_set_name | daily_budget | start_time | end_time | targeting_override | status | created_id |
|---|---|---|---|---|---|---|---|
| 123456789 | Summer Sale DE | 1000 | 2026-05-01T00:00:00+02:00 | | | | |

- `daily_budget`: in Cent (1000 = 10 €)
- `start_time`/`end_time`: ISO-8601 Format
- `targeting_override`: JSON-String (optional, überschreibt Quell-Targeting)
- `status` + `created_id`: leer lassen — wird automatisch befüllt

### Tab `ads`
| ad_set_ref | ad_name | image_url | image_hash | video_id | body | body_2 | body_3 | headline | headline_2 | description | cta | destination_url | display_link | status | created_id |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

- `ad_set_ref`: muss dem `ad_set_name` aus Tab `ad_sets` exakt entsprechen
- **Asset**: genau eines von `image_url`, `image_hash` oder `video_id` setzen
  - `image_url`: URL zu einem Bild — nur Einzel-Variation möglich
  - `image_hash`: Meta Image Hash (nach Upload via API) — Multi-Variation möglich
  - `video_id`: Meta Video ID — Multi-Variation möglich
- `body_2`…`body_5`, `headline_2`…`headline_3`: optionale Zusatz-Variationen (Meta rotiert automatisch)

### Tab `log`
| timestamp | action | entity_id | result |
|---|---|---|---|

Leer lassen — wird automatisch vom Uploader befüllt.

---

## 8. Ersten Lauf testen

```bash
# Abhängigkeiten installieren
pip install -r requirements.txt

# .env anlegen
cp .env.example .env
# → Alle Werte in .env eintragen

# Erst Dry-Run (keine echten API-Calls)
python main.py --dry-run

# Dann echter Lauf
python main.py
```

---

## Häufige Fehler

| Fehler | Ursache | Lösung |
|---|---|---|
| `(#200) The user hasn't authorized the application` | App im Entwickler-Modus | App auf Live-Modus umstellen (Schritt 3) |
| `Invalid OAuth access token` | Token abgelaufen oder falsch | System User Token neu generieren |
| `Unsupported image type` | WebP-Bild hochgeladen | Bild vorher in JPG/PNG konvertieren |
| `Ad account is not authorized` | System User hat keinen Zugriff auf Ad Account | Assets in Business Manager zuweisen (Schritt 4) |
| `Page not found` | Falsche Page ID oder kein Zugriff | Page ID prüfen, System User Zugriff auf Page geben |
