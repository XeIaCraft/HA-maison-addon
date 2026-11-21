#!/usr/bin/env python3
"""Tests de l'application HA-maison Sync (aucun Home Assistant requis) : python tests/run_tests.py"""
import json, os, pathlib, subprocess, sys, tempfile
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ha_maison_sync" / "app"))
fails = []


def ok(c, m):
    print("OK  " if c else "FAIL", m)
    if not c:
        fails.append(m)


def sh(*a, cwd=None):
    subprocess.run(a, cwd=cwd, check=True, capture_output=True)


# ───────── 1) branchement de configuration.yaml ─────────
tmp = pathlib.Path(tempfile.mkdtemp())
os.environ.update(HA_MAISON_DATA=str(tmp / "data"), HA_MAISON_CONFIG=str(tmp / "cfg"), HA_MAISON_OPTIONS=str(tmp / "options.json"))
(tmp / "cfg" / ".storage").mkdir(parents=True)
import sync  # noqa: E402


def load(t):
    return yaml.load(t, Loader=sync._CfgLoader)


def full(d):
    return ("packages" in (d.get("homeassistant") or {}) and "themes" in (d.get("frontend") or {})
            and all(n in ((d.get("lovelace") or {}).get("dashboards") or {}) for n in sync.WIRE_DASHBOARDS))


default = "default_config:\n\nfrontend:\n  themes: !include_dir_merge_named themes\n\nautomation: !include automations.yaml\n"
new, add, man = sync.plan_wiring(default)
ok(full(load(new)) and len(add) == 2 and not man and new.startswith(default.rstrip("\n")), "config par défaut : 2 ajouts, contenu conservé")
ok(sync.plan_wiring(new)[1] == [], "idempotent")
new, add, man = sync.plan_wiring("")
ok(full(load(new)) and len(add) == 3, "fichier vide : 3 blocs")
new, add, man = sync.plan_wiring("homeassistant:\n    name: Maison   # c\n    time_zone: Europe/Brussels\n")
ok("    packages: !include_dir_named packages" in new and "# c" in new and full(load(new)), "bloc existant : indentation + commentaires conservés")
new, add, man = sync.plan_wiring("lovelace:\n  mode: storage\n  resources:\n    - url: /x.js\n      type: module\n")
d = load(new)
ok(full(d) and len(d["lovelace"]["resources"]) == 1, "lovelace existant : resources intactes")
new, add, man = sync.plan_wiring("frontend: !include frontend.yaml\n")
ok(any("frontend" in m for m in man) and "frontend: !include frontend.yaml" in new, "include : consigne manuelle")
new, add, man = sync.plan_wiring("homeassistant:\n  packages:\n    p:\n      sensor: []\n")
ok(any("packages" in m for m in man) and "sensor: []" in new, "packages personnalisés : rien écrasé")
ok(sync.plan_wiring("homeassistant: [oups\n") == ("homeassistant: [oups\n", [], sync.plan_wiring("homeassistant: [oups\n")[2]), "fichier illisible : inchangé")
ok("\n" not in sync.plan_wiring("default_config:\r\n")[0].replace("\r\n", ""), "CRLF conservé")

