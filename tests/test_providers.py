import json
import os
import signal
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import providers as pv

ENV = "/nonexistent/.env"


class FakeProfiles:
    def __init__(self, keys):
        self.keys = keys
        self.calls = []

    def credential_for(self, provider, path, environ=None):
        self.calls.append((provider, path, environ))
        return self.keys.get(provider)


def profiles(keys):
    return patch.dict(sys.modules, {"profiles": FakeProfiles(keys)})


class FakePopen:
    instances = []

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.kw = kw
        self.pid = 4321
        self.returncode = 0
        self.communicate_calls = 0
        self.tmp = kw["env"]["PI_CODING_AGENT_DIR"]
        with open(os.path.join(self.tmp, "models.json"), encoding="utf-8") as fh:
            self.models = json.load(fh)
        with open(os.path.join(self.tmp, "settings.json"), encoding="utf-8") as fh:
            self.settings = json.load(fh)
        FakePopen.instances.append(self)

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        return 'event ' + self.kw["env"][pv.KEY_VAR], 'stderr ' + self.kw["env"][pv.KEY_VAR]


class TimeoutPopen(FakePopen):
    def communicate(self, timeout=None):
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            raise pv.subprocess.TimeoutExpired("pi", timeout)
        return "", ""


class KeyAndPolicyTests(unittest.TestCase):
    def test_get_key_delegates_to_profiles(self):
        fake = FakeProfiles({"nvidia": "nv-key"})
        with patch.dict(sys.modules, {"profiles": fake}):
            self.assertEqual(pv.get_key("nvidia", ENV), "nv-key")
        self.assertEqual(fake.calls, [("nvidia", ENV, None)])

    def test_get_key_missing_is_none(self):
        with profiles({}):
            for name in pv.PROVIDERS:
                self.assertIsNone(pv.get_key(name, ENV))

    def test_unknown_provider(self):
        with self.assertRaises(ValueError):
            pv.get_key("does-not-exist", ENV)
        with self.assertRaises(ValueError):
            pv.discover("does-not-exist", ENV)

    def test_inventory_never_leaks_credentials(self):
        with profiles({"nvidia": "sk-LEAK-123", "openrouter": "or-LEAK-456"}):
            rows = pv.inventory(ENV)
        blob = json.dumps(rows)
        self.assertNotIn("LEAK", blob)
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(set(by_name), set(pv.PROVIDERS))
        self.assertTrue(by_name["nvidia"]["credential_present"])
        self.assertEqual(by_name["nvidia"]["state"], "dev_evaluation_opt_in")
        self.assertEqual(by_name["openrouter"]["state"], "free_only_discovery")
        self.assertEqual(by_name["groq"]["state"], "no_credential")
        self.assertFalse(by_name["groq"]["credential_present"])
        self.assertIsInstance(by_name["groq"]["credential_present"], bool)
        self.assertEqual(by_name["cloudflare"]["free_policy"], "credential_only")

    def test_free_policies_are_closed(self):
        self.assertEqual(pv.PROVIDERS["openrouter"]["free_policy"], "free_only")
        self.assertEqual(pv.PROVIDERS["nvidia"]["free_policy"], "dev_evaluation")
        for name in ("groq", "cerebras", "gemini", "sambanova"):
            self.assertEqual(pv.PROVIDERS[name]["free_policy"],
                             "account_free_tier_unverified")
        for name in ("mistral", "cohere", "together", "deepinfra"):
            self.assertEqual(pv.PROVIDERS[name]["free_policy"],
                             "paid_or_trial_opt_in")
        for name in ("cloudflare", "huggingface"):
            cfg = pv.PROVIDERS[name]
            self.assertIsNone(cfg["base_url"])
            self.assertFalse(cfg["pi"])
        self.assertEqual(set(pv.DEFAULT_MODELS), {"nvidia", "openrouter"})
        self.assertFalse(pv.PROVIDERS["sambanova"]["pi"])

    def test_endpoints_are_official_https(self):
        expected = {
            "nvidia": "https://integrate.api.nvidia.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "groq": "https://api.groq.com/openai/v1",
            "cerebras": "https://api.cerebras.ai/v1",
            "mistral": "https://api.mistral.ai/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
            "cohere": "https://api.cohere.com/compatibility/v1",
            "together": "https://api.together.xyz/v1",
            "deepinfra": "https://api.deepinfra.com/v1/openai",
        }
        for name, url in expected.items():
            self.assertEqual(pv.PROVIDERS[name]["base_url"], url)


