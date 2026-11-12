"""Free-LLM directory integration.

The catalog at ``data/freellm_providers.json`` is generated from the
[Free-LLM](https://github.com/nejib1/Free-LLM) provider directory (README
Quick Reference + Permanent/Renewable/Trial tables + per-provider
``code-examples/``). It is the read-only source of base URLs, free model ids,
card requirements and limits.

This module is deliberately independent of the rest of free-agent: it can be
used by the ``scripts/freellm`` CLI, by tests, or by other tools. It never
prints a credential; :func:`resolve_key` returns the source label and the
value, and callers decide what to do with it.

Key resolution order: process environment first, then ``~/.env`` (last
non-empty assignment, because real dotenv files carry duplicates and blank
placeholders). Candidate names come from the provider name/slug plus the
curated :data:`PROVIDER_KEY_ALIASES` map, so one provider never borrows
another provider's key.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CATALOG = ROOT / "data" / "freellm_providers.json"
DEFAULT_TIMEOUT = 30

# Env var aliases for providers whose key name is not derivable from the
# provider name. Keeps ``check`` from grabbing an unrelated provider's key.
PROVIDER_KEY_ALIASES = {
    "groq": ["GROQ_API_KEY", "AI_GROQ_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "llm7-io": ["LLM7_API_KEY"],
    "google-ai-studio": ["GEMINI_API_KEY", "GOOGLE_API_KEY", "AI_GEMINI_KEY"],
    "nvidia-nim": ["NVIDIA_API_KEY"],
    "z-ai": ["Z_AI_API_KEY", "AI_ZHIPU_KEY", "ZHIPU_API_KEY"],
    "zhipu-bigmodel": ["AI_ZHIPU_KEY", "ZHIPU_API_KEY"],
    "cloudflare-workers-ai": ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_KEY"],
    "aion-labs": ["AION_LABS"],
    "kilo-ai-gateway": ["KILO_API_KEY"],
    "ollama-cloud": ["OLLAMA_CLOUD_API_KEY"],
    "nous-portal": ["NOUS_API_KEY"],
    "modelscope": ["MODELSCOPE_API_KEY"],
    "siliconflow": ["SILICONFLOW_API_KEY"],
    "cohere": ["COHERE_API_KEY", "AI_COHERE_KEY"],
    "mistral": ["MISTRAL_API_KEY", "AI_MISTRAL_KEY"],
    "cerebras": ["CEREBRAS_API_KEY", "AI_CEREBRAS_KEY"],
    "together": ["TOGETHER_API_KEY", "TOGETHERAI_API_KEY"],
    "deepinfra": ["DEEPINFRA_API_KEY"],
    "sambanova": ["SAMBANOVA_API_KEY", "AI_SAMBA_NOVA_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "xai": ["XAI_API_KEY"],
    "ai-horde": ["AI_HORDE_API_KEY"],
}


def load_catalog(path: Path | str | None = None) -> dict:
    p = Path(path) if path else CATALOG
    if not p.exists():
        raise FileNotFoundError(f"Free-LLM catalog missing: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def find(catalog: dict, needle: str) -> dict:
    """Exact slug/name match first, then a unique substring match."""
    n = needle.strip().lower()
    for p in catalog["providers"]:
        if p["slug"].lower() == n or p["name"].lower() == n:
            return p
    hits = [p for p in catalog["providers"]
            if n in p["slug"].lower() or n in p["name"].lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise KeyError(f"no provider matching {needle!r}")
    raise KeyError(f"ambiguous provider {needle!r}: "
                   + ", ".join(p["slug"] for p in hits))


def candidate_key_envs(p: dict) -> list[str]:
    """Reasonable env var names for a provider key, most specific first."""
    out: list[str] = []
    for n in {p["name"], p["slug"].replace("-", "_")}:
        base = re.sub(r"[^A-Za-z0-9]+", "_", n).strip("_").upper()
        out += [f"{base}_API_KEY", f"AI_{base}_KEY"]
    out += PROVIDER_KEY_ALIASES.get(p["slug"], [])
    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def read_dotenv(key: str, path: Path | str | None = None) -> str:
    """Last NON-EMPTY assignment for ``key`` in a dotenv file."""
    p = Path(path) if path else Path.home() / ".env"
    if not p.exists():
        return ""
    val = ""
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() != key:
                continue
            v = v.strip().strip('"').strip("'").strip()
            if v:
                val = v
    except OSError:
        return ""
    return val


def resolve_key(p: dict, explicit: str | None = None) -> tuple[str, str] | None:
    """Return ``(source_label, key)`` from the environment, then ``~/.env``."""
    names = [explicit] if explicit else candidate_key_envs(p)
    for name in names:
        if not name:
            continue
        val = os.environ.get(name, "").strip()
        if val and val not in ("YOUR_API_KEY", "YOUR_KEY", "..."):
            return name, val
    for name in names:
        if not name:
            continue
        val = read_dotenv(name)
        if val:
            return f"~/.env:{name}", val
    return None


def check_provider(p: dict, model: str | None = None,
                   timeout: int = DEFAULT_TIMEOUT,
                   key_env: str | None = None) -> tuple[int, str, str]:
    """Live one-line completion probe.

    Returns ``(http_status, content_or_error, key_source)``. Uses ``curl``
    because some providers' edges block ``urllib``'s TLS fingerprint.
    """
    if not p["openai_compatible"]:
        return 2, "not OpenAI-compatible; no automatic probe", ""
    resolved = resolve_key(p, key_env)
    source, key = resolved if resolved else ("", "")
    model = model or p["example_model"]
    if not model:
        return 2, "no model known; pass one explicitly", source
    url = p["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: FREE_LLM_OK"}],
        "max_tokens": 256,
    })
    headers = ["-H", "Content-Type: application/json"]
    if key:
        headers += ["-H", f"Authorization: Bearer {key}"]
    try:
        proc = subprocess.run(
            ["curl", "-sS", "-m", str(timeout), "-w", "\n%{http_code}", url]
            + headers
            + ["-d", body],
            capture_output=True, text=True, timeout=timeout + 10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 0, f"{type(exc).__name__}: {exc}", source
    out = (proc.stdout or "").strip()
    payload, _, status = out.rpartition("\n") if "\n" in out else (out, "", "0")
    try:
        code = int(status)
    except ValueError:
        code = 0
    if code != 200:
        return code, payload[:500], source
    try:
        data = json.loads(payload)
    except ValueError:
        return 200, payload[:500], source
    if isinstance(data, dict) and data.get("error"):
        return 400, json.dumps(data, ensure_ascii=False)[:500], source
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return 200, text, source


def opencode_snippet(p: dict, key_env: str | None = None,
                     model: str | None = None) -> dict:
    """An opencode.json provider block for this provider."""
    model = model or p["example_model"]
    if not model:
        raise ValueError(f"{p['slug']}: no model known")
    options: dict[str, str] = {"baseURL": p["base_url"]}
    if key_env:
        options["apiKey"] = "{env:" + key_env + "}"
    return {
        p["slug"].replace("-", "_"): {
            "npm": "@ai-sdk/openai-compatible",
            "name": p["name"],
            "options": options,
            "models": {model: {"name": model}},
        }
    }
