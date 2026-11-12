# Deterministic workflows

`workflows.py` is the free-agent side of Replant:
the native `replant` binary records a verified recipe once, then replays the
exact request with **zero model/provider calls**. free-agent only inspects
private state, validates an operator-owned manifest, invokes the native binary,
and writes review-only candidates. It never creates a tool registry, approves a
candidate, or records a recipe automatically.

## Progressive determinization

1. **Plan one-off.** Use the existing planner/executor path for a task that has
   no learned recipe. The result is a normal model run, not a deterministic
   capability.
2. **Verify.** Run an independent check (tests, lint, a hash-producing script).
   A model claiming success is not verification.
3. **Propose.** Call `workflows.propose(...)` with the prompt, workdir, the
   independent check command, and the input files the recipe depends on.
   This writes `.free-agent/candidates/<hash>.json` with
   `status: "needs_operator_review"`. Nothing runs and nothing is approved.
4. **Operator review.** A human reviews the candidate, pins the operator-owned
   tool registry (programs plus `fingerprint_files`), the Replant store, the
   input SHA-256 hashes, and the independent checks, then records it with the
   native `replant record` command. free-agent never performs this step.
5. **Replay first.** Call `workflows.replay(...)` before dispatching a planner.
   `replayed` means the exact verified recipe ran with `provider_calls: 0`.
   Only `miss` permits a planner fallback.

There is no separate "learned total" invented by free-agent. Counts come from
the native journal (`replant stats` / `replant history`). As observed on this
host:

| store | learned prompt | verified records | verified replays | provider calls |
| --- | --- | --- | --- | --- |
| `~/dev/hermes-physis/.replant` (historical) | `physis replant demo` | 1 | 2 | 0 |
| `~/dev/free-agent/.replant` (new, empty) | none | 0 | 0 | 0 |

Those are measurements from the existing journal, not projections. A fresh
free-agent store stays at zero until an operator records a recipe.

## Manifest

The manifest is an operator-owned file outside the project whenever possible.
`replay()` resolves a relative manifest path against the workdir, but absolute
paths are preferred. See [../workflows.example.json](../workflows.example.json).

```json
{
  "version": 1,
  "tools": "/opt/free-agent/tools.json",
  "store": "/var/lib/free-agent/.replant",
  "workflows": [
    {
      "prompt": "run the repository test suite and report the result",
      "context": {},
      "input": {},
      "artifacts": {
        "/opt/free-agent/inputs/pyproject.toml": "<64 hex sha256>"
      }
    }
  ]
}
```

Rules enforced before any process starts:

* `version` is exactly `1`; `tools` and `store` are absolute paths.
* Every workflow has a non-empty prompt, object `context`/`input`, and a
  non-empty `artifacts` map.
* Every artifact path is absolute, has a 64-hex SHA-256, is a regular file, is
  not a symlink, is at most 1 MiB, and still hashes to the pinned value.
* The manifest and tool registry are regular files, not symlinks, not
  world-writable, at most 1 MiB, and valid JSON.
* The registry is passed to native Replant unchanged. Native Replant still
  fingerprints the tool programs and `fingerprint_files`; free-agent does not
  attempt to replace that policy hash.
* Relative paths are resolved against the workdir with `os.path.abspath` so a
  symlink is visible and rejected rather than silently followed.

Same-user mutation of these files is **not** a security boundary. They protect
against accidental drift by a cooperative process, not against a malicious
same-user process. Do not store credentials in manifests, registries, artifacts
or candidate files.

## `replay(prompt, workdir, manifest_path, timeout=120)`

Selects the unique manifest workflow whose `prompt` matches exactly; more than one match is `blocked` as ambiguous. On a match it sends that workflow's manifest `input` and `context` on stdin:

```json
{"prompt": "<prompt>", "input": { ... }, "context": { ... }}
```

to:

```sh
replant run --tools <registry> --store <store>
```

and maps the native receipt as follows:

| native result | returned status | planner fallback |
| --- | --- | --- |
| exit 0, `status: "succeeded"`, `operation: "replay"` | `replayed` | no |
| exit 0, `status: "miss"` | `miss` | **yes** |
| exit 0, `status: "review"` | `blocked` | no |
| `status: "blocked"` or `status: "failed"` | `blocked` | no |
| non-zero exit, timeout, malformed output | `blocked` / `error` | no |
| unknown or unexpected status | `error` | no |
| no manifest configured | `disabled` | no |
| binary not found | `unavailable` | no |

Only the exact `succeeded`/`replay` pair counts as `replayed`. An unknown
status is never treated as a replay. The native receipt is returned under
`receipt`; its `provider_calls` value is copied to the top level.

Fail-closed behaviour:

* changed or missing artifact → `blocked` before the process is launched;
* ambiguous prompt match in the manifest → `blocked` before the process is launched;
* review candidate → `blocked`, never auto-approved or replayed;
* timeout → the whole process group is killed (no orphan tool programs);
* stderr is truncated and credential-shaped values are redacted; full stderr
  and environment values are never returned.

## `inspect_store(path)`

Read-only. Returns:

```python
{
  "store": "/abs/store",
  "stats": {...},              # native `replant stats --store PATH`
  "history": [...],            # native `replant history --store PATH`
  "learned": [{"id", "prompt", "steps": [tool], "checks": [tool]}],
  "candidates": [{"file", "kind", "keys", "note"}],
  "stale": [record_id],        # entry points at a missing/invalid record
  "unindexed": [record_id],    # valid record file with no active entry
  "invalid": [{"file", "reason"}],
  "truncated": False,
}
```

Only records that are both structurally valid **and** referenced by an active
entry in `entries/*.json` appear in `learned`. Raw records are read from
`records/*.json` with schema `{id, request:{prompt,input,context},
recipe:{steps:[{tool,input}], checks:[{tool,input}]}}`. Symlinks, files over
1 MiB, and directories with more than 1000 entries are rejected or marked
truncated. Plugin candidates under `candidates/*.json` have an unknown schema:
their file name, JSON kind and top-level keys are reported and they are never
executed or guessed at. A missing store is reported without creating it.

## `propose(prompt, workdir, check, artifact_paths, metadata=None)`

Writes a candidate with the request, current artifact hashes, the independent
check command, and optional metadata (for example `{"worker": "cline"}`). The
document is explicitly:

* `status: "needs_operator_review"`, `approved: false`, `replayable: false`;
* the check command is stored as untrusted and never run;
* no Replant run, record, approval or registry creation happens here.

Candidates are written atomically with mode `0600` below
`<workdir>/.free-agent/candidates/`. They are proposals for a human, not
state that `replay()` will consume.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_workflows.py' -v
```

The tests mock processes and use temporary fixtures. They cover binary lookup
precedence, indexed-versus-unindexed records, stale entries, symlink rejection,
artifact drift before process launch, missing artifacts, review/blocked/miss
mapping, unknown statuses, timeout process-group kill, stderr redaction, and
the review-only candidate contract.
