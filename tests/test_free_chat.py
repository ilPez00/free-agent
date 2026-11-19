import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import free_chat


def ok_payload(content="HELLO_OK"):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


class FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class ResolveKeyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = os.path.join(self._tmp.name, "keys.env")

    def test_last_nonempty_wins(self):
        with open(self.env, "w") as fh:
            fh.write("MYKEY=\nMYKEY=first-key\nMYKEY=last-key\n")
        self.assertEqual(free_chat.resolve_key(self.env, "MYKEY"), "last-key")

    def test_quoted_key_stripped(self):
        with open(self.env, "w") as fh:
            fh.write('MYKEY="quoted-key"\n')
        self.assertEqual(free_chat.resolve_key(self.env, "MYKEY"), "quoted-key")

    def test_missing_file_is_empty(self):
        self.assertEqual(free_chat.resolve_key(self.env, "MYKEY"), "")


class PostChatTests(unittest.TestCase):
    def test_key_never_in_argv_and_config_removed(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            cfg = cmd[cmd.index("-K") + 1]
            seen["cfg_content"] = open(cfg).read()
            seen["cfg_mode"] = oct(os.stat(cfg).st_mode & 0o777)
            seen["cfg_path"] = cfg
            return FakeCompleted(stdout=json.dumps(ok_payload()) + "\nHTTP:200")

        with patch.object(free_chat.subprocess, "run", side_effect=fake_run):
            code, text = free_chat.post_chat("https://x.example/v1", {"m": 1}, "SECRET-KEY", 30)
        self.assertEqual(code, 200)
        self.assertNotIn("SECRET-KEY", " ".join(seen["cmd"]))
        self.assertIn("Bearer SECRET-KEY", seen["cfg_content"])
        self.assertEqual(seen["cfg_mode"], "0o600")
        self.assertFalse(os.path.exists(seen["cfg_path"]))

    def test_no_key_no_config_flag(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return FakeCompleted(stdout=json.dumps(ok_payload()) + "\nHTTP:200")

        with patch.object(free_chat.subprocess, "run", side_effect=fake_run):
            free_chat.post_chat("https://x.example/v1", {}, "", 30)
        self.assertNotIn("-K", seen["cmd"])

    def test_curl_failure_maps_to_code_zero(self):
        with patch.object(free_chat.subprocess, "run",
                          return_value=FakeCompleted(stderr="connection refused", returncode=7)):
            code, text = free_chat.post_chat("https://x.example/v1", {}, "", 30)
        self.assertEqual(code, 0)
        self.assertIn("connection refused", text)


class MainTests(unittest.TestCase):
    def run_main(self, argv, code=200, payload=None, text=None):
        out, err = io.StringIO(), io.StringIO()
        body_text = text if text is not None else json.dumps(payload)
        with patch.object(free_chat, "post_chat", return_value=(code, body_text)) as pc, \
             redirect_stdout(out), redirect_stderr(err):
            rc = free_chat.main(argv)
        return rc, out.getvalue(), err.getvalue(), pc

    def base_argv(self, task="say hi"):
        return ["--base-url", "https://x.example/v1", "--model", "m", task]

    def test_success_prints_content_only(self):
        rc, out, err, _ = self.run_main(self.base_argv(), payload=ok_payload())
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "HELLO_OK")
        self.assertEqual(err, "")

    def test_empty_content_is_failure(self):
        rc, out, err, _ = self.run_main(
            self.base_argv(), payload={"choices": [{"message": {"content": ""},
                                                    "finish_reason": "stop"}]})
        self.assertEqual(rc, 1)
        self.assertIn("empty response", err)

    def test_error_payload_is_failure(self):
        rc, out, err, _ = self.run_main(self.base_argv(),
                                        payload={"error": {"message": "quota exceeded"}})
        self.assertEqual(rc, 1)
        self.assertIn("quota exceeded", err)

    def test_http_error_is_failure(self):
        rc, out, err, _ = self.run_main(self.base_argv(), code=401, text='{"error":"bad key"}')
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 401", err)

    def test_non_json_is_failure(self):
        rc, out, err, _ = self.run_main(self.base_argv(), code=200, text="<html>challenge</html>")
        self.assertEqual(rc, 1)
        self.assertIn("non-JSON", err)

    def test_task_with_quotes_and_newlines_is_json_safe(self):
        task = 'line1\nline2 "quoted" \\ backslash'
        rc, out, err, pc = self.run_main(
            ["--base-url", "https://x.example/v1", "--model", "m", task], payload=ok_payload())
        self.assertEqual(rc, 0)
        body = pc.call_args[0][1]
        self.assertEqual(body["messages"][0]["content"], task)
        self.assertFalse(body["stream"])

    def test_key_env_resolved_and_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.path.join(tmp, "k.env")
            with open(env, "w") as fh:
                fh.write("MYKEY=the-key\n")
            rc, out, err, pc = self.run_main(
                self.base_argv() + ["--key-env", "MYKEY", "--env-file", env],
                payload=ok_payload())
        self.assertEqual(rc, 0)
        self.assertEqual(pc.call_args[0][2], "the-key")


if __name__ == "__main__":
    unittest.main()