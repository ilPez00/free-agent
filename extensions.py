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
    "groq-cloud", "cerebras", "mistral", "gemini", "cohere", "together", "deepinfra",
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
    env_model = os.environ.get(OPT_IN_MODEL_ENV)
    if env_model:
        return env_model
    if provider in pv.DEFAULT_MODELS:
        return pv.DEFAULT_MODELS[provider]
    return ""


def _pi_installed() -> bool:
    return shutil.which("pi") is not None


def _pi_model_order(provider: str) -> list[str]:
    """Return the model_order list for a provider from config, or default single model."""
    import json
    model_order = profiles.named_config_value(provider, "model_order")
    if model_order:
        try:
            return json.loads(model_order) if isinstance(model_order, str) else model_order
        except (json.JSONDecodeError, TypeError):
            pass
    model = _pi_model(provider)
    return [model] if model else []


def _pi_worker(fa, provider: str, automatic: bool, priority: int):
    model_order = _pi_model_order(provider)
    default_model = model_order[0] if model_order else _pi_model(provider)

    def run(task, workdir, timeout):
        last_err = ""
        for idx, model in enumerate(model_order):
            try:
                rc, out, err = pv.run_pi(provider, model, task, workdir, timeout, env_path())
            except (ValueError, OSError) as exc:
                last_err = f"{provider}: {exc}"
                continue

            # Skip models the provider does not offer on the free route; try the
            # next entry instead of failing the whole worker.
            if rc == 2 and err and "not verified free" in err:
                last_err = err
                continue

            # Check for exhaustion signals in response
            exhausted = False
            if rc in (429, 402) or (err and any(p in err.lower() for p in
                ["quota", "rate limit", "usage limit", "exhausted", "payment required", "insufficient credits", "out of credits", "limit exceeded", "daily limit", "weekly limit", "monthly limit", "spend_limited", "model_locked", "model_unavailable", "ip_capped", "premium_slot_taken", "purchase_capacity"])):
                exhausted = True

            # Also check stdout for quota messages
            if not exhausted and out:
                out_lower = out.lower()
                if any(p in out_lower for p in
                    ["quota", "rate limit", "usage limit", "exhausted", "payment required", "insufficient credits", "out of credits", "limit exceeded", "daily limit", "weekly limit", "monthly limit", "spend_limited", "model_locked", "model_unavailable", "ip_capped", "premium_slot_taken", "purchase_capacity"]):
                    exhausted = True

            if exhausted and idx + 1 < len(model_order):
                # Try next model
                continue

            # Success or final failure - return result
            return rc, out, err

        # All models exhausted
        return 429, "", f"{provider}: all models in order exhausted" + (f" ({last_err})" if last_err else "")

    def extract(rc, out, err):
        result = fa.pi_extract(rc, out, err)
        result.model = default_model or provider
        result.cost = "0" if provider == "openrouter" and result.ok else "unknown"
        return result

    def quota():
        model = default_model or "no model selected"
        return {"available": None, "pct": None,
                "text": f"Pi {provider}/{model}; model_order: {model_order}",
                "logged_in": None}

    def login():
        if _key(provider):
            return {"logged_in": None, "credential_present": True,
                    "text": f"{provider}: credential present (unverified)"}
        return {"logged_in": False, "credential_present": False,
                "text": f"{provider}: no credential"}

    return fa.Worker(
        name=f"pi-{provider}",
        description=f"Pi coding worker ({provider}/{default_model or 'model via env'})",
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
        model=default_model or "",
    )


def _pi_workers(fa):
    workers = {}
    # Primary free providers (automatic when credential present)
    for priority, provider in ((40, "nvidia"), (41, "openrouter")):
        model = _pi_model(provider)
        if model:
            workers[f"pi-{provider}"] = _pi_worker(
                fa, provider, bool(_key(provider)), priority
            )
    # All other providers with pi: True and a credential: register opt-in pi variants
    # They are NOT automatic (free tier unverified); activate via config or env.
    import providers as pv
    offset = 62
    for name, cfg in pv.PROVIDERS.items():
        if name in ("nvidia", "openrouter"):
            continue
        if not cfg.get("pi"):
            continue
        if cfg.get("free_policy") in ("paid_or_trial_opt_in", "credential_only"):
            continue
        if not _key(name):
            continue
        model = _pi_model(name)
        if not model:
            continue
        workers[f"pi-{name}"] = _pi_worker(fa, name, False, offset)
        offset += 1
    # Legacy opt-in env path (kept for compatibility)
    if os.environ.get(OPT_IN_MODEL_ENV):
        for offset, provider in enumerate(OPT_IN_PROVIDERS, start=offset):
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


def _cf_account_id() -> str:
    return os.environ.get("CLOUDFLARE_ACCOUNT_ID") or profiles._named_config_value("cloudflare", "account_id") or ""


