"""Optional CLI extensions: Pi providers, user agent config, workflows.

This module is imported by ``free_agent`` and integrated with one call:

    extensions.initialize(free_agent)

``initialize`` registers builtin ``pi-*`` workers and, when a validated user
config exists, the agents declared in it. It is idempotent: a second call
replaces only the entries it registered itself and never touches builtins.

Configuration is user-owned. ``profiles.validate_config`` enforces argv-only
commands, known keys and placeholders; it does not sandbox the configured
program. ``role.tools`` is metadata for the caller, not enforcement: pass the
matching CLI flags in ``command``.

Secrets: provider keys are resolved privately and handed to
``providers.run_pi`` only; they never appear in argv. ``key_env`` reads one
named variable, never the whole env file, and command output is redacted.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import profiles
import providers as pv
import workflows

DEFAULT_ROLE = "executor"
DEFAULT_ROLE_PROMPT = "Complete the task: {task}"

# Providers that may bill or need an unverified free tier are opt-in only.
OPT_IN_PROVIDERS = (
    "groq", "cerebras", "mistral", "gemini", "cohere", "together", "deepinfra",
)
OPT_IN_MODEL_ENV = "FREE_AGENT_PROVIDER_MODEL"

# A user config must not be able to rewrite the loader path or the credential
# scope of the child process (accident prevention, not a security boundary;
# disclosed in docs/configuration.md). The name list is shared with profiles so
# key_env cannot reach a variable env entries may not set.
BLOCKED_ENV = profiles.BLOCKED_ENV_NAMES
BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "TERM")

_FA = None
_CONFIG = None
_CONFIG_PATH = None
_ENV_PATH = None
_REGISTERED: set[str] = set()


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def config_path() -> str:
    return os.environ.get("FREE_AGENT_CONFIG") or str(
        Path.home() / ".config/free-agent/agents.json"
    )


def env_path() -> str:
    return os.environ.get("FREE_AGENT_ENV_FILE") or str(Path.home() / ".env")


def manifest_default():
    return os.environ.get("FREE_AGENT_WORKFLOW_MANIFEST") or None


# ---------------------------------------------------------------------------
# private credential helpers
# ---------------------------------------------------------------------------

def _key(provider: str, path: str | None = None):
    """Provider key or None. A malformed env file is treated as absent."""
    try:
        return pv.get_key(provider, path or env_path())
    except (ValueError, OSError):
        return None


def _named_secret(name: str, path: str | None = None):
    """One named value from the environment or env file only; never the map."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        values = profiles.read_env_file(path or env_path(), strict=False, keep_nonempty=True)
    except (ValueError, OSError):
        return None
    value = values.get(name)
    return value or None


def _redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "[REDACTED]") if secret and text else text


# ---------------------------------------------------------------------------
# child environment for configured argv agents
# ---------------------------------------------------------------------------

def _base_env():
    """The minimal inherited environment: base keys plus a temp dir."""
    env = {key: os.environ[key] for key in BASE_ENV_KEYS if os.environ.get(key)}
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("TMPDIR", tempfile.gettempdir())
    return env


def _child_env(agent, path: str | None = None):
    env = _base_env()
    for key, value in (agent.get("env") or {}).items():
        if key not in BLOCKED_ENV:
            env[key] = value
    secret = None
    name = agent.get("key_env")
    if name:
        secret = _named_secret(name, path)
        if secret:
            env[name] = secret
    return env, secret


# ---------------------------------------------------------------------------
# pi provider workers
# ---------------------------------------------------------------------------

def _pi_model(provider: str) -> str:
    if provider in pv.DEFAULT_MODELS:
        return pv.DEFAULT_MODELS[provider]
    return os.environ.get(OPT_IN_MODEL_ENV) or ""


def _pi_installed() -> bool:
    return shutil.which("pi") is not None


