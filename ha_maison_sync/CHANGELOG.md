## 0.8.0
- « Envoyer » exporte aussi l'historique des entités listées dans `observation.json` du dépôt (par défaut 10 jours) : `exports/history/observation_5min.csv` (une ligne toutes les 5 min) et `exports/history/observation_evenements.csv` (changements de mode, marche/arrêt…). Lecture seule, uniquement les entités listées ; sert à observer la chaudière et le chauffage.

## 0.7.0
- « Appliquer » désactive aussi les entités listées dans `cleanup.json` (`disable_entities` : identifiants exacts ou `re:expression`), par exemple les réglages modifiables inutiles d'ebusd. Réversible (réactive l'entité dans HA). Garde-fou : jamais plus de 400 entités d'un coup. La carte « Nettoyage » propose la même désactivation.

## 0.6.0
- « Appliquer » supprime maintenant tout seul les anciennes automatisations de l'interface que le dépôt remplace (fichier `cleanup.json` du dépôt). Garde-fous : la remplaçante doit être chargée dans HA ; une copie de l'ancienne est gardée dans `/data/deleted_automations.json`. La carte « Nettoyage » propose aussi cette suppression.
- Export : le journal d'erreurs essaie plusieurs sources (fichier, `core/logs/latest`, `core/logs`), retire les codes couleur, et indique la raison dans le journal d'envoi s'il reste introuvable.

## 0.5.0
- Nouveau : carte « Nettoyage ». « 🔍 Chercher ce qui est inutile » liste les helpers `input_boolean.maison_*` orphelins (plus définis nulle part) et les ressources Lovelace jamais utilisées par tes tableaux de bord ; « 🧹 Nettoyer la sélection » les supprime via l'API WebSocket de HA.
- Sécurité : `card-mod` et `browser_mod` ne sont jamais proposées ; les ressources retirées sont notées dans `/data/deleted_resources.json` (URL + type) pour pouvoir les remettre.

## 0.4.2
- Masque les secrets écrits dans une adresse : `?password=…`, `&token=…`, `&email=…`, `rtsp://utilisateur:motdepasse@hôte` (flux caméra go2rtc, Frigate…).

