"""Agent profiles: config validation, safe rendering, env and credential lookup.

Commands are argv lists, never shell strings; no shell is invoked. Only {task}
and {workdir} are substituted, and substituted text is never rescanned; `{{` and
`}}` stay literal braces. Env files are parsed with shlex, never sourced or
evaluated, and command substitution is rejected. Credential values are returned
to the caller only, never logged or written.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from pathlib import Path

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
COMMAND_SYNTAX_RE = re.compile(r"`|\$\(|\$\{")

VERSION = 1
DEFAULT_TIMEOUT = 1800
COMMAND_PLACEHOLDERS = frozenset({"task", "workdir"})
PROMPT_PLACEHOLDERS = frozenset({"task"})
AGENT_KEYS = frozenset({"name", "command", "roles", "specialty", "automatic",
                        "timeout", "env", "key_env"})
ROLE_KEYS = frozenset({"description", "prompt", "tools"})

# A user config must not be able to rewrite the loader path or the credential
# scope of the child process. Shared so no credential path can bypass it.
BLOCKED_ENV_NAMES = frozenset({
    "PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "PYTHONPATH",
    "PYTHONHOME", "PYTHONSTARTUP", "BASH_ENV", "ENV", "NODE_OPTIONS",
    "PERL5LIB", "RUBYOPT", "GCONV_PATH", "IFS", "SHELLOPTS", "BASHOPTS",
    "CDPATH", "GIT_SSH_COMMAND",
})
RESERVED_ROLES = frozenset({"runner"})

# Canonical alias order; env beats file, and earlier aliases beat later ones.
KEY_ALIASES = {
    "nvidia": ("NVIDIA_API_KEY", "AI_NVIDIA_KEY"),
    "openrouter": ("OPENROUTER_API_KEY", "AI_OPENROUTER_KEY"),
    "groq": ("GROQ_API_KEY", "AI_GROQ_KEY", "GROQ_AI1"),
    "cerebras": ("CEREBRAS_API_KEY", "AI_CEREBRAS_KEY", "CEREBRAS"),
    "mistral": ("MISTRAL_API_KEY", "AI_MISTRAL_KEY", "MISTRAL"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "AI_GEMINI_KEY"),
    "huggingface": ("HF_TOKEN", "HUGGINGFACE_API_KEY", "SVC_HUGGINGFACE_TOKEN"),
    "cloudflare": ("CLOUDFLARE_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE"),
    "sambanova": ("SAMBANOVA_API_KEY", "AI_SAMBANOVA_KEY"),
    "cohere": ("COHERE_API_KEY", "AI_COHERE_KEY"),
    "together": ("TOGETHER_API_KEY", "AI_TOGETHER_KEY"),
    "deepinfra": ("DEEPINFRA_API_KEY", "AI_DEEPINFRA_KEY"),
}
PROVIDERS = tuple(KEY_ALIASES)


def _fail(message):
    raise ValueError(message)


def _render(template, values, allowed):
    """Substitute an explicit allow-list of placeholders; anything else errors."""
    if not isinstance(template, str):
        _fail("template must be a string")
    out, i = [], 0
    while i < len(template):
        ch, pair = template[i], template[i:i + 2]
        if pair in ("{{", "}}"):
            out.append(ch)
            i += 2
        elif ch == "{":
            end = template.find("}", i + 1)
            if end < 0:
                _fail("unterminated placeholder")
            name = template[i + 1:end]
            if name not in allowed:
                _fail("unsupported placeholder {%s}" % name)
            out.append(values[name])
            i = end + 1
        elif ch == "}":
            _fail("unmatched '}'")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _validate_roles(roles):
    if not isinstance(roles, dict):
        _fail("roles must be a mapping")
    clean = {}
    for name, spec in roles.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            _fail("bad role name: %r" % (name,))
        if not isinstance(spec, dict):
            _fail("role %s must be an object" % name)
        if set(spec) - ROLE_KEYS:
            _fail("role %s has unknown keys" % name)
        prompt = spec.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            _fail("role %s needs a non-empty prompt" % name)
        _render(prompt, {"task": ""}, PROMPT_PLACEHOLDERS)
        description, tools = spec.get("description", ""), spec.get("tools", [])
        if not isinstance(description, str) or not isinstance(tools, list) or not all(
            isinstance(t, str) for t in tools
        ):
            _fail("role %s needs a string description and string-array tools" % name)
        clean[name] = {"description": description, "prompt": prompt, "tools": list(tools)}
    return clean


def _validate_agents(agents, roles):
    if not isinstance(agents, list):
        _fail("agents must be an array")
    seen, clean = set(), []
    for entry in agents:
        if not isinstance(entry, dict):
            _fail("each agent must be an object")
        if set(entry) - AGENT_KEYS:
            _fail("agent has unknown keys")
        name = entry.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            _fail("bad agent name: %r" % (name,))
        if name in seen:
            _fail("duplicate agent name: %s" % name)
        seen.add(name)
        command = entry.get("command")
        if isinstance(command, str):
            _fail("agent %s command must be an argv array, not a shell string" % name)
        if not isinstance(command, list) or not command:
            _fail("agent %s command must be a non-empty argv array" % name)
        for item in command:
            if not isinstance(item, str) or "\x00" in item:
                _fail("agent %s command items must be strings" % name)
            _render(item, {"task": "", "workdir": ""}, COMMAND_PLACEHOLDERS)
        wanted = entry.get("roles")
        if not isinstance(wanted, list) or not wanted:
            _fail("agent %s roles must be a non-empty array" % name)
        for role in wanted:
            if not isinstance(role, str) or role not in roles:
                _fail("agent %s references unknown role: %r" % (name, role))
            if role in RESERVED_ROLES:
                _fail("agent %s may not claim reserved role: %s" % (name, role))
        specialty = entry.get("specialty")
        if specialty is not None and not isinstance(specialty, str):
            _fail("agent %s specialty must be a string" % name)
        automatic, timeout = entry.get("automatic", False), entry.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(automatic, bool):
            _fail("agent %s automatic must be a boolean" % name)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            _fail("agent %s timeout must be an integer from 1 to 3600" % name)
        key_env = entry.get("key_env")
        if key_env is not None and (
            not isinstance(key_env, str) or not ENV_NAME_RE.match(key_env)
        ):
            _fail("agent %s key_env must be an env var name" % name)
        if key_env in BLOCKED_ENV_NAMES:
            _fail("agent %s key_env may not name a blocked variable: %s" % (name, key_env))
        env = entry.get("env") or {}
        if not isinstance(env, dict):
            _fail("agent %s env must be an object" % name)
        for key, value in env.items():
            if not isinstance(key, str) or not ENV_NAME_RE.match(key) or not isinstance(
                value, str
            ) or "\x00" in value:
                _fail("agent %s has invalid env entry: %r" % (name, key))
        clean.append(
            {
                "name": name,
                "command": list(command),
                "roles": list(wanted),
                "specialty": specialty or "",
                "automatic": automatic,
                "timeout": timeout,
                "env": dict(env),
                "key_env": key_env,
            }
        )
    return clean


def validate_config(data):
    """Return a normalised deep copy of a version-1 config, or raise ValueError."""
    if not isinstance(data, dict) or set(data) - {"version", "agents", "roles"}:
        _fail("config must be an object with version, agents and roles only")
    if data.get("version") != VERSION:
        _fail("unsupported version: %r" % (data.get("version"),))
    roles = _validate_roles(data.get("roles"))
    return {"version": VERSION, "agents": _validate_agents(data.get("agents"), roles), "roles": roles}


def load_config(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON config: %s" % exc) from None
    return validate_config(data)


def save_config(path, data):
    """Atomically write a validated config with mode 600 (parents created 700)."""
    clean = validate_config(data)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".profiles-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(clean, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def render_command(definition, task, workdir):
    """Render an agent definition's argv; returns a new list, never a string."""
    if not isinstance(definition, dict) or "command" not in definition:
        _fail("definition must be a config agent object")
    if not isinstance(task, str) or not isinstance(workdir, str):
        _fail("task and workdir must be strings")
    values = {"task": task, "workdir": workdir}
    return [_render(item, values, COMMAND_PLACEHOLDERS) for item in definition["command"]]