def _pi_worker(fa, provider: str, automatic: bool, priority: int):
    def run(task, workdir, timeout):
        model = _pi_model(provider)
        try:
            return pv.run_pi(provider, model, task, workdir, timeout, env_path())
        except (ValueError, OSError) as exc:
            return 78, "", f"{provider}: {exc}"

    def extract(rc, out, err):
        result = fa.pi_extract(rc, out, err)
        model = _pi_model(provider)
        result.model = model or provider
        result.cost = "0" if provider == "openrouter" and result.ok else "unknown"
        return result

    def quota():
        model = _pi_model(provider) or "no model selected"
        return {"available": None, "pct": None,
                "text": f"Pi {provider}/{model}; no inference probe",
                "logged_in": None}

    def login():
        if _key(provider):
            return {"logged_in": None, "credential_present": True,
                    "text": f"{provider}: credential present (unverified)"}
        return {"logged_in": False, "credential_present": False,
                "text": f"{provider}: no credential"}

    model = _pi_model(provider)
    return fa.Worker(
        name=f"pi-{provider}",
        description=f"Pi coding worker ({provider}/{model or 'model via env'})",
        priority=priority,
        headless=True,
        reauth=f"pi auth login {provider}",
        run=run,
        extract=extract,
        quota=quota,
        login=login,
        installed=_pi_installed,
        roles=["executor"],
        automatic=automatic,
        specialty="implementation",
        model=model or "",
    )


def _pi_workers(fa):
    workers = {}
    for priority, provider in ((40, "nvidia"), (41, "openrouter")):
        model = _pi_model(provider)
        if model:
            workers[f"pi-{provider}"] = _pi_worker(
                fa, provider, bool(_key(provider)), priority
            )
    if os.environ.get(OPT_IN_MODEL_ENV):
        for offset, provider in enumerate(OPT_IN_PROVIDERS, start=62):
            workers[f"pi-{provider}"] = _pi_worker(fa, provider, False, offset)
    return workers


# ---------------------------------------------------------------------------
# user configured agents
# ---------------------------------------------------------------------------

def _config_worker(fa, agent):
    def run(task, workdir, timeout):
        limit = min(float(timeout), float(agent["timeout"]))
        try:
            argv = profiles.render_command(agent, task, workdir)
        except ValueError as exc:
            return 2, "", str(exc)
        env, secret = _child_env(agent)
        rc, out, err = fa.run_cmd(argv, cwd=workdir, timeout=limit, env=env)
        return rc, _redact(out, secret), _redact(err, secret)

    def extract(rc, out, err):
        return fa._generic_extract(rc, out, err)

    def quota():
        return {"available": None, "pct": None,
                "text": f"user agent ({agent['specialty'] or 'unspecified'}); "
                        "no inference probe", "logged_in": None}

    def login():
        name = agent.get("key_env")
        if not name:
            return {"logged_in": None, "text": "no key_env declared; not probed"}
        if _named_secret(name):
            return {"logged_in": None, "credential_present": True,
                    "text": f"{name}: present (unverified)"}
        return {"logged_in": False, "credential_present": False,
                "text": f"{name}: missing"}

    def installed():
        return shutil.which(agent["command"][0]) is not None

    return fa.Worker(
        name=agent["name"],
        description=f"user agent: {agent['specialty'] or agent['roles'][0]}",
        priority=90,
        headless=True,
        reauth=f"check key_env/env of {agent['name']}",
        run=run,
        extract=extract,
        quota=quota,
        login=login,
        installed=installed,
        roles=list(agent["roles"]),
        automatic=agent["automatic"],
        specialty=agent["specialty"],
        model="",
    )


def _native_extras(fa):
    """OAuth Gemini CLI is opt-in; its account/config is never inferred."""
    def run(task, workdir, timeout):
        # Minimal env keeps the stored Google login (HOME) without inheriting
        # unrelated variables or silently opting into paid Vertex/API.
        env = _base_env()
        return fa.run_cmd(["gemini", "-p", task, "--output-format", "json",
                           "--approval-mode", "auto_edit"], cwd=workdir,
                          timeout=timeout, env=env)
    def extract(rc, out, err):
        try:
            payload = json.loads(out)
        except ValueError:
            return fa.Result(False, "", reason="Gemini returned no JSON response")
        text = payload.get("response", "")
        if not isinstance(text, str):
            text = ""
        return fa.Result(rc == 0 and bool(text) and not payload.get("error"), text,
                         reason="" if text else "Gemini login/configuration required",
                         model="gemini-oauth", cost="unknown")
    return {"gemini": fa.Worker(
        name="gemini", description="Google-login coding CLI; account allowance",
        priority=65, headless=True, reauth="gemini (official Google login)",
        run=run, extract=extract,
        quota=lambda: {"text": "Use Gemini /stats model; account quota unknown"},
        login=lambda: {"logged_in": None, "text": "Google login not probed"},
        installed=lambda: shutil.which("gemini") is not None,
        roles=["executor"], automatic=False, specialty="implementation")}


def _builtin_names() -> set[str]:
    workers = _FA.WORKERS if _FA is not None else {}
    return {name for name in workers if name not in _REGISTERED}


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------

