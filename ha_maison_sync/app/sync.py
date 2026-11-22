"""Logique de synchronisation HA <-> GitHub (bibliothèque standard + PyYAML).

Principes de sécurité :
  - le jeton Supervisor et la clé SSH restent dans /data (jamais copiés vers le dépôt) ;
  - ce qui monte vers GitHub est une liste blanche, sans secrets (analyse + masquage) ;
  - ce qui descend vers HA est limité à `apply_paths`, jamais secrets.yaml / .storage / configuration.yaml,
    jamais de lien symbolique, jamais de chemin qui sort de /config ;
  - toute application est précédée d'une validation du dépôt et d'une sauvegarde, avec retour arrière.
"""
import difflib
import json
import os
import pathlib
import base64
import re
import shutil
import socket
import struct
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

DATA = pathlib.Path(os.environ.get("HA_MAISON_DATA", "/data"))
OPTIONS_FILE = pathlib.Path(os.environ.get("HA_MAISON_OPTIONS", str(DATA / "options.json")))
REPO = DATA / "repo"
SSH_DIR = DATA / "ssh"
KEY = SSH_DIR / "id_ed25519"
SNAPS = DATA / "snapshots"
STATE_FILE = DATA / "state.json"

DEFAULTS = {
    "repository": "",
    "branch": "main",
    "apply_paths": ["themes", "dashboards", "packages"],
    "export_paths": ["themes", "dashboards", "packages", "automations.yaml", "scripts.yaml", "scenes.yaml", "blueprints"],
    "export_full": True,
    "full_access": False,
    "restart_after_apply": False,
    "git_user_name": "HA-maison Sync",
    "git_user_email": "ha-maison-sync@localhost",
}
# Jamais appliqué vers HA, quoi qu'il y ait dans les options.
FORBIDDEN_APPLY = {"secrets.yaml", "configuration.yaml", ".storage", "custom_components", ".git", ".ha-maison", "home-assistant_v2.db"}
INVENTORY_EXCLUDE = {"person", "device_tracker", "zone", "persistent_notification"}
MAX_FILE = 2 * 1024 * 1024
SKIP_NAMES = {".git", "__pycache__", ".DS_Store"}


class SyncError(Exception):
    pass


# ───────────────────────── options / état ─────────────────────────
def options():
    o = dict(DEFAULTS)
    try:
        o.update(json.loads(OPTIONS_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    return o


def safe_rel(p):
    """Chemin relatif sûr (pas d'absolu, pas de '..'), sinon SyncError."""
    pp = pathlib.PurePosixPath(p)
    if pp.is_absolute() or ".." in pp.parts or not pp.parts:
        raise SyncError(f"Chemin refusé : {p!r}")
    return pp


def apply_paths(o):
    out = []
    for p in o["apply_paths"]:
        pp = safe_rel(p)
        if pp.parts[0] in FORBIDDEN_APPLY or str(pp) in FORBIDDEN_APPLY:
            raise SyncError(f"« {p} » ne peut pas être appliqué (protégé).")
        out.append(str(pp))
    return out


def export_paths(o):
    return [str(safe_rel(p)) for p in o["export_paths"]]


def config_dir():
    env = os.environ.get("HA_MAISON_CONFIG")
    if env:
        return pathlib.Path(env)
    for c in ("/homeassistant", "/config"):
        if pathlib.Path(c, "configuration.yaml").exists():
            return pathlib.Path(c)
    raise SyncError("Dossier de configuration HA introuvable (ni /homeassistant ni /config).")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_state(st):
    DATA.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


# ───────────────────────── git ─────────────────────────
def git(*args, cwd=None, check=True):
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = (f"ssh -i {KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
                              f"-o UserKnownHostsFile={SSH_DIR / 'known_hosts'}")
    env["GIT_TERMINAL_PROMPT"] = "0"
    p = subprocess.run(["git", *args], cwd=str(cwd or REPO), env=env, capture_output=True, text=True, timeout=180)
    if check and p.returncode != 0:
        raise SyncError(f"git {args[0]} a échoué : {(p.stderr or p.stdout).strip()[:500]}")
    return (p.stdout or "").strip()


def ensure_key():
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    if not KEY.exists():
        try:
            subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "ha-maison-sync", "-f", str(KEY)],
                           check=True, capture_output=True)
        except FileNotFoundError:      # absent seulement hors de l'add-on (tests locaux)
            return None
    os.chmod(KEY, 0o600)
    return (KEY.with_suffix(".pub")).read_text(encoding="utf-8").strip()


def ensure_repo(log):
    o = options()
    if not o["repository"]:
        raise SyncError("Renseigne l'option « repository » de l'add-on (URL SSH du dépôt).")
    ensure_key()
    if not (REPO / ".git").exists():
        DATA.mkdir(parents=True, exist_ok=True)
        git("clone", o["repository"], str(REPO), cwd=DATA)
        log("Dépôt cloné.")
    git("remote", "set-url", "origin", o["repository"])
    git("config", "user.name", o["git_user_name"])
    git("config", "user.email", o["git_user_email"])
    git("config", "core.autocrlf", "false")
    return o


def fetch_remote(o):
    git("fetch", "origin", "--prune")
    sha = git("rev-parse", "--verify", "-q", f"origin/{o['branch']}", check=False)
    return sha or None


def reset_to_remote(o, sha):
    if sha:
        git("checkout", "-q", "-B", o["branch"], f"origin/{o['branch']}")
        git("reset", "-q", "--hard", f"origin/{o['branch']}")
        git("clean", "-q", "-fd")
    else:
        # dépôt distant vide : on repart d'une branche vide
        git("checkout", "-q", "--orphan", o["branch"], check=False)


# ───────────────────────── fichiers ─────────────────────────
def iter_files(base, paths):
    """Fichiers réguliers (pas de liens symboliques) sous `paths`, chemins relatifs à `base`."""
    base = pathlib.Path(base)
    for p in paths:
        root = base / p
        if root.is_symlink() or not root.exists():
            continue
        cands = [root] if root.is_file() else sorted(root.rglob("*"))
        for f in cands:
            if f.is_symlink() or not f.is_file():
                continue
            if any(part in SKIP_NAMES for part in f.relative_to(base).parts):
                continue
            if f.stat().st_size > MAX_FILE:
                continue
            yield str(f.relative_to(base).as_posix())


# ───────────────────────── secrets ─────────────────────────
SENSITIVE_KEY = re.compile(
    r"(?im)^\s*-?\s*[\w\-]*(password|passwd|token|api[_-]?key|apikey|secret|client_secret|access_token|bearer|private[_-]?key|webhook_id|psk|network[_-]?key|encryption[_-]?key|master[_-]?key|latitude|longitude)\s*:\s*"
    r"(?!\s*!secret)(?!\s*<masqué>)(?!\s*$)(?!\s*[\"']?(true|false|null|none)[\"']?\s*$)(?!\s*[|>])\S+")
