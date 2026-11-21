## 0.4.0
- **Masquage valeur par valeur** : un fichier contenant un mot de passe ou un jeton en clair n'est plus écarté en entier.
  Il est envoyé avec les seules valeurs sensibles remplacées par `<masqué>` (copie lisible dans `exports/`, ex. `exports/configuration.yaml`).
- **Option `full_access`** (désactivée par défaut) : tout le dossier de config en lecture dans `exports/total/`.
- Un fichier contenant `<masqué>` n'est jamais réappliqué à HA.