def _cf_model() -> str:
    return os.environ.get(OPT_IN_MODEL_ENV) or profiles._named_config_value("cloudflare", "model") or ""


def _cf_model_order() -> list[str]:
    """Return the model_order list for Cloudflare from config, or default single model."""
    import json
    model_order = profiles.named_config_value("cloudflare", "model_order")
    if model_order:
        try:
            return json.loads(model_order) if isinstance(model_order, str) else model_order
        except (json.JSONDecodeError, TypeError):
            pass
    model = _cf_model()
    return [model] if model else []


def _cf_worker(fa):
    model_order = _cf_model_order()
    default_model = model_order[0] if model_order else _cf_model()

    def run(task, workdir, timeout):
        account = _cf_account_id()
        if not account:
            return 78, "", "cloudflare: account not configured (providers setup cloudflare)"
        last_err = ""
        for idx, model in enumerate(model_order):
            if not model:
                continue
            try:
                rc, out, err = pv.run_cloudflare(model, task, workdir, timeout, env_path(), account)
            except (ValueError, OSError) as exc:
                last_err = f"cloudflare: {exc}"
                continue

            # Check for exhaustion signals
            exhausted = False
            if rc in (429, 402) or (err and any(p in err.lower() for p in
                ["quota", "rate limit", "usage limit", "exhausted", "payment required", "insufficient credits", "out of credits", "limit exceeded", "daily limit", "weekly limit", "monthly limit", "spend_limited", "model_locked", "model_unavailable", "ip_capped", "premium_slot_taken", "purchase_capacity"])):
                exhausted = True

            if not exhausted and out:
                out_lower = out.lower()
                if any(p in out_lower for p in
                    ["quota", "rate limit", "usage limit", "exhausted", "payment required", "insufficient credits", "out of credits", "limit exceeded", "daily limit", "weekly limit", "monthly limit", "spend_limited", "model_locked", "model_unavailable", "ip_capped", "premium_slot_taken", "purchase_capacity"]):
                    exhausted = True

            if exhausted and idx + 1 < len(model_order):
                continue

            return rc, out, err

        return 429, "", f"cloudflare: all models in order exhausted" + (f" ({last_err})" if last_err else "")

    def extract(rc, out, err):
        try:
            payload = json.loads(out)
        except ValueError:
            text = out.strip()
            return fa.Result(rc == 0 and bool(text), text,
                             reason="cloudflare: no JSON response" if rc == 0 else "cloudflare: failed")
        content = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        text = ((content.get("response") or "") if isinstance(content, dict) else "")
        if not text:
            text = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        if not isinstance(text, str):
            text = ""
        ok = rc == 0 and bool(text) and not payload.get("errors")
        return fa.Result(ok, text, reason="" if ok else (payload.get("errors") or "cloudflare: failed"),
                         model=default_model or "cloudflare", cost="unknown")

    def quota():
        return {"available": None, "pct": None,
                "text": f"Cloudflare Workers AI; model_order: {model_order}", "logged_in": None}

    def login():
        if _key("cloudflare") and _cf_account_id():
            return {"logged_in": None, "credential_present": True,
                    "text": "Cloudflare token/account configured (unverified)"}
        return {"logged_in": False, "credential_present": bool(_key("cloudflare")),
                "text": "Cloudflare token/account not fully configured"}

    return fa.Worker(
        name="cloudflare", description="Cloudflare Workers AI (explicit opt-in)",
        priority=66, headless=True, reauth="Cloudflare dashboard token/account",
        run=run, extract=extract, quota=quota, login=login,
        installed=lambda: True, roles=["executor"], automatic=False,
        specialty="implementation", model=default_model)


def _native_extras(fa):
    """Cloudflare Workers AI plus OAuth Gemini CLI; account/config explicit."""
    workers = {"cloudflare": _cf_worker(fa)}

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
    workers["gemini"] = fa.Worker(
        name="gemini", description="Google-login coding CLI; account allowance",
        priority=65, headless=True, reauth="gemini (official Google login)",
        run=run, extract=extract,
        quota=lambda: {"text": "Use Gemini /stats model; account quota unknown"},
        login=lambda: {"logged_in": None, "text": "Google login not probed"},
        installed=lambda: shutil.which("gemini") is not None,
        roles=["executor"], automatic=False, specialty="implementation")
    return workers


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

def _edit_config(path, providers=None, cli_agents=None):
    if not os.path.exists(path):
        config = {"version": profiles.VERSION, "agents": [], "roles": {}, "providers": providers or {}, "cli_agents": cli_agents or []}
        try:
            profiles.validate_config(config)
        except ValueError:
            return {"version": profiles.VERSION, "agents": [], "roles": {
                DEFAULT_ROLE: {"description": "Import an agent",
                               "prompt": DEFAULT_ROLE_PROMPT, "tools": []}},
                "providers": providers or {}, "cli_agents": cli_agents or []}
        return config
    config = profiles.load_config(path)
    if providers:
        config["providers"] = providers
    if cli_agents is not None:
        config["cli_agents"] = cli_agents
    return config


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


