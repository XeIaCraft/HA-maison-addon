# HA-maison — application Home Assistant

**HA-maison Sync** synchronise ta configuration Home Assistant avec **un dépôt GitHub privé** et applique les modifications
en un clic, avec sauvegarde, contrôle de la config et retour arrière automatique.

- ⬆️ **Envoyer** : copie ta config (sans secrets : mots de passe, jetons et identifiants ne partent jamais) vers ton dépôt privé.
- 👀 / ✅ **Voir / Appliquer** : récupère les modifications du dépôt (thème, dashboards, helpers…) et les applique après validation.
- 🔌 **Brancher dans configuration.yaml** : ajoute seulement les lignes manquantes (aperçu + sauvegarde + retour arrière).
- ↩️ **Annuler** la dernière application.

Ce dépôt ne contient que le code de l'application : **aucune donnée personnelle**. Ta configuration reste dans TON dépôt privé.

## Installation
1. Home Assistant → Paramètres → **Applications** → Boutique → menu **⋮** → **Dépôts** → ajoute :
   `https://github.com/XeIaCraft/HA-maison-addon`
2. Installe **HA-maison Sync**. Dans l'onglet *Configuration*, renseigne `repository` :
   `git@github.com:TON-COMPTE/TON-DEPOT.git`. Démarre l'application et active « Afficher dans la barre latérale ».
3. Ouvre **HA-maison** : copie la **clé publique** affichée dans GitHub → ton dépôt privé → Settings → **Deploy keys**
   (coche **Allow write access**).

## Mises à jour
Quand une nouvelle version sort, Home Assistant affiche un bouton **Mettre à jour** sur l'application.
Active **Mise à jour automatique** dans la page de l'application pour ne plus rien faire.

## Sécurité
L'application demande l'accès en écriture à ta configuration et le rôle `hassio_role: manager` (contrôle de config,
rechargement, redémarrage). Lis `ha_maison_sync/config.yaml` et le code (`ha_maison_sync/app/`) avant d'installer.
Elle n'écrit jamais `secrets.yaml`, refuse les chemins dangereux et les liens symboliques, n'est joignable que via l'interface
de Home Assistant (Ingress), et sauvegarde avant chaque modification.

## Tests
`python tests/run_tests.py` (aucun Home Assistant requis).
