import json
import os
import signal
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import workflows as wf

PROMPT = "run the repository test suite"


def sha(path):
    return wf._sha256(str(path))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def make_binary(tmp):
    path = Path(tmp) / "replant"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def make_store(tmp):
    store = Path(tmp) / ".replant"
    (store / "records").mkdir(parents=True)
    (store / "entries").mkdir()
    return store


def add_record(store, record_id, prompt, indexed=True, steps=("increment",), checks=("verify",)):
    record = {
        "schema": 1,
        "id": record_id,
        "request": {"prompt": prompt, "input": {}, "context": {}},
        "recipe": {
            "steps": [{"tool": name, "input": {}} for name in steps],
            "checks": [{"tool": name, "input": {}} for name in checks],
        },
    }
    write_json(store / "records" / (record_id + ".json"), record)
    if indexed:
        write_json(store / "entries" / (record_id + "-key.json"), record_id)
    return record


class FindBinaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_env_override_wins(self):
        binary = make_binary(self.tmp.name)
        with patch.dict(os.environ, {"FREE_AGENT_REPLANT_BIN": str(binary)}), patch.object(
            wf.shutil, "which", return_value="/usr/bin/other"
        ):
            self.assertEqual(wf.find_binary(), str(binary))

    def test_which_second(self):
        binary = make_binary(self.tmp.name)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FREE_AGENT_REPLANT_BIN", None)
            with patch.object(wf.shutil, "which", return_value=str(binary)):
                self.assertEqual(wf.find_binary(), str(binary))

    def test_known_path_third(self):
        binary = make_binary(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if k != "FREE_AGENT_REPLANT_BIN"}
        with patch.dict(os.environ, env, clear=True), patch.object(
            wf.shutil, "which", return_value=None
        ), patch.object(wf, "KNOWN_BINARIES", (str(binary),)):
            self.assertEqual(wf.find_binary(), str(binary))

    def test_missing(self):
        env = {k: v for k, v in os.environ.items() if k != "FREE_AGENT_REPLANT_BIN"}
        with patch.dict(os.environ, env, clear=True), patch.object(
            wf.shutil, "which", return_value=None
        ), patch.object(wf, "KNOWN_BINARIES", ()):
            self.assertIsNone(wf.find_binary())


class InspectStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = make_store(self.tmp.name)

    def run_inspect(self):
        def fake_exec(argv, payload=None, cwd=None, timeout=None):
            if argv[1] == "stats":
                return 0, json.dumps({"verified_records": 1, "verified_replays": 2}), "", False
            if argv[1] == "history":
                return 0, "[]", "", False
            raise AssertionError(argv)

        with patch.object(wf, "find_binary", return_value="/fake/replant"), patch.object(
            wf, "_exec", side_effect=fake_exec
        ):
            return wf.inspect_store(self.store)

    def test_only_indexed_valid_records_are_learned(self):
        add_record(self.store, "a" * 64, "learned prompt")
        add_record(self.store, "b" * 64, "unindexed prompt", indexed=False)
        report = self.run_inspect()
        self.assertEqual(report["stats"]["verified_records"], 1)
        self.assertEqual([item["prompt"] for item in report["learned"]], ["learned prompt"])
        self.assertEqual(report["learned"][0]["steps"], ["increment"])
        self.assertEqual(report["learned"][0]["checks"], ["verify"])
        self.assertEqual(report["unindexed"], ["b" * 64])

    def test_stale_entry_separate(self):
        write_json(self.store / "entries" / "missing-key.json", "c" * 64)
        report = self.run_inspect()
        self.assertEqual(report["stale"], ["c" * 64])
        self.assertEqual(report["learned"], [])

    def test_symlink_record_rejected(self):
        add_record(self.store, "a" * 64, "real prompt")
        os.symlink(
            self.store / "records" / ("a" * 64 + ".json"),
            self.store / "records" / ("d" * 64 + ".json"),
        )
        report = self.run_inspect()
        self.assertEqual([item["id"] for item in report["learned"]], ["a" * 64])
        self.assertTrue(any(item["reason"] == "symlink rejected" for item in report["invalid"]))

    def test_unknown_candidate_schema_reported_not_guessed(self):
        write_json(self.store / "candidates" / "one.json", {"whatever": True})
        report = self.run_inspect()
        self.assertEqual(len(report["candidates"]), 1)
        self.assertEqual(report["candidates"][0]["keys"], ["whatever"])
        self.assertIn("never executed", report["candidates"][0]["note"])
        self.assertNotIn("similarity", report["candidates"][0])

    def test_missing_store_is_read_without_creating_it(self):
        target = Path(self.tmp.name) / "absent"
        with patch.object(wf, "_exec", side_effect=AssertionError("no CLI")):
            report = wf.inspect_store(target)
        self.assertEqual(report["stats"]["error"], "store directory not found")
        self.assertFalse(target.exists())


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binary = make_binary(self.tmp.name)
        self.registry = write_json(
            self.root / "tools.json",
            {"increment": {"kind": "action", "program": "/bin/true"}},
        )
        self.artifact = self.root / "input.txt"
        self.artifact.write_text("pinned\n", encoding="utf-8")
        self.store = self.root / ".replant"

    def manifest(self, artifacts=None, prompt=PROMPT):
        document = {
            "version": 1,
            "tools": str(self.registry),
            "store": str(self.store),
            "workflows": [
                {
                    "prompt": prompt,
                    "context": {},
                    "input": {},
                    "artifacts": artifacts
                    if artifacts is not None
                    else {str(self.artifact): sha(self.artifact)},
                }
            ],
        }
        return write_json(self.root / "workflows.json", document)

    def run_replay(self, manifest, receipt, rc=0, err="", exec_mock=None):
        def default_exec(argv, payload=None, cwd=None, timeout=None):
            return rc, json.dumps(receipt), err, False

        call = exec_mock or default_exec
        with patch.object(wf, "find_binary", return_value=str(self.binary)), patch.object(
            wf, "_exec", side_effect=call
        ):
            return wf.replay(PROMPT, self.root, manifest)

    def test_no_manifest_disabled(self):
        with patch.object(wf, "_exec", side_effect=AssertionError("no process")):
            result = wf.replay(PROMPT, self.root, None)
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["planner_fallback"])

    def test_missing_workflow_is_miss(self):
        manifest = self.manifest(prompt="some other prompt")
        result = self.run_replay(manifest, {})
        self.assertEqual(result["status"], "miss")
        self.assertTrue(result["planner_fallback"])

    def test_changed_artifact_blocks_before_process(self):
        manifest = self.manifest()
        self.artifact.write_text("changed\n", encoding="utf-8")
        result = self.run_replay(
            manifest, {}, exec_mock=AssertionError("process must not run")
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("artifact changed", result["reason"])
        self.assertFalse(result["planner_fallback"])

    def test_missing_artifact_blocks_before_process(self):
        manifest = self.manifest(artifacts={str(self.root / "absent.txt"): "0" * 64})
        result = self.run_replay(
            manifest, {}, exec_mock=AssertionError("process must not run")
        )
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["planner_fallback"])

    def test_missing_binary_is_unavailable(self):
        manifest = self.manifest()
        with patch.object(wf, "find_binary", return_value=None), patch.object(
            wf, "_exec", side_effect=AssertionError("no process")
        ):
            result = wf.replay(PROMPT, self.root, manifest)
        self.assertEqual(result["status"], "unavailable")

    def test_world_writable_manifest_rejected(self):
        manifest = self.manifest()
        manifest.chmod(0o666)
        result = self.run_replay(
            manifest, {}, exec_mock=AssertionError("process must not run")
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("world-writable", result["reason"])

    def test_world_writable_registry_rejected(self):
        manifest = self.manifest()
        self.registry.chmod(0o666)
        result = self.run_replay(
            manifest, {}, exec_mock=AssertionError("process must not run")
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("world-writable", result["reason"])

    def test_empty_artifacts_blocked(self):
        manifest = self.manifest(artifacts={})
        result = self.run_replay(
            manifest, {}, exec_mock=AssertionError("process must not run")
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("artifacts", result["reason"])

    def test_symlink_manifest_rejected(self):
        real = self.manifest()
        link = self.root / "link.json"
        os.symlink(real, link)
        result = self.run_replay(
            link, {}, exec_mock=AssertionError("process must not run")
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("symlink", result["reason"])

    def test_exact_replay_counts_zero_provider_calls(self):
        manifest = self.manifest()
        receipt = {
            "status": "succeeded",
            "operation": "replay",
            "provider_calls": 0,
            "reason": None,
        }
        result = self.run_replay(manifest, receipt)
        self.assertEqual(result["status"], "replayed")
        self.assertEqual(result["provider_calls"], 0)
        self.assertFalse(result["planner_fallback"])
        self.assertEqual(result["receipt"], receipt)

    def test_native_miss_allows_fallback(self):
        manifest = self.manifest()
        receipt = {
            "status": "miss",
            "operation": "lookup",
            "provider_calls": 0,
            "reason": "no verified recipe for this exact request",
        }
        result = self.run_replay(manifest, receipt)
        self.assertEqual(result["status"], "miss")
        self.assertTrue(result["planner_fallback"])

    def test_review_is_blocked_not_miss(self):
        manifest = self.manifest()
        receipt = {
            "status": "review",
            "operation": "lookup",
            "provider_calls": 0,
            "reason": "near-match recipes require explicit approval",
        }
        result = self.run_replay(manifest, receipt)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["planner_fallback"])

    def test_unknown_status_is_error_not_replayed(self):
        manifest = self.manifest()
        receipt = {"status": "mystery", "operation": "lookup", "provider_calls": 0}
        result = self.run_replay(manifest, receipt)
        self.assertEqual(result["status"], "error")
        self.assertNotEqual(result["status"], "replayed")
        self.assertFalse(result["planner_fallback"])

    def test_nonzero_native_exit_is_blocked(self):
        manifest = self.manifest()
        receipt = {"status": "failed", "operation": "replay", "reason": "tool failed"}
        result = self.run_replay(manifest, receipt, rc=1)
        self.assertEqual(result["status"], "blocked")

    def test_timeout_kills_process_group(self):
        manifest = self.manifest()

        class TimeoutPopen:
            pid = 4321
            returncode = -signal.SIGKILL

            def __init__(self, *args, **kwargs):
                self.calls = 0

            def communicate(self, input=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("replant", timeout)
                return "", ""

        with patch.object(wf, "find_binary", return_value=str(self.binary)), patch.object(
            wf.subprocess, "Popen", TimeoutPopen
        ), patch.object(wf.os, "getpgid", return_value=4321), patch.object(
            wf.os, "killpg"
        ) as killpg:
            result = wf.replay(PROMPT, self.root, manifest)
        killpg.assert_called_once_with(4321, signal.SIGKILL)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["timed_out"])

    def test_malformed_output_stderr_truncated_and_secrets_redacted(self):
        manifest = self.manifest()
        secret = "sk-live-SECRETVALUE123456"
        with patch.dict(os.environ, {"FREE_AGENT_TEST_API_KEY": secret}):
            result = self.run_replay(
                manifest, "not-a-receipt", rc=1, err=secret + " " + ("noise " * 500)
            )
        self.assertEqual(result["status"], "error")
        stderr = result.get("stderr", "")
        self.assertLessEqual(len(stderr), wf.STDERR_PREVIEW)
        self.assertNotIn(secret, stderr)


class ProposeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.artifact = self.root / "input.txt"
        self.artifact.write_text("pinned\n", encoding="utf-8")

    def test_candidate_is_review_only_not_replayable(self):
        with patch.object(wf, "_exec", side_effect=AssertionError("no process")):
            path = wf.propose(
                PROMPT,
                self.root,
                "python3 -m unittest discover -s tests",
                ["input.txt"],
                metadata={"worker": "cline"},
            )
        candidate = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertTrue(str(path).startswith(str(self.root / ".free-agent" / "candidates")))
        self.assertEqual(candidate["status"], "needs_operator_review")
        self.assertFalse(candidate["approved"])
        self.assertFalse(candidate["replayable"])
        self.assertFalse(candidate["check"]["trusted"])
        self.assertEqual(candidate["check"]["command"], "python3 -m unittest discover -s tests")
        self.assertEqual(candidate["metadata"], {"worker": "cline"})
        self.assertEqual(candidate["request"]["prompt"], PROMPT)
        self.assertEqual(candidate["artifacts"][str(self.artifact)], sha(self.artifact))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_missing_artifact_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            wf.propose(PROMPT, self.root, "check", ["absent.txt"])
        self.assertFalse((self.root / ".free-agent").exists())

    def test_symlink_artifact_rejected(self):
        link = self.root / "link.txt"
        os.symlink(self.artifact, link)
        with self.assertRaises(ValueError):
            wf.propose(PROMPT, self.root, "check", [str(link)])


class ExampleManifestTests(unittest.TestCase):
    def test_shipped_example_is_structurally_valid(self):
        path = Path(__file__).resolve().parents[1] / "workflows.example.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        tools, store, workflows = wf._manifest(data)
        self.assertTrue(os.path.isabs(tools))
        self.assertTrue(os.path.isabs(store))
        self.assertEqual(workflows[0]["context"], {})
        self.assertEqual(workflows[0]["input"], {})


if __name__ == "__main__":
    unittest.main()
