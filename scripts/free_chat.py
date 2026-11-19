#!/usr/bin/env python3
"""Generic non-streaming OpenAI-compatible chat runner for free-agent cli workers.

Some free endpoints break pi's streaming transport ("Stream ended without
finish_reason") but answer plain non-streaming chat/completions fine. This
runner gives those routes a first-class worker without a pi dependency.

Transport is curl, not urllib: several free edges (Cloudflare 1010) block
non-browser TLS fingerprints. The key travels via a 0600 curl config file,
never argv, and is removed afterwards.

Usage:
  free_chat.py --base-url URL --model ID [--key-env NAME] [--env-file PATH]
               [--max-tokens N] [--timeout SEC] TASK

The key is read from the env file (last non-empty assignment wins, matching
profiles.read_env_file semantics); it is never printed. Exit codes: 0 success,
1 request/HTTP/empty-response failure (message on stderr).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile


def resolve_key(env_file, name):
    key = ""
    try:
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^(?:export\s+)?" + re.escape(name) + r"=(.*)", line.strip())
                if m:
                    value = m.group(1).strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    if value:
                        key = value
    except OSError:
        pass
    return key


def post_chat(base_url, body, key, timeout):
    """POST via curl; returns (http_code, response_text). Key never in argv."""
    url = base_url.rstrip("/") + "/chat/completions"
    cfg_path = None
    cmd = ["curl", "-sS", "-m", str(timeout), "-w", "\nHTTP:%{http_code}",
           url, "-H", "Content-Type: application/json", "-d", json.dumps(body)]
    try:
        if key:
            fd, cfg_path = tempfile.mkstemp(prefix="free-chat-", suffix=".cfg")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write('header = "Authorization: Bearer %s"\n' % key)
            os.chmod(cfg_path, 0o600)
            cmd += ["-K", cfg_path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
    finally:
        if cfg_path:
            os.unlink(cfg_path)
    text, _, code = r.stdout.rpartition("HTTP:")
    if r.returncode != 0 and not code.strip():
        return 0, (r.stderr or "curl failed")[:300]
    try:
        return int(code.strip()), text
    except ValueError:
        return 0, text[:300]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--key-env", default=None)
    p.add_argument("--env-file", default=os.path.expanduser("~/.env"))
    p.add_argument("--max-tokens", type=int, default=2048)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("task")
    a = p.parse_args(argv)

    key = resolve_key(a.env_file, a.key_env) if a.key_env else ""
    body = {"model": a.model, "stream": False, "max_tokens": a.max_tokens,
            "messages": [{"role": "user", "content": a.task}]}
    try:
        code, text = post_chat(a.base_url, body, key, a.timeout)
    except Exception as e:
        print(f"request failed: {e}", file=sys.stderr)
        return 1
    if code != 200:
        print(f"HTTP {code}: {text[:300]}", file=sys.stderr)
        return 1
    try:
        d = json.loads(text)
    except ValueError:
        print(f"non-JSON response: {text[:200]}", file=sys.stderr)
        return 1
    if d.get("error"):
        print(json.dumps(d["error"])[:300], file=sys.stderr)
        return 1
    choice = (d.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    if not content.strip():
        print(f"empty response (finish_reason={choice.get('finish_reason')})", file=sys.stderr)
        return 1
    print(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())