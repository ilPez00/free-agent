# Configuration

free-agent has three optional layers on top of the builtin worker registry:

1. **Credential files and provider workers** — Pi coding workers for NVIDIA and
   OpenRouter, plus explicit opt-in providers.
2. **A user agent config** — your own argv commands with role prompts.
3. **Deterministic workflows** — Replant replay and review-only proposals.

Nothing here is required. With no config and no credentials, the CLI runs the
builtin workers exactly as before.

## Discovery

| setting | default | meaning |
| --- | --- | --- |
| `FREE_AGENT_ENV_FILE` | `~/.env` | credential file parsed as `KEY=VALUE` |
| `FREE_AGENT_CONFIG` | `~/.config/free-agent/agents.json` | user agent/role config |
| `FREE_AGENT_WORKFLOW_MANIFEST` | unset | default Replant workflow manifest |
| `FREE_AGENT_REPLANT_BIN` | auto | native `replant` executable override |
| `FREE_AGENT_PROVIDER_MODEL` | unset | opt-in model for the extra Pi providers |

They are read by `extensions.initialize(...)`, called once from `free_agent.main`
before the argument parser is built, so `--worker`, `--role`, `--planner` and
`reset` see the final registry.

## Credentials and Pi providers

`pi-nvidia` and `pi-openrouter` are always registered and use
`providers.run_pi` with `free_agent.pi_extract`:

| worker | default model | automatic |
| --- | --- | --- |
| `pi-nvidia` | `providers.DEFAULT_MODELS["nvidia"]` | only when a credential is present |
| `pi-openrouter` | `providers.DEFAULT_MODELS["openrouter"]` | only when a credential is present |

Registration and `status` never call a provider. `installed` checks only that
`pi` exists; the credential is read lazily at run/login time. `login` reports
"credential present" or "no credential" and never prints the key.

The remaining providers are **opt-in** and are not registered unless
`FREE_AGENT_PROVIDER_MODEL` is set to a model id:

```
pi-groq  pi-cerebras  pi-mistral  pi-gemini  pi-sambanova  pi-cohere  pi-together  pi-deepinfra
```

They are never `automatic`, even with a key, because their free tier is
unverified or may bill. Select one explicitly with `--worker`.

### `providers` commands

```sh
free-agent providers [--env-file PATH] list [--json]
free-agent providers [--env-file PATH] models NAME [--json]
free-agent providers [--env-file PATH] run NAME TASK [--model MODEL] \
    [-w DIR] [--timeout SEC] [--read-only] [--json]
```

* `list` prints credential presence and free policy. No values.
* `models` prints a sanitized catalog (`id`, `name`, `tools` only).
* `run` launches one isolated Pi process. The key travels only inside
  `providers.run_pi` (child environment), never in argv.
* `cost` is `0` only for an OpenRouter run that `run_pi` verified free;
  every other provider reports `unknown`. It is never faked as zero.

## User agent config

Example (`~/.config/free-agent/agents.json`):

```json
{
  "version": 1,
  "agents": [
    {
      "name": "pi-review",
      "command": ["pi", "--print", "--mode", "json", "--no-extensions", "--no-mcp", "--tools", "read,grep,find,ls", "{task}"],
      "roles": ["reviewer"],
      "specialty": "review",
      "automatic": false,
      "timeout": 900,
      "env": {"NO_COLOR": "1"},
      "key_env": "MY_PI_TOKEN"
    }
  ],
  "roles": {
    "reviewer": {
      "description": "Read-only reviewer.",
      "prompt": "Review the work read-only. Do not edit any file. Task: {task}",
      "tools": ["read"]
    }
  }
}
```

Rules enforced by `profiles.validate_config` before anything runs:

* `command` is a **non-empty argv array**, never a shell string. No shell is
  invoked, and `{task}` / `{workdir}` are substituted without rescanning.
* `roles` must reference roles declared in the same file.
* `timeout` is a positive integer; the CLI uses
  `min(--timeout, definition timeout)`.
* Names match `^[a-z][a-z0-9_-]*$`.

A user agent colliding with a builtin worker name is rejected. Calling
`initialize` again replaces only the entries it registered itself (idempotent).

### `agent` and `roles` commands

```sh
free-agent agent list   [--config PATH] [--json]
free-agent agent add NAME --command-json '["pi","--print","{task}"]' \
    [--role ROLE] [--specialty TEXT] [--automatic] [--config PATH] [--json]
free-agent agent remove NAME [--config PATH] [--json]
free-agent agent validate [--config PATH] [--json]

free-agent roles list [--config PATH] [--json]
free-agent roles set NAME --prompt TEXT [--description TEXT] \
    [--tools CSV] [--config PATH] [--json]
```

