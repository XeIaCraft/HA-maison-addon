#!/usr/bin/with-contenv bashio
# Lance l'interface (Ingress) de l'add-on.
bashio::log.info "HA-maison Sync démarre (interface dans la barre latérale « HA-maison »)."
exec python3 /app/server.py