# ───────── 2) envoi / application / retour arrière / sécurité ─────────
cfg, remote = tmp / "cfg", tmp / "remote.git"
(cfg / "configuration.yaml").write_text("homeassistant:\n  latitude: 50.1\n  longitude: 4.6\n  external_url: https://x.example\nmqtt:\n  broker: 10.0.0.5\n  password: hunter2\n  username: bob\n")
(cfg / "secrets.yaml").write_text("api_key: SUPERSECRET123\n")
(cfg / "automations.yaml").write_text("- alias: test\n  trigger: []\n  action: []\n")
(cfg / "scripts.yaml").write_text("s:\n  sequence:\n    - data:\n        api_key: sk-abcdefghijklmnopqrstuvwxyz123456\n")
(cfg / ".storage" / "lovelace.x").write_text('{"data":{"config":{"views":[]}}}')
(cfg / ".storage" / "core.config_entries").write_text('{"data":{"entries":[{"domain":"template","title":"Hum abs","data":{"api_key":"TOPSECRETKEY"},"options":{"state":"{{ states(\'sensor.a\') }}","token":{"access_token":"ATOK123456789"}}}]}}')
(cfg / ".storage" / "auth").write_text('{"data":{"refresh_tokens":[{"token":"REFRESHTOKEN-ADMIN"}]}}')
(cfg / ".storage" / "cloud").write_text('{"data":{"cloudhook":"CLOUDSECRET"}}')
(cfg / "zigbee2mqtt").mkdir()
(cfg / "zigbee2mqtt" / "coordinator_backup.json").write_text('{"pan_id":"A1B2","extended_pan_id":"DEADBEEF","network_key":{"key":[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16],"frame_counter":99},"channel":11}')
(cfg / "esphome").mkdir()
(cfg / "esphome" / "det.yaml").write_text("api:\n  encryption:\n    key: Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MDEyMzQ1Njc4OTA=\nwifi:\n  ssid: Maison\n  psk: wifipass123\n")
(cfg / "go2rtc.yaml").write_text("streams:\n  CamArriere:\n    - tuya://protect-eu.example.me?device_id=abc123&email=bob%40example.com&password=SuperPwd99\n  Entree:\n    - rtsp://admin:camPass12@192.168.1.9:554/stream1\n")
(cfg / "custom").mkdir()
(cfg / "custom" / "extra.yaml").write_text("sensor:\n  - name: X\n    password: p4ss\n    value: 42\n")
(cfg / "home-assistant_v2.db").write_text("DBDATA")
seed = tmp / "seed"
for f, t in {"themes/maison/maison.yaml": "Maison:\n  ha-card-border-radius: 24px\n  modes:\n    light: {primary-color: '#fff'}\n    dark: {primary-color: '#000'}\n",
             "dashboards/d.yaml": "views:\n  - title: A\n    path: a\n    cards: []\n",
             "packages/p.yaml": "input_select:\n  tab_x:\n    options: [A, B]\n"}.items():
    (seed / f).parent.mkdir(parents=True, exist_ok=True)
    (seed / f).write_text(t)
