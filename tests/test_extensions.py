import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import extensions
import free_agent as fa
import profiles
import providers as pv


def agent_config(name="my-agent", role="executor"):
    return {
        "version": 1,
        "agents": [
            {
                "name": name,
                "command": ["echo", "{task}"],
                "roles": [role],
            }
        ],
        "roles": {role: {"description": "test role", "prompt": "Do: {task}"}},
    }


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.env_file = self.tmp / "keys.env"
        self.config = self.tmp / "agents.json"

        blank = {alias: "" for aliases in profiles.KEY_ALIASES.values()
                 for alias in aliases}
        blank.update({
            "FREE_AGENT_ENV_FILE": str(self.env_file),
            "FREE_AGENT_CONFIG": str(self.config),
            "FREE_AGENT_WORKFLOW_MANIFEST": "",
        })
        patcher = patch.dict(os.environ, blank)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.addCleanup(patch.stopall)
        patch.object(fa, "WORKERS", dict(fa.WORKERS)).start()
        patch.object(extensions, "_REGISTERED", set()).start()
        patch.object(extensions, "_CONFIG", None).start()
        patch.object(fa, "STATE_PATH", self.tmp / "state.json").start()

    def write_env(self, text):
        self.env_file.write_text(text, encoding="utf-8")
        return self.env_file

    def write_config(self, data):
        self.config.write_text(json.dumps(data), encoding="utf-8")
        return self.config

    def invoke(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fa.main(argv)
        return rc, out.getvalue()

    def dynamic(self):
        return set(fa.WORKERS) & set(extensions._REGISTERED)


TEXT_EVENT = json.dumps({"type": "text", "part": {"text": "done"}})


class PiRegistryTests(Base):
    def test_pi_workers_registered_with_lazy_credential(self):
        self.write_env("NVIDIA_API_KEY=nv-secret\n")
        extensions.initialize(fa)
        self.assertIn("pi-nvidia", fa.WORKERS)
        self.assertIn("pi-openrouter", fa.WORKERS)
        nvidia = fa.WORKERS["pi-nvidia"]
        self.assertTrue(nvidia.automatic)
        self.assertEqual(nvidia.model, pv.DEFAULT_MODELS["nvidia"])
        self.assertFalse(fa.WORKERS["pi-openrouter"].automatic)

        with patch.object(pv, "run_pi", return_value=(0, TEXT_EVENT, "")) as run:
            rc, out, err = nvidia.run("fix it", str(self.tmp), 7)
        self.assertEqual((rc, err), (0, ""))
        run.assert_called_once_with(
            "nvidia", pv.DEFAULT_MODELS["nvidia"], "fix it", str(self.tmp), 7,
            str(self.env_file),
        )

        result = nvidia.extract(0, TEXT_EVENT, "")
        self.assertTrue(result.ok)
        self.assertEqual(result.model, nvidia.model)

    def test_login_never_prints_key(self):
        self.write_env("NVIDIA_API_KEY=nv-secret\n")
        extensions.initialize(fa)
        login = fa.WORKERS["pi-nvidia"].login()
        self.assertIsNone(login["logged_in"])
        self.assertTrue(login["credential_present"])
        self.assertNotIn("nv-secret", json.dumps(login))

    def test_missing_credential_is_reported_without_inference(self):
        extensions.initialize(fa)
        worker = fa.WORKERS["pi-nvidia"]
        self.assertFalse(worker.automatic)
        login = worker.login()
        self.assertFalse(login["logged_in"])
        with patch.object(pv, "run_pi", side_effect=AssertionError("no inference")):
            self.assertIsNone(worker.quota()["pct"])

    def test_opt_in_providers_need_explicit_model_env(self):
        extensions.initialize(fa)
        self.assertNotIn("pi-groq", fa.WORKERS)
        with patch.dict(os.environ, {"FREE_AGENT_PROVIDER_MODEL": "some/model"}):
            extensions.initialize(fa)
        groq = fa.WORKERS["pi-groq"]
        self.assertFalse(groq.automatic)
        self.assertEqual(groq.model, "some/model")


class RegistryIntegrationTests(Base):
    def test_reinitialize_is_idempotent(self):
        self.write_config(agent_config())
        extensions.initialize(fa)
        first = self.dynamic()
        count = len(fa.WORKERS)
        extensions.initialize(fa)
        self.assertEqual(self.dynamic(), first)
        self.assertEqual(len(fa.WORKERS), count)
        self.assertIn("my-agent", fa.WORKERS)

    def test_builtin_collision_rejected(self):
        self.write_config(agent_config(name="opencode"))
        with self.assertRaises(ValueError):
            extensions.initialize(fa)

    def test_absent_config_is_builtin_only(self):
        extensions.initialize(fa)
        self.assertNotIn("my-agent", fa.WORKERS)
        self.assertEqual(extensions.loaded_config(), None)

    def test_role_prompt_used_for_configured_worker(self):
        self.write_config(agent_config(name="reviewer-agent", role="reviewer"))
        extensions.initialize(fa)
        worker = fa.WORKERS["reviewer-agent"]
        self.assertEqual(extensions.role_prompt_for(worker, "reviewer", "check"),
                         "Do: check")
        self.assertIsNone(extensions.role_prompt_for(worker, "executor", "check"))

    def test_custom_argv_env_is_minimal_and_redacted(self):
        self.write_env("MY_TOKEN=file-secret\n")
        config = agent_config()
        config["agents"][0].update({
            "env": {"SAFE": "yes", "LD_PRELOAD": "/tmp/evil.so"},
            "key_env": "MY_TOKEN",
        })
        self.write_config(config)
        extensions.initialize(fa)
        worker = fa.WORKERS["my-agent"]
        with patch.object(fa, "run_cmd",
                          return_value=(0, "secret file-secret", "file-secret")) as run:
            rc, out, err = worker.run("task", str(self.tmp), 900)
        self.assertEqual(rc, 0)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["SAFE"], "yes")
        self.assertEqual(env["MY_TOKEN"], "file-secret")
        self.assertNotIn("LD_PRELOAD", env)
        self.assertNotEqual(env["PATH"], "")
        self.assertNotIn("file-secret", out + err)
        self.assertEqual(run.call_args.args[0], ["echo", "task"])
        self.assertEqual(run.call_args.kwargs["timeout"], 900)

    def test_gemini_oauth_env_is_minimal(self):
        worker = extensions._native_extras(fa)["gemini"]
        ambient = {"UNRELATED_TOKEN": "leak", "GEMINI_API_KEY": "paid",
                   "GOOGLE_CLOUD_PROJECT": "billing"}
        with patch.dict(os.environ, ambient), \
                patch.object(fa, "run_cmd", return_value=(0, "{}", "")) as run:
            worker.run("task", str(self.tmp), 5)
        env = run.call_args.kwargs["env"]
        self.assertNotIn("UNRELATED_TOKEN", env)
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertNotIn("GOOGLE_CLOUD_PROJECT", env)
        self.assertEqual(env["HOME"], os.environ["HOME"])
        self.assertIn("TMPDIR", env)
        self.assertTrue(set(env) <= set(extensions.BASE_ENV_KEYS) | {"TMPDIR"})