def initialize(fa, path: str | None = None, skip_user_config: bool = False) -> None:
    """Register pi and user agents once. Idempotent.

    Collides with a builtin name -> ValueError. A second call replaces the
    entries this module registered earlier and leaves everything else alone.
    """
    global _FA, _CONFIG, _CONFIG_PATH, _ENV_PATH
    _FA = fa
    _CONFIG_PATH = path or config_path()
    _ENV_PATH = env_path()

    config = None
    if not skip_user_config and os.path.isfile(_CONFIG_PATH):
        config = profiles.load_config(_CONFIG_PATH)

    dynamic = _pi_workers(fa)
    dynamic.update(_native_extras(fa))
    if config:
        for agent in config["agents"]:
            name = agent["name"]
            if name in dynamic:
                raise ValueError(f"agent {name!r} collides with a builtin pi worker")
            dynamic[name] = _config_worker(fa, agent)

    base = {name: worker for name, worker in fa.WORKERS.items()
            if name not in _REGISTERED}
    for name in dynamic:
        if name in base:
            raise ValueError(f"agent {name!r} collides with a builtin worker")

    for name in list(_REGISTERED):
        fa.WORKERS.pop(name, None)
    fa.WORKERS.update(dynamic)
    _REGISTERED.clear()
    _REGISTERED.update(dynamic)
    _CONFIG = config


def role_prompt_for(worker, role, task):
    """Role prompt for this worker, or None when no matching role exists."""
    if not _CONFIG or not role or role not in worker.roles:
        return None
    if role not in _CONFIG["roles"]:
        return None
    return profiles.role_prompt(_CONFIG, role, task)


def loaded_config():
    return _CONFIG


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------

def _emit(args, payload, text):
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(text)


def _fail(args, message: str) -> int:
    _emit(args, {"ok": False, "error": message}, f"error: {message}")
    return 1


# ---------------------------------------------------------------------------
# agent / roles commands
# ---------------------------------------------------------------------------

def _edit_config(path):
    if not os.path.exists(path):
        config = {"version": profiles.VERSION, "agents": [], "roles": {}}
        try:
            profiles.validate_config(config)
        except ValueError:
            return {"version": profiles.VERSION, "agents": [], "roles": {
                DEFAULT_ROLE: {"description": "Import an agent",
                               "prompt": DEFAULT_ROLE_PROMPT, "tools": []}}}
        return config
    return profiles.load_config(path)


def _agent_payload(path, config):
    summary = profiles.public_summary(config) if config else {"agents": [], "roles": {}}
    return {"ok": True, "config": path, "exists": os.path.exists(path),
            "agents": summary["agents"], "roles": summary["roles"]}





def cmd_agent(args) -> int:
    path = args.config or config_path()
    try:
        if args.agent_cmd == "list":
            config = profiles.load_config(path) if os.path.exists(path) else None
            payload = _agent_payload(path, config)
            lines = [f"config: {path} ({'present' if os.path.exists(path) else 'absent, builtin only'})"]
            for agent in payload["agents"]:
                lines.append(
                    "{name} roles={roles} automatic={automatic} command={command}".format(
                        name=agent["name"], roles=",".join(agent["roles"]),
                        automatic=agent["automatic"], command=" ".join(agent["command"]),
                    )
                )
            if not payload["agents"]:
                lines.append("no user agents")
            _emit(args, payload, "\n".join(lines))
            return 0

        if args.agent_cmd == "validate":
            if not os.path.exists(path):
                _emit(args, {"ok": True, "config": path, "exists": False,
                             "agents": 0, "roles": 0},
                      f"{path}: absent, builtin workers only")
                return 0
            config = profiles.load_config(path)
            payload = _agent_payload(path, config)
            payload.update({"ok": True, "agents": len(config["agents"]),
                            "roles": len(config["roles"])})
            _emit(args, payload, f"{path}: ok ({len(config['agents'])} agents, "
                                 f"{len(config['roles'])} roles)")
            return 0

        if args.agent_cmd == "add":
            config = _edit_config(path)
            if any(agent["name"] == args.name for agent in config["agents"]):
                return _fail(args, f"duplicate agent name: {args.name}")
            if args.name in _REGISTERED or args.name in _builtin_names():
                return _fail(args, f"agent {args.name!r} collides with a builtin worker")
            try:
                command = json.loads(args.command_json)
            except ValueError as exc:
                return _fail(args, f"--command-json is not valid JSON: {exc}")
            if isinstance(command, str):
                return _fail(args, "command must be an argv array, not a shell string")
            if not isinstance(command, list) or not command or not all(
                isinstance(item, str) for item in command
            ):
                return _fail(args, "command must be a non-empty JSON array of strings")
            role = args.role or DEFAULT_ROLE
            if role in profiles.RESERVED_ROLES:
                return _fail(args, f"role {role!r} is reserved for deterministic replay")
            if role not in config["roles"]:
                config["roles"][role] = {
                    "description": args.specialty or "imported agent role",
                    "prompt": DEFAULT_ROLE_PROMPT,
                    "tools": [],
                }
            config["agents"].append({
                "name": args.name,
                "command": command,
                "roles": [role],
                "specialty": args.specialty or "",
                "automatic": bool(args.automatic),
                "timeout": profiles.DEFAULT_TIMEOUT,
                "env": {},
                "key_env": None,
            })
            profiles.save_config(path, config)
            _emit(args, {"ok": True, "config": path, "agent": args.name, "role": role},
                  f"added {args.name} ({role}) to {path}")
            return 0

        if args.agent_cmd == "remove":
            if not os.path.exists(path):
                return _fail(args, f"config not found: {path}")
            config = profiles.load_config(path)
            remaining = [agent for agent in config["agents"]
                         if agent["name"] != args.name]
            if len(remaining) == len(config["agents"]):
                return _fail(args, f"unknown agent: {args.name}")
            config["agents"] = remaining
            profiles.save_config(path, config)
            _emit(args, {"ok": True, "config": path, "removed": args.name,
                         "config_removed": False},
                  f"removed {args.name} from {path}")
            return 0
    except (ValueError, OSError) as exc:
        return _fail(args, str(exc))
    return _fail(args, "unknown agent command")


