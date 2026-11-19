"""Provider registry, free-model discovery and an isolated Pi launch.

Key aliases are canonical in profiles.KEY_ALIASES; profiles.credential_for is the
single authority for credential resolution. Key values are never returned by
inventory(), never written to disk and never echoed in errors or Pi output.

free_policy meanings:
  free_only                    OpenRouter: only models proven $0 prompt+completion
  dev_evaluation               NVIDIA dev evaluation, not an account free tier
  account_free_tier_unverified vendor advertises a free tier but it is unverified
  paid_or_trial_opt_in         may bill; never probed automatically
  credential_only              usable only with extra account/config, not runnable
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

TIMEOUT = 20
MAX_BODY = 5 * 1024 * 1024
KEY_VAR = "FREE_AGENT_PROVIDER_KEY"

PROVIDERS = {
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "free_policy": "dev_evaluation", "pi": True,
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "free_policy": "free_only", "pi": True,
    },
    "groq-cloud": {
        "base_url": "https://api.groq.com/openai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "free_policy": "paid_or_trial_opt_in", "pi": True,
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "sambanova": {
        "base_url": "https://api.sambanova.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": False,
    },
    "cohere": {
        "base_url": "https://api.cohere.com/compatibility/v1",
        "free_policy": "paid_or_trial_opt_in", "pi": True,
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "free_policy": "paid_or_trial_opt_in", "pi": True,
    },
    "deepinfra": {
        "base_url": "https://api.deepinfra.com/v1/openai",
        "free_policy": "paid_or_trial_opt_in", "pi": True,
    },
    "cloudflare": {
        "base_url": "https://api.cloudflare.com/client/v4/accounts",
        "free_policy": "account_free_tier_unverified", "pi": False,
    },
    "huggingface": {
        "base_url": None,
        "free_policy": "credential_only", "pi": False,
    },
    "aion-labs": {
        "base_url": "https://api.aionlabs.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "ai-horde": {
        "base_url": "https://oai.aihorde.net/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "aihubmix": {
        "base_url": "https://aihubmix.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "anyapi": {
        "base_url": "https://api.anyapi.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "api-airforce": {
        "base_url": "https://api.airforce/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "arli-ai": {
        "base_url": "https://api.arliai.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "bazaarlink": {
        "base_url": "https://api.bazaarlink.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "bytez": {
        "base_url": "https://api.bytez.com/models/v2/openai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "cerebrium": {
        "base_url": "https://api.cortex.cerebrium.ai/v4",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "dahl-inference": {
        "base_url": "https://inference.dahl.global/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "experiential-labs": {
        "base_url": "https://api.experientiallabs.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "fireworks-ai": {
        "base_url": "https://api.fireworks.ai/inference/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "freeinference": {
        "base_url": "https://freeinference.org/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "friendli-ai": {
        "base_url": "https://inference.friendli.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "gonka-broker": {
        "base_url": "https://proxy.gonkabroker.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "gonkarouter": {
        "base_url": "https://api.gonkarouter.io/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "google-vertex-ai-express-mode": {
        "base_url": "https://aiplatform.googleapis.com/v1/publishers/google/models/MODEL_ID:generateContent",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "grokified": {
        "base_url": "https://api.grokified.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "hetzner-inference": {
        "base_url": "https://inference.hetzner.com/api/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "hpc-ai": {
        "base_url": "https://api.hpc-ai.com/inference/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "hyperbolic": {
        "base_url": "https://api.hyperbolic.ai/inference/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "ibm-watsonx": {
        "base_url": "https://us-south.ml.cloud.ibm.com/ml/v1/text/chat?version=2025-02-11",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "iflytek-spark": {
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "inception-labs": {
        "base_url": "https://api.inceptionlabs.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "inferbase": {
        "base_url": "https://api.inferbase.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "iteracompute": {
        "base_url": "https://api.iteracompute.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "jina-ai": {
        "base_url": "https://api.jina.ai/v1/embeddings",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "kilo-ai-gateway": {
        "base_url": "https://ai-gateway.kilo.ai/api/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "kiosapi": {
        "base_url": "https://api.kiosapi.id/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "llm-kiwi": {
        "base_url": "https://api.llm.kiwi/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "llm7-io": {
        "base_url": "https://api.llm7.io/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "llmtr": {
        "base_url": "https://llmtr.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "meganova": {
        "base_url": "https://api.meganova.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "mixlayer": {
        "base_url": "https://models.mixlayer.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "moark": {
        "base_url": "https://api.moark.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "modelscope": {
        "base_url": "https://api-inference.modelscope.cn/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "nous-portal": {
        "base_url": "https://inference-api.nousresearch.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "novai": {
        "base_url": "https://aiapi-pro.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "novita-ai": {
        "base_url": "https://api.novita.ai/v3/openai",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "nvidia-nim": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "ollama-cloud": {
        "base_url": "https://ollama.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "opentyphoon": {
        "base_url": "https://api.opentyphoon.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "ovh-ai": {
        "base_url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "pollinations-ai": {
        "base_url": "https://gen.pollinations.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True, "keyless": True,
    },
    "qwen-alibaba": {
        "base_url": "https://dashscope-intl.aliyuncs.com/api/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "regolo-ai": {
        "base_url": "https://api.regolo.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "replicate": {
        "base_url": "https://api.replicate.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "requesty": {
        "base_url": "https://router.requesty.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "routeway": {
        "base_url": "https://api.routeway.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "sail-research": {
        "base_url": "https://api.sailresearch.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "sarvam-ai": {
        "base_url": "https://api.sarvam.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "scaleway": {
        "base_url": "https://api.scaleway.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "sea-lion": {
        "base_url": "https://api.sea-lion.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "sensenova": {
        "base_url": "https://token.sensenova.cn/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "simplellm": {
        "base_url": "https://api.simplellm.eu/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "tencent-cloud-tokenhub": {
        "base_url": "https://tokenhub-intl.tencentmaas.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "token-harbor": {
        "base_url": "https://tokenharbor.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "upstage": {
        "base_url": "https://api.upstage.ai/v1/solar",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "vectoree": {
        "base_url": "https://vectoree.ai/api/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "vercel-ai-gateway": {
        "base_url": "https://ai-gateway.vercel.sh/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "vlm-run": {
        "base_url": "https://gateway.vlm.run/v1/openai",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "voyage-ai": {
        "base_url": "https://api.voyageai.com/v1/embeddings",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "z-ai": {
        "base_url": "https://api.z.ai/api/paas/v4",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "zhipu-bigmodel": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "zro": {
        "base_url": "https://zro.moonmath.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "apifreellm": {
        "base_url": "https://apifreellm.com/api/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "zenmux": {
        "base_url": "https://zenmux.ai/api/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "venice": {
        "base_url": "https://api.venice.ai/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "github-models": {
        "base_url": "https://models.github.ai/inference",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "agnes-ai": {
        "base_url": "https://apihub.agnes-ai.com/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
    "gptoss-proxy": {
        "base_url": "https://broken-water-d859.junioralive.workers.dev/v1",
        "free_policy": "account_free_tier_unverified", "pi": True, "keyless": True,
    },
    "g4f": {
        "base_url": "https://g4f.space/v1",
        "free_policy": "account_free_tier_unverified", "pi": True, "keyless": True,
    },
    "ollama-public": {
        "base_url": "http://108.181.196.208:11434/v1",
        "free_policy": "account_free_tier_unverified", "pi": True, "keyless": True,
    },
    "commandcode": {
        "base_url": "https://api.commandcode.ai/provider/v1",
        "free_policy": "account_free_tier_unverified", "pi": True,
    },
}

# Only these two have a default model; every other provider needs an explicit one.
DEFAULT_MODELS = {
    "nvidia": "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "openrouter": "openrouter/free",
    "groq-cloud": "openai/gpt-oss-120b",
    "cerebras": "cerebras-labs/llama-3.3-70b",
    "gemini": "gemini-2.5-flash",
    "sambanova": "Meta-Llama-3.3-70B-Instruct",
    "cloudflare": "@cf/meta/llama-3.1-8b-fast-v2",
    "aion-labs": "aion-1.0-plus",
    "nvidia-nim": "nvidia/llama-3.3-nemotron-super-49b-a12b-v1",
    "scaleway": "meta-llama/llama-3.3-70b-instruct",
    "ai-horde": "llama-3.3-70b",
    "pollinations-ai": "llama-3.3-70b",
    "z-ai": "glm-4.5-flash",
    "zhipu-bigmodel": "glm-4.5-flash",
    "siliconflow": "Qwen/Qwen2.5-Coder-32B-Instruct",
    "qwen-alibaba": "qwen2.5-coder-32b-instruct",
    "upstage": "solar-pro",
    "sarvam-ai": "sarvam-34b",
    "sensenova": "gpt-4o-mini",
    "hyperbolic": "meta-llama/llama-3.3-70b-instruct",
    "deepinfra": "meta-llama/llama-3.3-70b-instruct",
    "fireworks-ai": "accounts/fireworks/models/llama-v3p3-70b-instruct",
    "novita-ai": "meta-llama/llama-3.3-70b-instruct",
    "friendli-ai": "meta-llama-llama-3.3-70b-instruct",
    "llm-kiwi": "meta-llama/llama-3.3-70b-instruct",
    "llm7-io": "codestral-latest",
    "meganova": "meta-llama/llama-3.3-70b-instruct",
    "mixlayer": "meta-llama/llama-3.3-70b-instruct",
    "moark": "meta-llama/llama-3.3-70b-instruct",
    "modelscope": "Qwen/Qwen2.5-Coder-32B-Instruct",
    "nous-portal": "qwen/qwen4-max:free",
    "novai": "meta-llama/llama-3.3-70b-instruct",
    "opentyphoon": "meta-llama/llama-3.3-70b-instruct",
    "requesty": "meta-llama/llama-3.3-70b-instruct",
    "routeway": "meta-llama/llama-3.3-70b-instruct",
    "simplellm": "meta-llama/llama-3.3-70b-instruct",
    "token-harbor": "meta-llama/llama-3.3-70b-instruct",
    "vectoree": "meta-llama/llama-3.3-70b-instruct",
    "vlm-run": "meta-llama/llama-3.3-70b-instruct",
    "zro": "meta-llama/llama-3.3-70b-instruct",
    "apifreellm": "apifree-flash",
    "zenmux": "z-ai/glm-4.7-flash-free",
    "venice": "llama-3.3-70b",
    "agnes-ai": "agnes-3.0-flash",
    "gptoss-proxy": "gpt-oss-120b",
    "g4f": "auto",
    "ollama-public": "llama3.2:latest",
    "aihubmix": "nemotron-3.5-super-vl-free",
    "api-airforce": "gpt-oss-120b",
    "kilo-ai-gateway": "kilo-auto/free",
    "ollama-cloud": "gpt-oss:120b",
    "commandcode": "stealth/glyph-cluster:free",
}

AUTO_DEFAULT_MODELS = {
    "nvidia": "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "openrouter": "openrouter/free",
}

_STATE = {
    "free_only": "free_only_discovery",
    "dev_evaluation": "dev_evaluation_opt_in",
    "account_free_tier_unverified": "requires_opt_in",
    "paid_or_trial_opt_in": "requires_opt_in",
    "credential_only": "credential_only_not_runnable",
}

_HTTP_ERROR = {401: "unauthorized", 402: "payment_required", 403: "forbidden",
               404: "not_found", 413: "response_too_large", 429: "rate_limited"}


def _config(provider: str) -> dict:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise ValueError(f"unknown provider: {provider}") from None


def _profiles():
    # profiles.py is a sibling built separately; import late so this module
    # stays importable on its own and tests can inject a fake.
    import profiles
    return profiles


def get_key(provider: str, env_path):
    """Return the provider credential privately, or None. Never log or store it."""
    _config(provider)
    return _profiles().credential_for(provider, env_path) or None


def inventory(env_path) -> list[dict]:
    """Rows of name, credential_present, free_policy, state. No credentials."""
    rows = []
    for name, cfg in PROVIDERS.items():
        present = bool(get_key(name, env_path))
        rows.append({
            "name": name,
            "credential_present": present,
            "free_policy": cfg["free_policy"],
            "state": _STATE[cfg["free_policy"]] if present else "no_credential",
        })
    return rows


def _same_origin(a: str, b: str) -> bool:
    def key(url):
        u = urlsplit(url)
        scheme = u.scheme.lower()
        port = u.port or (443 if scheme == "https" else 80)
        return scheme, (u.hostname or "").lower(), port
    return key(a) == key(b)


class _SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _same_origin(req.full_url, newurl):
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        raise HTTPError(newurl, code, "cross-origin redirect denied", headers, fp)


def _http_json(url: str, key: str):
    """GET url with the key in a header only. Returns (status, data, error)."""
    req = Request(url, headers={"Authorization": f"Bearer {key}",
                                "Accept": "application/json"})
    try:
        with build_opener(_SameOriginRedirect).open(req, timeout=TIMEOUT) as resp:
            body = resp.read(MAX_BODY + 1)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        return status, None, _HTTP_ERROR.get(
            status, "redirect_denied" if status in (301, 302, 303, 307, 308) else "http_error")
    except Exception:
        return 0, None, "network_error"
    if len(body) > MAX_BODY:
        return 413, None, "response_too_large"
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return 200, None, "bad_json"
    return 200, data, ""


def _zero_price(model: dict) -> bool:
    pricing = model.get("pricing")
    if not isinstance(pricing, dict):
        return False
    for field in ("prompt", "completion"):
        try:
            value = Decimal(str(pricing[field]))
        except (KeyError, InvalidOperation, ValueError):
            return False
        if not value.is_finite() or value != 0:
            return False
    return True


def _tools(model: dict):
    params = model.get("supported_parameters")
    return ("tools" in params) if isinstance(params, list) else None


def _openrouter_free(model: dict) -> bool:
    mid = model.get("id")
    if not isinstance(mid, str) or not mid:
        return False
    return (mid.endswith(":free") or mid == "openrouter/free") and _zero_price(model)


def _models(provider: str, data):
    if not isinstance(data, dict):
        return None
    raw = data.get("data", data.get("models"))
    if not isinstance(raw, list):
        return None
    rows = []
    for model in raw:
        if not isinstance(model, dict):
            continue
        if provider == "openrouter" and not _openrouter_free(model):
            continue
        mid = model.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        rows.append({"id": mid, "name": model.get("name"), "tools": _tools(model)})
    return rows


def _fail(status: int, error: str) -> dict:
    return {"ok": False, "status": status, "models": [], "error": error}


def discover(provider: str, env_path) -> dict:
    """GET <fixed base>/models. Errors are short codes, never server text or keys."""
    cfg = _config(provider)
    key = get_key(provider, env_path)
    if not key:
        return _fail(0, "no_credential")
    if not cfg["base_url"]:
        return _fail(0, "not_runnable")
    status, data, error = _http_json(cfg["base_url"].rstrip("/") + "/models", key)
    if error or status != 200:
        return _fail(status, error or _HTTP_ERROR.get(status, "http_error"))
    models = _models(provider, data)
    if models is None:
        return _fail(status, "unexpected_response")
    return {"ok": True, "status": status, "models": models, "error": ""}


_TOOLS_READ_ONLY = "read,grep,find,ls"
_TOOLS_WRITE = "read,bash,write,edit"


def _write_pi_config(tmp: str, provider: str, model: str, base_url: str,
                     keyless: bool = False) -> None:
    models = {"providers": {f"fa-{provider}": {
        "baseUrl": base_url,
        "api": "openai-completions",
        # Keyless endpoints ignore Authorization; pi still wants a non-empty value.
        "apiKey": "keyless" if keyless else "${" + KEY_VAR + "}",
        "models": [{"id": model, "contextWindow": 65536,
                    "maxTokens": 8192, "input": ["text"]}],
    }}}
    for name, payload in (("models.json", models), ("settings.json", {})):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.chmod(path, 0o600)


def _child_env(tmp: str, key: str) -> dict:
    # Minimal allowlist: no ambient API keys. The bash tool's children inherit
    # the provider key; there is no sandbox guarantee.
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", tmp),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TMPDIR": tmp,
        "TERM": os.environ.get("TERM", "dumb"),
        "PI_CODING_AGENT_DIR": tmp,
        KEY_VAR: key,
    }
    return env


def _spawn(cmd, cwd, timeout, env):
    timeout = max(0.05, min(float(timeout), 3600))
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    except OSError as exc:
        return 127, "", f"could not launch {cmd[0]}: {exc.strerror}"
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = proc.communicate()
        return 124, out or "", (err or "") + f"\ntimeout after {timeout}s"


def _redact(text: str, key: str) -> str:
    return text.replace(key, "[REDACTED]") if key else text


def run_cloudflare(model: str, task: str, workdir: str, timeout: float,
                   env_path, account_id: str):
    """Run one Cloudflare Workers AI chat completion. Key stays in the header."""
    key = get_key("cloudflare", env_path)
    if not key:
        return 78, "", "cloudflare: no credential"
    if not account_id:
        return 78, "", "cloudflare: account_id required"
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions"
    payload = json.dumps({"model": model, "messages": [{"role": "user", "content": task}],
                          "max_tokens": 8192}).encode()
    req = Request(url, data=payload, headers={"Authorization": f"Bearer {key}",
                                               "Content-Type": "application/json"})
    try:
        with build_opener(_SameOriginRedirect).open(req, timeout=max(1, min(float(timeout), 3600))) as resp:
            body = resp.read(MAX_BODY)
            data = json.loads(body.decode("utf-8", "replace"))
    except HTTPError as exc:
        return exc.code, "", "cloudflare: http_error"
    except Exception:
        return 0, "", "cloudflare: network_error"
    choices = data.get("choices") or []
    text = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
    return (0 if text else 1), text, "" if text else "cloudflare: empty response"


def run_pi(provider: str, model: str, task: str, workdir: str, timeout: float,
           env_path, read_only: bool = False):
    """Run one isolated Pi coding process. Returns (rc, out, err), key redacted.

    The key travels only in the child env (models.json holds the ${...}
    placeholder). Pi passes it to the model request; children of the bash tool
    inherit it, so this is credential isolation, not a sandbox.
    """
    cfg = _config(provider)
    if not cfg["pi"] or not cfg["base_url"]:
        return 78, "", f"{provider}: not runnable without extra account/config"
    # Only the verified free routes auto-default; every other provider needs an
    # explicit model so a paid account is never silently selected.
    model = model or AUTO_DEFAULT_MODELS.get(provider)
    if not model:
        return 2, "", f"{provider}: explicit model required"
    key = get_key(provider, env_path)
    if not key and not cfg.get("keyless"):
        return 78, "", f"{provider}: no credential"
    key = key or ""
    if provider == "openrouter":
        found = {m["id"] for m in discover(provider, env_path).get("models", [])}
        if model not in found:
            return 2, "", f"openrouter: model not verified free: {model}"

    tools = _TOOLS_READ_ONLY if read_only else _TOOLS_WRITE
    cmd = ["pi", "--provider", f"fa-{provider}", "--model", model,
           "--mode", "json", "-p", "--no-extensions", "--no-mcp", "--no-skills",
           "--no-prompt-templates", "--no-themes", "--no-context-files",
           "--no-session", "--offline", "--no-approve", "--tools", tools, "--", task]

    tmp = tempfile.mkdtemp(prefix="fa-pi-")
    try:
        _write_pi_config(tmp, provider, model, cfg["base_url"],
                         keyless=bool(cfg.get("keyless")))
        rc, out, err = _spawn(cmd, workdir, timeout, _child_env(tmp, key))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rc, _redact(out, key), _redact(err, key)