class DiscoveryTests(unittest.TestCase):
    def test_missing_key_does_not_touch_network(self):
        with profiles({}), patch.object(pv, "_http_json",
                                        side_effect=AssertionError("no network")):
            result = pv.discover("openrouter", ENV)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_credential")
        self.assertEqual(result["models"], [])

    def test_credential_only_not_runnable(self):
        with profiles({"cloudflare": "cf-key"}):
            result = pv.discover("cloudflare", ENV)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not_runnable")

    def test_http_status_mapping(self):
        for status, error in ((401, "unauthorized"), (402, "payment_required"),
                              (429, "rate_limited"), (0, "network_error"),
                              (500, "http_error")):
            with self.subTest(status=status):
                with profiles({"groq": "g-key"}), \
                        patch.object(pv, "_http_json", return_value=(status, None, error)):
                    result = pv.discover("groq", ENV)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], status)
                self.assertEqual(result["error"], error)

    def test_bad_payload(self):
        with profiles({"groq": "g-key"}), \
                patch.object(pv, "_http_json", return_value=(200, {"nope": 1}, "")):
            self.assertEqual(pv.discover("groq", ENV)["error"], "unexpected_response")
        with profiles({"groq": "g-key"}), \
                patch.object(pv, "_http_json", return_value=(200, None, "bad_json")):
            self.assertEqual(pv.discover("groq", ENV)["error"], "bad_json")

    def test_zero_price_is_decimal_and_finite(self):
        def price(prompt, completion):
            return {"pricing": {"prompt": prompt, "completion": completion}}
        self.assertTrue(pv._zero_price(price("0", "0.000000")))
        self.assertTrue(pv._zero_price(price("-0.0", "0E-10")))
        self.assertFalse(pv._zero_price(price("1e-9", "0")))
        self.assertFalse(pv._zero_price(price("NaN", "0")))
        self.assertFalse(pv._zero_price(price("Infinity", "0")))
        self.assertFalse(pv._zero_price(price("", "0")))
        self.assertFalse(pv._zero_price(price(None, "0")))
        self.assertFalse(pv._zero_price({"pricing": {"prompt": "0"}}))
        self.assertFalse(pv._zero_price({}))

    def test_openrouter_drops_everything_not_proven_free(self):
        payload = {"data": [
            {"id": "vendor/a:free", "name": "A",
             "pricing": {"prompt": "0", "completion": "0"},
             "supported_parameters": ["tools", "temperature"]},
            {"id": "openrouter/free", "pricing": {"prompt": "0", "completion": "0.0"}},
            {"id": "vendor/paid", "pricing": {"prompt": "0", "completion": "0.5"}},
            {"id": "vendor/prompt-paid:free",
             "pricing": {"prompt": "0.1", "completion": "0"}},
            {"id": "vendor/nan:free", "pricing": {"prompt": "NaN", "completion": "0"}},
            {"id": "vendor/no-price:free"},
            {"id": "vendor/plain", "pricing": {"prompt": "0", "completion": "0"}},
        ]}
        with profiles({"openrouter": "or-key"}), \
                patch.object(pv, "_http_json", return_value=(200, payload, "")):
            result = pv.discover("openrouter", ENV)
        self.assertTrue(result["ok"])
        self.assertEqual([m["id"] for m in result["models"]],
                         ["vendor/a:free", "openrouter/free"])
        self.assertIs(result["models"][0]["tools"], True)
        self.assertIsNone(result["models"][1]["tools"])

    def test_same_origin_redirects_only(self):
        self.assertTrue(pv._same_origin("https://openrouter.ai/api/v1/models",
                                        "https://openrouter.ai/api/v2/models"))
        self.assertFalse(pv._same_origin("https://openrouter.ai/api/v1/models",
                                         "https://evil.example/models"))
        self.assertFalse(pv._same_origin("http://openrouter.ai/x",
                                         "https://openrouter.ai/x"))
        self.assertFalse(pv._same_origin("https://openrouter.ai:443/x",
                                         "https://openrouter.ai:8443/x"))
        req = Request("https://openrouter.ai/api/v1/models")
        with self.assertRaises(HTTPError):
            pv._SameOriginRedirect().redirect_request(
                req, None, 302, "Found", {}, "https://evil.example/steal")
        self.assertIsNotNone(pv._SameOriginRedirect().redirect_request(
            req, None, 302, "Found", {}, "https://openrouter.ai/api/v2/models"))


