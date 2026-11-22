# HA-maison Sync

**Première fois :** dans l'onglet *Configuration*, renseigne `repository` avec l'URL SSH de ton dépôt de config (`git@github.com:TON-COMPTE/TON-DEPOT.git`), puis démarre l'application.

1. Copie la **clé publique** affichée dans l'interface vers GitHub → dépôt → Settings → Deploy keys (**Allow write access**).
2. **⬆️ Envoyer ma config vers GitHub** : met à jour le dépôt avec ta config (sans secrets) pour que Claude la lise.
3. **👀 Voir les modifications** : liste des fichiers que « Appliquer » ajouterait / modifierait / supprimerait, avec les différences.
4. **✅ Appliquer à mon HA** : validation, sauvegarde, copie, contrôle de la config, rechargement. Retour arrière automatique en cas d'erreur.
5. **🔌 Brancher dans configuration.yaml** (une fois) : ajoute les lignes manquantes (packages, themes, dashboards Maison) après aperçu, sauvegarde et contrôle ; retour arrière automatique si HA refuse.
6. **↩️ Annuler la dernière application** : restaure la sauvegarde.

## Options
| Option | Rôle |
|---|---|
| `repository` | URL SSH du dépôt (`git@github.com:XeIaCraft/HA-maison.git`) |
| `branch` | Branche suivie (`main`) |
| `apply_paths` | Fichiers/dossiers applicables à HA (jamais `secrets.yaml`, `configuration.yaml`, `.storage`) |
| `export_paths` | Fichiers/dossiers envoyés vers GitHub |
| `export_full` | Envoyer aussi registres, helpers, journal d'erreurs masqué, add-ons/extensions installés, noms des secrets (jamais d'identifiants) |
| `full_access` | **Accès total** (désactivé par défaut) : envoie en lecture seule, dans `exports/total/`, tous les fichiers texte/JSON de ta config et tout `.storage`, coordonnées comprises. Les VALEURS de mots de passe / jetons sont masquées (`<masqué>`). Jamais envoyés : jetons d'authentification de HA et du cloud (`.storage/auth*`, `cloud`, `onboarding`, `mobile_app`), valeurs de `secrets.yaml`, bases de données |
| `restart_after_apply` | Redémarrer HA après chaque application |

## Nettoyage
Carte « Nettoyage » : détecte les helpers `input_boolean.maison_*` orphelins et les ressources Lovelace inutilisées (ex. apexcharts-card), puis les supprime après ta confirmation (cases à cocher). `card-mod` et `browser_mod` sont protégées. Les ressources retirées sont journalisées dans `/data/deleted_resources.json`. Ferme et rouvre l'application HA ensuite.