def cmd_roles(args) -> int:
    path = args.config or config_path()
    try:
        if args.roles_cmd == "list":
            config = profiles.load_config(path) if os.path.exists(path) else None
            roles = config["roles"] if config else {}
            payload = {"ok": True, "config": path, "roles": roles}
            lines = [f"config: {path}"] + [
                "{name}: {description} tools={tools} prompt_chars={chars}".format(
                    name=name, description=spec["description"],
                    tools=",".join(spec["tools"]) or "-", chars=len(spec["prompt"]),
                )
                for name, spec in roles.items()
            ]
            if not roles:
                lines.append("no roles")
            _emit(args, payload, "\n".join(lines))
            return 0

        if args.roles_cmd == "set":
            config = _edit_config(path)
            existing = config["roles"].get(args.name, {})
            tools = existing.get("tools", [])
            if args.tools is not None:
                tools = [item.strip() for item in args.tools.split(",") if item.strip()]
            config["roles"][args.name] = {
                "description": args.description
                if args.description is not None else existing.get("description", ""),
                "prompt": (Path(args.prompt_file).read_text() if getattr(args, "prompt_file", None) else args.prompt),
                "tools": tools,
            }
            profiles.save_config(path, config)
            _emit(args, {"ok": True, "config": path, "role": args.name},
                  f"set role {args.name} in {path}")
            return 0
    except (ValueError, OSError) as exc:
        return _fail(args, str(exc))
    return _fail(args, "unknown roles command")


# ---------------------------------------------------------------------------
# providers command
# ---------------------------------------------------------------------------

def _models_payload(name, result):
    models = [
        {"id": model.get("id"), "name": model.get("name"), "tools": model.get("tools")}
        for model in result.get("models") or []
        if isinstance(model, dict)
    ]
    return {"ok": bool(result.get("ok")), "provider": name,
            "status": result.get("status"), "error": result.get("error", ""),
            "models": models}


