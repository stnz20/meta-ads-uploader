#!/usr/bin/env bash
# Cloud-Shell-Setup für den Meta-Ads-Upload (siehe Sicherheits-Review).
#
# Ablauf in Google Cloud Shell (shell.cloud.google.com):
#   1. gh auth login && git clone https://github.com/stnz20/meta-ads-uploader.git && gh auth logout
#   2. .env + credentials.json per Editor-Upload ins Repo-Root legen
#   3. cd meta-ads-uploader && bash cloudshell_run.sh        → Setup + Dry-Run
#   4. tmux new -s upload
#      source venv/bin/activate && python3 main.py 2>&1 | tee upload-run.log
#   5. Nach Abschluss: bash cloudshell_cleanup.sh   → Secrets/Cache entfernen (Pflicht!)
#
# Hinweis: Cloud Shell wird ~20 Min. nach Schließen des Tabs recycelt — tmux
# überlebt das NICHT. Abbruch ist unkritisch: einfach neu starten, DONE-Zeilen
# im Sheet und der Upload-Cache verhindern Doppel-Uploads.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> 1/4 Secrets prüfen"
for f in .env credentials.json; do
    if [[ ! -f "$f" ]]; then
        echo "FEHLER: '$f' fehlt im Repo-Root. Per Cloud-Shell-Editor hochladen (Rechtsklick → Upload Files)." >&2
        exit 1
    fi
    chmod 600 "$f"
done
echo "    .env + credentials.json vorhanden, Rechte auf 600 gesetzt."

echo "==> 2/4 ffmpeg-Libs installieren (für PyAV)"
if ! command -v ffmpeg >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg
else
    echo "    ffmpeg bereits vorhanden."
fi

echo "==> 3/4 Python-Umgebung + Dependencies"
if [[ ! -d venv ]]; then
    python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "==> 4/4 Dry-Run (keine Meta-Calls, keine Sheet-Writes)"
python3 main.py --dry-run

cat <<'EOF'

✅ Setup fertig, Dry-Run beendet — Ausgabe oben prüfen!

Echter Upload (in tmux, damit kurze Verbindungsdrops nicht stören):
    tmux new -s upload
    source venv/bin/activate && python3 main.py 2>&1 | tee upload-run.log

Danach PFLICHT:
    bash cloudshell_cleanup.sh
EOF
