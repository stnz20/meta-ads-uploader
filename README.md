# Meta Ads Uploader

Google-Sheet-gesteuerter Bulk-Uploader für Meta Ads. Ein Google Sheet ist die Steuerzentrale, Google Drive die Ablage für Creatives, Meta das Ziel. Das Tool legt Ad Sets und Ads in einem Lauf an, klont Targeting und Creative-Defaults von bestehenden Referenz-Kampagnen, lädt Videos und Bilder aus Drive hoch und schreibt jeden Status zurück ins Sheet.

Entstanden im Performance-Marketing-Team von mammaly. Produktliste, Landingpages und Namenskonvention sind auf diesen Anwendungsfall zugeschnitten und lassen sich in `ad_namer/naming.py` sowie über `.env` anpassen.

## Was das Tool macht

**Bulk-Upload.** Liest die Tabs `ad_sets` und `ads` aus dem Sheet, validiert URLs und Kampagnen, erzeugt Ad Sets und dann Ads (drei parallel) über das `facebook-business` SDK. `DONE` oder `ERROR` plus die neue Meta-ID landen in der Zeile, ein `log`-Tab protokolliert jeden Lauf. Bereits angelegte Ad Sets werden wiederverwendet, sodass sich neue Ads später einreihen können.

**Ad Naming.** Agenturen liefern Creatives mit beliebigen Dateinamen. Das Modul scannt Drive-Ordner, schlägt Namen nach der Team-Konvention vor, benennt nach Freigabe um und füllt den Naming-Tab, aus dem der Upload seine Zeilen baut. Aktualisierte Dateien können bestehende Ads ersetzen.

**Frame.io Import.** Assets aus Frame.io-Share-Links auflisten, auswählen, umbenennen und in Originalqualität direkt in einen Drive-Staging-Ordner übertragen.

**Asset Link Sync.** Findet zu Sheet-Zeilen die passenden Drive-Dateien anhand der Meta-Ad-ID und trägt die Links ein, wenn der Treffer eindeutig ist.

**Dashboard und History.** Verbindungsstatus, Zähler für offene und erledigte Zeilen, Live-Fortschritt während eines Uploads, Historie aller Läufe.

## Ablauf eines Uploads

1. Sheet lesen: `ad_sets` und `ads` parsen und validieren.
2. Referenz klonen: Targeting vom Referenz-Ad-Set, Texte, Headlines, CTA und URL von dessen erster Ad.
3. Assets holen: Drive-Download, Cache, Chunk-Upload zu Meta, Thumbnail aus dem ersten Frame.
4. Anlegen: Ad Sets, dann Ads, mit Backoff und Ratenbegrenzung.
5. Status zurück ins Sheet.

Jeder Lauf beginnt als Dry-Run. Erst wenn Validierung und Vorschau stimmen, geht es gegen die API.

## Setup

```bash
pip install -r requirements.txt      # braucht ffmpeg-Bibliotheken für PyAV (macOS: brew install ffmpeg)
cp .env.example .env                 # Werte eintragen, siehe Kommentare in der Datei
```

Voraussetzungen:

- Eine Meta-Developer-App im **Live-Modus** mit einem langlebigen System-User-Token (`ads_management`, `ads_read`, `business_management`, `pages_show_list`). Schritt-für-Schritt-Anleitung in [META-APP-SETUP.md](META-APP-SETUP.md).
- Ein Google-Service-Account (`credentials.json`, nicht committen) mit Editor-Rechten auf dem Sheet, den Agentur-Ordnern und dem Ablage-Ordner.
- Für den Frame.io-Import zusätzlich ein OAuth-Client und Refresh-Token eines Workspace-Nutzers, weil Service-Accounts keine My-Drive-Quota haben. Erzeugen mit `python3 mint_drive_refresh_token.py`.

## Befehle

| Befehl | Zweck |
|---|---|
| `python3 main.py --dry-run` | Kompletter Durchlauf ohne Meta-Calls und ohne Sheet-Schreibzugriffe. Immer zuerst. |
| `python3 main.py` | Echter Upload: Ad Sets und Ads aus dem Sheet anlegen |
| `python3 main.py --sheet-id <ID>` | `GOOGLE_SHEET_ID` überschreiben |
| `python3 main.py --source-campaign-id / --source-adset-id <ID>` | Referenz-IDs für alle neuen Ad Sets überschreiben |
| `python3 main.py --fetch-meta-config` | Pages, IG-Accounts und Advertiser in den Tab `meta_config` schreiben |
| `streamlit run app.py` | Web-Oberfläche mit allen Modulen |
| `python3 ad_namer.py analyze <ordner-url> --creator <name>` dann `python3 ad_namer.py execute` | Ad Naming per CLI, zweistufig |

Für gehostete Läufe liegen `cloudshell_run.sh` und `cloudshell_cleanup.sh` bei (Google Cloud Shell), außerdem eine Devcontainer-Konfiguration.

## Namenskonvention

```
Ad:     {meta_id}_{format}_{produkt}_{beschreibung}_{creator}_{version}
Ad Set: {YYYYMMDD}_{meta_id}_{format}_{produkt}_{beschreibung}_{creator}_{lp_id}
```

Agentur-Kürzel werden über `PRODUCT_ABBREVIATIONS` auf Produktnamen abgebildet, Landingpage-URLs und -IDs pro Produkt liegen in `LP_DEFAULTS`. Versionen desselben Konzepts teilen sich den Ad-Set-Namen der ersten Version.

## Schutzmechanismen

- Dry-Run zuerst, ohne API-Calls und Sheet-Schreibzugriffe.
- Das Sheet ist der Zustandsspeicher: `status` und `created_id` entscheiden, was verarbeitet wird. Abgebrochene Läufe lassen sich fortsetzen.
- Downloads und Uploads werden pro Drive-Datei in `cache/` gecacht, erledigte Ad Sets nicht doppelt erzeugt.
- Pause zwischen API-Aufrufen, maximal drei parallele Ad-Uploads, Retry mit Backoff.
- Tokens werden in jeder Fehlermeldung und jedem Log maskiert (`config.mask_token()`).

## Architektur

- `main.py` CLI-Einstieg, `uploader.py` Orchestrierung.
- `sheets/reader.py` Zeilen-Parsing und Validierung.
- `meta/` SDK-Wrapper für Ad Sets, Ads, Kampagnen, Creative-Defaults, Asset-Upload, Config- und Referenz-Abfragen.
- `ad_namer/` Drive-Operationen, Naming-Logik, Batch-Vorbereitung (Python-Port von `BatchUpload.gs`).
- `frameio/` Client für Frame.io-Share-Links.
- `auth/` Meta-SDK-Init und Google-Auth.
- `pages/`, `components/` Streamlit-Oberfläche.
- `BatchUpload.gs` Apps Script, das im Google Sheet selbst läuft. Bei Änderungen am Tab- oder Spaltenlayout mit `batch_prepare.py` synchron halten.

Details für die Arbeit mit KI-Coding-Assistenten stehen in [CLAUDE.md](CLAUDE.md) und [AGENTS.md](AGENTS.md).