def cmd_providers(args) -> int:
    path = args.env_file or env_path()
    try:
        if args.provider_cmd == "list":
            rows = pv.inventory(path)
            lines = [f"env file: {path}"] + [
                "{name}: credential={credential_present} state={state} policy={free_policy}".format(**row)
                for row in rows
            ]
            _emit(args, {"ok": True, "env_file": path, "providers": rows},
                  "\n".join(lines))
            return 0

        if args.provider_cmd == "models":
            payload = _models_payload(args.name, pv.discover(args.name, path))
            lines = [f"{args.name}: {payload['status']} {payload['error']}".strip()] + [
                "{id} tools={tools}".format(**model) for model in payload["models"]
            ]
            _emit(args, payload, "\n".join(lines))
            return 0 if payload["ok"] else 1

        if args.provider_cmd == "run":
            workdir = os.path.abspath(args.workdir or os.getcwd())
            if not os.path.isdir(workdir):
                return _fail(args, f"workdir not found: {workdir}")
            model = args.model or pv.DEFAULT_MODELS.get(args.name) or ""
            rc, out, err = pv.run_pi(args.name, model, args.task, workdir,
                                     args.timeout, path, read_only=args.read_only)
            result = _FA.validated_extract(_FA.pi_extract, rc, out, err)
            result.model = model or args.name
            # Only an OpenRouter run is verified free by run_pi; never invent 0.
            cost = "0" if args.name == "openrouter" and result.ok else "unknown"
            payload = {
                "ok": result.ok, "provider": args.name, "model": result.model,
                "cost": cost, "text": result.text, "status": rc,
                "error": "" if result.ok else (result.reason or err.strip()[:400]),
            }
            text = result.text if result.ok else f"{args.name}: {payload['error']}"
            _emit(args, payload, text)
            return 0 if result.ok else 2
    except (ValueError, OSError) as exc:
        return _fail(args, str(exc))
    return _fail(args, "unknown providers command")


# ---------------------------------------------------------------------------
# workflows command
# ---------------------------------------------------------------------------

def _store_list(args):
    workdir = os.path.abspath(args.workdir or os.getcwd())
    stores = [os.path.abspath(os.path.join(workdir, args.store))]
    if args.all_stores:
        for candidate in ("~/.hermes/.replant",
                          "~/.hermes/plugins/physis/.replant",
                          "~/dev/hermes-physis/.replant"):
            path = os.path.realpath(os.path.expanduser(candidate))
            if path not in stores:
                stores.append(path)
    return workdir, stores


def _compact_store(path):
    report = workflows.inspect_store(path)
    history = report.get("history")
    history_count = len(history) if isinstance(history, list) else None
    # A long native journal is not useful in a listing; keep only its size.
    return {
        "store": report.get("store", path),
        "stats": report.get("stats"),
        "history_count": history_count,
        "learned": report.get("learned", []),
        "candidates": len(report.get("candidates", [])),
        "stale": report.get("stale", []),
        "unindexed": report.get("unindexed", []),
        "invalid": report.get("invalid", []),
        "truncated": report.get("truncated", False),
    }


def _proposals(workdir):
    directory = os.path.join(workdir, ".free-agent", "candidates")
    rows = []
    try:
        names = sorted(os.listdir(directory))[:100]
    except OSError:
        return rows
    for name in names:
        row = {"file": name}
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                row.update({"status": data.get("status"),
                            "approved": data.get("approved"),
                            "replayable": data.get("replayable")})
        except (OSError, ValueError):
            row["error"] = "unreadable candidate"
        rows.append(row)
    return rows


def cmd_workflows(args) -> int:
    try:
        if args.workflows_cmd == "list":
            workdir, stores = _store_list(args)
            payload = {"ok": True, "workdir": workdir,
                       "stores": [_compact_store(store) for store in stores],
                       "proposals": _proposals(workdir)}
            lines = [f"workdir: {workdir}"]
            for store in payload["stores"]:
                lines.append(
                    f"{store['store']}: learned={len(store['learned'])} "
                    f"history={store['history_count']} stale={len(store['stale'])} "
                    f"unindexed={len(store['unindexed'])} "
                    f"invalid={len(store['invalid'])}"
                )
            lines.append(f"own candidates: {len(payload['proposals'])}")
            _emit(args, payload, "\n".join(lines))
            return 0

        if args.workflows_cmd == "run":
            workdir = os.path.abspath(args.workdir or os.getcwd())
            manifest = args.manifest or manifest_default()
            result = workflows.replay(args.task, workdir, manifest, timeout=args.timeout)
            payload = {"ok": result.get("status") == "replayed", "replay": result}
            _emit(args, payload,
                  f"{result.get('status')}: {result.get('reason')}")
            return 0 if payload["ok"] else 1

        if args.workflows_cmd == "propose":
            workdir = os.path.abspath(args.workdir or os.getcwd())
            path = workflows.propose(
                args.task, workdir, args.check, args.artifact or [],
                metadata={"source": "free-agent workflows propose"},
            )
            _emit(args, {"ok": True, "candidate": path, "approved": False,
                         "replayable": False},
                  f"review-only candidate: {path}")
            return 0
    except (ValueError, OSError) as exc:
        return _fail(args, str(exc))
    return _fail(args, "unknown workflows command")