def _provider_config_setup(args) -> int:
    path = args.config or config_path()
    provider = args.name
    if provider not in profiles.KEY_ALIASES:
        return _fail(args, f"unknown provider: {provider}")
    account_id = args.account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID") or ""
    model = args.model or os.environ.get(OPT_IN_MODEL_ENV) or ""
    if provider == "cloudflare":
        if not account_id:
            return _fail(args, "cloudflare: account_id required")
        if not model:
            return _fail(args, "cloudflare: explicit model required")
    providers = {provider: {"account_id": account_id, "model": model}}
    try:
        config = _edit_config(path)
        existing = config.get("providers") or {}
        existing.update(providers)
        config["providers"] = existing
        profiles.save_config(path, config)
    except (ValueError, OSError) as exc:
        return _fail(args, str(exc))
    _emit(args, {"ok": True, "config": path, "provider": provider,
                 "account_id_set": bool(account_id), "model": model or None},
          f"configured {provider} in {path}")
    return 0


def cmd_providers(args) -> int:
    path = args.env_file or env_path()
    try:
        if args.provider_cmd == "setup":
            return _provider_config_setup(args)

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
            if args.name == "cloudflare":
                account = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or profiles.named_config_value(
                    "cloudflare", "account_id")
                if not account:
                    return _fail(args, "cloudflare: account_id required")
                key = _key("cloudflare", path)
                if not key:
                    return _fail(args, "cloudflare: no credential")
                status, data, error = pv._http_json(
                    f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/models/search"
                    f"?task=Text%20Generation&per_page=100", key)
                if status != 200 or data is None:
                    return _fail(args, f"cloudflare: {error or 'http_error'}")
                raw = data.get("result") or []
                rows = [{"id": m.get("name"), "name": None,
                         "tools": m.get("properties") and "tools" in str(m.get("properties"))}
                        for m in raw if isinstance(m, dict) and m.get("name")]
                payload = {"ok": True, "provider": "cloudflare", "status": status,
                           "error": "", "models": rows}
                _emit(args, payload, "\n".join([f"cloudflare: {status}"] + [
                    f"{row['id']} tools={row['tools']}" for row in rows]))
                return 0
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
            if args.name == "cloudflare":
                account = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or profiles.named_config_value(
                    "cloudflare", "account_id")
                if not account:
                    return _fail(args, "cloudflare: account_id required")
                if not model:
                    return _fail(args, "cloudflare: explicit model required")
                rc, out, err = pv.run_cloudflare(model, args.task, workdir, args.timeout,
                                                 path, account)
            else:
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
# cli_agents command
# ---------------------------------------------------------------------------

def _cli_agent_payload(path, config):
    cli_agents = config.get("cli_agents") if config else []
    return {"ok": True, "config": path, "cli_agents": cli_agents}


