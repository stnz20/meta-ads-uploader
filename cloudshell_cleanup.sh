#!/usr/bin/env bash
# Entfernt Secrets, Cache und Logs aus der Cloud Shell nach dem Upload-Lauf.
# PFLICHT nach jedem Cloud-Shell-Einsatz — das Home-Verzeichnis persistiert
# zwischen Sessions (bis zu 120 Tage), sonst liegt das Meta-Token dort weiter.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Secrets löschen"
rm -f .env credentials.json

echo "==> Cache + Logs löschen"
rm -rf cache
rm -f ./*.log

echo "==> GitHub-Token invalidieren (falls noch eingeloggt)"
# Nur in Cloud Shell ausloggen (CLOUD_SHELL=true wird dort von Google gesetzt) —
# ein lokaler Testlauf darf das gh-Login auf dem Mac nicht zerstören.
if [[ "${CLOUD_SHELL:-}" != "true" ]]; then
    echo "    nicht in Cloud Shell — gh-Login bleibt unangetastet."
elif command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    gh auth logout --hostname github.com >/dev/null 2>&1 || true
    echo "    gh auth logout ausgeführt."
else
    echo "    kein aktives gh-Login — ok."
fi

echo "==> Verifikation"
leftover=0
for f in .env credentials.json; do
    if [[ -e "$f" ]]; then
        echo "    ⚠️  '$f' existiert noch!" >&2
        leftover=1
    fi
done
if [[ -d cache ]]; then
    echo "    ⚠️  'cache/' existiert noch!" >&2
    leftover=1
fi
if compgen -G "./*.log" >/dev/null; then
    echo "    ⚠️  Log-Dateien existieren noch!" >&2
    leftover=1
fi

if [[ $leftover -eq 0 ]]; then
    echo "✅ Cleanup vollständig — keine Secrets, kein Cache, keine Logs mehr im Repo-Verzeichnis."
else
    echo "❌ Cleanup unvollständig — Reste oben manuell entfernen!" >&2
    exit 1
fi
