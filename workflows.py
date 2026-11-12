"""Deterministic replay workflows for free-agent, backed by native Replant.

This module never calls a model provider. It only inspects private Replant
state, validates operator-owned manifests, runs the native `replant` binary,
and writes review-only candidate proposals.

Statuses returned by :func:`replay`:

    replayed     exact verified recipe ran, zero provider calls
    miss         exact request has no learned recipe; planner fallback allowed
    blocked      malformed input, changed artifact, review, timeout or failure
    unavailable  native binary not found
    disabled     no manifest configured
    error        native output could not be understood

Manifest, registry and artifact files are operator-owned. Same-user mutation is
not a security boundary; this module does not create registries, approve
candidates or record recipes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile

MAX_BYTES = 1 << 20
MAX_FILES = 1000
STDERR_PREVIEW = 400
KNOWN_BINARIES = (
    "~/.hermes/plugins/physis/bin/replant",
    "~/dev/hermes-physis/bin/replant",
)
TRUST_NOTE = (
    "manifest, registry and artifact files are operator-owned; same-user "
    "mutation is not a security boundary"
)

_SECRET = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|authorization)\b"
    r"\s*[:=]\s*\S+"
)
_SECRET_NAME = re.compile(r"(?i)(key|token|secret|password|credential)")


def _executable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find_binary():
    """Return the native replant executable, or None."""
    override = os.environ.get("FREE_AGENT_REPLANT_BIN")
    if _executable(override):
        return os.path.abspath(override)
    found = shutil.which("replant")
    if found:
        return found
    for candidate in KNOWN_BINARIES:
        path = os.path.expanduser(candidate)
        if _executable(path):
            return path
    return None


def _resolve(path, base):
    path = os.fspath(path)
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.abspath(path)


def _stat_regular(path):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"symlink rejected: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"not a regular file: {path}")
    if info.st_size > MAX_BYTES:
        raise ValueError(f"file exceeds 1 MiB: {path}")
    return info


def _read_bytes(path):
    _stat_regular(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        return handle.read(MAX_BYTES + 1)


def _load_json(path, label, operator=False):
    info = _stat_regular(path)
    if operator and info.st_mode & stat.S_IWOTH:
        raise ValueError(f"{label} is world-writable: {path}")
    try:
        return json.loads(_read_bytes(path))
    except ValueError as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from exc


def _sha256(path):
    _stat_regular(path)
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _redact(text):
    if not text:
        return ""
    text = _SECRET.sub(r"\1=<redacted>", text)
    for name, value in os.environ.items():
        if len(value) >= 8 and _SECRET_NAME.search(name):
            text = text.replace(value, "<redacted>")
    return text


def _exec(argv, payload=None, cwd=None, timeout=30):
    """Run a command in its own process group; kill the group on timeout."""
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    stdin = json.dumps(payload) if payload is not None else None
    timed_out = False
    try:
        out, err = process.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            out, err = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
    return process.returncode, (out or "")[:MAX_BYTES], err or "", timed_out


def _cli_json(binary, args):
    try:
        rc, out, err, timed_out = _exec([binary, *args], timeout=30)
    except OSError as exc:
        return {"error": str(exc)}
    if timed_out:
        return {"error": "replant timed out"}
    try:
        return json.loads(out)
    except ValueError:
        reason = _redact(err or out)[:STDERR_PREVIEW] or f"exit {rc}"
        return {"error": reason}


def _files(directory, report):
    try:
        entries = sorted(os.scandir(directory), key=lambda item: item.name)
    except FileNotFoundError:
        return []
    paths = []
    for entry in entries:
        if len(paths) >= MAX_FILES:
            report["truncated"] = True
            break
        if entry.is_symlink():
            report["invalid"].append(
                {"file": entry.name, "reason": "symlink rejected"}
            )
            continue
        if not entry.is_file(follow_symlinks=False):
            continue
        paths.append(entry.path)
    return paths


def _tool_names(recipe, key):
    if not isinstance(recipe, dict):
        return None
    calls = recipe.get(key)
    if not isinstance(calls, list):
        return None
    names = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
            return None
        names.append(call["tool"])
    return names


def inspect_store(path):
    """Read a Replant store without writing: stats, history and learned records."""
    root = os.path.abspath(os.fspath(path))
    report = {
        "store": root,
        "stats": None,
        "history": None,
        "learned": [],
        "candidates": [],
        "stale": [],
        "unindexed": [],
        "invalid": [],
        "truncated": False,
    }
    if not os.path.isdir(root):
        report["stats"] = {"error": "store directory not found"}
        report["history"] = {"error": "store directory not found"}
        return report

    binary = find_binary()
    if binary is None:
        report["stats"] = {"error": "replant binary not found"}
        report["history"] = {"error": "replant binary not found"}
    else:
        report["stats"] = _cli_json(binary, ["stats", "--store", root])
        report["history"] = _cli_json(binary, ["history", "--store", root])

    indexed = {}
    for entry in _files(os.path.join(root, "entries"), report):
        try:
            value = _load_json(entry, "entry")
        except ValueError as exc:
            report["invalid"].append(
                {"file": os.path.basename(entry), "reason": str(exc)}
            )
            continue
        if not isinstance(value, str) or not value:
            report["invalid"].append(
                {"file": os.path.basename(entry), "reason": "entry is not a record id"}
            )
            continue
        indexed[value] = os.path.basename(entry)

    seen = set()
    for record in _files(os.path.join(root, "records"), report):
        name = os.path.basename(record)
        record_id = name[:-5] if name.endswith(".json") else name
        try:
            data = _load_json(record, "record")
        except ValueError as exc:
            report["invalid"].append({"file": name, "reason": str(exc)})
            continue
        request = data.get("request") if isinstance(data, dict) else None
        recipe = data.get("recipe") if isinstance(data, dict) else None
        prompt = request.get("prompt") if isinstance(request, dict) else None
        steps = _tool_names(recipe, "steps")
        checks = _tool_names(recipe, "checks")
        if (
            not isinstance(data, dict)
            or data.get("id") != record_id
            or not isinstance(prompt, str)
            or not prompt.strip()
            or steps is None
            or checks is None
        ):
            report["invalid"].append(
                {"file": name, "reason": "record does not match the learned schema"}
            )
            continue
        seen.add(record_id)
        if record_id in indexed:
            report["learned"].append(
                {
                    "id": record_id,
                    "prompt": prompt,
                    "steps": steps,
                    "checks": checks,
                }
            )
        else:
            report["unindexed"].append(record_id)

    report["stale"] = sorted(set(indexed) - seen)
    report["unindexed"].sort()
    report["learned"].sort(key=lambda item: item["id"])

    for candidate in _files(os.path.join(root, "candidates"), report):
        name = os.path.basename(candidate)
        try:
            data = _load_json(candidate, "candidate")
        except ValueError as exc:
            report["candidates"].append(
                {
                    "file": name,
                    "error": str(exc),
                    "note": "unknown schema; reported only, never executed",
                }
            )
            continue
        if isinstance(data, dict):
            kind, keys = "object", sorted(data)
        elif isinstance(data, list):
            kind, keys = "array", []
        else:
            kind, keys = type(data).__name__, []
        report["candidates"].append(
            {
                "file": name,
                "kind": kind,
                "keys": keys,
                "note": "unknown schema; reported only, never executed",
            }
        )
    return report


def _manifest(manifest):
    """Validate manifest shape; return (tools, store, workflows)."""
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("manifest version must be 1")
    tools = manifest.get("tools")
    store = manifest.get("store")
    workflows = manifest.get("workflows")
    for name, value in (("tools", tools), ("store", store)):
        if not isinstance(value, str) or not os.path.isabs(value):
            raise ValueError(f"manifest {name} must be an absolute path")
    if not isinstance(workflows, list):
        raise ValueError("manifest workflows must be a list")
    for workflow in workflows:
        if not isinstance(workflow, dict):
            raise ValueError("each workflow must be an object")
        if not isinstance(workflow.get("prompt"), str) or not workflow["prompt"].strip():
            raise ValueError("workflow prompt must be a non-empty string")
        if not isinstance(workflow.get("context", {}), dict):
            raise ValueError("workflow context must be an object")
        if not isinstance(workflow.get("input", {}), dict):
            raise ValueError("workflow input must be an object")
        artifacts = workflow.get("artifacts")
        if not isinstance(artifacts, dict) or not artifacts:
            raise ValueError("workflow artifacts must be a non-empty mapping")
    return tools, store, workflows


def _check_artifacts(workflow):
    """Validate every declared artifact hash before any native action."""
    current = {}
    for path, expected in workflow["artifacts"].items():
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError("artifact paths must be absolute")
        if not isinstance(expected, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", expected
        ):
            raise ValueError(f"artifact fingerprint missing or invalid: {path}")
        actual = _sha256(path)
        if not hmac.compare_digest(actual, expected.lower()):
            raise ValueError(f"artifact changed: {path}")
        current[path] = actual
    return current


def _result(status, prompt, reason, **extra):
    result = {
        "status": status,
        "prompt": prompt,
        "workflow": None,
        "provider_calls": None,
        "planner_fallback": status == "miss",
        "reason": reason,
        "receipt": None,
        "binary": None,
        "timed_out": False,
        "trust_note": TRUST_NOTE,
    }
    result.update(extra)
    return result


def _native_result(rc, out, err, timed_out):
    if timed_out:
        return "blocked", "replant timed out; process group killed", None
    try:
        receipt = json.loads(out)
    except ValueError:
        preview = _redact(err or out)[:STDERR_PREVIEW] or f"exit {rc}"
        return "error", f"malformed replant output: {preview}", None
    if not isinstance(receipt, dict):
        return "error", "malformed replant output: expected a JSON object", None
    status = receipt.get("status")
    operation = receipt.get("operation")
    if rc == 0 and status == "succeeded" and operation == "replay":
        if type(receipt.get("provider_calls")) is not int or receipt["provider_calls"] != 0:
            return "blocked", "zero-provider replay receipt required", receipt
        return "replayed", receipt.get("reason") or "exact verified replay", receipt
    if rc == 0 and status == "miss":
        return "miss", receipt.get("reason") or "no verified recipe", receipt
    if status in ("review", "blocked", "failed") or rc != 0:
        reason = receipt.get("reason") or f"replant status {status!r}, exit {rc}"
        return "blocked", reason, receipt
    return "error", f"unexpected replant status: {status!r}", receipt


def replay(prompt, workdir, manifest_path, timeout=120):
    """Replay an exact learned workflow with zero provider calls.

    Only status ``miss`` permits a planner fallback. Any other non-replayed
    status must be resolved by an operator or by fixing the manifest.
    """
    workdir = os.path.abspath(os.fspath(workdir))
    if not manifest_path:
        return _result("disabled", prompt, "no workflow manifest configured")
    if not os.path.isdir(workdir):
        return _result("blocked", prompt, f"workdir not found: {workdir}")
    if not isinstance(prompt, str) or not prompt.strip():
        return _result("blocked", prompt, "prompt must be a non-empty string")

    manifest_file = _resolve(manifest_path, workdir)
    if not os.path.lexists(manifest_file):
        return _result("disabled", prompt, f"manifest not found: {manifest_file}")
    try:
        manifest = _load_json(manifest_file, "manifest", operator=True)
        tools, store, workflows = _manifest(manifest)
    except (OSError, ValueError) as exc:
        return _result("blocked", prompt, str(exc))

    matches = [item for item in workflows if item["prompt"] == prompt]
    if len(matches) > 1:
        return _result("blocked", prompt, "ambiguous prompt in workflow manifest")
    workflow = matches[0] if matches else None
    if workflow is None:
        return _result(
            "miss", prompt, "no workflow matches this exact prompt, input and context"
        )

    binary = find_binary()
    if binary is None:
        return _result(
            "unavailable", prompt, "replant binary not found", workflow=prompt
        )

    try:
        artifacts = _check_artifacts(workflow)
        _load_json(tools, "tool registry", operator=True)
    except (OSError, ValueError) as exc:
        return _result("blocked", prompt, str(exc), workflow=prompt, binary=binary)

    payload = {"prompt": prompt, "input": workflow.get("input", {}), "context": workflow.get("context", {})}
    try:
        rc, out, err, timed_out = _exec(
            [binary, "run", "--tools", tools, "--store", store],
            payload,
            cwd=workdir,
            timeout=timeout,
        )
    except OSError as exc:
        return _result(
            "unavailable", prompt, str(exc), workflow=prompt, binary=binary
        )

    status, reason, receipt = _native_result(rc, out, err, timed_out)
    extra = {
        "workflow": prompt,
        "binary": binary,
        "artifacts": artifacts,
        "timed_out": timed_out,
        "native_status": receipt.get("status") if receipt else None,
        "operation": receipt.get("operation") if receipt else None,
        "provider_calls": receipt.get("provider_calls") if receipt else None,
        "receipt": receipt,
    }
    if receipt is None:
        extra["stderr"] = _redact(err)[:STDERR_PREVIEW]
    return _result(status, prompt, reason, **extra)


def _write_json(path, value):
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def propose(prompt, workdir, check, artifact_paths, metadata=None):
    """Persist a review-only candidate. Never runs, approves or replays it."""
    workdir = os.path.abspath(os.fspath(workdir))
    if not os.path.isdir(workdir):
        raise ValueError(f"workdir not found: {workdir}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if not isinstance(check, str) or not check.strip():
        raise ValueError("check must be a non-empty command string")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")

    artifacts = {}
    for item in artifact_paths or ():
        path = _resolve(item, workdir)
        artifacts[path] = _sha256(path)

    document = {
        "schema": 1,
        "kind": "replant-candidate",
        "status": "needs_operator_review",
        "approved": False,
        "replayable": False,
        "request": {"prompt": prompt, "input": {}, "context": {}},
        "workdir": workdir,
        "artifacts": artifacts,
        "check": {"command": check, "trusted": False, "replayable": False},
        "metadata": metadata or {},
        "note": (
            "Candidate only. A human must review it, pin the tool registry and "
            "input hashes, and record it with native Replant. The check command "
            "is untrusted and is never executed by this proposal."
        ),
    }

    directory = os.path.join(workdir, ".free-agent", "candidates")
    for parent in (os.path.join(workdir, ".free-agent"), directory):
        if os.path.islink(parent):
            raise ValueError(f"symlink rejected: {parent}")
    os.makedirs(directory, mode=0o700, exist_ok=True)

    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    name = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32] + ".json"
    path = os.path.join(directory, name)
    _write_json(path, document)
    return path