* `agent add` creates the role if it is not declared, defaulting to
  `executor` with the prompt `Complete the task: {task}`.
* `--command-json` must be a JSON array. A JSON string is rejected, because a
  command is never a shell line.
* `roles set --tools` stores names only; see the metadata note below.
* All writes are atomic (`profiles.save_config`, mode `0600`).
* Empty `agents` arrays are supported. Removing the last agent preserves your
  durable roles. `roles set` can create a roles-only config first.
* `roles set NAME --prompt-file roles/reviewer.md` imports plain Markdown
  instructions. Include `{task}` where the current task belongs. Unsupported
  placeholders fail validation, rather than silently dropping the role.
* Credential lookup parses the shared env file without executing it. Unrelated
  malformed lines are skipped. For credentials, the last nonempty assignment
  per variable wins so generated blank duplicate placeholders do not mask real
  keys. The strict dotenv parser retains ordinary last-assignment semantics.
  Disable a provider through routing/configuration, not by appending blank keys.
  No failed/quota-limited key is rotated to another account.

## Role prompts

`free-agent run ... --role ROLE` selects workers whose `roles` include `ROLE`
and, when the user config declares that role, sends the rendered
`profiles.role_prompt(config, ROLE, task)` instead of the bare task.

`--role` accepts any custom role name; it is not restricted to
`executor`/`planner`/`orchestrator`. Builtin planners keep their `PLAN ONLY`
prefix when no user role prompt applies.

**Role prompts and `tools` are instructions and metadata, not a sandbox.**
free-agent does not enforce `tools`; the configured program decides what it can
do. Put the real restriction in the command itself, for example:

```json
"command": ["pi", "--print", "--mode", "json", "--no-extensions", "--no-mcp", "--tools", "read,grep,find,ls", "{task}"]
```

`runner` is special: `free-agent run --role runner` performs only a deterministic
workflow replay (see below). It never dispatches a model or a configured agent.

## Process environment for configured agents

Configured argv agents run with a minimal environment:

* `PATH`, `HOME`, `LANG`, `TERM`, plus a `TMPDIR` fallback;
* explicit entries from the agent's `env` object, except loader/credential
  overrides (`PATH`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONPATH`,
  `NODE_OPTIONS`, `BASH_ENV`, `GIT_SSH_COMMAND`, …) which are ignored;
* one value for `key_env`, read from the environment or the env file and passed
  under that same name. The rest of the env file is never loaded into the child.

Command output is redacted: the resolved `key_env` value is replaced with
`[REDACTED]` in captured stdout/stderr.

The config is **user-owned**. These checks prevent accidents and ambient
credential leaks, not a malicious same-user process: anyone who can edit the
config or the env file can run their own commands with their own values.

## Deterministic workflows

```sh
free-agent workflows list [--store PATH] [--all-stores] [-w DIR] [--json]
free-agent workflows run TASK [--manifest PATH] [-w DIR] [--timeout SEC] [--json]
free-agent workflows propose TASK --check CMD --artifact PATH [--artifact PATH ...] \
    [-w DIR] [--json]
```

* `list` inspects Replant stores read-only. The default store is
  `<workdir>/.replant`; `--all-stores` adds `~/.hermes/.replant`,
  `~/.hermes/plugins/physis/.replant` and `~/dev/hermes-physis/.replant`.
  Native history is reduced to a count, and candidates produced by free-agent
  are listed separately from `<workdir>/.free-agent/candidates`.
* `run` calls `workflows.replay` only. It cannot approve or record anything.
* `propose` writes a candidate with `status: "needs_operator_review"`,
  `approved: false`, `replayable: false`. The check command is stored untrusted
  and never executed here.

### Loop integration

```sh
free-agent loop TASK --check CMD \
    [--workflow-manifest PATH] [--learn --artifact PATH ...] ...
```

When a manifest is configured, replay runs **before any planner**:

| replay status | loop behaviour |
| --- | --- |
| `replayed` | print `ok`, `provider_calls: 0`, exit 0 |
| `miss` | only exact miss falls through to the planner/worker loop |
| `blocked`, `error`, `unavailable` | stop nonzero, no planner |
| `disabled` (manifest configured but missing) | stop nonzero |

After a verified success (`--check` exits 0), `--learn` with at least one
`--artifact` writes a review-only candidate using the actual worker and check
exit code as metadata. It is never approved, recorded or replayed automatically.

## JSON output

Commands accepting `--json` emit one JSON document. Errors are
`{"ok": false, "error": "..."}` rather than a traceback.