def cmd_cli_agents(args) -> int:
    path = args.config or config_path()
    try:
        if args.cli_agent_cmd == "list":
            config = profiles.load_config(path) if os.path.exists(path) else None
            payload = _cli_agent_payload(path, config)
            lines = [f"config: {path}"]
            for ca in payload["cli_agents"]:
                lines.append(
                    "{name} priority={priority} automatic={automatic} headless={headless} roles={roles} model_env={model_env} model_flag={model_flag}".format(
                        name=ca["name"], priority=ca["priority"], automatic=ca["automatic"],
                        headless=ca["headless"], roles=",".join(ca["roles"]),
                        model_env=ca.get("model_env") or "-", model_flag=ca.get("model_flag") or "-",
                    )
                )
            if not payload["cli_agents"]:
                lines.append("no cli agents")
            _emit(args, payload, "\n".join(lines))
            return 0

        if args.cli_agent_cmd == "validate":
            if not os.path.exists(path):
                _emit(args, {"ok": True, "config": path, "exists": False, "cli_agents": 0},
                      f"{path}: absent, builtin workers only")
                return 0
            config = profiles.load_config(path)
            payload = _cli_agent_payload(path, config)
            _emit(args, {"ok": True, "config": path, "cli_agents": len(config.get("cli_agents", []))},
                  f"{path}: ok ({len(config.get('cli_agents', []))} cli agents)")
            return 0

        if args.cli_agent_cmd == "add":
            config = _edit_config(path)
            if any(ca["name"] == args.name for ca in config.get("cli_agents", [])):
                return _fail(args, f"duplicate cli_agent name: {args.name}")
            if args.name in _REGISTERED or args.name in _builtin_names():
                return _fail(args, f"cli_agent {args.name!r} collides with a builtin worker")
            try:
                command = json.loads(args.command_json)
            except ValueError as exc:
                return _fail(args, f"--command-json is not valid JSON: {exc}")
            if isinstance(command, str):
                return _fail(args, "command must be an argv array, not a shell string")
            if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
                return _fail(args, "command must be a non-empty JSON array of strings")

            ca = {
                "name": args.name,
                "command": command,
                "model_filter": args.model_filter,
                "model_env": args.model_env,
                "model_flag": args.model_flag,
                "quota_check": json.loads(args.quota_check_json) if args.quota_check_json else None,
                "login_check": json.loads(args.login_check_json) if args.login_check_json else None,
                "headless": not args.no_headless,
                "automatic": args.automatic,
                "priority": args.priority,
                "roles": [r.strip() for r in args.roles.split(",")] if args.roles else ["executor"],
                "specialty": args.specialty or "",
                "timeout": args.timeout,
                "env": {},
            }
            config.setdefault("cli_agents", []).append(ca)
            profiles.save_config(path, config)
            _emit(args, {"ok": True, "config": path, "cli_agent": args.name},
                  f"added cli_agent {args.name} to {path}")
            return 0

        if args.cli_agent_cmd == "remove":
            if not os.path.exists(path):
                return _fail(args, f"config not found: {path}")
            config = profiles.load_config(path)
            remaining = [ca for ca in config.get("cli_agents", []) if ca["name"] != args.name]
            if len(remaining) == len(config.get("cli_agents", [])):
                return _fail(args, f"unknown cli_agent: {args.name}")
            config["cli_agents"] = remaining
            profiles.save_config(path, config)
            _emit(args, {"ok": True, "config": path, "removed": args.name},
                  f"removed {args.name} from {path}")
            return 0

    except (ValueError, OSError, json.JSONDecodeError) as exc:
        return _fail(args, str(exc))
    return _fail(args, "unknown cli_agents command")


def _load_cli_agent_workers(fa, path=None):
    """Load CLI agents from config and create Worker objects."""
    if path is None:
        path = config_path()
    try:
        config = profiles.load_config(path)
    except (ValueError, OSError):
        return {}
    cli_agents = config.get("cli_agents", [])
    workers = {}
    for ca in cli_agents:
        name = ca["name"]
        command_template = ca["command"]
        model_filter = ca.get("model_filter")
        model_env = ca.get("model_env")
        model_flag = ca.get("model_flag")
        quota_check = ca.get("quota_check")
        login_check = ca.get("login_check")
        headless = ca["headless"]
        automatic = ca["automatic"]
        priority = ca["priority"]
        roles = ca["roles"]
        specialty = ca["specialty"]
        timeout = ca["timeout"]
        env = ca.get("env", {})

        def make_runner(cmd_tpl, mf, me, mflag, qc, lc, hd, env_vars):
            def run(task, workdir, timeout):
                cmd = [profiles._render(item, {"task": task, "workdir": workdir}, profiles.COMMAND_PLACEHOLDERS)
                       for item in cmd_tpl]
                if me and mf:
                    pass
                if mflag and mf:
                    pass
                run_env = os.environ.copy()
                run_env.update(env_vars)
                return fa.run_cmd(cmd, cwd=workdir, timeout=timeout, env=run_env)
            return run

        def make_extractor(qc, lc):
            def extract(rc, out, err):
                return fa.Result(rc == 0 and bool(out.strip()), out.strip(),
                                 reason="" if rc == 0 else "cli agent failed",
                                 model=name, cost="unknown")
            return extract

        def make_quota(qc):
            def quota():
                if qc:
                    rc, out, err = fa.run_cmd(qc, timeout=30)
                    return {"available": None, "pct": None, "text": out.strip()[:120] if rc == 0 else "quota check failed", "logged_in": None}
                return {"available": None, "pct": None, "text": "CLI agent (no quota check configured)", "logged_in": None}
            return quota

        def make_login(lc):
            def login():
                if lc:
                    rc, out, err = fa.run_cmd(lc, timeout=30)
                    return {"logged_in": rc == 0, "text": out.strip()[:120]}
                return {"logged_in": None, "text": "CLI agent (no login check configured)"}
            return login

        workers[name] = fa.Worker(
            name=name,
            description=f"CLI agent: {name}",
            priority=priority,
            headless=headless,
            reauth=f"check {name} login/quota",
            run=make_runner(command_template, model_filter, model_env, model_flag, quota_check, login_check, headless, env),
            extract=make_extractor(quota_check, login_check),
            quota=make_quota(quota_check),
            login=make_login(login_check),
            installed=lambda n=name: shutil.which(command_template[0]) is not None,
            roles=roles,
            automatic=automatic,
            specialty=specialty,
            model="",
        )
    return workers


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