def role_prompt(config, role_name, task):
    """Render a role prompt for a task; prompts may only use {task}."""
    roles = config.get("roles") if isinstance(config, dict) else None
    if not isinstance(task, str):
        _fail("task must be a string")
    if not isinstance(roles, dict) or role_name not in roles:
        raise KeyError("unknown role: %s" % role_name)
    return _render(roles[role_name]["prompt"], {"task": task}, PROMPT_PLACEHOLDERS)


def _strip_unquoted_comment(raw):
    """Strip a trailing # comment only when outside quotes/escapes and at start or after whitespace."""
    out = []
    i = 0
    in_single = in_double = False
    escaped = False
    while i < len(raw):
        ch = raw[i]
        if escaped:
            out.append(ch)
            escaped = False
        elif in_single:
            out.append(ch)
            if ch == "'":
                in_single = False
        elif in_double:
            out.append(ch)
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif ch == "#":
            if i == 0 or raw[i - 1].isspace():
                break
            out.append(ch)
        else:
            out.append(ch)
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "\\":
                escaped = True
        i += 1
    return "".join(out)


def read_env_file(path, strict=True, keep_nonempty=False):
    """Parse assignments without shell evaluation; lenient mode skips other lines.

    Credential lookup uses lenient mode because a shared .env may contain shell
    headings or unrelated malformed entries. Neither mode evaluates substitutions.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        try:
            if "=" not in line:
                _fail("expected KEY=VALUE")
            key, raw = line.split("=", 1)
            key = key.strip()
            if not ENV_NAME_RE.match(key):
                _fail("invalid name")
            raw = _strip_unquoted_comment(raw.strip())
            tokens = shlex.split(raw, comments=False)
            if len(tokens) > 1:
                _fail("unquoted whitespace in value")
            value = tokens[0] if tokens else ""
            if COMMAND_SYNTAX_RE.search(value):
                _fail("substitution is not allowed")
        except ValueError:
            if strict:
                _fail("invalid env assignment at line %d" % lineno)
            continue
        if value or not keep_nonempty or key not in values:
            values[key] = value
    return values


def _usable(mapping, alias):
    value = (mapping or {}).get(alias)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return "" if not value or value.startswith("$") or COMMAND_SYNTAX_RE.search(value) else value


def credential_for(provider, path, environ=None):
    """Return the first usable key value for a provider, env before file, or None."""
    aliases = KEY_ALIASES.get(provider)
    if aliases is None:
        raise KeyError("unknown provider: %s" % provider)
    env = os.environ if environ is None else environ
    for values in (env, read_env_file(path, strict=False, keep_nonempty=True)):
        for alias in aliases:
            value = _usable(values, alias)
            if value:
                return value
    return None


def public_summary(config):
    """Describe a config for logs/output: no key values, env values, or argv arguments."""
    clean = validate_config(config)
    agents = [
        {
            "name": a["name"],
            "command": [os.path.basename(a["command"][0])],
            "argument_count": len(a["command"]) - 1,
            "roles": list(a["roles"]),
            "specialty": a["specialty"],
            "automatic": a["automatic"],
            "timeout": a["timeout"],
            "env_names": sorted(a["env"]),
            "has_key_env": bool(a["key_env"]),
        }
        for a in clean["agents"]
    ]
    roles = {name: {"description": r["description"], "tools": list(r["tools"]),
                    "prompt_chars": len(r["prompt"])} for name, r in clean["roles"].items()}
    return {"version": clean["version"], "agents": agents, "roles": roles}
