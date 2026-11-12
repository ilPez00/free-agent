import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import profiles as p


def base_config(**overrides):
    config = {
        "version": 1,
        "agents": [
            {
                "name": "pi-review",
                "command": ["pi", "--print", "{task}"],
                "roles": ["reviewer"],
            }
        ],
        "roles": {
            "reviewer": {
                "description": "read-only reviewer",
                "prompt": "Review read-only: {task}",
                "tools": ["read"],
            }
        },
    }
    config.update(overrides)
    return config


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name, text, mode=None):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        if mode is not None:
            path.chmod(mode)
        return path


class ValidationTests(Tmp):
    def test_accepts_minimal_config(self):
        path = self.write("c.json", json.dumps(base_config()))
        cfg = p.load_config(path)
        self.assertEqual(cfg["agents"][0]["name"], "pi-review")
        self.assertEqual(cfg["agents"][0]["timeout"], p.DEFAULT_TIMEOUT)
        self.assertFalse(cfg["agents"][0]["automatic"])
        self.assertEqual(cfg["roles"]["reviewer"]["tools"], ["read"])

    def test_rejects_bad_version(self):
        with self.assertRaises(ValueError):
            p.validate_config(base_config(version=2))

    def test_rejects_shell_string_command(self):
        config = base_config()
        config["agents"][0]["command"] = "pi --print {task}"
        with self.assertRaises(ValueError):
            p.validate_config(config)

    def test_rejects_duplicate_names(self):
        config = base_config()
        config["agents"].append(dict(config["agents"][0]))
        with self.assertRaises(ValueError):
            p.validate_config(config)

    def test_rejects_bad_names(self):
        for bad in ("Pi", "1pi", "pi review", "pi.review", ""):
            config = base_config()
            config["agents"][0]["name"] = bad
            with self.assertRaises(ValueError, msg=bad):
                p.validate_config(config)

    def test_rejects_unknown_role(self):
        config = base_config()
        config["agents"][0]["roles"] = ["missing"]
        with self.assertRaises(ValueError):
            p.validate_config(config)

    def test_rejects_blocked_key_env(self):
        for name in ("PATH", "LD_PRELOAD", "PYTHONPATH", "BASH_ENV"):
            config = base_config()
            config["agents"][0]["key_env"] = name
            with self.assertRaises(ValueError, msg=name):
                p.validate_config(config)

    def test_rejects_agent_claiming_reserved_runner_role(self):
        config = base_config()
        config["roles"]["runner"] = {"description": "replay", "prompt": "Replay: {task}"}
        config["agents"][0]["roles"] = ["runner"]
        with self.assertRaises(ValueError):
            p.validate_config(config)
        # A runner role stays usable as deterministic instructions only.
        config["agents"] = []
        self.assertIn("runner", p.validate_config(config)["roles"])

    def test_accepts_empty_agents_and_roles_for_repair(self):
        empty = {"version": 1, "agents": [], "roles": {}}
        self.assertEqual(p.validate_config(empty),
                         {"version": 1, "agents": [], "roles": {}, "providers": {}, "cli_agents": []})
        with self.assertRaises(ValueError):
            p.validate_config({"version": 1, "agents": [], "roles": []})

    def test_rejects_bad_timeout(self):
        for bad in (0, -1, True, "30", 1.5):
            config = base_config()
            config["agents"][0]["timeout"] = bad
            with self.assertRaises(ValueError, msg=repr(bad)):
                p.validate_config(config)

    def test_rejects_unknown_placeholder_in_command(self):
        config = base_config()
        config["agents"][0]["command"] = ["pi", "{prompt}"]
        with self.assertRaises(ValueError):
            p.validate_config(config)

    def test_rejects_env_values_that_are_not_strings(self):
        config = base_config()
        config["agents"][0]["env"] = {"TOKEN": 1}
        with self.assertRaises(ValueError):
            p.validate_config(config)

    def test_rejects_invalid_json(self):
        path = self.write("c.json", "{not json")
        with self.assertRaises(ValueError):
            p.load_config(path)


