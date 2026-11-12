# Quickstart: providers, profiles and deterministic runners

Run from any working directory; the symlink-safe launcher finds this checkout.

## Two working API-backed coding workers

```sh
free-agent providers list --json
free-agent providers models openrouter --json
free-agent run 'Implement the requested change and run tests' --worker pi-nvidia -w /path/to/repo --json
free-agent run 'Implement the requested change and run tests' --worker pi-openrouter -w /path/to/repo --json
```

Keys are read locally from `~/.env` (override `FREE_AGENT_ENV_FILE`). Do not paste them into argv, role Markdown, config JSON, issues or chat. OpenRouter is free-only; NVIDIA account evaluation limits/cost are not inferred from a key. The Pi process can write files/run commands as your user: use an isolated worktree or sandbox for untrusted work.

Other configured API vendors are explicit opt-in because billing eligibility is not established by a key:

```sh
free-agent providers run groq 'Review this code; do not change it' --model openai/gpt-oss-120b --read-only -w /path/to/repo --json
free-agent providers run cerebras 'Review this code; do not change it' --model gpt-oss-120b --read-only -w /path/to/repo --json
```

Verify your account plan first. Discover current IDs with `providers models PROVIDER`; a catalog is not an inference readiness test. Mistral/HF are NOT automatically labelled free API inference; Gemini's selected API key needs repair. SambaNova requires a selected key/model. Cloudflare needs account-scoped setup. Full sourced findings: [free-capacity-and-routing.md](free-capacity-and-routing.md).

## Grok-Bot-style durable roles

The installed host starter config is `~/.config/free-agent/agents.json`, if no existing config was present. The reusable secret-free version is [agents.free-api.example.json](../agents.free-api.example.json).

```sh
free-agent agent list --json
free-agent roles list
free-agent run 'Specify the feature and acceptance tests; no edits' --role architect -w /path/to/repo --json
free-agent run 'Review the current diff; no edits' --role reviewer -w /path/to/repo --json
free-agent run 'Implement the approved specification' --role builder -w /path/to/repo --json
```

These select `nvidia-architect`, `nvidia-reviewer` and `openrouter-builder`. Architect/reviewer wrappers pass Pi's read-only tool allowlist; other role tool lists are merely metadata. These subprocesses are not OS sandboxes and share your filesystem.

Add a named agent without editing Python (this example is a harmless local echo fixture, not an LLM):

```sh
free-agent agent add echo-check --command-json '["printf","%s\n","{task}"]' --role reviewer
free-agent roles set reviewer --prompt-file /home/gio/dev/free-agent/roles/reviewer.md
free-agent run 'Check the profile handover' --worker echo-check --role reviewer --json
free-agent agent remove echo-check
```

`agent add` defaults to manual-only; choose `--automatic` deliberately to make it eligible for role routing. For a real harness, replace the argv array with its verified headless invocation. Unknown placeholders, shell-string command configs and builtin-name collisions are rejected. Put actual permission restrictions in CLI flags, not only in prose. `--config PATH` edits another config; `FREE_AGENT_CONFIG=PATH` activates it for task dispatch.

Markdown profiles are plain durable instructions with `{task}` where the current task belongs. Keep credentials out. A production worker must not approve its own work; use a separate reviewer and an operator decision.

## Verified learning, then deterministic replay

```sh
free-agent workflows list --all-stores --json
free-agent loop 'Implement this task' --worker pi-nvidia -w /path/to/repo --check 'python3 -m unittest discover -s tests' --max-iterations 3 --learn --artifact pyproject.toml --artifact src/main.py --json
```

List each known input/dependency with `--artifact`; hashes are captured after a passing check. The result is a private review-only candidate under `<repo>/.free-agent/candidates`. It is not automatically replayable, and it may still need a complete deterministic action recipe. Never fingerprint credentials as task inputs.

An operator then reviews and records the actual tools, actions, independent checks, workdir, side effects/rollback and freshness evidence in a trusted native Replant registry. This CLI intentionally has NO approve command. [workflows.example.json](../workflows.example.json) is an operator manifest template, not an approved production workflow.

```sh
free-agent workflows run 'the exact recorded request' --manifest /operator-owned/workflows.json -w /path/to/repo --json
free-agent run 'the exact recorded request' --role runner --workflow-manifest /operator-owned/workflows.json -w /path/to/repo --json
free-agent loop 'the exact recorded request' --workflow-manifest /operator-owned/workflows.json --check 'python3 -m unittest discover -s tests' -w /path/to/repo --json
```

`runner` cannot dispatch a model. Replay-first `loop` falls back to agent work only on an exact MISS; a changed input, missing/invalid manifest, review request, failed native check or unavailable engine stops it. A successful replay runs its native actions and independent checks again with a proved zero-provider receipt. The separate `--check` belongs to the model branch and is not substituted for the native recipe's checks.

No production recipe was self-approved during this implementation. The existing host recipe is the bundled `physis replant demo`, not a learned deployment pipeline. Local smoke fixtures/proposals are excluded from git.

## Additional free-plan agents

Gemini CLI is registered, manual-only; use official Google login/configuration before relying on it. The installed older version's `plan` approval mode is gated behind an experimental setting, so the adapter uses `auto_edit` and is not a read-only reviewer.

Jules is installed but asynchronous and requires separate login/repository consent:

```sh
jules login
jules new --repo OWNER/REPO 'Implement this scoped task'
jules remote list --session
jules remote pull --session SESSION_ID
```

Do not add `--apply` or assume submission means completion. Review the resulting patch before applying/merging. No Jules remote job was submitted by this setup. Other candidate services and routing tools are evaluated in the sourced research notes rather than silently installed/enabled.