class RunPiTests(unittest.TestCase):
    def setUp(self):
        FakePopen.instances = []

    def test_missing_key_never_spawns(self):
        with profiles({}), patch.object(pv, "_spawn",
                                        side_effect=AssertionError("no spawn")):
            rc, out, err = pv.run_pi("nvidia", "", "task", "/tmp", 10, ENV)
        self.assertEqual(rc, 78)
        self.assertIn("no credential", err)

    def test_other_providers_require_explicit_model(self):
        with profiles({"groq": "g-key"}), patch.object(pv, "_spawn",
                                                       side_effect=AssertionError):
            rc, _, err = pv.run_pi("groq", "", "task", "/tmp", 10, ENV)
        self.assertEqual(rc, 2)
        self.assertIn("explicit model", err)

    def test_credential_only_not_launched(self):
        with profiles({"cloudflare": "cf-key"}), patch.object(pv, "_spawn",
                                                              side_effect=AssertionError):
            rc, _, err = pv.run_pi("cloudflare", "", "task", "/tmp", 10, ENV)
        self.assertEqual(rc, 78)
        self.assertIn("not runnable", err)

    def test_openrouter_rejects_unverified_model(self):
        found = {"ok": True, "status": 200, "error": "",
                 "models": [{"id": "vendor/other:free"}]}
        with profiles({"openrouter": "or-key"}), \
                patch.object(pv, "discover", return_value=found), \
                patch.object(pv, "_spawn", side_effect=AssertionError("no spawn")):
            rc, _, err = pv.run_pi("openrouter", "openrouter/free", "task",
                                   "/tmp", 10, ENV)
        self.assertEqual(rc, 2)
        self.assertIn("not verified free", err)

    def test_isolation_redaction_and_flags(self):
        key = "sk-live-SECRET-XYZ"
        with profiles({"nvidia": key}), \
                patch.object(pv.subprocess, "Popen", FakePopen):
            rc, out, err = pv.run_pi("nvidia", "", "fix it", "/tmp", 10, ENV,
                                     read_only=True)
        self.assertEqual(rc, 0)
        self.assertNotIn(key, out + err)
        self.assertIn("[REDACTED]", out + err)
        proc = FakePopen.instances[0]
        self.assertEqual(proc.kw["cwd"], "/tmp")
        self.assertNotIn("--api-key", proc.cmd)
        for flag in ("--no-mcp", "--no-skills", "--no-session", "--offline",
                     "--no-context-files", "--no-approve", "--no-extensions"):
            self.assertIn(flag, proc.cmd)
        self.assertEqual(proc.cmd[proc.cmd.index("--tools") + 1],
                         "read,grep,find,ls")
        self.assertEqual(proc.cmd[proc.cmd.index("--provider") + 1], "fa-nvidia")
        self.assertEqual(proc.cmd[proc.cmd.index("--model") + 1],
                         pv.DEFAULT_MODELS["nvidia"])
        env = proc.kw["env"]
        self.assertEqual(set(env), {"PATH", "HOME", "LANG", "TMPDIR", "TERM",
                                    pv.KEY_VAR, "PI_CODING_AGENT_DIR"})
        self.assertEqual(env[pv.KEY_VAR], key)
        self.assertEqual(env["PI_CODING_AGENT_DIR"], proc.tmp)
        self.assertEqual(proc.settings, {})
        spec = proc.models["providers"]["fa-nvidia"]
        self.assertEqual(spec["baseUrl"], pv.PROVIDERS["nvidia"]["base_url"])
        self.assertEqual(spec["api"], "openai-completions")
        self.assertEqual(spec["apiKey"], "${" + pv.KEY_VAR + "}")
        self.assertEqual(spec["models"], [{
            "id": pv.DEFAULT_MODELS["nvidia"], "contextWindow": 65536,
            "maxTokens": 8192, "input": ["text"]}])
        self.assertFalse(os.path.exists(proc.tmp))

    def test_write_tools(self):
        with profiles({"nvidia": "k"}), patch.object(pv.subprocess, "Popen", FakePopen):
            pv.run_pi("nvidia", "some/model", "task", "/tmp", 10, ENV)
        cmd = FakePopen.instances[0].cmd
        self.assertEqual(cmd[cmd.index("--tools") + 1], "read,bash,write,edit")

    def test_timeout_kills_process_group(self):
        with profiles({"nvidia": "k"}), \
                patch.object(pv.subprocess, "Popen", TimeoutPopen), \
                patch.object(pv.os, "killpg") as killpg:
            rc, _, err = pv.run_pi("nvidia", "m", "task", "/tmp", 0.1, ENV)
        self.assertEqual(rc, 124)
        self.assertIn("timeout", err)
        killpg.assert_called_once_with(4321, signal.SIGKILL)

    def test_launch_failure_is_reported(self):
        with profiles({"nvidia": "k"}), \
                patch.object(pv.subprocess, "Popen", side_effect=OSError(2, "no pi")):
            rc, _, err = pv.run_pi("nvidia", "m", "task", "/tmp", 10, ENV)
        self.assertEqual(rc, 127)
        self.assertIn("could not launch", err)


if __name__ == "__main__":
    unittest.main()