class RenderTests(unittest.TestCase):
    def definition(self, command):
        return {"name": "x", "command": command, "roles": ["reviewer"]}

    def test_substitutes_task_and_workdir(self):
        argv = p.render_command(
            self.definition(["run", "--dir", "{workdir}", "{task}"]), "do it", "/repo"
        )
        self.assertEqual(argv, ["run", "--dir", "/repo", "do it"])

    def test_returns_new_list(self):
        command = ["run", "{task}"]
        argv = p.render_command(self.definition(command), "x", "/r")
        self.assertEqual(command, ["run", "{task}"])
        self.assertIsNot(argv, command)

    def test_escaped_braces_stay_literal(self):
        argv = p.render_command(self.definition(["echo", "{{task}}", "{{}}"]), "x", "/r")
        self.assertEqual(argv, ["echo", "{task}", "{}"])

    def test_substituted_value_is_not_rescanned(self):
        argv = p.render_command(self.definition(["run", "{task}"]), "{workdir}", "/r")
        self.assertEqual(argv, ["run", "{workdir}"])

    def test_rejects_unsupported_placeholder(self):
        for command in (["run", "{cwd}"], ["run", "{task!r}"], ["run", "{task"]):
            with self.assertRaises(ValueError, msg=repr(command)):
                p.render_command(self.definition(command), "x", "/r")

    def test_rejects_non_string_definition(self):
        with self.assertRaises(ValueError):
            p.render_command({"name": "x"}, "x", "/r")


class RolePromptTests(unittest.TestCase):
    def test_renders_task(self):
        cfg = p.validate_config(base_config())
        self.assertEqual(
            p.role_prompt(cfg, "reviewer", "check auth"),
            "Review read-only: check auth",
        )

    def test_unknown_role(self):
        cfg = p.validate_config(base_config())
        with self.assertRaises(KeyError):
            p.role_prompt(cfg, "nope", "x")

    def test_prompt_cannot_use_workdir(self):
        cfg = p.validate_config(base_config())
        cfg["roles"]["reviewer"]["prompt"] = "in {workdir}"
        with self.assertRaises(ValueError):
            p.role_prompt(cfg, "reviewer", "x")


class EnvFileTests(Tmp):
    def test_quotes_comments_exports(self):
        path = self.write(
            "e.env",
            "\n".join(
                [
                    "# comment",
                    "",
                    "export A=plain",
                    "B='single quoted'",
                    'C="double # not a comment"',
                    "D=value # trailing comment",
                    "E=",
                    "F=abc#def",
                    "G=# placeholder",
                ]
            ),
        )
        self.assertEqual(
            p.read_env_file(path),
            {
                "A": "plain",
                "B": "single quoted",
                "C": "double # not a comment",
                "D": "value",
                "E": "",
                "F": "abc#def",
                "G": "",
            },
        )

    def test_duplicate_last_wins_including_empty(self):
        path = self.write("e.env", "K=one\nK=two\nK=\nJ=\nJ=last\n")
        self.assertEqual(p.read_env_file(path), {"K": "", "J": "last"})

    def test_never_interpolates_variables(self):
        path = self.write("e.env", "K=$HOME\n")
        self.assertEqual(p.read_env_file(path), {"K": "$HOME"})

    def test_rejects_command_substitution(self):
        for line in ("K=$(touch pwned)\n", "K=`touch pwned`\n", "K=${HOME}\n"):
            path = self.write("e.env", line)
            with self.assertRaises(ValueError, msg=line):
                p.read_env_file(path)
            self.assertFalse((self.tmp / "pwned").exists())

    def test_rejects_malformed_lines(self):
        for line in ("JUSTAKEY\n", "1BAD=x\n", "K=two words\n", 'K="unterminated\n'):
            path = self.write("e.env", line)
            with self.assertRaises(ValueError, msg=line):
                p.read_env_file(path)

    def test_missing_file_is_empty(self):
        self.assertEqual(p.read_env_file(self.tmp / "absent"), {})


