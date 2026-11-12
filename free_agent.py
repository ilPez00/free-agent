#!/usr/bin/env python3
"""free-agent — route coding tasks across free-tier agent CLIs.

Orchestrates free coding agents (opencode, kiro-cli, cline, pi, freebuff, agy)
with live quota probing, automatic failover, and a re-login nudge when a worker
runs out of credit or its session expires.

Usage:
  free-agent status [--json]              # live quota/login state for every worker
  free-agent run "<task>" [--workdir DIR] [--worker NAME] [--timeout SEC] [--json]
  free-agent nudge [--json]               # re-prompt for every exhausted worker
  free-agent watch [--interval SEC]       # poll quotas and nudge on change
  free-agent workers                      # list the registry

Exit codes: 0 ok, 1 usage error, 2 all workers failed / reauth required.

Designed to be called by Hermes (terminal tool) or a shell. Machine-readable
output is emitted with --json or when stdout is not a TTY and --json is implied
by the FREE_AGENT_JSON=1 environment variable.
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
import time
import signal
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import extensions
import workflows

STATE_PATH = Path(os.environ.get("FREE_AGENT_STATE", Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "free-agent/state.json"))
NOTIFY = shutil.which("notify-send") or ""
TMUX = shutil.which("tmux") or ""
HERMES_BIN = os.environ.get("FREE_AGENT_HERMES_BIN", str(Path.home() / ".hermes/hermes-agent/.hermes/bin/hermes"))

# Runnable from anywhere: make sure the standard user bin dirs are on PATH for
# worker discovery, whatever shell/cron/SSH session launched this script.
for _p in (str(Path.home() / ".local/bin"), str(Path.home() / ".npm-global/bin"), str(Path.home() / "bin")):
    if os.path.isdir(_p) and _p not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _p + os.pathsep + os.environ.get("PATH", "")

# ---------------------------------------------------------------------------
# exhaustion / auth signatures
# ---------------------------------------------------------------------------

EXHAUST_PATTERNS = [
    r"free usage limit reached", r"usage limit reached", r"usage limit",
    r"rate.?limit", r"too many requests", r"\b429\b", r"\b402\b", r"payment required",
    r"insufficient credits", r"out of credits", r"no credits", r"credit[s]? (?:is|are) (?:exhausted|depleted)",
    r"quota (?:exceeded|exhausted|reached|limit)", r"limit exceeded", r"exceeded your (?:quota|limit)",
    r"spend_limited", r"model_locked", r"model_unavailable", r"ip_capped",
    r"premium_slot_taken", r"purchase_capacity", r"daily limit", r"weekly limit",
    r"monthly (?:limit|quota)", r"no hours remaining", r"out of hours", r"0/\d+ hours",
    r"balance unavailable", r"balance is unavailable",
    r"subscription (?:expired|required)", r"plan (?:expired|limit)",
]
AUTH_PATTERNS = [
    r"authentication required", r"not authenticated", r"please log ?in", r"please sign ?in",
    r"login required", r"log ?in required", r"sign in to", r"sign-in required",
    r"unauthorized", r"invalid (?:api )?key", r"invalid credential", r"\b401\b",
    r"authentication failed", r"auth required", r"re-?auth", r"needs-auth", r"not logged in", r"no authentication information found",
    r"credentials? (?:expired|invalid|required)", r"token expired", r"session expired",
]
EXHAUST_RE = re.compile("|".join(EXHAUST_PATTERNS), re.I)
AUTH_RE = re.compile("|".join(AUTH_PATTERNS), re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]|\x1b[=>]")


def _snippet(text: str, m: re.Match) -> str:
    start = text.rfind("\n", 0, m.start()) + 1
    end = text.find("\n", m.end())
    line = text[start:end if end != -1 else len(text)].strip()
    line = re.sub(r"\s+", " ", line)
    return line[:160] or m.group(0)


def classify(text: str) -> tuple[bool, bool, str]:
    """Return (exhausted, auth_required, reason)."""
    m = EXHAUST_RE.search(text)
    if m:
        return True, False, m.group(0)
    m = AUTH_RE.search(text)
    if m:
        return False, True, m.group(0)
    return False, False, ""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run_cmd(cmd: list[str], cwd: str | None = None, timeout: float = 120,
            env: dict | None = None, input_text: str | None = None) -> tuple[int, str, str]:
    timeout = max(0.05, min(float(timeout), 3600))
    try:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
        try:
            out, err = p.communicate(input_text, timeout=timeout)
            return p.returncode, out or "", err or ""
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate()
            return 124, out or "", (err or "") + f"\ntimeout after {timeout}s"
    except OSError as e:
        return 127, "", f"could not launch {cmd[0]}: {e.strerror}"


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def notify(title: str, body: str, urgency: str = "critical") -> None:
    if not NOTIFY:
        return
    try:
        subprocess.run([NOTIFY, "-u", urgency, "-a", "free-agent", title, body],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"version": 1, "workers": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".state-", dir=STATE_PATH.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(name, STATE_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def mark(state: dict, name: str, reason: str) -> None:
    state.setdefault("workers", {})[name] = {"exhausted": True, "reason": reason, "ts": int(time.time())}
    save_state(state)


def clear(state: dict, name: str) -> None:
    if name in state.get("workers", {}):
        del state["workers"][name]
        save_state(state)


# ---------------------------------------------------------------------------
# worker result
# ---------------------------------------------------------------------------

@dataclass
class Result:
    ok: bool
    text: str
    exhausted: bool = False
    auth_required: bool = False
    reason: str = ""
    cost: str = ""
    model: str = ""


@dataclass
class Worker:
    name: str
    description: str
    priority: int
    headless: bool
    reauth: str
    run: Callable[[str, str, float], tuple[int, str, str]]
    extract: Callable[[int, str, str], Result]
    quota: Callable[[], dict]
    login: Callable[[], dict]
    installed: Callable[[], bool] = lambda: True
    roles: list = field(default_factory=lambda: ["executor"])
    automatic: bool = True
    specialty: str = "implementation"
    model: str = ""


# ---------------------------------------------------------------------------
# worker: opencode
# ---------------------------------------------------------------------------

def oc_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return run_cmd(
        ["opencode", "run", "--format", "json", "--agent", "build", "--auto", "--archived", task],
        cwd=workdir, timeout=timeout,
    )


def oc_extract(rc: int, out: str, err: str) -> Result:
    text_parts: list[str] = []
    cost = ""
    model = ""
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        part = d.get("part", {}) or {}
        if d.get("type") == "text" or part.get("type") == "text":
            t = part.get("text") or d.get("text") or ""
            if t:
                text_parts.append(t)
        if d.get("type") == "step_finish":
            cost = str(part.get("cost", ""))
        if part.get("model"):
            model = part["model"]
    blob = out + "\n" + err
    ex, au, reason = classify(blob)
    ok = rc == 0 and bool(text_parts) and not ex and not au
    return Result(ok=ok, text="\n".join(text_parts).strip(), exhausted=ex, auth_required=au,
                  reason=reason, cost=cost, model=model)


def oc_quota() -> dict:
    rc, out, err = run_cmd(["opencode", "auth", "list"], timeout=30)
    logged = rc == 0 and "stored" in out.lower() or "environment" in out.lower()
    return {"available": None, "pct": None,
            "text": "free models (no CLI quota meter); auth list: " + ("ok" if logged else "check"),
            "logged_in": logged if rc == 0 else None}


def oc_login() -> dict:
    rc, out, err = run_cmd(["opencode", "auth", "list"], timeout=30)
    if rc != 0:
        return {"logged_in": None, "text": (err or out).strip()[:160]}
    return {"logged_in": True, "text": "providers stored"}


# ---------------------------------------------------------------------------
# worker: kiro-cli
# ---------------------------------------------------------------------------

def kiro_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return run_cmd(
        ["kiro-cli", "chat", "--no-interactive", "--output-format", "stream-json",
         "--trust-all-tools", "--trust-workspace", task],
        cwd=workdir, timeout=timeout,
    )


def kiro_extract(rc: int, out: str, err: str) -> Result:
    chunks: list[str] = []
    final = ""
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "runFinished":
            final = (d.get("data") or {}).get("finalText", "") or final
        upd = (d.get("data") or {}).get("update") or {}
        if upd.get("sessionUpdate") == "agent_message_chunk":
            t = (upd.get("content") or {}).get("text")
            if t:
                chunks.append(t)
    text = final or "".join(chunks)
    blob = out + "\n" + err
    ex, au, reason = classify(blob)
    ok = rc == 0 and bool(text.strip()) and not ex and not au
    return Result(ok=ok, text=text.strip(), exhausted=ex, auth_required=au, reason=reason)


def kiro_quota() -> dict:
    rc, out, err = run_cmd(
        ["kiro-cli", "chat", "--no-interactive", "--output-format", "stream-json",
         "--trust-workspace", "/usage"],
        timeout=60,
    )
    text = ""
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        upd = (d.get("data") or {}).get("update") or {}
        if upd.get("sessionUpdate") == "agent_message_chunk":
            text += (upd.get("content") or {}).get("text", "")
    m = re.search(r"Credits \(([\d.]+) of ([\d.]+)[^)]*\),\s*([\d.]+)%", text)
    if m:
        used, total, pct = float(m.group(1)), float(m.group(2)), float(m.group(3))
        return {"available": True, "pct": pct, "used": used, "total": total,
                "text": f"{used}/{total} credits ({pct}%)", "logged_in": True}
    if "KIRO FREE" in text or "Credits" in text:
        return {"available": True, "pct": None, "text": text.strip()[:160], "logged_in": True}
    return {"available": None, "pct": None, "text": (text or err or out).strip()[:160],
            "logged_in": None}


def kiro_login() -> dict:
    rc, out, err = run_cmd(["kiro-cli", "whoami"], timeout=30)
    if rc == 0 and ("Logged in" in out or "@" in out):
        return {"logged_in": True, "text": out.strip().splitlines()[0] if out.strip() else "logged in"}
    return {"logged_in": False if rc != 0 else None, "text": (err or out).strip()[:160]}


# ---------------------------------------------------------------------------
# worker: cline
# ---------------------------------------------------------------------------

def cline_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return run_cmd(["cline", task, "--json", "--auto-approve", "true"],
                   cwd=workdir, timeout=timeout)


def cline_extract(rc: int, out: str, err: str) -> Result:
    text = ""
    cost = ""
    model = ""
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "run_result":
            text = d.get("text", "") or text
            cost = str((d.get("usage") or {}).get("totalCost", ""))
            model = ((d.get("model") or {}).get("id")) or model
    blob = out + "\n" + err
    ex, au, reason = classify(blob)
    ok = rc == 0 and bool(text.strip()) and not ex and not au
    return Result(ok=ok, text=text.strip(), exhausted=ex, auth_required=au, reason=reason,
                  cost=cost, model=model)


def cline_quota() -> dict:
    # Cline free models report totalCost 0; there is no CLI quota meter.
    return {"available": None, "pct": None,
            "text": "free models (cost 0 per run); no CLI quota meter", "logged_in": None}


def cline_login() -> dict:
    rc, out, err = run_cmd(["cline", "auth"], timeout=30)
    text = strip_ansi(out or err).strip()
    blob = text.lower()
    if rc == 0 and ("authenticated" in blob or "provider" in blob or "logged" in blob):
        return {"logged_in": True, "text": text[:160]}
    return {"logged_in": None, "text": text[:160]}


# ---------------------------------------------------------------------------
# worker: pi
# ---------------------------------------------------------------------------

PI_PROVIDER = os.environ.get("FREE_AGENT_PI_PROVIDER", "openrouter")
PI_MODEL = os.environ.get("FREE_AGENT_PI_MODEL", "openrouter/free")


def pi_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return run_cmd(
        ["pi", "-p", "--mode", "json", "--provider", PI_PROVIDER, "--model", PI_MODEL, "--tools", "read,bash,write,edit", task],
        cwd=workdir, timeout=timeout,
    )


def pi_extract(rc: int, out: str, err: str) -> Result:
    text = extract_json_text(out)
    blob = out + "\n" + err
    ex, au, reason = classify(blob)
    ok = rc == 0 and bool(text) and not ex and not au
    return Result(ok=ok, text=text, exhausted=ex, auth_required=au, reason=reason,
                  model=f"{PI_PROVIDER}/{PI_MODEL}")


def pi_quota() -> dict:
    return {"available": None, "pct": None,
            "text": f"free model {PI_PROVIDER}/{PI_MODEL}; no CLI quota meter", "logged_in": None}


def pi_login() -> dict:
    rc, out, err = run_cmd(["pi", "auth", "check", "--provider", PI_PROVIDER, "--json"], timeout=30)
    try:
        d = json.loads(out.strip().splitlines()[-1])
        status = d.get("status")
        return {"logged_in": status == "ready", "text": f"{status}: {d.get('reason','')}".strip()}
    except Exception:
        return {"logged_in": None, "text": (out or err).strip()[:160]}


# ---------------------------------------------------------------------------
# worker: freebuff (PTY via tmux — no headless mode exists)
# ---------------------------------------------------------------------------

FB_SESSION = "fa-fb"


def _tmux(*args: str, timeout: float = 20) -> tuple[int, str, str]:
    return run_cmd([TMUX, *args], timeout=timeout)


def _fb_pane() -> str:
    rc, out, err = _tmux("capture-pane", "-p", "-t", FB_SESSION)
    return strip_ansi(out)


def _fb_wait(markers: list[str], timeout: float, stability: int = 3) -> str:
    """Wait until a marker appears, or the pane is stable for `stability` polls."""
    last = None
    stable = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        pane = _fb_pane()
        if any(m in pane for m in markers):
            return pane
        if pane == last:
            stable += 1
            if stable >= stability:
                return pane
        else:
            stable = 0
            last = pane
        time.sleep(1.5)
    return _fb_pane()


def fb_installed() -> bool:
    return shutil.which("freebuff") is not None and bool(TMUX)


def fb_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    _tmux("kill-session", "-t", FB_SESSION)
    rc, out, err = _tmux("new-session", "-d", "-s", FB_SESSION, "-c", workdir,
                         "-x", "220", "-y", "50")
    if rc != 0:
        return rc, "", f"tmux new-session failed: {err}"
    time.sleep(1.5)
    _tmux("send-keys", "-t", FB_SESSION,
          f"freebuff --cwd {shlex.quote(workdir)} --trust-agents", "Enter")
    pane = _fb_wait(["Enter a coding task", "Add to the current task", "Press ENTER to login"], 90)
    if "Press ENTER to login" in pane:
        _tmux("kill-session", "-t", FB_SESSION)
        return 3, pane, "freebuff is not logged in (Press ENTER to login)"
    _tmux("send-keys", "-t", FB_SESSION, "-l", task)
    _tmux("send-keys", "-t", FB_SESSION, "Enter")
    # wait for the reply to stream in and the pane to settle
    deadline = time.time() + timeout
    last = None
    stable = 0
    while time.time() < deadline:
        pane = _fb_pane()
        if "thinking..." not in pane and "esc to stop" not in pane.lower():
            if pane == last:
                stable += 1
                if stable >= 5:
                    break
            else:
                stable = 0
                last = pane
        time.sleep(1.5)
    _tmux("kill-session", "-t", FB_SESSION)
    return 0, pane, ""


def fb_extract(rc: int, out: str, err: str) -> Result:
    blob = out + "\n" + err
    ex, au, reason = classify(blob)
    if rc == 3:
        return Result(ok=False, text="", exhausted=False, auth_required=True,
                      reason="freebuff not logged in")
    # keep the last non-empty block of the pane (the assistant reply)
    lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
    text = "\n".join(lines[-25:]).strip()
    ok = rc == 0 and bool(text) and not ex and not au
    return Result(ok=ok, text=text, exhausted=ex, auth_required=au, reason=reason,
                  model="freebuff-default")


def fb_quota() -> dict:
    pane = ""
    if fb_installed():
        # a live session is needed to read the balance; report from state instead
        pass
    rc, out, err = run_cmd(["freebuff", "doctor"], timeout=60)
    warnings = [ln.strip() for ln in (out + err).splitlines() if "WARNING" in ln or "crash" in ln.lower()]
    return {"available": None, "pct": None,
            "text": ("doctor: " + ("; ".join(warnings) if warnings else "ok"))[:200],
            "logged_in": None}


def fb_login() -> dict:
    creds = Path.home() / ".config/manicode/credentials.json"
    if not creds.exists():
        return {"logged_in": False, "text": "no credentials.json"}
    try:
        d = json.loads(creds.read_text())
        has = bool(d.get("default", {}).get("tokenStore") or d.get("default", {}).get("authToken"))
        return {"logged_in": True if has else False,
                "text": "credentials present" if has else "credentials incomplete"}
    except Exception as e:
        return {"logged_in": None, "text": f"credentials unreadable: {e}"}


# ---------------------------------------------------------------------------
# worker: agy (Antigravity CLI — headless -p, weekly free quota)
# ---------------------------------------------------------------------------

def agy_installed() -> bool:
    return shutil.which("agy") is not None


def agy_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return run_cmd(["agy", "-p", task, "--output-format", "json"], cwd=workdir, timeout=timeout)


def agy_extract(rc: int, out: str, err: str) -> Result:
    text = ""
    status = ""
    try:
        d = json.loads(out.strip().splitlines()[-1])
        status = d.get("status", "")
        text = d.get("response", "") or ""
    except Exception:
        text = out.strip()
    blob = out + "\n" + err
    ex, au, reason = classify(blob)
    if status == "ERROR" and not reason:
        reason = "agy status ERROR"
    ok = rc == 0 and status == "SUCCESS" and bool(text.strip()) and not ex and not au
    return Result(ok=ok, text=text.strip(), exhausted=ex, auth_required=au, reason=reason)


def agy_quota() -> dict:
    # `agy -p` blocks 60s waiting for interactive auth when signed out; do not probe here.
    return {"available": None, "pct": None,
            "text": "Antigravity free weekly quota (no non-interactive meter)", "logged_in": None}


def agy_login() -> dict:
    if not agy_installed():
        return {"logged_in": None, "text": "not installed"}
    rc, out, err = run_cmd(["agy", "--version"], timeout=20)
    if rc == 0:
        return {"logged_in": None, "text": f"installed ({out.strip()[:40]}); auth checked at run time"}
    return {"logged_in": None, "text": (err or out).strip()[:120]}


# ---------------------------------------------------------------------------
# worker: copilot (GitHub Copilot CLI — free plan includes CLI + agent mode)
# ---------------------------------------------------------------------------

def copilot_installed() -> bool:
    return shutil.which("copilot") is not None


def copilot_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return run_cmd(
        ["copilot", "-p", task, "-s", "--output-format", "json",
         "--allow-all-tools", "--no-ask-user", "--trust-folder"],
        cwd=workdir, timeout=timeout,
    )


def copilot_extract(rc: int, out: str, err: str) -> Result:
    parts: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            for key in ("text", "response", "content", "message", "result", "output"):
                v = d.get(key)
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            if item["text"].strip():
                                parts.append(item["text"].strip())
        else:
            parts.append(line)
    text = "\n".join(parts).strip() or out.strip()
    blob = out + "\n" + err
    ex, au, reason = classify(blob)
    if "no authentication information found" in blob.lower():
        au, reason = True, "copilot: no authentication information found"
    ok = rc == 0 and bool(text) and not ex and not au
    return Result(ok=ok, text=text, exhausted=ex, auth_required=au, reason=reason)


def copilot_quota() -> dict:
    return {"available": None, "pct": None,
            "text": "Copilot Free: limited chat/agent AI credits (no CLI meter)", "logged_in": None}


def copilot_login() -> dict:
    # OAuth lives in the system keychain (service copilot-cli) or ~/.copilot/config.json.
    cfg = Path.home() / ".copilot/config.json"
    if cfg.exists():
        try:
            d = json.loads(cfg.read_text())
            if any(k in d for k in ("accessToken", "token", "oauth")):
                return {"logged_in": True, "text": "copilot config has token"}
        except Exception:
            pass
    return {"logged_in": None, "text": "copilot auth not proven (runs a real turn to check)"}


# ---------------------------------------------------------------------------
# worker: free-pi-cli (ad-supported, GitHub sign-in, daily allowance)
# ---------------------------------------------------------------------------

def freepi_installed() -> bool:
    return shutil.which("free-pi-cli") is not None


def pty_generic(cmd: list[str], ready_markers: list[str], task: str,
                workdir: str, timeout: float, session: str) -> tuple[int, str, str]:
    """Drive a TUI-only agent in tmux: start it, type the task, capture the pane."""
    _tmux("kill-session", "-t", session)
    rc, out, err = _tmux("new-session", "-d", "-s", session, "-c", workdir, "-x", "220", "-y", "50")
    if rc != 0:
        return rc, "", f"tmux new-session failed: {err}"
    time.sleep(1.5)
    _tmux("send-keys", "-t", session, " ".join(shlex.quote(c) for c in cmd), "Enter")
    pane = _fb_wait_session(session, ready_markers, 90)
    _tmux("send-keys", "-t", session, "-l", task)
    _tmux("send-keys", "-t", session, "Enter")
    deadline = time.time() + timeout
    last, stable = None, 0
    while time.time() < deadline:
        pane = _fb_pane_session(session)
        if pane == last:
            stable += 1
            if stable >= 6:
                break
        else:
            stable = 0
            last = pane
        time.sleep(1.5)
    _tmux("kill-session", "-t", session)
    return 0, pane, ""


def _fb_pane_session(session: str) -> str:
    rc, out, err = _tmux("capture-pane", "-p", "-t", session)
    return strip_ansi(out)


def _fb_wait_session(session: str, markers: list[str], timeout: float, stability: int = 4) -> str:
    last, stable = None, 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        pane = _fb_pane_session(session)
        if any(m in pane for m in markers):
            return pane
        if pane == last:
            stable += 1
            if stable >= stability:
                return pane
        else:
            stable = 0
            last = pane
        time.sleep(1.5)
    return _fb_pane_session(session)


def freepi_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return pty_generic(["free-pi-cli"], ["Ask", "prompt", "›", "free-pi", "Chat", "Type", "chat"],
                       task, workdir, timeout, "fa-freepi")


def freepi_extract(rc: int, out: str, err: str) -> Result:
    parts: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") in ("agent_end", "turn_end"):
                for m in d.get("messages", []) if isinstance(d.get("messages"), list) else []:
                    if m.get("role") == "assistant":
                        c = m.get("content")
                        if isinstance(c, str) and c.strip():
                            parts.append(c.strip())
                        elif isinstance(c, list):
                            for item in c:
                                if isinstance(item, dict) and isinstance(item.get("text"), str):
                                    if item["text"].strip():
                                        parts.append(item["text"].strip())
            for key in ("text", "response"):
                v = d.get(key)
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
        else:
            parts.append(line)
    text = "\n".join(parts).strip() or out.strip()
    blob = out + "\n" + err
    ex, au, reason = classify(blob)
    ok = rc == 0 and bool(text) and not ex and not au
    return Result(ok=ok, text=text, exhausted=ex, auth_required=au, reason=reason)


def freepi_quota() -> dict:
    return {"available": None, "pct": None,
            "text": "free-pi daily allowance (ad-supported; no CLI meter)", "logged_in": None}


def freepi_login() -> dict:
    cfg = Path.home() / ".free-pi/agent"
    return {"logged_in": True if cfg.exists() else False,
            "text": "free-pi token present" if cfg.exists() else "free-pi not signed in"}


def extract_json_text(out: str) -> str:
    """Read assistant/result events only; never return tool input or startup logs."""
    parts = []
    def content(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(str(v.get("text", "")) for v in value if isinstance(v, dict) and v.get("type") in ("text", "output_text"))
        return ""
    try:
        # CLI wrappers return one indented JSON document, not JSONL events.
        lines = [json.dumps(json.loads(out))]
    except ValueError:
        lines = out.splitlines()
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        text = ""
        if t == "text":
            text = d.get("part", {}).get("text", d.get("text", ""))
        elif t in ("result", "run_result"):
            text = d.get("result", d.get("text", d.get("response", "")))
        elif t == "assistant":
            msg = d.get("message", {})
            text = content(msg.get("content", d.get("content", ""))) if isinstance(msg, dict) else ""
        elif t in ("message_end", "turn_end"):
            msg = d.get("message", {})
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                text = content(msg.get("content", ""))
        elif t == "item.completed":
            item = d.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text", "")
        elif d.get("ok") is True and not t and isinstance(d.get("text"), str):
            text = d["text"]
        elif d.get("status") == "SUCCESS":
            text = d.get("response", "")
        if isinstance(text, str) and text.strip() and (not parts or text.strip() != parts[-1]):
            parts.append(text.strip())
    return "\n".join(parts)


def _generic_json_worker(cmd: list[str], task: str, workdir: str, timeout: float,
                         input_text: str | None = None) -> tuple[int, str, str]:
    return run_cmd(cmd, cwd=workdir, timeout=timeout, input_text=input_text)


def _generic_extract(rc: int, out: str, err: str) -> Result:
    has_json = any(line.lstrip().startswith("{") for line in out.splitlines())
    text = extract_json_text(out) if has_json else out.strip()
    blob = out + "\n" + err
    ex, au, reason = classify(blob)
    ok = rc == 0 and bool(text.strip()) and not ex and not au
    return Result(ok=ok, text=text.strip(), exhausted=ex, auth_required=au, reason=reason)


# ---------------------------------------------------------------------------
# worker: kilo (Kilo Code CLI — Auto Free models)
# ---------------------------------------------------------------------------

def kilo_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return _generic_json_worker(["kilo", "run", task, "--format", "json", "--model", os.environ.get("FREE_AGENT_KILO_MODEL", "kilo/kilo-auto/free")], task, workdir, timeout)


def kilo_extract(rc: int, out: str, err: str) -> Result:
    return _generic_extract(rc, out, err)


def kilo_quota() -> dict:
    return {"available": None, "pct": None, "text": "Kilo Auto Free (no CLI meter)", "logged_in": None}


def kilo_login() -> dict:
    rc, out, err = run_cmd(["kilo", "auth", "list"], timeout=30)
    blob = strip_ansi(out + err)
    if rc == 0 and any(k in blob.lower() for k in ("provider", "credential", "logged", "api key")):
        return {"logged_in": True, "text": blob.strip()[:120]}
    return {"logged_in": None, "text": blob.strip()[:120]}


# ---------------------------------------------------------------------------
# worker: crush (Charm Crush — BYOK / Hyper)
# ---------------------------------------------------------------------------

def crush_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return run_cmd(["crush", "run", task], cwd=workdir, timeout=timeout)


def crush_extract(rc: int, out: str, err: str) -> Result:
    return _generic_extract(rc, out, err)


def crush_quota() -> dict:
    return {"available": None, "pct": None, "text": "Crush: BYOK / Charm Hyper (no CLI meter)", "logged_in": None}


def crush_login() -> dict:
    cfg = Path.home() / ".config/crush/crush.json"
    return {"logged_in": True if cfg.exists() else False,
            "text": "crush config present" if cfg.exists() else "crush not configured"}


# ---------------------------------------------------------------------------
# worker: codex (OpenAI Codex CLI — ChatGPT Free includes Codex)
# ---------------------------------------------------------------------------

def codex_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return _generic_json_worker(["codex", "exec", task, "--json", "--sandbox", "read-only"], task, workdir, timeout)


def codex_extract(rc: int, out: str, err: str) -> Result:
    return _generic_extract(rc, out, err)


def codex_quota() -> dict:
    return {"available": None, "pct": None, "text": "ChatGPT Free Codex allowance (no CLI meter)", "logged_in": None}


def codex_login() -> dict:
    auth = Path.home() / ".codex/auth.json"
    if auth.exists():
        return {"logged_in": True, "text": "codex auth.json present"}
    rc, out, err = run_cmd(["codex", "doctor"], timeout=60)
    blob = (out + err).lower()
    if "no codex credentials" in blob or "run codex login" in blob:
        return {"logged_in": False, "text": "codex: no credentials"}
    return {"logged_in": None, "text": (out or err).strip()[:120]}


# ---------------------------------------------------------------------------
# worker: cursor (Cursor CLI — Hobby free)
# ---------------------------------------------------------------------------

def cursor_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return _generic_json_worker(
        ["agent", "-p", task, "--output-format", "json", "--force", "--trust"],
        task, workdir, timeout)


def cursor_extract(rc: int, out: str, err: str) -> Result:
    return _generic_extract(rc, out, err)


def cursor_quota() -> dict:
    return {"available": None, "pct": None, "text": "Cursor Hobby free (no CLI meter)", "logged_in": None}


def cursor_login() -> dict:
    rc, out, err = run_cmd(["agent", "status"], timeout=30)
    blob = strip_ansi(out + err)
    if "not logged in" in blob.lower():
        return {"logged_in": False, "text": "cursor: not logged in"}
    if rc == 0 and blob.strip():
        return {"logged_in": True, "text": blob.strip()[:120]}
    return {"logged_in": None, "text": blob.strip()[:120]}


# ---------------------------------------------------------------------------
# worker: vibe (Mistral Vibe — free plan / BYOK)
# ---------------------------------------------------------------------------

def vibe_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return _generic_json_worker(["vibe", "-p", task, "--output-format", "json"], task, workdir, timeout)


def vibe_extract(rc: int, out: str, err: str) -> Result:
    return _generic_extract(rc, out, err)


def vibe_quota() -> dict:
    return {"available": None, "pct": None, "text": "Vibe free plan (no CLI meter)", "logged_in": None}


def vibe_login() -> dict:
    cfg = Path.home() / ".vibe"
    return {"logged_in": True if cfg.exists() else False,
            "text": "vibe config present" if cfg.exists() else "vibe not set up"}


# ---------------------------------------------------------------------------
# worker: qwen (Qwen Code — BYOK / local; free OAuth tier ended)
# ---------------------------------------------------------------------------

def qwen_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return run_cmd(["qwen", "-p", task], cwd=workdir, timeout=timeout)


def qwen_extract(rc: int, out: str, err: str) -> Result:
    return _generic_extract(rc, out, err)


def qwen_quota() -> dict:
    return {"available": None, "pct": None, "text": "Qwen: BYOK / local (no CLI meter)", "logged_in": None}


def qwen_login() -> dict:
    cfg = Path.home() / ".qwen"
    return {"logged_in": True if cfg.exists() else False,
            "text": "qwen config present" if cfg.exists() else "qwen not logged in"}


# ---------------------------------------------------------------------------
# worker: goose (AAIF Goose — BYOK / ACP subscriptions)
# ---------------------------------------------------------------------------

def goose_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return run_cmd(["goose", "run", "-i", "-", "--no-session"], cwd=workdir, timeout=timeout,
                   input_text=task)


def goose_extract(rc: int, out: str, err: str) -> Result:
    return _generic_extract(rc, out, err)


def goose_quota() -> dict:
    return {"available": None, "pct": None, "text": "Goose: BYOK / ACP (no CLI meter)", "logged_in": None}


def goose_login() -> dict:
    cfg = Path.home() / ".config/goose/config.yaml"
    return {"logged_in": True if cfg.exists() else False,
            "text": "goose config present" if cfg.exists() else "goose not configured"}


# ---------------------------------------------------------------------------
# worker: aider (BYOK; git-first pair programming)
# ---------------------------------------------------------------------------

def aider_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return run_cmd(["aider", "--yes", "--message", task], cwd=workdir, timeout=timeout)


def aider_extract(rc: int, out: str, err: str) -> Result:
    return _generic_extract(rc, out, err)


def aider_quota() -> dict:
    return {"available": None, "pct": None, "text": "Aider: BYOK (no CLI meter)", "logged_in": None}


def aider_login() -> dict:
    conf = Path.home() / ".aider.conf.yml"
    key = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))
    return {"logged_in": True if (conf.exists() or key) else False,
            "text": "aider config/key present" if (conf.exists() or key) else "aider needs a model key"}


# ---------------------------------------------------------------------------
# worker: claude (Claude Code — pro subscription planner)
# ---------------------------------------------------------------------------

def claude_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return _generic_json_worker(
        ["claude", "-p", task, "--output-format", "json", "--permission-mode", "plan"],
        task, workdir, timeout)


def claude_extract(rc: int, out: str, err: str) -> Result:
    return _generic_extract(rc, out, err)


def claude_quota() -> dict:
    return {"available": None, "pct": None, "text": "Claude Pro/Max subscription (no CLI meter)", "logged_in": None}


def claude_login() -> dict:
    cfg = Path.home() / ".claude.json"
    if cfg.exists():
        return {"logged_in": True, "text": "claude auth present (~/.claude.json)"}
    return {"logged_in": False, "text": "claude not signed in"}


# ---------------------------------------------------------------------------
# worker: grok (Grok Build CLI — pro subscription planner)
# ---------------------------------------------------------------------------

def grok_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return _generic_json_worker(["grok", "-p", task, "--output-format", "json"], task, workdir, timeout)


def grok_extract(rc: int, out: str, err: str) -> Result:
    return _generic_extract(rc, out, err)


def grok_quota() -> dict:
    return {"available": None, "pct": None, "text": "Grok/xAI subscription (no CLI meter)", "logged_in": None}


def grok_login() -> dict:
    cfg = Path.home() / ".grok"
    if cfg.exists() or os.environ.get("XAI_API_KEY"):
        return {"logged_in": True, "text": "grok credentials present"}
    return {"logged_in": False, "text": "grok not signed in"}


# ---------------------------------------------------------------------------
# worker: kimi (Kimi Code CLI — pro membership planner)
# ---------------------------------------------------------------------------

def kimi_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    return _generic_json_worker(["kimi", "-p", task, "--output-format", "stream-json"], task, workdir, timeout)


def kimi_extract(rc: int, out: str, err: str) -> Result:
    return _generic_extract(rc, out, err)


def kimi_quota() -> dict:
    return {"available": None, "pct": None, "text": "Kimi membership quota (no CLI meter)", "logged_in": None}


def kimi_login() -> dict:
    cfg = Path.home() / ".kimi-code"
    if cfg.exists():
        return {"logged_in": True, "text": "kimi config present (~/.kimi-code)"}
    return {"logged_in": False, "text": "kimi not logged in"}


# ---------------------------------------------------------------------------
# worker: hermes (Hermes itself — the loop, runnable from anywhere)
# ---------------------------------------------------------------------------

def hermes_run(task: str, workdir: str, timeout: float) -> tuple[int, str, str]:
    env = os.environ.copy()
    depth = int(env.get("FREE_AGENT_DEPTH", "0"))
    if depth >= 2:
        return 2, "", "free-agent recursion depth limit reached"
    env["FREE_AGENT_DEPTH"] = str(depth + 1)
    task = "Complete directly. Do not launch free-agent or another Hermes process.\n" + task
    return run_cmd([HERMES_BIN, "-z", task], cwd=workdir, timeout=timeout, env=env)


def hermes_extract(rc: int, out: str, err: str) -> Result:
    text = extract_json_text(out) or out.strip()
    blob = out + "\n" + err
    ex, au, reason = classify(blob)
    ok = rc == 0 and bool(text.strip()) and not ex and not au
    return Result(ok=ok, text=text.strip(), exhausted=ex, auth_required=au, reason=reason)


def hermes_quota() -> dict:
    return {"available": None, "pct": None, "text": "Hermes (Nous/other provider)", "logged_in": None}


def hermes_login() -> dict:
    if os.path.exists(HERMES_BIN):
        return {"logged_in": True, "text": "hermes CLI present"}
    return {"logged_in": False, "text": "hermes CLI missing"}


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

WORKERS: dict[str, Worker] = {
    "opencode": Worker(
        name="opencode", description="OpenCode v2 (free Zen/commandcode models)",
        priority=10, headless=True,
        reauth="opencode auth login   # or set OPENROUTER_API_KEY",
        run=oc_run, extract=oc_extract, quota=oc_quota, login=oc_login,
    ),
    "cline": Worker(
        name="cline", description="Cline CLI (free models, cost 0)",
        priority=20, headless=True,
        reauth="cline auth -p <provider>",
        run=cline_run, extract=cline_extract, quota=cline_quota, login=cline_login,
    ),
    "kiro": Worker(
        name="kiro", description="Kiro CLI (AWS free tier, 50 credits/mo)",
        priority=30, headless=True,
        reauth="kiro-cli login",
        run=kiro_run, extract=kiro_extract, quota=kiro_quota, login=kiro_login,
    ),
    "copilot": Worker(
        name="copilot", description="GitHub Copilot CLI (Free plan, limited AI credits)",
        priority=35, headless=True,
        reauth="copilot login   # OAuth device flow (classic PAT not accepted)",
        run=copilot_run, extract=copilot_extract, quota=copilot_quota, login=copilot_login,
        installed=copilot_installed,
    ),
    "pi": Worker(
        name="pi", description=f"Pi agent ({PI_PROVIDER}/{PI_MODEL})",
        priority=40, headless=True,
        reauth=f"pi auth login {PI_PROVIDER}",
        run=pi_run, extract=pi_extract, quota=pi_quota, login=pi_login,
    ),
    "freebuff": Worker(
        name="freebuff", description="Freebuff (PTY/tmux; no headless mode)",
        priority=50, headless=False,
        reauth="freebuff login",
        run=fb_run, extract=fb_extract, quota=fb_quota, login=fb_login,
        installed=fb_installed,
    ),
    "freepi": Worker(
        name="freepi", description="free-pi-cli (GitHub sign-in, ad-supported; PTY/tmux, TUI-only)",
        priority=55, headless=False,
        reauth="free-pi-cli   # GitHub sign-in (browser or --device-login)",
        run=freepi_run, extract=freepi_extract, quota=freepi_quota, login=freepi_login,
        installed=freepi_installed,
    ),
    "agy": Worker(
        name="agy", description="Antigravity CLI (Google free weekly quota)",
        priority=60, headless=True,
        reauth="agy   # interactive Google sign-in",
        run=agy_run, extract=agy_extract, quota=agy_quota, login=agy_login,
        installed=agy_installed,
    ),
    "kilo": Worker(
        name="kilo", description="Kilo Code CLI (Auto Free models)",
        priority=25, headless=True,
        reauth="kilo auth login",
        run=kilo_run, extract=kilo_extract, quota=kilo_quota, login=kilo_login,
        installed=lambda: shutil.which("kilo") is not None,
    ),
    "codex": Worker(
        name="codex", description="[PLANNER] OpenAI Codex CLI (ChatGPT Pro/Free)",
        priority=14, headless=True,
        reauth="codex login",
        run=codex_run, extract=codex_extract, quota=codex_quota, login=codex_login,
        installed=lambda: shutil.which("codex") is not None,
        roles=["planner"],
    ),
    "cursor": Worker(
        name="cursor", description="Cursor CLI (Hobby free, limited agent requests)",
        priority=42, headless=True,
        reauth="agent login",
        run=cursor_run, extract=cursor_extract, quota=cursor_quota, login=cursor_login,
        installed=lambda: shutil.which("agent") is not None,
    ),
    "vibe": Worker(
        name="vibe", description="Mistral Vibe (free plan / BYOK)",
        priority=44, headless=True,
        reauth="vibe --setup   # Mistral sign-in or API key",
        run=vibe_run, extract=vibe_extract, quota=vibe_quota, login=vibe_login,
        installed=lambda: shutil.which("vibe") is not None,
    ),
    "crush": Worker(
        name="crush", description="Charm Crush (BYOK / Charm Hyper)",
        priority=45, headless=True,
        reauth="crush   # interactive provider setup",
        run=crush_run, extract=crush_extract, quota=crush_quota, login=crush_login,
        installed=lambda: shutil.which("crush") is not None,
    ),
    "qwen": Worker(
        name="qwen", description="Qwen Code (BYOK / local models)",
        priority=46, headless=True,
        reauth="qwen   # interactive login / provider setup",
        run=qwen_run, extract=qwen_extract, quota=qwen_quota, login=qwen_login,
        installed=lambda: shutil.which("qwen") is not None,
    ),
    "goose": Worker(
        name="goose", description="AAIF Goose (BYOK / ACP subscriptions)",
        priority=48, headless=True,
        reauth="goose configure",
        run=goose_run, extract=goose_extract, quota=goose_quota, login=goose_login,
        installed=lambda: shutil.which("goose") is not None,
    ),
    "aider": Worker(
        name="aider", description="Aider (BYOK; git-first pair programming)",
        priority=58, headless=True,
        reauth="aider --openai-api-key ...   # or set OPENAI_API_KEY",
        run=aider_run, extract=aider_extract, quota=aider_quota, login=aider_login,
        installed=lambda: shutil.which("aider") is not None,
    ),
}


# Subscription planners are deliberately separate from the executor pool.
for _name, _run, _extract, _login, _specialty, _reauth in (
    ("claude", claude_run, claude_extract, claude_login, "architecture", "claude auth login"),
    ("grok", grok_run, grok_extract, grok_login, "research", "grok"),
    ("kimi", kimi_run, kimi_extract, kimi_login, "synthesis", "kimi login"),
):
    WORKERS[_name] = Worker(_name, "Subscription planner", 15, True, _reauth,
                           _run, _extract, lambda: {"text": "Quota unknown"},
                           _login, roles=["planner"], specialty=_specialty)
WORKERS["codex"].specialty = "implementation-plan"
WORKERS["agy"].roles = ["planner"]
WORKERS["agy"].specialty = "ecosystem"
WORKERS["agy"].automatic = False  # deferred by user
WORKERS["hermes"] = Worker("hermes", "Hermes worker/orchestrator", 70, True,
    HERMES_BIN + " model", hermes_run, hermes_extract, hermes_quota, hermes_login,
    roles=["executor", "orchestrator"], specialty="coordination")

# Unmetered/BYOK adapters must be explicitly selected: never silently spend a
# configured paid provider just because the client itself is open source.
for _name in ("cursor", "vibe", "crush", "qwen", "goose", "aider", "freebuff", "freepi"):
    WORKERS[_name].automatic = False
WORKERS["amp"] = Worker("amp", "BYOK/subscription harness; no free inference promised", 80, True,
    "amp login", lambda task, wd, timeout: run_cmd(["amp", "--execute", task, "--stream-json", "--visibility", "private"], cwd=wd, timeout=timeout),
    _generic_extract, lambda: {"text": "Provider quota unknown"},
    lambda: {"logged_in": None, "text": "Use amp account list; no inference probe"}, automatic=False)
for _name in ("claude", "grok", "kimi", "vibe", "crush", "qwen", "goose", "aider", "freebuff", "freepi", "cline", "codex"):
    WORKERS[_name].login = lambda: {"logged_in": None, "text": "Unknown until an authenticated run; directory presence is not proof"}
for _name, _w in WORKERS.items():
    _binary = {"kiro": "kiro-cli", "cursor": "agent", "freepi": "free-pi-cli"}.get(_name, _name)
    if _name == "hermes":
        _w.installed = lambda: os.path.isfile(HERMES_BIN) and os.access(HERMES_BIN, os.X_OK)
    else:
        _w.installed = lambda b=_binary: shutil.which(b) is not None


def error_evidence(rc: int, out: str, err: str) -> str:
    """Never classify ordinary assistant replies or echoed prompts as failures."""
    errors = [err] if rc else [line for line in err.splitlines() if re.search(r"(?i)(error|failed|unauthorized|authentication required|quota exceeded)", line)]
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        data = d.get("data", {})
        msg = d.get("message", {})
        if (d.get("type") == "error" or d.get("error") or d.get("is_error")
                or d.get("status") in ("ERROR", "error", "failed")
                or (isinstance(data, dict) and data.get("status") in ("error", "failed"))
                or (isinstance(msg, dict) and msg.get("stopReason") == "error")
                or d.get("finishReason") in ("error", "failed")):
            errors.append(json.dumps(d))
    if rc and not errors:
        errors.append(out)
    if rc and not any(errors):
        errors.append(out)
    return "\n".join(errors)


def validated_extract(extract, rc, out, err):
    evidence = error_evidence(rc, out, err)
    ex, au, reason = classify(evidence)
    result = extract(rc, out, err)
    had_reply = result.ok or result.exhausted or result.auth_required
    if extract is fb_extract:
        had_reply = bool(re.search(r"•\s*\d+s\s*•", out)) and "Enter a coding task" in out
    if extract is freepi_extract:
        had_reply = False  # no stable machine-readable completion contract yet
    result.exhausted, result.auth_required = ex, au
    # Only actual error channels matter, not keyword matches in successful text.
    result.ok = rc == 0 and had_reply and bool(result.text.strip()) and not evidence
    result.reason = reason or ("worker error" if evidence else "empty response" if not result.text.strip() else "")
    return result


def ordered(names: list[str] | None = None, role: str | None = None) -> list[Worker]:
    ws = [w for w in WORKERS.values() if names is None or w.name in names]
    if names is None and role:
        ws = [w for w in ws if role in w.roles and w.automatic]
    return sorted(ws, key=lambda w: w.priority)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_workers(args) -> int:
    rows = [{"worker": w.name, "roles": w.roles, "specialty": w.specialty, "installed": w.installed(), "automatic": w.automatic, "headless": w.headless} for w in ordered()]
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print("{worker:10} roles={roles} specialty={specialty} installed={installed} auto={automatic}".format(**r))
    return 0


def cmd_status(args) -> int:
    state = load_state()
    rows = []
    for w in ordered():
        st = state.get("workers", {}).get(w.name, {})
        inst = w.installed()
        login = w.login() if inst else {"logged_in": None, "text": "not installed"}
        quota = w.quota() if inst else {"text": "not installed", "pct": None}
        exhausted = bool(st.get("exhausted"))
        rows.append({
            "worker": w.name, "installed": inst,
            "logged_in": login.get("logged_in"), "login_text": login.get("text", ""),
            "quota_text": quota.get("text", ""), "quota_pct": quota.get("pct"),
            "exhausted": exhausted, "reason": st.get("reason", ""),
            "reauth": w.reauth,
        })
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        flag = "EXHAUSTED" if r["exhausted"] else ("no" if r["logged_in"] is False else "yes" if r["logged_in"] is True else "unknown")
        print(f"{r['worker']:10} [{flag:9}] quota={r['quota_text'][:60]!r} login={r['login_text'][:40]!r}")
        if r["exhausted"]:
            print(f"           -> {r['reason']}  | reauth: {r['reauth']}")
    return 0


def nudge_worker(w: Worker, reason: str, state: dict, do_notify: bool = True, quota: bool = False) -> None:
    mark(state, w.name, reason)
    st = state["workers"][w.name]
    st["kind"] = "quota" if quota else "auth"
    if quota:
        st["retry_at"] = time.time() + 1800
    save_state(state)
    code = "QUOTA_EXHAUSTED" if quota else "REAUTH_REQUIRED"
    advice = "wait for provider reset; login does not refill credits" if quota else "run: " + w.reauth
    line = f"{code} {w.name} {reason} | {advice}"
    print(line, file=sys.stderr)
    if do_notify:
        notify(f"Free agent: {w.name}", line)


def cmd_nudge(args) -> int:
    state = load_state()
    rows = []
    for name, st in state.get("workers", {}).items():
        if name not in WORKERS or not st.get("exhausted"):
            continue
        if st.get("kind") == "quota" and st.get("retry_at", 0) <= time.time():
            continue
        w = WORKERS[name]
        quota = st.get("kind") == "quota"
        code = "QUOTA_EXHAUSTED" if quota else "REAUTH_REQUIRED"
        advice = "Wait for reset; login does not replenish quota" if quota else w.reauth
        row = {"worker": name, "status": code, "reason": st.get("reason", ""), "action": advice}
        rows.append(row)
        print(f"{code} {name} | {advice}", file=sys.stderr)
        if not args.no_notify:
            notify(f"Free agent: {name}", advice)
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("no recorded workers need attention")
    return 2 if rows else 0


def cmd_run_replay(args, workdir: str) -> int:
    """The runner role is deterministic: replay only, never an agent."""
    manifest = getattr(args, "workflow_manifest", None) or extensions.manifest_default()
    try:
        result = workflows.replay(args.task, workdir, manifest, timeout=args.timeout)
    except (ValueError, OSError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "role": "runner", "error": str(exc)}))
        else:
            print(f"runner replay error: {exc}", file=sys.stderr)
        return 1
    ok = result.get("status") == "replayed"
    if args.json:
        print(json.dumps({"ok": ok, "role": "runner",
                          "provider_calls": result.get("provider_calls"),
                          "replay": result}, indent=2))
    else:
        print(f"[runner] {result.get('status')}: {result.get('reason')}")
        if result.get("status") == "miss":
            print("runner role does not fall back to a planner or agent", file=sys.stderr)
    if ok:
        return 0
    return 1 if result.get("status") in ("miss", "disabled") else 2


def cmd_run(args) -> int:
    state = load_state()
    workdir = os.path.abspath(args.workdir or os.getcwd())
    if not os.path.isdir(workdir):
        if args.json:
            print(json.dumps({"ok": False, "error": "workdir not found", "attempts": []}))
        else:
            print(f"workdir not found: {workdir}", file=sys.stderr)
        return 1
    if getattr(args, "role", "") == "runner":
        return cmd_run_replay(args, workdir)
    names = [args.worker] if args.worker else None
    workers = ordered(names, getattr(args, "role", "executor"))
    if not workers:
        if args.json:
            print(json.dumps({"ok": False, "error": "no matching worker", "attempts": []}))
        elif args.worker:
            print(f"unknown worker: {args.worker}", file=sys.stderr)
        else:
            print(f"no worker for role: {getattr(args, 'role', '')}", file=sys.stderr)
        return 1

    attempts = []
    for w in workers:
        st = state.get("workers", {}).get(w.name, {})
        if st.get("exhausted") and not args.force and (st.get("kind") != "quota" or st.get("retry_at", 0) > time.time()):
            attempts.append({"worker": w.name, "skipped": f"exhausted: {st.get('reason')}"})
            continue
        if not w.installed():
            attempts.append({"worker": w.name, "skipped": "not installed"})
            continue
        login = w.login()
        if login.get("logged_in") is False:
            if "not found" in login.get("text", "").lower():
                attempts.append({"worker": w.name, "skipped": "binary not found on PATH"})
                continue
            reason = "not logged in: " + login.get("text", "")
            nudge_worker(w, reason, state, do_notify=not args.no_notify)
            attempts.append({"worker": w.name, "reauth": reason})
            continue
        task = args.task
        prompt = extensions.role_prompt_for(w, getattr(args, "role", ""), args.task)
        if prompt is not None:
            task = prompt
        elif "planner" in w.roles:
            task = "PLAN ONLY. Do not modify files or execute state-changing commands. Specialty: " + w.specialty + ".\n" + task
        rc, out, err = w.run(task, workdir, args.timeout)
        res = validated_extract(w.extract, rc, out, err)
        res.model = res.model or w.model or w.name
        if res.ok:
            clear(state, w.name)
            attempts.append({"worker": w.name, "ok": True, "model": res.model, "cost": res.cost})
            if args.json:
                print(json.dumps({"ok": True, "worker": w.name, "model": res.model,
                                  "cost": res.cost, "text": res.text, "attempts": attempts}, indent=2))
            else:
                print(f"[{w.name}] {res.model} cost={res.cost}")
                print(res.text)
            return 0
        if res.exhausted or res.auth_required:
            nudge_worker(w, res.reason, state, do_notify=not args.no_notify, quota=res.exhausted)
            attempts.append({"worker": w.name, "reauth": res.reason})
            continue
        attempts.append({"worker": w.name, "error": res.reason or f"rc={rc}"})
        continue

    if args.json:
        print(json.dumps({"ok": False, "text": "", "attempts": attempts}, indent=2))
    else:
        print("ALL_WORKERS_FAILED", file=sys.stderr)
        for a in attempts:
            print(f"  {a}", file=sys.stderr)
    return 2


def cmd_reset(args) -> int:
    state = load_state()
    clear(state, args.worker)
    print(f"reset {args.worker}")
    return 0


def cmd_loop(args) -> int:
    import contextlib
    import io
    workdir = os.path.abspath(args.workdir or os.getcwd())
    history = []
    task = args.task
    manifest = getattr(args, "workflow_manifest", None) or extensions.manifest_default()
    if manifest:
        # Replay before any planner; only an exact miss may fall through.
        try:
            replay = workflows.replay(args.task, workdir, manifest, timeout=args.timeout)
        except (ValueError, OSError) as exc:
            print(json.dumps({"ok": False, "stage": "replay", "error": str(exc)}))
            return 1
        status = replay.get("status")
        if status == "replayed":
            print(json.dumps({"ok": True, "stage": "replay",
                              "provider_calls": replay.get("provider_calls"),
                              "replay": replay}, indent=2))
            return 0
        if status != "miss":
            print(json.dumps({"ok": False, "stage": "replay", "replay": replay}, indent=2))
            return 1 if status == "disabled" else 2
    def dispatch(prompt, worker=None, role="executor"):
        opts = argparse.Namespace(task=prompt, workdir=workdir, worker=worker,
                                  role=role, timeout=args.timeout, force=args.force,
                                  no_notify=args.no_notify, json=True,
                                  workflow_manifest=None)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = cmd_run(opts)
        return rc, json.loads(buffer.getvalue())
    if args.planner:
        rc, plan = dispatch(task, args.planner, "planner")
        if rc:
            print(json.dumps({"ok": False, "stage": "planner", "result": plan}))
            return rc
        task += "\nImplementation plan:\n" + plan["text"]
    for i in range(args.max_iterations):
        rc, result = dispatch(task, args.worker)
        history.append({"iteration": i + 1, "result": result})
        if rc:
            break
        check_rc, out, err = run_cmd(["/bin/sh", "-c", args.check], cwd=workdir, timeout=args.timeout)
        history[-1]["check_exit"] = check_rc
        history[-1]["check_output"] = (out + err)[-8000:]
        if check_rc == 0:
            if getattr(args, "learn", False) and getattr(args, "artifact", None):
                # Review-only proposal: never approved, recorded or replayed here.
                try:
                    history[-1]["candidate"] = workflows.propose(
                        args.task, workdir, args.check, args.artifact,
                        metadata={"worker": result.get("worker", ""),
                                  "check_exit": check_rc,
                                  "iterations": i + 1})
                except (ValueError, OSError) as exc:
                    history[-1]["candidate_error"] = str(exc)
            print(json.dumps({"ok": True, "iterations": i + 1, "history": history}, indent=2))
            return 0
        task = args.task + "\nPrevious verification failed. Fix it:\n" + (out + err)[-8000:]
    print(json.dumps({"ok": False, "iterations": len(history), "history": history}, indent=2))
    return 2


def cmd_watch(args) -> int:
    print(f"watching every {args.interval}s (Ctrl+C to stop)")
    prev = {}
    while True:
        state = load_state()
        changed = []
        for w in ordered():
            if not w.installed():
                continue
            login = w.login()
            quota = w.quota()
            key = (login.get("logged_in"), quota.get("pct"), quota.get("text"))
            if w.name in prev and prev[w.name] != key:
                changed.append((w, login, quota))
            prev[w.name] = key
            if login.get("logged_in") is False:
                nudge_worker(w, "not logged in: " + login.get("text", ""), state,
                             do_notify=not args.no_notify)
        for w, login, quota in changed:
            msg = f"quota/login changed: {quota.get('text','')[:120]}"
            print(f"[watch] {w.name}: {msg}")
            if not args.no_notify:
                notify(f"Free agent: {w.name}", msg, urgency="normal")
        time.sleep(args.interval)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_flag = "--json" in argv
    skip_user_config = any(flag in argv for flag in ("-h", "--help")) or (
        bool(argv) and argv[0] in ("agent", "roles")
    )
    try:
        # Before parser choices: registers pi-* and any user-config agents.
        extensions.initialize(sys.modules[__name__], skip_user_config=skip_user_config)
    except (ValueError, OSError) as exc:
        # A broken user config must not take management commands down with it;
        # dispatch stays fail-closed because an unvalidated config may not run.
        if not argv or argv[0] in ("run", "loop"):
            if json_flag:
                print(json.dumps({"ok": False, "error": str(exc)}))
            else:
                print(str(exc), file=sys.stderr)
            return 1
        extensions.initialize(sys.modules[__name__], skip_user_config=True)

    p = argparse.ArgumentParser(prog="free-agent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="live quota/login state")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("run", help="run a task on the best available worker")
    r.add_argument("task")
    r.add_argument("--workdir", "-w", default=None)
    r.add_argument("--worker", default=None)
    r.add_argument("--role", default="executor")
    r.add_argument("--workflow-manifest",
                   default=os.environ.get("FREE_AGENT_WORKFLOW_MANIFEST"))
    r.add_argument("--timeout", type=float, default=600)
    r.add_argument("--force", action="store_true", help="retry exhausted workers")
    r.add_argument("--no-notify", action="store_true")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_run)

    n = sub.add_parser("nudge", help="re-prompt for exhausted workers")
    n.add_argument("--no-notify", action="store_true")
    n.add_argument("--json", action="store_true")
    n.set_defaults(func=cmd_nudge)

    w = sub.add_parser("watch", help="poll quotas and nudge on change")
    w.add_argument("--interval", type=int, default=1800)
    w.add_argument("--no-notify", action="store_true")
    w.set_defaults(func=cmd_watch)

    reg = sub.add_parser("workers", help="list the registry")
    reg.add_argument("--json", action="store_true")
    reg.set_defaults(func=cmd_workers)
    reset = sub.add_parser("reset", help="clear cooldown/auth state after recovery")
    reset.add_argument("worker", choices=list(WORKERS))
    reset.set_defaults(func=cmd_reset)
    loop = sub.add_parser("loop", help="bounded execute/verify loop")
    loop.add_argument("task")
    loop.add_argument("--workdir", "-w", default=None)
    loop.add_argument("--worker", default=None)
    loop.add_argument("--planner", default=None, choices=[n for n,w in WORKERS.items() if "planner" in w.roles])
    loop.add_argument("--check", required=True)
    loop.add_argument("--workflow-manifest",
                      default=os.environ.get("FREE_AGENT_WORKFLOW_MANIFEST"))
    loop.add_argument("--learn", action="store_true",
                      help="propose a review-only candidate after a verified success")
    loop.add_argument("--artifact", action="append", default=[],
                      help="input file to fingerprint (repeatable, with --learn)")
    loop.add_argument("--max-iterations", type=int, choices=range(1,11), default=2)
    loop.add_argument("--timeout", type=float, default=600)
    loop.add_argument("--force", action="store_true")
    loop.add_argument("--no-notify", action="store_true")
    loop.add_argument("--json", action="store_true")
    loop.set_defaults(func=cmd_loop)

    # user agent config
    agent = sub.add_parser("agent", help="list/add/remove/validate user agents")
    asub = agent.add_subparsers(dest="agent_cmd", required=True)
    al = asub.add_parser("list", help="list configured agents (no secrets)")
    al.add_argument("--config", default=None)
    al.add_argument("--json", action="store_true")
    al.set_defaults(func=extensions.cmd_agent)
    aa = asub.add_parser("add", help="add an argv agent to the user config")
    aa.add_argument("name")
    aa.add_argument("--command-json", required=True,
                    help="JSON argv array, e.g. '[\"pi\",\"--print\",\"{task}\"]'")
    aa.add_argument("--role", default=None)
    aa.add_argument("--specialty", default="")
    aa.add_argument("--automatic", action="store_true")
    aa.add_argument("--config", default=None)
    aa.add_argument("--json", action="store_true")
    aa.set_defaults(func=extensions.cmd_agent)
    ar = asub.add_parser("remove", help="remove an agent from the user config")
    ar.add_argument("name")
    ar.add_argument("--config", default=None)
    ar.add_argument("--json", action="store_true")
    ar.set_defaults(func=extensions.cmd_agent)
    av = asub.add_parser("validate", help="validate the user config")
    av.add_argument("--config", default=None)
    av.add_argument("--json", action="store_true")
    av.set_defaults(func=extensions.cmd_agent)

    # roles are instructions, not enforcement
    roles = sub.add_parser("roles", help="list/set role prompts in the user config")
    rsub = roles.add_subparsers(dest="roles_cmd", required=True)
    rl = rsub.add_parser("list", help="list role prompts")
    rl.add_argument("--config", default=None)
    rl.add_argument("--json", action="store_true")
    rl.set_defaults(func=extensions.cmd_roles)
    rs = rsub.add_parser("set", help="set a role prompt")
    rs.add_argument("name")
    role_text = rs.add_mutually_exclusive_group(required=True)
    role_text.add_argument("--prompt")
    role_text.add_argument("--prompt-file", help="plain Markdown durable role instructions")
    rs.add_argument("--description", default=None)
    rs.add_argument("--tools", default=None, help="comma-separated tool names (metadata)")
    rs.add_argument("--config", default=None)
    rs.add_argument("--json", action="store_true")
    rs.set_defaults(func=extensions.cmd_roles)

    # provider registry and isolated Pi runs
    prov = sub.add_parser("providers", help="list providers, model catalogs, run Pi")
    prov.add_argument("--env-file", default=None,
                      help="env file with credentials (default: FREE_AGENT_ENV_FILE or ~/.env)")
    psub = prov.add_subparsers(dest="provider_cmd", required=True)
    plist = psub.add_parser("list", help="credential presence and free policy (no values)")
    plist.add_argument("--json", action="store_true")
    plist.set_defaults(func=extensions.cmd_providers)
    pmodels = psub.add_parser("models", help="sanitized model catalog")
    pmodels.add_argument("name")
    pmodels.add_argument("--json", action="store_true")
    pmodels.set_defaults(func=extensions.cmd_providers)
    prun = psub.add_parser("run", help="run one isolated Pi coding process")
    prun.add_argument("name")
    prun.add_argument("task")
    prun.add_argument("--model", default=None)
    prun.add_argument("--workdir", "-w", default=None)
    prun.add_argument("--timeout", type=float, default=600)
    prun.add_argument("--read-only", action="store_true")
    prun.add_argument("--json", action="store_true")
    prun.set_defaults(func=extensions.cmd_providers)

    # deterministic workflows
    wfs = sub.add_parser("workflows", help="list stores, replay, propose a candidate")
    wsub = wfs.add_subparsers(dest="workflows_cmd", required=True)
    wl = wsub.add_parser("list", help="inspect Replant stores without writing")
    wl.add_argument("--store", default=".replant")
    wl.add_argument("--all-stores", action="store_true",
                    help="also inspect known ~/.hermes and ~/dev/hermes-physis stores")
    wl.add_argument("--workdir", "-w", default=None)
    wl.add_argument("--json", action="store_true")
    wl.set_defaults(func=extensions.cmd_workflows)
    wrun = wsub.add_parser("run", help="replay an exact learned workflow only")
    wrun.add_argument("task")
    wrun.add_argument("--manifest",
                      default=os.environ.get("FREE_AGENT_WORKFLOW_MANIFEST"))
    wrun.add_argument("--workdir", "-w", default=None)
    wrun.add_argument("--timeout", type=float, default=120)
    wrun.add_argument("--json", action="store_true")
    wrun.set_defaults(func=extensions.cmd_workflows)
    wprop = wsub.add_parser("propose", help="write a review-only candidate")
    wprop.add_argument("task")
    wprop.add_argument("--check", required=True)
    wprop.add_argument("--artifact", action="append", default=[])
    wprop.add_argument("--workdir", "-w", default=None)
    wprop.add_argument("--json", action="store_true")
    wprop.set_defaults(func=extensions.cmd_workflows)

    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else int(code)
    try:
        if int(os.environ.get("FREE_AGENT_DEPTH", "0")) >= 2 and args.cmd in ("run", "loop"):
            if getattr(args, "json", False):
                print(json.dumps({"ok": False, "error": "recursion depth limit", "attempts": []}))
            else:
                print("free-agent recursion depth limit reached", file=sys.stderr)
            return 2
        return args.func(args)
    except (ValueError, OSError) as e:
        if json_flag:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(str(e), file=sys.stderr)
        return 1
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else int(code)


if __name__ == "__main__":
    sys.exit(main())