sh("git", "init", "-q", "-b", "main", str(seed))
sh("git", "-C", str(seed), "add", "-A")
sh("git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
sh("git", "clone", "-q", "--bare", str(seed), str(remote))
(tmp / "options.json").write_text(json.dumps({"repository": f"file://{remote}"}))

r = sync.export()
alltext = "\n".join(f.read_text(errors="ignore") for f in sync.REPO.rglob("*") if f.is_file() and ".git" not in f.parts)
ok("SUPERSECRET123" not in alltext and "TOPSECRETKEY" not in alltext and "50.1" not in alltext, "export : aucun secret ni coordonnée")
ok("scripts.yaml" in r["skipped"], "export : fichier avec clé en clair signalé")
red = (sync.REPO / "exports/redacted/scripts.yaml")
ok(red.exists() and "api_key: <masqué>" in red.read_text() and "sequence:" in red.read_text() and not (sync.REPO / "scripts.yaml").exists(),
   "export : copie masquée lisible dans exports/redacted (jamais au chemin géré)")
cf = (sync.REPO / "exports/configuration.yaml").read_text()
ok("hunter2" not in alltext and "password: <masqué>" in cf and "broker: 10.0.0.5" in cf and "mqtt:" in cf, "configuration.yaml envoyé (valeurs sensibles masquées, reste lisible)")
ok(not (sync.REPO / "exports/total").exists(), "mode standard : pas de dossier exports/total")
ok((sync.REPO / "automations.yaml").exists() and (sync.REPO / "exports/lovelace/lovelace.x").exists(), "export : automations + dashboards interface")
a = sync.apply()
ok((cfg / "themes/maison/maison.yaml").exists() and (cfg / "packages/p.yaml").exists(), "application : fichiers copiés")
ok((cfg / "secrets.yaml").read_text().startswith("api_key: SUPER"), "application : secrets.yaml intact")
w = tmp / "claude"
sh("git", "clone", "-q", str(remote), str(w))
th = w / "themes/maison/maison.yaml"
th.write_text(th.read_text().replace("24px", "32px"))
g = lambda *x: sh("git", "-c", "user.name=c", "-c", "user.email=c@c", "-C", str(w), *x)
g("commit", "-qam", "m"); g("push", "-q", "origin", "main")
p = sync.preview()
ok(p["modify"] == ["themes/maison/maison.yaml"], "aperçu : modification détectée")
sync.apply(); sync.rollback()
ok("24px" in (cfg / "themes/maison/maison.yaml").read_text(), "retour arrière")
(w / "dashboards/d.yaml").write_text("views: [ {oops")
g("commit", "-qam", "x"); g("push", "-q", "origin", "main")
before = (cfg / "dashboards/d.yaml").read_text()
try:
    sync.apply(); ok(False, "YAML cassé accepté")
except sync.SyncError:
    ok((cfg / "dashboards/d.yaml").read_text() == before, "YAML cassé refusé, HA inchangé")
for bad in (["../etc"], ["secrets.yaml"], ["/etc/passwd"], [".storage"]):
    (tmp / "options.json").write_text(json.dumps({"repository": f"file://{remote}", "apply_paths": bad}))
    try:
        sync.apply(); ok(False, f"{bad} accepté")
    except sync.SyncError:
        ok(True, f"chemin {bad} refusé")
# un fichier contenant des valeurs masquées ne doit jamais être réappliqué
(w / "dashboards/d.yaml").write_text(before)
(w / "packages/p.yaml").write_text("input_select:\n  tab_x:\n    options: [A, B]\n    password: <masqué>\n")
g("commit", "-qam", "masqué"); g("push", "-q", "origin", "main")
(tmp / "options.json").write_text(json.dumps({"repository": f"file://{remote}"}))
before_p = (cfg / "packages/p.yaml").read_text()
try:
    sync.apply(); ok(False, "fichier masqué appliqué !")
except sync.SyncError as e:
    ok("masquées" in str(e) and (cfg / "packages/p.yaml").read_text() == before_p, "fichier avec <masqué> refusé à l'application")
(w / "packages/p.yaml").write_text(before_p); g("commit", "-qam", "ok"); g("push", "-q", "origin", "main")

# mode accès total
(tmp / "options.json").write_text(json.dumps({"repository": f"file://{remote}", "full_access": True}))
r = sync.export(force=True)
tot = sync.REPO / "exports/total"
tracked = subprocess.run(["git", "-C", str(sync.REPO), "ls-tree", "-r", "HEAD", "--name-only"], capture_output=True, text=True).stdout
allt = "\n".join(f.read_text(errors="ignore") for f in sync.REPO.rglob("*") if f.is_file() and ".git" not in f.parts)
ok((tot / "custom/extra.yaml").exists() and "value: 42" in (tot / "custom/extra.yaml").read_text(), "accès total : fichiers de toute l'arborescence")
ok("p4ss" not in allt and "TOPSECRETKEY" not in allt and "ATOK123456789" not in allt and "hunter2" not in allt, "accès total : valeurs de mots de passe / jetons masquées")
ce = (tot / ".storage/core.config_entries").read_text()
ok("sensor.a" in ce and "template" in ce, "accès total : configuration des intégrations lisible (modèles, options)")
zb = json.loads((tot / "zigbee2mqtt/coordinator_backup.json").read_text())
es = (tot / "esphome/det.yaml").read_text()
ok(zb["network_key"] == "<masqué>" and zb["channel"] == 11 and "Zm9vYmFy" not in es and "key: <masqué>" in es and "wifipass123" not in es and "ssid: Maison" in es,
   "accès total : clé réseau Zigbee et clé de chiffrement ESPHome masquées, le reste lisible")
g2 = (tot / "go2rtc.yaml").read_text()
ok("SuperPwd99" not in allt and "camPass12" not in allt and "bob%40example" not in allt and "device_id=abc123" in g2 and "554/stream1" in g2,
   "accès total : mot de passe / e-mail / identifiants dans une URL masqués, le reste de l'adresse lisible")
ok(not (tot / ".storage/auth").exists() and not (tot / ".storage/cloud").exists() and "REFRESHTOKEN" not in allt and "CLOUDSECRET" not in allt, "accès total : jetons d'authentification jamais exportés")
ok(not list(tot.rglob("*.db")) and "SUPERSECRET123" not in allt, "accès total : ni base de données ni valeurs de secrets.yaml")
ok("50.1" in (sync.REPO / "exports/configuration.yaml").read_text() and "exports/total/custom/extra.yaml" in tracked and any(x.startswith("exports/total/.storage/") for x in tracked.splitlines()), "accès total : coordonnées lisibles, dossier .storage bien versionné")
(tmp / "options.json").write_text(json.dumps({"repository": f"file://{remote}"}))
sync.export(force=True)
ok(not (sync.REPO / "exports/total").exists(), "retour au mode standard : exports/total supprimé")

# branchement de bout en bout + restauration si HA refuse
(tmp / "options.json").write_text(json.dumps({"repository": f"file://{remote}"}))
orig = (cfg / "configuration.yaml").read_text()
real = sync.core_check
sync.core_check = lambda log: False
try:
    sync.wire_apply(); ok(False, "wire aurait dû échouer")
except sync.SyncError:
    ok((cfg / "configuration.yaml").read_text() == orig, "branchement refusé par HA : configuration.yaml restauré")
sync.core_check = real
ok(sync.wire_apply()["changed"] == ["configuration.yaml"] and sync.wire_apply()["changed"] == [], "branchement appliqué puis idempotent")

print("\nÉCHECS :", fails or "aucun")
sys.exit(1 if fails else 0)