class CredentialTests(Tmp):
    def test_alias_order_and_env_precedence(self):
        path = self.write("e.env", "AI_NVIDIA_KEY=file-alias\n")
        self.assertEqual(
            p.credential_for("nvidia", path, {"NVIDIA_API_KEY": "env-first"}),
            "env-first",
        )
        # any env alias outranks the file
        self.assertEqual(
            p.credential_for("nvidia", path, {"AI_NVIDIA_KEY": "env-third"}),
            "env-third",
        )
        self.assertEqual(p.credential_for("nvidia", path, {}), "file-alias")

    def test_lowercase_provider_alias_is_found(self):
        path = self.write("e.env", "cloudflare=cfut-test-token\n")
        self.assertEqual(p.credential_for("cloudflare", path), "cfut-test-token")

    def test_cloudflare_token_beats_account_id(self):
        path = self.write("e.env", "cloudflare=cfut-test-token\nCLOUDFLARE=ee4920c6d8e0a7d0f7946f5de532a5f7\n")
        self.assertEqual(p.credential_for("cloudflare", path), "cfut-test-token")

    def test_empty_env_value_falls_through_to_file(self):
        path = self.write("e.env", "GROQ_API_KEY=file\n")
        self.assertEqual(p.credential_for("groq", path, {"GROQ_API_KEY": "  "}), "file")
        self.assertIsNone(p.credential_for("groq", self.tmp / "absent", {"GROQ_API_KEY": ""}))

    def test_ignores_dollar_and_command_syntax(self):
        path = self.write("e.env", "MISTRAL_API_KEY=file\n")
        for bad in ("$HOME", "$(echo x)", "`x`"):
            self.assertEqual(
                p.credential_for("mistral", path, {"MISTRAL_API_KEY": bad}), "file"
            )

    def test_all_documented_aliases_resolve(self):
        expected = {
            "nvidia": "NVIDIA_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "groq": "GROQ_API_KEY",
            "cerebras": "CEREBRAS_API_KEY",
            "mistral": "MISTRAL_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "huggingface": "HF_TOKEN",
            "cloudflare": "CLOUDFLARE_API_KEY",
            "cohere": "COHERE_API_KEY",
            "together": "TOGETHER_API_KEY",
            "deepinfra": "DEEPINFRA_API_KEY",
        }
        expected["sambanova"] = "AI_SAMBA_NOVA_KEY"
        for provider, alias in expected.items():
            self.assertEqual(
                p.credential_for(provider, self.tmp / "absent", {alias: "k"}), "k"
            )

    def test_unknown_provider(self):
        with self.assertRaises(KeyError):
            p.credential_for("nope", self.tmp / "absent", {})


class SaveAndSummaryTests(Tmp):
    def test_save_is_atomic_private_and_reloads(self):
        cfg = p.validate_config(base_config())
        target = self.tmp / "nested" / "profiles.json"
        p.save_config(target, cfg)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(p.load_config(target), cfg)
        self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_public_summary_hides_env_values_keys_and_argv(self):
        config = base_config()
        config["agents"][0]["command"] = ["pi", "--auth", "Authorization: Bearer secret-token", "{task}"]
        config["agents"][0]["env"] = {"SECRET": "do-not-print"}
        config["agents"][0]["key_env"] = "NVIDIA_API_KEY"
        summary = p.public_summary(config)
        text = json.dumps(summary)
        self.assertNotIn("do-not-print", text)
        self.assertNotIn("NVIDIA_API_KEY", text)
        self.assertNotIn("secret-token", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("{task}", text)
        self.assertEqual(summary["agents"][0]["env_names"], ["SECRET"])
        self.assertTrue(summary["agents"][0]["has_key_env"])
        self.assertEqual(summary["agents"][0]["command"], ["pi"])
        self.assertEqual(summary["agents"][0]["argument_count"], 3)
        self.assertNotIn("prompt", summary["roles"]["reviewer"])


class ExampleFileTests(unittest.TestCase):
    def test_example_configs_load(self):
        root = Path(__file__).resolve().parent.parent
        cfg = p.load_config(root / "agents.example.json")
        self.assertIn("reviewer", cfg["roles"])
        self.assertIn("builder", cfg["roles"])
        roles = json.loads((root / "roles.example.json").read_text(encoding="utf-8"))
        self.assertEqual(set(roles), {"architect", "reviewer", "researcher", "runner"})


if __name__ == "__main__":
    unittest.main()
