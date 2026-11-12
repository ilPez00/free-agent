# Vendor client installers

These are separate clients, not bundled code. Installation does not grant access to a model.
The local tested binaries/versions are recorded in `installed-versions.json`.

| Client | Official install route | Access model |
| --- | --- | --- |
| Kilo | `npm install -g @kilocode/cli` | Auto Free or paid/BYOK |
| Cursor | https://cursor.com/install | account/plan; CLI eligibility account-dependent |
| Copilot | `npm install -g @github/copilot` | GitHub account/allowance |
| Cline | `npm install -g cline` | configured model provider |
| Freebuff | `npm install -g freebuff` | official ad-supported TUI |
| free-pi | `npm install -g free-pi-cli` | official ad-supported TUI, consent and GitHub login |
| Codex | `npm install -g @openai/codex` | ChatGPT or API account, eligibility varies |
| Claude Code | `npm install -g @anthropic-ai/claude-code` | subscription or paid API |
| Kimi Code | `npm install -g @moonshot-ai/kimi-code` | Kimi membership or API |
| Grok Build | `npm install -g @xai-official/grok` | xAI login/plan or API |
| Amp | `npm install -g @ampcode/cli` | BYOK/subscription or metered inference; not free inference |
| Crush | `npm install -g @charmland/crush` | BYOK/Hyper |
| Qwen Code | `npm install -g @qwen-code/qwen-code` | configured plan/key/local model |
| Vibe | `uv tool install mistral-vibe` | account/free mode or BYOK; account-dependent |
| Aider | `uv tool install aider-chat` | BYOK/local model |
| Goose | https://github.com/aaif-goose/goose/releases/download/stable/download_cli.sh | BYOK/local/ACP |
| AGY | https://antigravity.google/cli/install.sh | Google account/plan; deferred by user |
| OpenCode | https://opencode.ai | configured provider; free models may rotate |
| Pi | https://pi.dev | configured provider |
| Kiro | https://kiro.dev/docs/cli/ | account allowance/subscription |
| Hermes | https://hermes-agent.nousresearch.com/docs | configured provider |
| Google Cloud SDK | https://cloud.google.com/sdk/docs/install | Vertex API service, billing/project required |

Review installers before executing. On npm versions with install-script blocking, explicitly approve necessary native-binary postinstalls instead of disabling all protection globally.

Amazon Q CLI is now Kiro CLI, per https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line.html . Do not install an obsolete duplicate expecting independent credits.

Vertex: the Google Cloud CLI is installed. No Google login, project creation, API enablement, billing activation or trial enrollment was performed. `gcloud init` and application-default credentials can incur cloud charges after account/project setup. This router does not register Vertex as an autonomous worker because it is a provider service, not an agent harness.

## Host verification

Live acceptance: a Cline job created `add.py`, then the loop executed an assertion against it and printed `LIVE_LOOP_OK` (exit 0, one iteration; Cline reported cost 0). Hermes returned `HERMES_WORKER_OK` through its own subprocess adapter. These tests ran from outside this repository. Unit tests do not call remote models. Most newly installed clients still require their own login; their `--version` success is not an inference smoke test.
