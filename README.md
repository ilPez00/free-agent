# free-agent

A stdlib-Python CLI router for coding-agent CLIs. Use free-tier executors for implementation and subscription planners for design. Hermes is also a worker/orchestrator. Requires Python 3.10+, Linux/macOS and separately installed vendor CLIs.

## Install and run from any directory

```sh
sh scripts/install.sh
free-agent workers --json
free-agent status --json
free-agent run "Implement the requested change and run tests" -w /path/to/repo --json
free-agent run "Design a migration plan" --role planner -w /path/to/repo --json
free-agent run "Review the architecture, do not edit" --worker claude -w /path/to/repo
free-agent run "Complete this task directly" --worker hermes -w /path/to/repo
free-agent loop "Fix the failing tests" -w /path/to/repo --worker opencode --check 'python3 -m unittest discover -s tests' --max-iterations 3
free-agent loop "Implement the feature" -w /path/to/repo --planner codex --worker cline --check 'npm test' --max-iterations 2
```

The loop optionally plans, dispatches an executor, runs YOUR verification command, then sends failing output into the next bounded iteration. It stops on verification success, unavailable workers, or its iteration cap. `--check` executes via `/bin/sh -c`; only pass commands you trust. It is not a sandbox or a background service. Run independent jobs in separate worktrees. Interrupted loops do not have automatic checkpoint/resume.

## Providers, durable roles and Replant

Start here: [quickstart](docs/quickstart.md), [configuration](docs/configuration.md),
[sourced capacity/routing research](docs/free-capacity-and-routing.md), and
[deterministic workflows](docs/deterministic-workflows.md).

New commands: `providers list/models/run`, `agent add/list/remove/validate`,
`roles set/list`, `workflows list/run/propose`. `pi-nvidia` and `pi-openrouter`
are real isolated Pi coding workers using local keys; OpenRouter is free-only.
Other provider accounts require explicit plan/model decisions. Durable Markdown
profiles live under `roles/`; `agents.free-api.example.json` supplies reusable
architect/reviewer/builder wrappers. `runner` is reserved for zero-model Replant
replay, not another LLM persona. `loop --learn --artifact ...` emits review-only
candidates; `--workflow-manifest` tries approved replay before any planner.

## Roles

| Worker | Default role | Specialty |
| --- | --- | --- |
| Claude Code | planner | architecture |
| Codex | planner | implementation plan |
| Grok Build | planner | research |
| AGY | planner, explicit opt-in only | ecosystem |
| Kimi Code | planner | synthesis |
| Hermes | executor and orchestrator | coordination |
| OpenCode, Cline, Kilo, Kiro, Copilot, Pi, Continue, Cody | executor | implementation |
| Cursor, Vibe, Crush, Qwen, Goose, Aider, Amp | explicit opt-in executor | configured provider |
| Freebuff, free-pi | executor (top priority, PTY/tmux) | implementation |

Planner-only workers are never selected by default executor routing. `--worker NAME` deliberately overrides role selection. Codex uses read-only sandbox; Claude uses plan permission mode. Grok/Kimi/AGY receive a plan-only instruction, which is NOT an OS-enforced sandbox. AGY is deferred and excluded from automatic routing. Hermes subprocess recursion is capped by `FREE_AGENT_DEPTH`; it uses the real Hermes launcher, not an unrelated `hermes` executable on PATH.

## Authentication and limits

Installation is NOT authentication, a subscription, or an inference allowance. Unknown login/quota status is reported as unknown. Model/provider costs depend on your account and local client configuration. Kilo explicitly selects `kilo/kilo-auto/free`; Pi defaults to `openrouter/free`. OpenCode/Cline/Hermes use your configured model, which can be paid: review those configurations before automatic routing. This tool cannot enforce a universal zero-dollar budget across all vendor CLIs.

* `REAUTH_REQUIRED`: authenticate in the official vendor CLI, then `free-agent reset NAME`.
* `QUOTA_EXHAUSTED`: the worker cools down for 30 minutes and other workers may be tried. Wait for the provider's actual reset; signing in again does NOT replenish credits. No account rotation or limit evasion is performed.
* `free-agent nudge` reports recorded failures; it does not launch inference or interactive login flows. A host cron can call it every 30 minutes. Desktop notifications use `notify-send` if present.
* No tokens, auth stores, user config, transcripts, or runtime state are shipped in this repo. State is written atomically with private file permissions to `$XDG_CACHE_HOME/free-agent/state.json` (default `~/.cache/...`). Set `FREE_AGENT_STATE` to override. Separate jobs should use separate state paths; concurrent state merging is not implemented.

Useful logins: `copilot login`, `agent login` (Cursor), `codex login`, `claude auth login`, `kimi login`, `kilo auth login`, `kiro-cli login`, `vibe --setup`, `grok`, `freebuff login`, `free-pi-cli`, `cn login` (Continue), `cody auth login`, `amp login`.

## Fleet configurator

`free-agent-configure` (source `scripts/configure.py`) detects, configures, installs and deploys the workers across every host in `~/.fleet_aliases.sh` (`*-ts` Tailscale aliases are deduplicated):

```sh
free-agent-configure scan      # installed / free-tier / priority for one host
free-agent-configure apply     # write ~/.config/free-agent/agents.json free routes
free-agent-configure install   # npm-install the missing catalog agents
free-agent-configure deploy    # apply + scp config + git pull on every host
free-agent-configure doctor    # fleet scan + gaps + exact login commands
```

The agent catalog at the top of `scripts/configure.py` is the single source of truth: adding an agent means adding one row (binary, npm package, login command, free-tier cadence, priority).

## Installation coverage

See [docs/installers.md](docs/installers.md). The host installation includes the Google Cloud SDK (`gcloud`) for Vertex AI. Vertex is an API service, NOT another agent executable or guaranteed free pool. Cloud login, project selection, Vertex API enablement, trial eligibility and billing setup require the account owner's decisions and have NOT been activated by this project. Amazon Q CLI has become Kiro CLI; it is not an independent quota pool.

Freebuff and free-pi are TUI-only, so their adapters drive a tmux pane. They are the top-priority automatic workers (priority 5 and 6) and are now the first executors routing selects. Freebuff is detected through its `• Ns •` completion marker; free-pi is detected through the reply block that follows the echoed task and precedes the ad panel, and a lone content block is rejected as an echo so an unanswered turn can never report success. PTY sessions can collide if run concurrently; do not run multiple jobs for the same TUI worker. No ads are suppressed, tokens spoofed, or hidden API gates bypassed.

Free tiers and promotional models change; no stable capacity is promised. Provider privacy/training policies vary. Do not send secrets or confidential code without checking the provider's policies. Executors can modify files and run commands; some existing executor recipes auto-approve tools. Use isolated worktrees or an OS sandbox, and review diffs before committing. A successful agent response is not proof the task is correct: use the loop's verification gate.

## Environment

* `FREE_AGENT_HERMES_BIN`: exact Hermes Agent launcher path.
* `FREE_AGENT_DEPTH`: child recursion depth (runs blocked at 2).
* `FREE_AGENT_STATE`: state file override.
* `FREE_AGENT_PI_PROVIDER`, `FREE_AGENT_PI_MODEL`: Pi provider/model.
* `FREE_AGENT_KILO_MODEL`: Kilo model (default `kilo/kilo-auto/free`).

Run `python3 -m unittest discover -s tests -v`. Unit tests use local mocks/fake workers and do not consume provider quota. Live provider readiness is separate from adapter/unit-test coverage.