class ConfigCliTests(Base):
    def test_help_with_broken_default_config(self):
        self.config.write_text("{not json", encoding="utf-8")
        rc, out = self.invoke(["--help"])
        self.assertEqual(rc, 0, out)
        self.assertIn("usage:", out.lower())

    def test_agent_validate_broken_default_config_returns_structured_error(self):
        self.config.write_text("{not json", encoding="utf-8")
        rc, out = self.invoke(["agent", "validate", "--json"])
        self.assertEqual(rc, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)

    def test_agent_validate_separate_config_when_default_broken(self):
        self.config.write_text("{not json", encoding="utf-8")
        good = self.tmp / "good.json"
        good.write_text(json.dumps(agent_config()), encoding="utf-8")
        rc, out = self.invoke(["agent", "validate", "--config", str(good), "--json"])
        self.assertEqual(rc, 0, out)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["agents"], 1)
    def test_agent_add_then_roles_set_then_remove(self):
        rc, out = self.invoke([
            "agent", "add", "cli-agent", "--command-json",
            json.dumps(["echo", "{task}"]), "--role", "executor",
            "--specialty", "test", "--json",
        ])
        self.assertEqual(rc, 0, out)
        cfg = profiles.load_config(self.config)
        self.assertEqual(cfg["agents"][0]["name"], "cli-agent")
        self.assertFalse(cfg["agents"][0]["automatic"])
        self.assertEqual(cfg["roles"]["executor"]["prompt"], extensions.DEFAULT_ROLE_PROMPT)

        rc, out = self.invoke([
            "roles", "set", "executor", "--prompt", "Do: {task}",
            "--description", "changed", "--tools", "read,write", "--json",
        ])
        self.assertEqual(rc, 0, out)
        cfg = profiles.load_config(self.config)
        self.assertEqual(cfg["roles"]["executor"]["prompt"], "Do: {task}")
        self.assertEqual(cfg["roles"]["executor"]["tools"], ["read", "write"])

        rc, out = self.invoke(["agent", "remove", "cli-agent", "--json"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.config.exists())
        saved = profiles.load_config(self.config)
        self.assertEqual(saved["agents"], [])
        self.assertIn("executor", saved["roles"])

    def test_shell_string_command_rejected(self):
        rc, out = self.invoke(["agent", "add", "bad", "--command-json",
                               json.dumps("echo {task}"), "--json"])
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["ok"])
        self.assertFalse(self.config.exists())

    def test_management_commands_survive_broken_config(self):
        self.config.write_text("{not json", encoding="utf-8")
        rc, out = self.invoke(["providers", "list", "--json"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(json.loads(out)["ok"])

    def test_dispatch_fails_closed_on_broken_config(self):
        self.config.write_text("{not json", encoding="utf-8")
        rc, out = self.invoke(["run", "task", "--json"])
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["ok"])

    def test_status_lists_custom_agents_from_valid_config(self):
        self.write_config(agent_config())
        rc, out = self.invoke(["status", "--json"])
        self.assertEqual(rc, 0, out)
        self.assertIn("my-agent", {row["worker"] for row in json.loads(out)})

    def test_agent_add_rejects_reserved_runner_role(self):
        rc, out = self.invoke(["agent", "add", "cli-agent", "--command-json",
                               json.dumps(["echo", "{task}"]), "--role", "runner",
                               "--json"])
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["ok"])
        self.assertFalse(self.config.exists())

    def test_unknown_role_names_the_role(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fa.main(["run", "task", "--role", "bogus"])
        self.assertEqual(rc, 1)
        self.assertIn("no worker for role: bogus", err.getvalue())
        self.assertNotIn("None", err.getvalue())

    def test_validate_reports_absent_config(self):
        rc, out = self.invoke(["agent", "validate", "--json"])
        self.assertEqual(rc, 0)
        self.assertFalse(json.loads(out)["exists"])

    def test_agent_list_hides_argv_secrets(self):
        config = agent_config()
        config["agents"][0]["command"] = ["echo", "Authorization: Bearer secret-token", "{task}"]
        self.write_config(config)
        rc, out = self.invoke(["agent", "list", "--json"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("secret-token", out)
        self.assertNotIn("Authorization", out)
        payload = json.loads(out)
        self.assertEqual(payload["agents"][0]["command"], ["echo"])
        self.assertEqual(payload["agents"][0]["argument_count"], 2)


class ProviderCliTests(Base):
    def test_list_redacts_credentials(self):
        self.write_env("NVIDIA_API_KEY=sk-LEAK-999\n")
        rc, out = self.invoke(["providers", "--env-file", str(self.env_file),
                               "list", "--json"])
        self.assertEqual(rc, 0)
        self.assertNotIn("LEAK", out)
        rows = {row["name"]: row for row in json.loads(out)["providers"]}
        self.assertTrue(rows["nvidia"]["credential_present"])

    def test_models_and_run_use_sanitized_catalog(self):
        self.write_env("NVIDIA_API_KEY=nv-secret\n")
        catalog = {"ok": True, "status": 200, "error": "",
                   "models": [{"id": "m", "name": "M", "tools": True,
                               "secret": "must-not-print"}]}
        with patch.object(pv, "discover", return_value=catalog):
            rc, out = self.invoke(["providers", "--env-file", str(self.env_file),
                                   "models", "nvidia", "--json"])
        self.assertEqual(rc, 0)
        self.assertNotIn("must-not-print", out)

        with patch.object(pv, "run_pi", return_value=(0, TEXT_EVENT, "")) as run:
            rc, out = self.invoke([
                "providers", "--env-file", str(self.env_file), "run", "nvidia",
                "task", "--model", "chosen/model", "-w", str(self.tmp), "--json",
            ])
        self.assertEqual(rc, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["model"], "chosen/model")
        self.assertEqual(payload["cost"], "unknown")
        self.assertEqual(payload["text"], "done")
        run.assert_called_once()

    def test_openrouter_verified_cost_is_zero(self):
        self.write_env("OPENROUTER_API_KEY=or-secret\n")
        with patch.object(pv, "run_pi", return_value=(0, TEXT_EVENT, "")):
            rc, out = self.invoke([
                "providers", "--env-file", str(self.env_file), "run", "openrouter",
                "task", "-w", str(self.tmp), "--json",
            ])
        self.assertEqual(rc, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["cost"], "0")
        self.assertEqual(payload["model"], pv.DEFAULT_MODELS["openrouter"])


class LoopReplayTests(Base):
    def test_replay_bypasses_dispatch(self):
        replay = {"status": "replayed", "provider_calls": 0, "reason": "ok",
                  "planner_fallback": False, "receipt": {"status": "succeeded"}}
        with patch.object(fa.workflows, "replay", return_value=replay), \
                patch.object(fa, "cmd_run", side_effect=AssertionError("no dispatch")):
            rc, out = self.invoke(["loop", "task", "--check", "true",
                                   "--workflow-manifest", str(self.tmp / "m.json")])
        self.assertEqual(rc, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["provider_calls"], 0)
        self.assertEqual(payload["stage"], "replay")

    def test_blocked_never_reaches_a_planner(self):
        replay = {"status": "blocked", "provider_calls": 0, "reason": "artifact changed",
                  "planner_fallback": False}
        with patch.object(fa.workflows, "replay", return_value=replay), \
                patch.object(fa, "cmd_run", side_effect=AssertionError("no dispatch")):
            rc, out = self.invoke(["loop", "task", "--check", "true",
                                   "--planner", "claude",
                                   "--workflow-manifest", str(self.tmp / "m.json")])
        self.assertNotEqual(rc, 0)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["stage"], "replay")

    def test_miss_allows_planner_fallback(self):
        replay = {"status": "miss", "provider_calls": None, "reason": "no recipe",
                  "planner_fallback": True}
        calls = []

        def fake_cmd_run(opts):
            calls.append(opts.role)
            print(json.dumps({"ok": True, "worker": "fake", "model": "fake",
                              "text": "done"}))
            return 0

        with patch.object(fa.workflows, "replay", return_value=replay), \
                patch.object(fa, "cmd_run", side_effect=fake_cmd_run):
            rc, out = self.invoke(["loop", "task", "--check", "true",
                                   "--workflow-manifest", str(self.tmp / "m.json")])
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls[0], "executor")

    def test_runner_role_uses_replay_only(self):
        replay = {"status": "replayed", "provider_calls": 0, "reason": "ok",
                  "planner_fallback": False}
        with patch.object(fa.workflows, "replay", return_value=replay), \
                patch.object(fa, "ordered", side_effect=AssertionError("no agents")):
            rc, out = self.invoke(["run", "task", "--role", "runner", "--json",
                                   "--workflow-manifest", str(self.tmp / "m.json")])
        self.assertEqual(rc, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["provider_calls"], 0)
        self.assertEqual(payload["role"], "runner")


class LearnTests(Base):
    def test_learned_candidate_is_never_approved(self):
        artifact = self.tmp / "input.txt"
        artifact.write_text("pinned\n", encoding="utf-8")
        worker = fa.Worker(
            "fake", "fake", 1, True, "login",
            lambda *a: (0, "done", ""),
            lambda r, o, e: fa.Result(r == 0 and bool(o), o),
            lambda: {"text": "unknown"}, lambda: {"logged_in": None},
            installed=lambda: True,
        )
        with patch.dict(fa.WORKERS, {"fake": worker}, clear=True):
            rc, out = self.invoke([
                "loop", "task", "--worker", "fake", "--check", "true",
                "--learn", "--artifact", "input.txt", "-w", str(self.tmp),
                "--no-notify",
            ])
        self.assertEqual(rc, 0, out)
        payload = json.loads(out)
        candidate = payload["history"][0]["candidate"]
        document = json.loads(Path(candidate).read_text(encoding="utf-8"))
        self.assertEqual(document["status"], "needs_operator_review")
        self.assertFalse(document["approved"])
        self.assertFalse(document["replayable"])
        self.assertEqual(document["metadata"]["worker"], "fake")
        self.assertEqual(document["metadata"]["check_exit"], 0)


if __name__ == "__main__":
    unittest.main()
