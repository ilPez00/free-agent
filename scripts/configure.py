#!/usr/bin/env python3
"""free-agent fleet configurator.

One tool to detect, configure, install and deploy the free coding-agent
workers across every host in the fleet.

    configure.py scan      # what is installed / authenticated / exhausted
    configure.py apply     # write ~/.config/free-agent/agents.json
    configure.py install   # install the missing agents (npm/pip/binary)
    configure.py deploy    # push config + repo to every fleet host
    configure.py doctor    # scan + gaps + the exact login commands to run

The agent catalog below is the single source of truth for what the
configurator knows about. Adding an agent means adding one row.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("FREE_AGENT_CONFIG")
                   or (Path.home() / ".config/free-agent/agents.json"))
REPO_ROOT = Path(__file__).resolve().parent.parent
FLEET_ALIASES = Path.home() / ".fleet_aliases.sh"

# ---------------------------------------------------------------------------
# Agent catalog
#
# name          worker/registry name
# binary        executable that must be on PATH
# package       npm package (or None)
# auth          exact command the operator must run to log in
# free          'daily' | 'monthly' | 'credits' | None (no free tier)
# priority      lower = tried earlier by automatic routing
# roles         planner roles are never auto-selected as executors
# note          caveat shown by doctor
# ---------------------------------------------------------------------------

CATALOG: list[dict] = [
    dict(name="freebuff", binary="freebuff", package=None, auth="freebuff login",
         free="daily", priority=5,
         note="PTY/tmux only; top priority by operator request"),
    dict(name="freepi", binary="free-pi-cli", package="free-pi-cli",
         auth="free-pi-cli --device-login",
         free="daily", priority=6,
         note="PTY/tmux only; ad-supported; top priority by operator request"),
    dict(name="opencode", binary="opencode", package=None,
         auth="opencode auth login   # or set OPENROUTER_API_KEY",
         free="daily", priority=10, note=""),
    dict(name="copilot", binary="copilot", package="@github/copilot",
         auth="copilot login --device-code", free="credits", priority=12,
         note="GitHub Free plan AI credits"),
    dict(name="codex", binary="codex", package="@openai/codex",
         auth="codex login", free="monthly", priority=14, roles=["planner"],
         note="ChatGPT free/pro allowance; planner role"),
    dict(name="cline", binary="cline", package=None,
         auth="cline auth -p <provider> -k <KEY> -m <model> -b <url>",
         free="daily", priority=20, note=""),
    dict(name="kilo", binary="kilo", package="@kilocode/cli",
         auth="kilo auth login", free="daily", priority=25,
         note="Kilo free models"),
    dict(name="continue", binary="cn", package="@continuedev/cli",
         auth="cn login", free="daily", priority=26,
         note="Continue CLI; headless -p --format json; free tier unverified"),
    dict(name="cody", binary="cody", package="@sourcegraph/cody",
         auth="cody auth login", free="daily", priority=27,
         note="Cody CLI; headless cody chat -m; Cody Free plan unverified"),
    dict(name="kiro", binary="kiro-cli", package=None,
         auth="kiro-cli login --license free --use-device-flow",
         free="monthly", priority=30, note="AWS free tier, 50 credits/mo"),
    dict(name="claude", binary="claude", package="@anthropic-ai/claude-code",
         auth="claude auth login", free=None, priority=35, roles=["planner"],
         note="subscription planner; --allow-scripts needed on install"),
    dict(name="grok", binary="grok", package="@xai-official/grok",
         auth="grok login --device-code", free=None, priority=36, roles=["planner"],
         note="subscription planner; --always-approve needed"),
    dict(name="pi", binary="pi", package="free-pi-cli",
         auth="pi auth login <provider> --api-key", free="daily", priority=40,
         note="aggregator; provider chosen by FREE_AGENT_PROVIDER_MODEL"),
    dict(name="agy", binary="agy", package=None, auth="agy   # Google sign-in",
         free="weekly", priority=45, roles=["planner"],
         note="Antigravity; Google free weekly quota; deferred/opt-in"),
    dict(name="cursor", binary="agent", package=None, auth="cursor auth login",
         free="daily", priority=50, note="Hobby free tier; BYOK/unmetered"),
    dict(name="qwen", binary="qwen", package="@qwen-code/qwen-code",
         auth="qwen login", free="daily", priority=52,
         note="Alibaba Qwen free models"),
    dict(name="vibe", binary="vibe", package=None, auth="vibe login",
         free=None, priority=54, note="Mistral; BYOK"),
    dict(name="crush", binary="crush", package=None, auth="crush auth login",
         free=None, priority=56, note="AWS; BYOK"),
    dict(name="goose", binary="goose", package=None, auth="goose auth login",
         free=None, priority=58, note="Block; BYOK"),
    dict(name="aider", binary="aider", package=None, auth="aider --key <KEY>",
         free=None, priority=60, note="BYOK"),
    dict(name="kimi", binary="kimi", package="@moonshot-ai/kimi-code",
         auth="kimi login", free=None, priority=62, roles=["planner"],
         note="paid subscription only; planner role"),
    dict(name="hermes", binary="hermes", package=None, auth="",
         free="daily", priority=70, roles=["executor", "orchestrator"],
         note="this orchestrator; recursion capped at depth 2"),
]

# Free-route model defaults applied by `apply`.
FREE_ROUTES = {
    "nvidia": {
        "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        "model_order": [
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
            "nvidia/nemotron-3-ultra-550b-a55b",
            "nvidia/nemotron-3-super-120b-a12b",
            "nvidia/nemotron-4-340b-instruct",
        ],
    },
    "openrouter": {
        "model": "openrouter/free",
        "model_order": [
            "openrouter/free",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "nvidia/nemotron-3.5-super-vl:free",
            "google/gemma-4-31b-it:free",
        ],
    },
}

# npm packages that must install with scripts allowed (native binaries).
ALLOW_SCRIPTS = {
    "@anthropic-ai/claude-code": "--allow-scripts=@anthropic-ai/claude-code",
    "@xai-official/grok": "--allow-scripts=@xai-official/grok",
    "@kilocode/cli": "--allow-scripts=@kilocode/cli",
    "@moonshot-ai/kimi-code": "--allow-scripts=@moonshot-ai/kimi-code",
}


def _run(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def _which(binary: str) -> str | None:
    return shutil.which(binary)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def scan_host(host: str | None = None) -> list[dict]:
    """Return one row per catalog agent. host=None scans the local machine."""
    rows = []
    for agent in CATALOG:
        name, binary = agent["name"], agent["binary"]
        if host is None:
            path = _which(binary)
        else:
            rc, out = _run(["ssh", "-o", "ConnectTimeout=8", host,
                            f"command -v {shlex.quote(binary)}"], timeout=25)
            path = out.strip().splitlines()[0] if rc == 0 and out.strip() else None
        rows.append({**agent, "host": host or "local", "installed": bool(path),
                     "path": path})
    return rows


def auth_status(host: str | None = None) -> dict[str, dict]:
    """Ask free-agent for login/quota state; fall back to per-agent probes."""
    rc, out = _run(["free-agent", "status", "--json"], timeout=90)
    status: dict[str, dict] = {}
    if rc == 0 and out.strip():
        try:
            for row in json.loads(out):
                status[row.get("worker", "")] = row
        except ValueError:
            pass
    for agent in CATALOG:
        known = status.get(agent["name"], {})
        if known.get("logged_in") is not None:
            continue
        binary = agent["binary"]
        probe = {
            "codex": [binary, "login", "status"],
            "claude": [binary, "auth", "status"],
            "grok": [binary, "status"],
            "copilot": [binary, "auth", "status"],
            "kilo": [binary, "auth", "list"],
            "kiro": [binary, "auth", "status"],
            "cline": [binary, "auth"],
            "kimi": [binary, "auth", "status"],
        }.get(agent["name"])
        if not probe:
            continue
        if host is None:
            prc, pout = _run(probe, timeout=40)
        else:
            prc, pout = _run(["ssh", "-o", "ConnectTimeout=8", host,
                              " ".join(shlex.quote(c) for c in probe)], timeout=50)
        status[agent["name"]] = {
            "worker": agent["name"], "logged_in": prc == 0,
            "text": pout.strip().splitlines()[-1][:120] if pout.strip() else "",
        }
    return status


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def apply_config() -> int:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if CONFIG_PATH.is_file():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except ValueError:
            print(f"! existing {CONFIG_PATH} is not valid JSON; starting fresh")
    providers = cfg.setdefault("providers", {})
    for name, route in FREE_ROUTES.items():
        block = providers.setdefault(name, {})
        block.update(route)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"wrote {CONFIG_PATH}")
    for name, route in FREE_ROUTES.items():
        print(f"  {name}: {route['model']}")
    return 0


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

def install_missing(host: str | None = None, dry: bool = False) -> int:
    rows = scan_host(host)
    missing = [r for r in rows if not r["installed"] and r["package"]]
    if not missing:
        print("nothing to install")
        return 0
    for r in missing:
        pkg, flag = r["package"], ALLOW_SCRIPTS.get(r["package"], "")
        cmd = f"npm install -g {pkg} {flag}".strip()
        print(f"[{r['host']}] {r['name']}: {cmd}")
        if dry:
            continue
        if host is None:
            _run(["npm", "install", "-g", pkg] + ([flag] if flag else []), timeout=900)
        else:
            _run(["ssh", host, cmd], timeout=900)
    return 0


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------

def fleet_hosts() -> list[str]:
    """Fleet hosts from ~/.fleet_aliases.sh, minus the *-ts Tailscale duplicates."""
    if not FLEET_ALIASES.is_file():
        return []
    hosts = []
    for line in FLEET_ALIASES.read_text().splitlines():
        m = re.match(r"^alias\s+([^=]+)=\s*'ssh\s+([^']+)'", line.strip())
        if not m:
            continue
        alias, host = m.group(1).strip(), m.group(2)
        if alias.endswith("-ts") or host.endswith("-ts") or host in hosts:
            continue
        hosts.append(host)
    return hosts


def deploy(hosts: list[str] | None = None) -> int:
    targets = hosts or fleet_hosts()
    if not targets:
        print(f"no fleet hosts found ({FLEET_ALIASES} missing or empty)")
        return 1
    apply_config()
    failures = 0
    for host in targets:
        rc, out = _run(["ssh", "-o", "ConnectTimeout=8", host, "true"], timeout=25)
        if rc != 0:
            print(f"{host}: BLOCKED (ssh unreachable)")
            failures += 1
            continue
        dest = f"{host}:.config/free-agent/agents.json"
        rc, out = _run(["scp", str(CONFIG_PATH), dest], timeout=90)
        if rc != 0:
            print(f"{host}: BLOCKED (scp failed: {out.strip()[:80]})")
            failures += 1
            continue
        rc, out = _run(["ssh", host,
                        "cd ~/dev/free-agent 2>/dev/null && git pull -q || true"],
                       timeout=120)
        print(f"{host}: OK (config deployed, repo {out.strip() or 'up to date'})")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def doctor(hosts: list[str] | None = None) -> int:
    targets = hosts or fleet_hosts() or []
    all_rows: dict[str, list[dict]] = {}
    for host in [None] + targets:
        label = host or "local"
        all_rows[label] = scan_host(host)
        print(f"scanned {label}: "
              f"{sum(1 for r in all_rows[label] if r['installed'])}/"
              f"{len(all_rows[label])} agents installed")
    status = auth_status()
    print()
    print("=== free-tier workers (operator priority order) ===")
    free_rows = [r for r in all_rows["local"] if r["free"]]
    for r in sorted(free_rows, key=lambda x: x["priority"]):
        st = status.get(r["name"], {})
        state = "ok" if st.get("logged_in") else "NEEDS LOGIN"
        print(f"  {r['name']:<12} prio={r['priority']:<3} {r['free']:<8} {state}")
        if r["note"]:
            print(f"               {r['note']}")
    print()
    print("=== login commands still required (free tier) ===")
    needed = [r for r in all_rows["local"]
              if r["installed"] and r["auth"] and r["free"]
              and not status.get(r["name"], {}).get("logged_in")]
    if not needed:
        print("  none - every installed free-tier agent is authenticated")
    for r in needed:
        print(f"  {r['name']:<12} {r['auth']}")
    print()
    print("=== login commands still required (subscription / BYOK) ===")
    needed = [r for r in all_rows["local"]
              if r["installed"] and r["auth"] and not r["free"]
              and not status.get(r["name"], {}).get("logged_in")]
    if not needed:
        print("  none")
    for r in needed:
        print(f"  {r['name']:<12} {r['auth']}")
    print()
    print("=== not installed locally ===")
    missing = [r for r in all_rows["local"] if not r["installed"]]
    if not missing:
        print("  none")
    for r in missing:
        pkg = f" (npm {r['package']})" if r["package"] else ""
        print(f"  {r['name']:<12}{pkg}")
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["scan", "apply", "install", "deploy", "doctor"])
    p.add_argument("--host", action="append", default=[],
                   help="target a specific host (repeatable)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.command == "scan":
        rows = scan_host(args.host[0] if args.host else None)
        for r in rows:
            print(f"{r['name']:<12} {'installed' if r['installed'] else 'missing':<9} "
                  f"{r['free'] or '-':<8} prio={r['priority']}")
        return 0
    if args.command == "apply":
        return apply_config()
    if args.command == "install":
        return install_missing(args.host[0] if args.host else None, args.dry_run)
    if args.command == "deploy":
        return deploy(args.host or None)
    if args.command == "doctor":
        return doctor(args.host or None)
    return 2


if __name__ == "__main__":
    sys.exit(main())
