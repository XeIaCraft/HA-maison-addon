## 0.5.0
- Nouveau : carte « Nettoyage ». « 🔍 Chercher ce qui est inutile » liste les helpers `input_boolean.maison_*` orphelins (plus définis nulle part) et les ressources Lovelace jamais utilisées par tes tableaux de bord ; « 🧹 Nettoyer la sélection » les supprime via l'API WebSocket de HA.
- Sécurité : `card-mod` et `browser_mod` ne sont jamais proposées ; les ressources retirées sont notées dans `/data/deleted_resources.json` (URL + type) pour pouvoir les remettre.

## 0.4.2
- Masque les secrets écrits dans une adresse : `?password=…`, `&token=…`, `&email=…`, `rtsp://utilisateur:motdepasse@hôte` (flux caméra go2rtc, Frigate…).

