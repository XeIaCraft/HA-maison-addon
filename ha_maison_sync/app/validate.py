# COPIE de scripts/validate.py (l'add-on exécute SA copie, jamais un script venu du dépôt). Garder identique (vérifié par la CI).
#!/usr/bin/env python3
"""Valide le dépôt : YAML correct, thème clair/sombre, dashboards cohérents.
Usage : python scripts/validate.py [racine_du_dépôt]   (aucune dépendance HA requise)"""
import pathlib, sys, yaml

ROOT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent.parent
errors, warnings = [], []

class Loader(yaml.SafeLoader):
    pass
for tag in ("!secret", "!include", "!include_dir_named", "!include_dir_merge_named",
            "!include_dir_list", "!include_dir_merge_list", "!env_var", "!input"):
    Loader.add_constructor(tag, lambda l, n: l.construct_scalar(n))

def load(path):
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=Loader)
    except Exception as e:  # noqa: BLE001
        errors.append(f"{path.relative_to(ROOT)} : YAML invalide ({e})")

def walk(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from walk(v)

# 1) packages : helpers disponibles
helpers = {}
for f in sorted((ROOT / "packages").glob("*.yaml")):
    d = load(f) or {}
    for name, cfg in (d.get("input_select") or {}).items():
        helpers[f"input_select.{name}"] = cfg.get("options", [])

# 2) thèmes
for f in sorted((ROOT / "themes").glob("*/*.yaml")):
    d = load(f)
    if not isinstance(d, dict):
        continue
    for name, theme in d.items():
        if name != "Maison":      # les autres thèmes installés ne sont pas les nôtres
            continue
        modes = (theme or {}).get("modes", {})
        if not {"light", "dark"} <= set(modes):
            warnings.append(f"thème {name} : il manque le mode light ou dark")

# 3) dashboards
for f in sorted((ROOT / "dashboards").glob("*.yaml")):
    d = load(f)
    if not isinstance(d, dict):
        continue
    rel = f.relative_to(ROOT)
    templates = set((d.get("button_card_templates") or {}))
    paths = [v.get("path") for v in d.get("views", [])]
    if len(paths) != len(set(paths)):
        errors.append(f"{rel} : chemins de vues en double")
    for n in walk(d.get("views", [])):
        if n.get("type") == "custom:button-card" and n.get("template"):
            for t in ([n["template"]] if isinstance(n["template"], str) else n["template"]):
                if t not in templates:
                    errors.append(f"{rel} : template button-card inconnu « {t} »")
        if n.get("type") == "conditional":
            for c in n.get("conditions", []):
                e, s = c.get("entity", ""), c.get("state")
                if e.startswith("input_select.tab"):
                    if e not in helpers:
                        errors.append(f"{rel} : helper manquant {e}")
                    elif s not in helpers[e]:
                        errors.append(f"{rel} : option « {s} » absente de {e}")
        if n.get("template") == "tab":
            opt = (n.get("variables") or {}).get("opt")
            sent = ((n.get("tap_action") or {}).get("data") or {}).get("option")
            if opt != sent:
                errors.append(f"{rel} : pilule « {n.get('name')} » : opt={opt!r} ≠ option envoyée {sent!r}")

for w in warnings:
    print("⚠️ ", w)
for e in errors:
    print("❌", e)
print(f"\n{len(errors)} erreur(s), {len(warnings)} avertissement(s)")
sys.exit(1 if errors else 0)