SENSITIVE_VALUE = re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")
MASK_KEYS = re.compile(r"(?im)^(\s*)(latitude|longitude|elevation|external_url|internal_url)\s*:.*$")


def scan_text(text):
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if SENSITIVE_KEY.match(line) or SENSITIVE_VALUE.search(line) or LONG_KEY.match(line) or URL_QUERY_SECRET.search(line) or URL_USERINFO.search(line):
            hits.append(i)
    return hits


def scan_file(path):
    try:
        return scan_text(pathlib.Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def sanitize_configuration(text):
    return MASK_KEYS.sub(lambda m: f"{m.group(1)}{m.group(2)}: <masqué>", text)


# ───────────────────────── API Supervisor / Core ─────────────────────────
def _call(url, method="GET", body=None, timeout=60, raw=False):
    tok = os.environ.get("SUPERVISOR_TOKEN")
    if not tok:
        return None
    data = json.dumps(body if body is not None else {}).encode() if method != "GET" else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode(errors="replace")
            if raw:
                return {"status": r.status, "text": txt}
            return {"status": r.status, "json": json.loads(txt or "null")}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return {"status": e.code, "json": json.loads(raw)}
        except ValueError:
            return {"status": e.code, "json": {"message": raw[:300]}}
    except (urllib.error.URLError, OSError) as e:
        return {"status": 0, "json": {"message": str(e)}}


def core_check(log):
    r = _call("http://supervisor/core/check", "POST", timeout=180)
    if r is None:
        log("⚠️ API Supervisor indisponible (mode test) : contrôle de config ignoré.")
        return True
    ok = r["status"] == 200 and (r["json"] or {}).get("result") == "ok"
    log("✅ Contrôle de la configuration HA : OK." if ok else f"❌ Contrôle de la configuration HA : {(r['json'] or {}).get('message', r['status'])}")
    return ok


def core_service(domain, service, log):
    r = _call(f"http://supervisor/core/api/services/{domain}/{service}", "POST", {})
    if r is None:
        log(f"⚠️ (mode test) {domain}.{service} non appelé.")
        return
    log(f"{'✅' if r['status'] == 200 else '⚠️'} {domain}.{service} → {r['status']}")


def core_restart(log):
    r = _call("http://supervisor/core/restart", "POST", timeout=30)
    log("♻️ Redémarrage de HA demandé." if r and r["status"] == 200 else "⚠️ Redémarrage impossible (API indisponible).")


def reload_for(changed, log):
    top = {c.split("/")[0] for c in changed}
    if "themes" in top:
        core_service("frontend", "reload_themes", log)
    if top & {"packages", "automations.yaml", "scripts.yaml", "scenes.yaml", "blueprints"}:
        core_service("homeassistant", "reload_all", log)
    if "dashboards" in top:
        log("ℹ️ Dashboards : recharge la page du navigateur pour voir les changements.")


def fetch_states():
    r = _call("http://supervisor/core/api/states")
    if r is None or r["status"] != 200:
        return None
    return r["json"]


# ───────────────────────── opérations ─────────────────────────
class Run:
    def __init__(self):
        self.lines = []

    def __call__(self, msg):
        self.lines.append(msg)


def status(refresh=False):
    st = load_state()
    res = {"pubkey": None, "repository": options()["repository"], "branch": options()["branch"],
           "last_applied": st.get("last_applied"), "last_export": st.get("last_export"),
           "last_apply": st.get("last_apply"), "pending": [], "error": None, "snapshots": len(list(SNAPS.glob("*.tar.gz"))) if SNAPS.exists() else 0}
    try:
        res["pubkey"] = ensure_key()
        if refresh and res["repository"]:
            log = Run()
            o = ensure_repo(log)
            sha = fetch_remote(o)
            res["remote"] = sha
            if sha:
                reset_to_remote(o, sha)
                pl = make_plan(o, sha)
                res["plan"] = {"add": len(pl["add"]), "modify": len(pl["modify"]), "delete": len(pl["delete"])}
            last = st.get("last_applied")
            if sha and sha != last:
                rng = f"{last}..origin/{o['branch']}" if last else f"origin/{o['branch']}"
                out = git("log", "--format=%h %s", "-n", "15", rng, check=False)
                res["pending"] = out.splitlines()
    except SyncError as e:
        res["error"] = str(e)
    except (OSError, subprocess.SubprocessError) as e:
        res["error"] = f"{type(e).__name__}: {e}"
    return res


# ───────────────────────── export « complet » (tout sauf les identifiants) ─────────────────────────
# Registres de HA : on ne garde que des champs utiles à l'analyse. Sont volontairement écartés :
# unique_id / identifiers / connections (adresses MAC, numéros de série), configuration_url (IP),
# et tout le contenu `data` / `options` des intégrations (là sont les mots de passe et jetons).
REGISTRY_KEEP = {
    "core.entity_registry": ("entities", ["entity_id", "platform", "name", "original_name", "device_id", "area_id", "disabled_by",
                                          "hidden_by", "entity_category", "device_class", "original_device_class",
                                          "unit_of_measurement", "icon", "original_icon"]),
    "core.device_registry": ("devices", ["id", "name", "name_by_user", "manufacturer", "model", "sw_version", "area_id",
                                         "via_device_id", "disabled_by", "entry_type"]),
    "core.area_registry": ("areas", ["id", "name", "floor_id", "aliases", "icon", "labels"]),
    "core.floor_registry": ("floors", ["floor_id", "name", "level", "aliases", "icon"]),
    "core.label_registry": ("labels", ["label_id", "name", "icon", "color", "description"]),
    "core.config_entries": ("entries", ["domain", "title", "source", "disabled_by", "version"]),
}
HELPER_STORES = ["input_boolean", "input_select", "input_number", "input_text", "input_datetime", "input_button",
                 "counter", "timer", "schedule"]
LOG_LEVEL = re.compile(r"^\d{4}-\d\d-\d\d[ T][\d:.]+\s+(\w+)")
REDACT = [
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"), "<mac>"),
    (re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+"), "<email>"),
    (re.compile(r"(?i)\b(token|password|passwd|api[_-]?key|apikey|secret|authorization|bearer)([=:\s\"']+)\S+"), r"\1\2<masqué>"),
    (re.compile(r"\?[^\s\"')]+"), "?<masqué>"),
    (re.compile(r"[A-Za-z0-9_\-]{32,}"), "<masqué>"),
]


def _load_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sanitize_registries(storage, out):
    n = 0
    out.mkdir(parents=True, exist_ok=True)
    for fname, (key, fields) in REGISTRY_KEEP.items():
        d = _load_json(storage / fname)
        items = ((d or {}).get("data") or {}).get(key)
        if not isinstance(items, list):
            continue
        rows = [{k: it.get(k) for k in fields if it.get(k) not in (None, [], "")} for it in items if isinstance(it, dict)]
        rows.sort(key=lambda r: json.dumps(r, sort_keys=True))
        (out / f"{fname}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        n += 1
    return n


def export_helpers(storage, out, skipped):
    n = 0
    out.mkdir(parents=True, exist_ok=True)
    for name in HELPER_STORES:
        f = storage / name
        if not f.is_file() or f.is_symlink():
            continue
        if scan_file(f):
            skipped.append(f".storage/{name}")
            continue
        items = ((_load_json(f) or {}).get("data") or {}).get("items")
        if items:
            (out / f"{name}.json").write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            n += 1
    return n


def export_hacs(cfg, out):
    """Cartes / intégrations installées (HACS + custom_components) : noms et versions seulement."""
    out.mkdir(parents=True, exist_ok=True)
    info = {"hacs": [], "custom_components": [], "www_community": []}
    d = _load_json(cfg / ".storage" / "hacs.repositories")
    for r in ((d or {}).get("data") or {}).values():
        if isinstance(r, dict) and r.get("installed"):
            info["hacs"].append({"repository": r.get("full_name"), "category": r.get("category"),
                                 "version": r.get("installed_version") or r.get("version_installed")})
    info["hacs"].sort(key=lambda r: str(r["repository"]))
    cc = cfg / "custom_components"
    if cc.is_dir():
        for m in sorted(cc.glob("*/manifest.json")):
            md = _load_json(m) or {}
            info["custom_components"].append({"domain": md.get("domain") or m.parent.name, "name": md.get("name"), "version": md.get("version")})
    wc = cfg / "www" / "community"
    if wc.is_dir():
        info["www_community"] = sorted(x.name for x in wc.iterdir() if x.is_dir())
    (out / "installed_extensions.json").write_text(json.dumps(info, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def secret_names(cfg):
    """Seulement les NOMS des secrets (jamais leurs valeurs) : permet de savoir quels !secret existent."""
    f = cfg / "secrets.yaml"
    if not f.is_file() or f.is_symlink():
        return None
    return sorted(set(re.findall(r"(?m)^([A-Za-z0-9_.\-]+)\s*:", f.read_text(encoding="utf-8", errors="replace"))))


def redact(line):
    for rx, rep in REDACT:
        line = rx.sub(rep, line)
    return line


def collect_errors(cfg, max_lines=400):
    f = cfg / "home-assistant.log"
    text = None
    if f.is_file() and not f.is_symlink():
        try:
            with open(f, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 2_000_000))
                text = fh.read().decode("utf-8", errors="replace")
        except OSError:
            text = None
    if text is None:
        r = _call("http://supervisor/core/logs", raw=True)
        if r and r["status"] == 200:
            text = r.get("text", "")
    if not text:
        return None
    keep, on = [], False
    for line in text.splitlines()[-3000:]:
        m = LOG_LEVEL.match(line)
        if m:
            on = m.group(1) in ("ERROR", "WARNING", "CRITICAL")
        if on:
            line = redact(line)
            if not scan_text(line):
                keep.append(line)
    return "\n".join(keep[-max_lines:]) + "\n" if keep else ""


def export_system(out):
    info = {}
    r = _call("http://supervisor/core/info")
    if r and r["status"] == 200:
        d = (r["json"] or {}).get("data") or {}
        info["core"] = {"version": d.get("version"), "machine": d.get("machine"), "arch": d.get("arch")}
    r = _call("http://supervisor/addons")
    if r and r["status"] == 200:
        info["addons"] = [{"name": a.get("name"), "slug": a.get("slug"), "version": a.get("version"), "state": a.get("state")}
                          for a in ((r["json"] or {}).get("data") or {}).get("addons", [])]
    if info:
        out.mkdir(parents=True, exist_ok=True)
        (out / "system.json").write_text(json.dumps(info, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return bool(info)


def export_full(cfg, ex, log, skipped):
    """Tout ce qui aide à comprendre et optimiser HA — sans identifiants. Régénéré à chaque envoi."""
    for sub in ("registry", "helpers", "logs"):
        shutil.rmtree(ex / sub, ignore_errors=True)
    storage = cfg / ".storage"
    parts = []
    if storage.is_dir():
        a = sanitize_registries(storage, ex / "registry")
        b = export_helpers(storage, ex / "helpers", skipped)
        parts.append(f"{a} registre(s), {b} type(s) de helpers")
    export_hacs(cfg, ex)
    names = secret_names(cfg)
    if names is not None:
        (ex / "secrets_keys.txt").write_text("# Noms des secrets (valeurs jamais envoyées)\n" + "\n".join(names) + "\n", encoding="utf-8")
        parts.append(f"{len(names)} nom(s) de secrets")
    errs = collect_errors(cfg)
    if errs is not None:
        (ex / "logs").mkdir(exist_ok=True)
        (ex / "logs" / "errors.log").write_text(errs, encoding="utf-8")
        parts.append(f"journal d'erreurs ({len(errs.splitlines())} lignes, masqué)")
    if export_system(ex):
        parts.append("version de HA + add-ons")
    log("📦 Export complet : " + (", ".join(parts) if parts else "rien de plus à envoyer") + ".")


# ───────────────────────── masquage des valeurs sensibles (au lieu d'écarter le fichier entier) ─────────────────────────
# On voit TOUT le fichier ; seules les VALEURS des mots de passe / jetons / clés sont remplacées par <masqué>.
CRED_WORDS = r"password|passwd|token|api[_-]?key|apikey|secret|client_secret|access_token|bearer|private[_-]?key|webhook_id|psk|network[_-]?key|encryption[_-]?key|master[_-]?key"
_PREFIX = r"(?im)^(\s*-?\s*[\w\-]*(?:%s)\s*:\s*)(?!!secret)(?!<masqué>)(?![|>])(?!\s*$)(?![\"']?(?:true|false|null|none)[\"']?\s*$)\S.*$"
REDACT_CRED = re.compile(_PREFIX % CRED_WORDS)
REDACT_GEO = re.compile(_PREFIX % r"latitude|longitude")
# `key:` seul (ex. ESPHome « encryption: key: … ») : masqué quand la valeur ressemble à une clé (32+ caractères base64/hex)
LONG_KEY = re.compile(r"(?im)^(\s*-?\s*key\s*:\s*)([\"']?)[A-Za-z0-9+/_\-=]{32,}\2\s*$")
CRED_JSON_KEY = re.compile(r"(?i)(%s|credential|authorization)" % CRED_WORDS)
MASKED = "<masqué>"
# Secrets écrits DANS une adresse : ...?password=xxx&email=... ou rtsp://utilisateur:motdepasse@hôte
URL_QUERY_SECRET = re.compile(r"(?i)([?&;](?:password|passwd|pwd|pass|token|api[_-]?key|apikey|secret|access_token|auth|signature|sign|email|mail|user|username|login)=)[^&\s\"'<>]+")
URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^@\s/]+)@")


def mask_urls(text):
    text = URL_QUERY_SECRET.sub(lambda m: m.group(1) + MASKED, text)
    return URL_USERINFO.sub(lambda m: m.group(1) + MASKED + ":" + MASKED + "@", text)


def redact_text(text, geo=True):
    text = REDACT_CRED.sub(lambda m: m.group(1) + MASKED, text)
    text = LONG_KEY.sub(lambda m: m.group(1) + MASKED, text)
    if geo:
        text = REDACT_GEO.sub(lambda m: m.group(1) + MASKED, text)
    return mask_urls(SENSITIVE_VALUE.sub(MASKED, text))


def redact_json(obj):
    if isinstance(obj, dict):
        def secret_value(k, v):
            if isinstance(v, bool) or v in ("", None, [], {}):
                return False
            if CRED_JSON_KEY.search(str(k)):          # valeur simple OU bloc entier (ex. network_key: {key: [...]})
                return True
            return str(k) == "key" and isinstance(v, str) and len(v) >= 32 and re.fullmatch(r"[A-Za-z0-9+/_\-=]+", v) is not None
        return {k: (MASKED if secret_value(k, v) else redact_json(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_json(v) for v in obj]
    if isinstance(obj, str):
        return mask_urls(SENSITIVE_VALUE.sub(MASKED, obj))
    return obj


def redacted_bytes(path, geo=True):
    """Contenu masqué d'un fichier texte ou JSON (None si illisible)."""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(text)
        return (json.dumps(redact_json(data), ensure_ascii=False, indent=1) + "\n").encode()
    except ValueError:
        return redact_text(text, geo=geo).encode()


# ───────────────────────── mode « accès total » (option full_access) ─────────────────────────
# Tout le dossier de config en lecture : fichiers texte / JSON de toute l'arborescence + tout .storage, avec
# VALEURS de mots de passe / jetons masquées. Jamais exportés, même en mode total : les fichiers d'authentification
# (jetons d'accès à HA et au cloud), les valeurs de secrets.yaml, les bases de données, les sauvegardes.
TOTAL_SKIP_DIRS = {".storage", ".cloud", "deps", "tts", "backup", "backups", ".git", "__pycache__", "node_modules",
                   ".venv", "custom_components", "image", "media", "recordings"}
TOTAL_SKIP_PREFIX = ("www/community",)
TOTAL_SUFFIX = {".yaml", ".yml", ".json", ".py", ".jinja", ".jinja2", ".txt", ".md", ".conf", ".cfg", ".ini", ".toml", ".sh", ".csv"}
STORAGE_DENY = re.compile(r"^(auth|auth_provider\..*|cloud|onboarding|mobile_app|core\.restore_state|core\.uuid|http\.auth)(\..*)?$")
TOTAL_FILE_MAX, STORAGE_FILE_MAX, TOTAL_MAX_FILES, TOTAL_MAX_BYTES = 512 * 1024, 3 * 1024 * 1024, 3000, 30 * 1024 * 1024


def export_total(cfg, ex, log):
    out = ex / "total"
    shutil.rmtree(out, ignore_errors=True)
    n, size, masked = 0, 0, 0

    def put(rel, src, geo=False):
        nonlocal n, size, masked
        if n >= TOTAL_MAX_FILES or size >= TOTAL_MAX_BYTES:
            return
        data = redacted_bytes(src, geo=geo)
        if data is None:
            return
        if MASKED.encode() in data:
            masked += 1
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        n += 1
        size += len(data)

    for root, dirs, files in os.walk(cfg, followlinks=False):
        rel_root = pathlib.PurePosixPath(pathlib.Path(root).relative_to(cfg).as_posix())
        dirs[:] = sorted(d for d in dirs if d not in TOTAL_SKIP_DIRS and not d.startswith(".")
                         and not str(rel_root / d).startswith(TOTAL_SKIP_PREFIX))
        for f in sorted(files):
            p = pathlib.Path(root) / f
            if p.is_symlink() or f == "secrets.yaml" or f.endswith((".log", ".db", ".db-shm", ".db-wal")):
                continue
            if p.suffix.lower() in TOTAL_SUFFIX and p.stat().st_size <= TOTAL_FILE_MAX:
                put(str(rel_root / f) if str(rel_root) != "." else f, p)
    storage = cfg / ".storage"
    if storage.is_dir():
        for f in sorted(storage.iterdir()):
            if f.is_file() and not f.is_symlink() and not STORAGE_DENY.match(f.name) and f.stat().st_size <= STORAGE_FILE_MAX:
                put(f".storage/{f.name}", f)
    log(f"🔓 Accès total : {n} fichier(s) dans exports/total/ ({size // 1024} Ko), {masked} avec des valeurs sensibles masquées.")
    return n


def export(force=False):
    log = Run()
    o = ensure_repo(log)
    cfg = config_dir()
    st = load_state()
    sha = fetch_remote(o)
    if sha and st.get("last_applied") and sha != st["last_applied"] and not force:
        raise SyncError("Des modifications sur GitHub n'ont pas encore été appliquées à HA. Applique-les d'abord "
                        "(ou force l'envoi : cela écraserait les fichiers du dépôt par ceux de HA).")
    reset_to_remote(o, sha)

    # 1) fichiers gérés (jamais de suppression côté dépôt)
    shutil.rmtree(REPO / "exports" / "redacted", ignore_errors=True)
    skipped, copied = [], 0
    full_access = bool(o.get("full_access"))
    for rel in iter_files(cfg, export_paths(o)):
        src = cfg / rel
        if scan_file(src):
            # valeurs sensibles : copie masquée en lecture seule (jamais au chemin géré, pour ne rien réappliquer par erreur)
            data = redacted_bytes(src, geo=not full_access)
            if data is not None:
                red = REPO / "exports" / "redacted" / rel
                red.parent.mkdir(parents=True, exist_ok=True)
                red.write_bytes(data)
            skipped.append(rel)
            continue
        dst = REPO / rel
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        copied += 1
    log(f"{copied} fichier(s) géré(s) mis à jour dans le dépôt.")

    # 2) exports en lecture seule (jamais appliqués à HA)
    ex = REPO / "exports"
    ex.mkdir(exist_ok=True)
    conf = cfg / "configuration.yaml"
    if conf.exists():
        text = conf.read_text(encoding="utf-8", errors="replace")
        if not full_access:
            text = sanitize_configuration(text)            # coordonnées / URL masquées
        if scan_text(text) or full_access:
            red = redact_text(text, geo=not full_access)   # valeurs sensibles masquées, le reste est lisible
            if red != text:
                skipped.append("configuration.yaml")
            text = red
        (ex / "configuration.yaml").write_text(text, encoding="utf-8")
    storage = cfg / ".storage"
    if storage.is_dir():
        (ex / "lovelace").mkdir(exist_ok=True)
        for f in sorted(storage.glob("lovelace*")):
            if f.is_file() and not f.is_symlink() and f.stat().st_size <= MAX_FILE:
                if scan_file(f):
                    data = redacted_bytes(f)
                    if data is not None:
                        (ex / "lovelace" / f.name).write_bytes(data)
                    skipped.append(f".storage/{f.name}")
                else:
                    shutil.copyfile(f, ex / "lovelace" / f.name)
    states = fetch_states()
    if states is not None:
        inv = []
        for s in states:
            dom = s["entity_id"].split(".")[0]
            if dom in INVENTORY_EXCLUDE:
                continue
            a = s.get("attributes", {})
            inv.append({"entity_id": s["entity_id"], "name": a.get("friendly_name"), "domain": dom,
                        "unit": a.get("unit_of_measurement"), "device_class": a.get("device_class"),
                        "unavailable": s.get("state") in ("unavailable", "unknown")})
        inv.sort(key=lambda e: e["entity_id"])
        (REPO / "inventory").mkdir(exist_ok=True)
        (REPO / "inventory" / "entities.json").write_text(json.dumps(inv, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        log(f"Inventaire : {len(inv)} entités (sans valeurs).")
    else:
        log("⚠️ Inventaire des entités non mis à jour (API Core indisponible).")
    if o.get("export_full", True) or full_access:
        export_full(cfg, ex, log, skipped)
    if full_access:
        export_total(cfg, ex, log)
    else:
        shutil.rmtree(ex / "total", ignore_errors=True)
    if skipped:
        log("🔒 Valeurs sensibles masquées (le reste du fichier est envoyé ; copie dans exports/) : " + ", ".join(sorted(set(skipped))))

    # 3) commit + push
    git("add", "-A")
    if full_access and (ex / "total").is_dir():
        git("add", "-f", "-A", "exports/total")      # .gitignore ignore .storage/ etc. : on force pour ce dossier voulu
    if not git("status", "--porcelain"):
        log("Rien à envoyer : GitHub est déjà à jour.")
        head = git("rev-parse", "HEAD", check=False) or None
    else:
        git("commit", "-q", "-m", f"Export depuis Home Assistant ({time.strftime('%Y-%m-%d %H:%M')})")
        git("push", "-u", "origin", f"HEAD:{o['branch']}")
        head = git("rev-parse", "HEAD")
        log(f"✅ Envoyé vers GitHub ({head[:7]}).")
    st["last_applied"] = head
    st["last_export"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(st)
    return {"log": log.lines, "skipped": sorted(set(skipped)), "redacted": sorted(set(skipped))}


def make_plan(o, sha):
    cfg = config_dir()
    paths = apply_paths(o)
    add, mod, same = [], [], 0
    for rel in iter_files(REPO, paths):
        dst = cfg / rel
        if not dst.exists():
            add.append(rel)
        elif dst.read_bytes() != (REPO / rel).read_bytes():
            mod.append(rel)
        else:
            same += 1
    delete = []
    last = load_state().get("last_applied")
    if last and sha and last != sha:
        out = git("diff", "--name-only", "--diff-filter=D", last, sha, "--", *paths, check=False)
        delete = [f for f in out.splitlines() if (cfg / f).is_file() and not (cfg / f).is_symlink()]
    diffs = {}
    for rel in mod:
        try:
            a = (cfg / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            b = (REPO / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            d = list(difflib.unified_diff(a, b, f"HA/{rel}", f"GitHub/{rel}", lineterm="", n=2))
            diffs[rel] = "\n".join(d[:400])
        except OSError:
            pass
    return {"add": add, "modify": mod, "delete": delete, "same": same, "diffs": diffs}


def preview():
    log = Run()
    o = ensure_repo(log)
    sha = fetch_remote(o)
    if not sha:
        raise SyncError("Le dépôt GitHub est vide : rien à appliquer.")
    reset_to_remote(o, sha)
    plan = make_plan(o, sha)
    plan["log"] = log.lines
    plan["commit"] = sha[:7]
    return plan


def validate_repo(log):
    script = pathlib.Path(__file__).resolve().parent / "validate.py"   # copie embarquée, pas celle du dépôt
    p = subprocess.run(["python3", str(script), str(REPO)], capture_output=True, text=True, timeout=120)
    out = (p.stdout + p.stderr).strip()
    log(("✅ Validation du dépôt : OK." if p.returncode == 0 else "❌ Validation du dépôt :\n" + out))
    return p.returncode == 0


def snapshot(cfg, plan, sha, previous):
    SNAPS.mkdir(parents=True, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S") + "-%03d" % (int(time.time() * 1000) % 1000)
    manifest = {"time": name, "sha": sha, "previous_applied": previous,
                "added": plan["add"], "modified": plan["modify"], "deleted": plan["delete"]}
    with tarfile.open(SNAPS / f"{name}.tar.gz", "w:gz") as t:
        for rel in plan["modify"] + plan["delete"]:
            t.add(cfg / rel, arcname=f"files/{rel}")
        mp = SNAPS / f"{name}.json"
        mp.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        t.add(mp, arcname="manifest.json")
        mp.unlink()
    for old in sorted(SNAPS.glob("*.tar.gz"))[:-10]:
        old.unlink()
    return name


def apply():
    log = Run()
    o = ensure_repo(log)
    cfg = config_dir()
    sha = fetch_remote(o)
    if not sha:
        raise SyncError("Le dépôt GitHub est vide : rien à appliquer.")
    reset_to_remote(o, sha)
    plan = make_plan(o, sha)
    if not (plan["add"] or plan["modify"] or plan["delete"]):
        log("Rien à appliquer : HA est déjà identique au dépôt.")
        st = load_state()
        st["last_applied"] = sha
        save_state(st)
        return {"log": log.lines, "changed": []}
    for rel in plan["add"] + plan["modify"]:
        if MASKED.encode() in (REPO / rel).read_bytes():
            raise SyncError(f"Application annulée : « {rel} » contient des valeurs masquées ({MASKED}). Remets les vraies valeurs "
                            "(ou une référence !secret) avant de l'appliquer. Rien n'a été modifié dans HA.")
    if not validate_repo(log):
        raise SyncError("Application annulée : la validation du dépôt a échoué (rien n'a été modifié dans HA).")
    st = load_state()
    snap = snapshot(cfg, plan, sha, st.get("last_applied"))
    log(f"💾 Sauvegarde des fichiers concernés : {snap}")
    changed = []
    try:
        for rel in plan["add"] + plan["modify"]:
            dst = cfg / rel
            if dst.is_symlink():
                raise SyncError(f"Lien symbolique refusé : {rel}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / rel, dst)
            changed.append(rel)
        for rel in plan["delete"]:
            (cfg / rel).unlink()
            changed.append(rel)
        log(f"📥 {len(plan['add'])} ajouté(s), {len(plan['modify'])} modifié(s), {len(plan['delete'])} supprimé(s).")
        if not core_check(log):
            raise SyncError("La configuration HA est invalide avec ces fichiers.")
    except Exception as e:  # retour arrière automatique
        log(f"↩️ Retour arrière automatique : {e}")
        _restore(snap, cfg, log)
        raise SyncError(f"Application annulée et fichiers restaurés. Détail : {e}") from e
    reload_for(changed, log)
    if o["restart_after_apply"]:
        core_restart(log)
    st["last_applied"] = sha
    st["last_apply"] = time.strftime("%Y-%m-%d %H:%M:%S") + f" ({sha[:7]})"
    st["last_snapshot"] = snap
    save_state(st)
    log("✅ Terminé.")
    return {"log": log.lines, "changed": changed}


def _restore(name, cfg, log):
    tpath = SNAPS / f"{name}.tar.gz"
    with tarfile.open(tpath) as t:
        manifest = json.loads(t.extractfile("manifest.json").read().decode())
        for rel in manifest["added"]:
            f = cfg / safe_rel(rel)
            if f.is_file() and not f.is_symlink():
                f.unlink()
                parent = f.parent
                while parent != cfg and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
        for rel in manifest["modified"] + manifest["deleted"]:
            member = t.getmember(f"files/{rel}")
            data = t.extractfile(member).read()
            dst = cfg / safe_rel(rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
    return manifest


def rollback():
    log = Run()
    cfg = config_dir()
    snaps = sorted(SNAPS.glob("*.tar.gz")) if SNAPS.exists() else []
    if not snaps:
        raise SyncError("Aucune sauvegarde disponible.")
    name = snaps[-1].name[:-7]
    manifest = _restore(name, cfg, log)
    snaps[-1].unlink()
    log(f"↩️ Restauration de {name} ({len(manifest['added'])} fichier(s) retiré(s), "
        f"{len(manifest['modified']) + len(manifest['deleted'])} restauré(s)).")
    reload_for(manifest["added"] + manifest["modified"] + manifest["deleted"], log)
    st = load_state()
    st["last_applied"] = manifest.get("previous_applied")
    st["last_apply"] = None
    save_state(st)
    return {"log": log.lines}


# ───────────────────────── branchement dans configuration.yaml ─────────────────────────
# On n'AJOUTE que les lignes manquantes (jamais de suppression, jamais de réécriture), on vérifie que le YAML obtenu
# contient bien ce qu'on veut, on sauvegarde, puis HA contrôle la config ; en cas d'échec : retour arrière.
WIRE_DASHBOARDS = {
    "maison-tablette": {"mode": "yaml", "title": "Maison", "icon": "mdi:home-variant", "show_in_sidebar": "true",
                        "filename": "dashboards/maison-tablette.yaml"},
    "maison-mobile": {"mode": "yaml", "title": "Maison mobile", "icon": "mdi:cellphone", "show_in_sidebar": "true",
                      "filename": "dashboards/maison-mobile.yaml"},
}
TOP_KEY = re.compile(r"^([A-Za-z0-9_]+):(.*)$")


class _CfgLoader(yaml.SafeLoader):
    pass


for _t in ("!secret", "!include", "!include_dir_named", "!include_dir_merge_named", "!include_dir_list",
           "!include_dir_merge_list", "!env_var", "!input"):
    _CfgLoader.add_constructor(_t, lambda l, n: l.construct_scalar(n))


def _blocks(lines):
    """{clé de 1er niveau: (index ligne clé, index de fin exclu, valeur en ligne)}."""
    out, keys = {}, []
    for i, ln in enumerate(lines):
        m = TOP_KEY.match(ln)
        if m and not ln.startswith("#"):
            keys.append((m.group(1), i, m.group(2).split("#")[0].strip()))
    for n, (k, i, v) in enumerate(keys):
        end = keys[n + 1][1] if n + 1 < len(keys) else len(lines)
        out[k] = (i, end, v)
    return out


def _child_indent(lines, i, end):
    for ln in lines[i + 1:end]:
        if ln.strip() and not ln.lstrip().startswith("#"):
            return len(ln) - len(ln.lstrip())
    return 2


def _children(lines, i, end, ind):
    res = {}
    for j in range(i + 1, end):
        ln = lines[j]
        if ln.strip() and not ln.lstrip().startswith("#") and len(ln) - len(ln.lstrip()) == ind:
            m = re.match(r"\s*([\w\-]+):(.*)$", ln)
            if m:
                res[m.group(1)] = (j, m.group(2).split("#")[0].strip())
    return res


def _dash_lines(name, d, ind):
    pad = " " * ind
    return [f"{pad}{name}:"] + [f"{pad}  {k}: {v}" for k, v in d.items()]


def plan_wiring(text):
    """Retourne (nouveau_texte, ajouts[list[str]], manuel[list[str]])."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    added, manual = [], []

    def need_block(key, body_lines, label):
        nonlocal lines
        b = _blocks(lines)
        if key not in b:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"{key}:")
            lines.extend(body_lines)
            added.append(label)
            return None
        return b[key]

    # homeassistant.packages
    hit = need_block("homeassistant", ["  packages: !include_dir_named packages"], "homeassistant → packages")
    if hit:
        i, end, inline = hit
        if inline:
            manual.append("homeassistant: est défini en une ligne / par include : ajoute `packages: !include_dir_named packages`.")
        else:
            ind = _child_indent(lines, i, end)
            ch = _children(lines, i, end, ind)
            if "packages" in ch:
                v = ch["packages"][1]
                if not ("!include_dir" in v and "packages" in v):
                    manual.append("homeassistant.packages existe déjà et n'inclut pas le dossier `packages` : à fusionner à la main.")
            else:
                lines.insert(i + 1, " " * ind + "packages: !include_dir_named packages")
                added.append("homeassistant → packages")
    # frontend.themes
    hit = need_block("frontend", ["  themes: !include_dir_merge_named themes"], "frontend → themes")
    if hit:
        i, end, inline = hit
        if inline:
            manual.append("frontend: est défini par un include : vérifie qu'il charge `themes: !include_dir_merge_named themes`.")
        else:
            ind = _child_indent(lines, i, end)
            ch = _children(lines, i, end, ind)
            if "themes" in ch:
                v = ch["themes"][1]
                if not ("!include_dir" in v and "themes" in v):
                    manual.append("frontend.themes existe déjà et n'inclut pas le dossier `themes` : à fusionner à la main.")
            else:
                lines.insert(i + 1, " " * ind + "themes: !include_dir_merge_named themes")
                added.append("frontend → themes")
    # lovelace.dashboards
    body = ["  dashboards:"]
    for n, d in WIRE_DASHBOARDS.items():
        body += _dash_lines(n, d, 4)
    hit = need_block("lovelace", body, "lovelace → dashboards Maison")
    if hit:
        i, end, inline = hit
        if inline:
            manual.append("lovelace: est défini par un include : ajoute-y les dashboards maison-tablette / maison-mobile.")
        else:
            ind = _child_indent(lines, i, end)
            ch = _children(lines, i, end, ind)
            if "dashboards" not in ch:
                new = [" " * ind + "dashboards:"]
                for n, d in WIRE_DASHBOARDS.items():
                    new += _dash_lines(n, d, ind + 2)
                lines[i + 1:i + 1] = new
                added.append("lovelace → dashboards Maison")
            else:
                j, inline_d = ch["dashboards"]
                if inline_d:
                    manual.append("lovelace.dashboards est défini en une ligne / par include : à compléter à la main.")
                else:
                    end2 = end
                    sub_ind = _child_indent(lines, j, end2)
                    have = _children(lines, j, end2, sub_ind)
                    new = []
                    for n, d in WIRE_DASHBOARDS.items():
                        if n not in have:
                            new += _dash_lines(n, d, sub_ind)
                            added.append(f"lovelace.dashboards → {n}")
                    lines[j + 1:j + 1] = new
    out = nl.join(lines) + nl
    # garde-fou : le YAML obtenu doit être valide et contenir ce qu'on a promis
    if added:
        try:
            d = yaml.load(out, Loader=_CfgLoader) or {}
            ok = ("packages" in (d.get("homeassistant") or {}) and "themes" in (d.get("frontend") or {})
                  and all(n in ((d.get("lovelace") or {}).get("dashboards") or {}) for n in WIRE_DASHBOARDS))
        except yaml.YAMLError:
            ok = False
        if not ok:
            return text, [], manual + ["Le fichier obtenu n'a pas pu être validé : aucune modification automatique (fais-le à la main)."]
    return (out if added else text), added, manual


def wire_preview():
    cfg = config_dir()
    f = cfg / "configuration.yaml"
    if not f.is_file() or f.is_symlink():
        raise SyncError("configuration.yaml introuvable.")
    old = f.read_text(encoding="utf-8")
    new, added, manual = plan_wiring(old)
    diff = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "configuration.yaml (actuel)",
                                          "configuration.yaml (après)", lineterm="", n=2))
    return {"added": added, "manual": manual, "diff": diff, "log": []}


def wire_apply():
    log = Run()
    cfg = config_dir()
    f = cfg / "configuration.yaml"
    if not f.is_file() or f.is_symlink():
        raise SyncError("configuration.yaml introuvable.")
    old = f.read_text(encoding="utf-8")
    new, added, manual = plan_wiring(old)
    for m in manual:
        log("✋ À faire à la main : " + m)
    if not added:
        log("Rien à ajouter : tout est déjà branché." if not manual else "Aucune modification automatique possible.")
        return {"log": log.lines, "changed": []}
    plan = {"add": [], "modify": ["configuration.yaml"], "delete": []}
    snap = snapshot(cfg, plan, None, load_state().get("last_applied"))
    log(f"💾 Sauvegarde de configuration.yaml : {snap}")
    f.write_text(new, encoding="utf-8")
    log("📝 Ajouté : " + ", ".join(added))
    if not core_check(log):
        _restore(snap, cfg, log)
        raise SyncError("La configuration HA est invalide avec ces lignes : configuration.yaml a été restauré.")
    log("✅ Terminé. Redémarre Home Assistant pour que le thème, les helpers et les dashboards apparaissent.")
    return {"log": log.lines, "changed": ["configuration.yaml"]}



# ───────────────────────── nettoyage : entités orphelines, ressources inutiles ─────────────────────────
# Parler au registre d'entités et aux ressources des dashboards demande le WebSocket de HA (pas d'API REST pour ça).
# Client WebSocket minimal (bibliothèque standard) : pas de dépendance à installer.
class HAWebSocket:
    def __init__(self, url=None, token=None, timeout=30):
        self.url = url or os.environ.get("HA_MAISON_WS_URL", "ws://supervisor/core/websocket")
        self.token = token or os.environ.get("HA_MAISON_WS_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
        if not self.token:
            raise SyncError("Jeton Supervisor absent : impossible de parler à Home Assistant (mode test ?).")
        u = urllib.parse.urlparse(self.url)
        host, port, path = u.hostname, u.port or 80, (u.path or "/") + (("?" + u.query) if u.query else "")
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise SyncError(f"WebSocket de HA injoignable ({e}).") from e
        self.buf = b""
        self.next_id = 0
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = self._read_until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise SyncError("Le WebSocket de HA a refusé la connexion.")
        hello = self._recv_json()
        if hello.get("type") != "auth_required":
            raise SyncError("Réponse inattendue du WebSocket de HA.")
        self._send_json({"type": "auth", "access_token": self.token})
        if self._recv_json().get("type") != "auth_ok":
            raise SyncError("HA a refusé l'authentification WebSocket.")

    def _read_until(self, marker):
        while marker not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise SyncError("Connexion WebSocket fermée.")
            self.buf += chunk
        i = self.buf.index(marker) + len(marker)
        out, self.buf = self.buf[:i], self.buf[i:]
        return out

    def _read_n(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise SyncError("Connexion WebSocket fermée.")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        head += bytes([0x80 | n]) if n < 126 else (bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _send_json(self, obj):
        self._send_frame(1, json.dumps(obj).encode())

    def _recv_json(self):
        msg = b""
        while True:
            b0, b1 = self._read_n(2)
            op, fin, n = b0 & 0x0F, b0 & 0x80, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_n(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_n(8))[0]
            payload = self._read_n(n)          # les trames du serveur ne sont pas masquées
            if op == 8:
                raise SyncError("Connexion WebSocket fermée par HA.")
            if op == 9:
                self._send_frame(10, payload)
                continue
            if op in (1, 0):
                msg += payload
                if fin:
                    return json.loads(msg.decode())

    def call(self, **msg):
        self.next_id += 1
        msg["id"] = self.next_id
        self._send_json(msg)
        while True:
            r = self._recv_json()
            if r.get("id") == self.next_id and r.get("type") == "result":
                if not r.get("success"):
                    raise SyncError(f"HA a refusé « {msg.get('type')} » : {(r.get('error') or {}).get('message', 'erreur')}")
                return r.get("result")

    def close(self):
        try:
            self._send_frame(8, b"")
            self.sock.close()
        except OSError:
            pass


# Aides générées par le dépôt (réglages d'affichage) : si elles ne sont plus définies dans les packages, elles restent dans HA
# comme entités « non fournies » (restored). On ne touche QU'À celles-là.
CLEAN_ENTITY_PREFIXES = ("input_boolean.maison_",)
# ressource (morceau de l'URL) → morceaux de texte qui prouvent qu'un dashboard l'utilise
RESOURCE_MARKERS = {
    "lovelace-mushroom": ["custom:mushroom-"], "mushroom": ["custom:mushroom-"],
    "button-card": ["custom:button-card"], "swipe-card": ["custom:swipe-card"],
    "clock-weather-card": ["custom:clock-weather-card"], "ha-today-card": ["custom:today-card"],
    "atomic-calendar-revive": ["custom:atomic-calendar-revive"], "rss-accordion": ["custom:rss-accordion"],
    "a-table": ["custom:a-table-card"], "stack-in-card": ["custom:stack-in-card"], "apexcharts-card": ["custom:apexcharts-card"],
    "mini-graph-card": ["custom:mini-graph-card"], "fold-entity-row": ["custom:fold-entity-row"],
    "hui-element": ["custom:hui-element"],
    "layout-card": ["custom:layout-card", "view_layout", "custom:grid-layout", "custom:masonry-layout", "custom:vertical-layout",
                    "custom:horizontal-layout", "layout:"],
}
RESOURCE_PROTECTED = ("browser_mod", "card-mod", "card_mod")      # jamais proposées à la suppression


def orphan_helpers(registry, states, prefixes=CLEAN_ENTITY_PREFIXES):
    """Entrées du registre qui ne correspondent plus à rien : préfixe de nos aides ET état « restauré » (non fourni)."""
    restored = {s["entity_id"] for s in states if (s.get("attributes") or {}).get("restored") is True}
    return sorted(e["entity_id"] for e in registry if e["entity_id"].startswith(prefixes) and e["entity_id"] in restored)


def dashboards_text(cfg):
    """Texte de tous les dashboards (YAML appliqués + dashboards créés dans l'interface), pour savoir quelles cartes servent."""
    parts = []
    for base in (cfg / "dashboards",):
        if base.is_dir():
            parts += [f.read_text(encoding="utf-8", errors="replace") for f in base.rglob("*.y*ml") if f.stat().st_size < 4_000_000]
    sto = cfg / ".storage"
    if sto.is_dir():
        for f in sto.glob("lovelace*"):
            if f.name not in ("lovelace_resources", "lovelace_dashboards") and f.stat().st_size < 8_000_000:
                parts.append(f.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts).lower()


def classify_resources(resources, corpus):
    out = []
    for r in resources:
        url = r.get("url", "")
        key = next((k for k in RESOURCE_MARKERS if k in url.lower()), None)
        protected = any(p in url.lower() for p in RESOURCE_PROTECTED)
        used = None if key is None else any(m in corpus for m in RESOURCE_MARKERS[key])
        out.append({"id": r.get("id"), "url": url, "type": r.get("type"), "protected": protected, "used": used,
                    "name": url.split("?")[0].rstrip("/").split("/")[-1] or url})
    return out


def clean_preview():
    cfg = config_dir()
    states = fetch_states()
    if states is None:
        raise SyncError("Impossible de lire les états de HA (API indisponible).")
    ws = HAWebSocket()
    try:
        registry = ws.call(type="config/entity_registry/list")
        try:
            resources = ws.call(type="lovelace/resources")
        except SyncError as e:           # dashboards en mode YAML : pas de ressources gérées par l'interface
            resources, note = [], str(e)
        else:
            note = ""
    finally:
        ws.close()
    orphans = orphan_helpers(registry, states)
    res = classify_resources(resources, dashboards_text(cfg))
    return {"orphans": orphans, "orphans_total": len(orphans), "resources": res, "note": note,
            "unused": [r["id"] for r in res if r["used"] is False and not r["protected"]]}


def clean_apply(body):
    do_entities = bool(body.get("entities"))
    wanted = set(body.get("resources") or [])
    log = Run()
    ws = HAWebSocket()
    try:
        if do_entities:
            registry = ws.call(type="config/entity_registry/list")
            states = fetch_states() or []
            ids = orphan_helpers(registry, states)
            gone = 0
            for eid in ids:
                try:
                    ws.call(type="config/entity_registry/remove", entity_id=eid)
                    gone += 1
                except SyncError as e:
                    log(f"⚠️ {eid} : {e}")
            log(f"🧹 {gone} entité(s) orpheline(s) supprimée(s) sur {len(ids)}.")
        if wanted:
            resources = {r["id"]: r for r in ws.call(type="lovelace/resources")}
            removed = []
            for rid in wanted:
                r = resources.get(rid)
                if not r:
                    log(f"⚠️ Ressource {rid} introuvable.")
                elif any(p in r.get("url", "").lower() for p in RESOURCE_PROTECTED):
                    log(f"🔒 {r['url']} : protégée, conservée.")
                else:
                    ws.call(type="lovelace/resources/delete", resource_id=rid)
                    removed.append({"url": r.get("url"), "type": r.get("type")})
                    log(f"🗑️ Ressource retirée : {r.get('url')}")
            if removed:      # de quoi la remettre si besoin
                DATA.mkdir(parents=True, exist_ok=True)
                prev = []
                f = DATA / "deleted_resources.json"
                if f.exists():
                    try:
                        prev = json.loads(f.read_text(encoding="utf-8"))
                    except ValueError:
                        prev = []
                f.write_text(json.dumps(prev + [{"date": time.strftime("%Y-%m-%d %H:%M"), **x} for x in removed], ensure_ascii=False, indent=1), encoding="utf-8")
                log("ℹ️ Ferme et rouvre HA / l'application pour que les ressources retirées ne se chargent plus.")
        if not do_entities and not wanted:
            log("Rien n'était sélectionné.")
    finally:
        ws.close()
    return {"log": log.lines}


def restart():
    log = Run()
    core_restart(log)
    return {"log": log.lines}
