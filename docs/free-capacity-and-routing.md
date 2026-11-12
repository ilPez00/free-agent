# Free capacity and complementary tooling: checked findings

Checked 2026-11-12. This is an evidence ledger and integration decision, not a promise that every account receives a quota. Free clients, free inference, temporary trials, subscription entitlements and local computation are different resources. No account rotation or undocumented quota bypass is implemented.

## What else is worth adding?

There are useful remaining options; “no agents left” would be an unjustified claim.

* **Gemini CLI** has a recurring Google-login allowance for eligible individual accounts. Its current quota page lists 1,000 model requests per user/day for Code Assist Individual and a separate, smaller unpaid API-key allowance. These are model requests, not completed coding tasks, and account eligibility/availability still apply. The existing local Gemini CLI is now registered as explicit-opt-in `gemini`; its live headless check did not succeed, so it is not advertised as authenticated or enabled automatically.[1]
* **Jules** is an asynchronous remote coding worker, not another local shell agent. The current official plan page lists 15 daily tasks and 3 concurrent tasks on the base plan. The Jules CLI was installed and `jules version`/help exercised. It still needs `jules login` and repository authorization; no remote task, PR or patch application was initiated. Job submission is not task completion, so it is deliberately outside the synchronous default executor pool.[2]
* **Rovo Dev** is a possible trial, not a recurring free allowance established by this investigation: the current pricing page shows a paid per-developer subscription with included credits and “Try it free.” Do not turn an old beta-free advertisement into an automatic free worker.[3]
* **iFlow** should not be added as another official free model pool: its first-party site/changelog announces service shutdown on April 17, 2026. A surviving BYOK client does not restore that pool.[4]
* Zed/Continue/other open-source harnesses can add interfaces and tool capabilities, but their being free software is not an independent grant of inference credits. Existing Pi/OpenCode/Goose/Aider are sufficient harnesses for the API-backed pool; adding more wrappers should have a concrete capability benefit.

## Provider-backed workers shipped here

The actual coding harness is Pi, in an isolated temporary configuration with extensions, MCP, skills, project context loading, startup network operations and session persistence disabled. The selected provider key is passed only in the child environment, never in argv or committed config. Each run uses the user's chosen single provider credential, not a rotating list of accounts.

| Provider | Current implementation / readiness | Automatic routing decision |
| --- | --- | --- |
| NVIDIA NIM/API catalog | `pi-nvidia`; live response plus real file-writing/verification loop passed. Pinned default `nvidia/nemotron-3.5-super-vl-120b-a12b`. Reported cost remains unknown. | Eligible when a usable key is present, as requested. Treat as development/evaluation capacity, not an infinite universal free budget. |
| OpenRouter | `pi-openrouter`; live response passed. `openrouter/free` default; live discovery accepts only explicit free IDs whose prompt AND completion prices are finite zero. | Eligible with key. No paid-model or paid-provider fallback. The official free router chooses from free models and filters for request features.[14] |
| Groq | API catalog read succeeded; explicit Pi route with a selected model. Published free-plan limits exist but apply per account/model.[16] | Opt-in; a key/catalog response is not proof of a free billing entitlement. |
| Cerebras | API catalog read succeeded; explicit Pi route. Its rate limits/quota replenishment are independent of other vendors.[17] | Opt-in; current account tier unverified. |
| Gemini API | Route implemented, but the selected stored key returned `API_KEY_INVALID` in the native API probe. CLI Google login is a separate auth path. Free-tier eligibility is project/account dependent.[18] | Opt-in; repair the selected key, do not cycle through the many other project keys in the shared file. |
| SambaNova | Explicit Pi route and credential aliases added; no selected key found/tested. Official docs distinguish no-payment-method Free Tier and paid Developer Tier; use the published base URL.[23][24] | Opt-in, explicit model required. |
| Mistral API | Catalog readable, but current billing docs explicitly say Free mode does NOT include API access; API use requires billing activation/PAYG.[22] | Paid/trial opt-in classification; never silently treat old “Experiment” claims as current API-free access. |
| Cloudflare Workers AI | Credential inventory only until an account ID and a deliberate adapter configuration are supplied. Official pricing includes 10,000 free Neurons/day, with paid overage requiring a paid plan.[19] | Not auto-runnable. Do not enable paid overages or invent an account ID. |
| Hugging Face Inference Providers | Credential inventory only. Current official pricing says free users receive no included monthly credits; purchasing credits or PRO is required.[20] | Not advertised as a free pool merely because a token exists. |
| Cohere / Together / DeepInfra | Explicit compatible-provider adapters, no selected usable credentials in the local inventory at verification time. | Paid/trial opt-in; no claim of recurring free inference. |

The NVIDIA documentation describes the API catalog/OpenAI-compatible NIM surface; it does not establish a perpetual account-independent free token grant.[15] API catalog HTTP 200 is recorded separately from inference success because some catalogs are public. A successful inference is still not proof of zero cost.

### Local key handling

The shared `/home/gio/.env` has duplicate assignments and generated empty placeholders, plus non-assignment material. The credential reader never sources it. It skips unrelated malformed lines and picks the last nonempty assignment per supported credential name; normal strict dotenv parsing still preserves last-assignment semantics. This prevents a blank placeholder hiding a real NVIDIA key. Non-credential settings such as `G4F_NVIDIA` are NOT accepted as NVIDIA API keys. Explicit process environment credentials take precedence. The file was not modified.

No key is printed by inventory/catalog output or written to git. Only the selected key enters the isolated Pi environment. Pi and configured agents are not OS sandboxes: a permitted bash tool runs as the same user and can inherit its selected key/read user files. Use isolated worktrees/OS sandboxing for untrusted tasks; do not confuse prompt instructions with an access-control boundary.

## Adjacent projects: use at the right layer

The literal search for `ree-models-router` did not identify an exact project. Two close but distinct candidates were verified: `bytonylee/free-router` and `tokkkie/free-model-router`. Neither is silently assumed to be the identifier the user intended.[5][6]

| Resource | Layer and useful idea | Integration decision / caution |
| --- | --- | --- |
| **OmniRoute** | Broad OpenAI-compatible gateway, quota-aware routing and provider failover. Its README explicitly says users bring separately eligible provider accounts; aggregate token estimates are not grants.[7] | Optional inference layer beneath a harness. Start with direct official endpoints to keep attribution and billing clear; do not install another daemon merely to duplicate working local routing. Audit configured destinations, paid fallbacks, auth bridges and telemetry first. |
| **free-router** | Interactive NVIDIA/OpenRouter model discovery, availability/latency ranking, and handoff to OpenCode/OpenClaw/Hermes.[5] | Useful optional model-selection cockpit, not an agent supervisor. It can rewrite target configs and optionally export plaintext keys. Its README describes frequent live pings and fallback remapping: inspect this before running; do not launch continuous probes against all free quotas. |
| **free-model-router** | OpenAI-compatible multi-provider proxy with discovery, 429/timeout failover, tool-call verification, and local Ollama fallback.[6] | Closest small reference for extracting our provider pool into a shared endpoint. Do not duplicate its proxy and our agent failover indiscriminately: bound retry budgets to avoid multiplied retries. |
| **freellmpool** | A broad catalog deliberately distinguishing recurring free, keyless, trials, pin-only local runtimes and disabled candidates; optional local proxy/CLI/library.[8] | Strong catalog/research complement. Its README's route counts are project-reported, not verified free capacity on this host. Audit each upstream and permission before enabling; do not forward private code into unknown keyless relays. |
| **LiteLLM Router** | Provider abstraction/routing behind a uniform client surface.[9] | Useful when budgets, observability and deployment policy justify the dependency. No new free entitlement; paid routes must remain explicitly excluded from a free-only pool. |
| **Bifrost** | OpenAI-compatible gateway with failover, balancing and caching.[10] | Optional production gateway. Semantic response caching is NOT deterministic tool-workflow replay; it must not replace Replant's actual actions/checks. |
| **CLIProxyAPI** | A bridge exposing multiple CLI/OAuth/API channels through compatible interfaces.[11] | Adjacent, not automatically authorized: vendor auth restrictions and plan eligibility still apply. No token impersonation, OAuth spoofing or bypassing service limits is enabled here. Prefer each vendor's supported CLI when that is the permitted access path. |
| **OpenRouter free router** | Hosted free-model selection rather than another local daemon.[14] | Already used by `pi-openrouter`, with catalog price checks before launch. Keep provenance/model selection in results; it does not grant a separate account quota. |

## Grok-Bot-style roles without a new framework

Grok's first-party guide separates durable Name/Title/Description-style instructions from the current task and describes an outer-loop agent preparing work for an inner-loop coding worker.[12] The community `grok-bot-profiles` collection makes the useful boundaries explicit: one outcome per bot, production separated from verification, and a named human approval boundary.[13]

Here the equivalent is deliberately small:

* `agents.json`: agent name, argv command, roles, specialty, automatic opt-in, timeout, optional single `key_env`.
* Markdown role files: durable objective, output contract, tools/permission expectations, STOP/BLOCKED conditions and handover boundary.
* `roles set NAME --prompt-file FILE`: import the durable profile without changing Python.
* The task stays a separate CLI argument; only `{task}` and `{workdir}` are explicitly substituted into argv. No shell-string config, eval or automatic approval is introduced.
* `runner` is reserved for deterministic Replant replay: no model dispatch even if another agent's profile claims that role. Other roles' tool lists are metadata; actual read-only enforcement must be in the harness flags.

## Physis/Replant: progressively retire repeated model work

Local, directly inspected evidence (not a web claim): canonical production stores contained one unique learned recipe, `physis replant demo`: `increment` action plus independent `verify` check, one verified record and two verified replays. The plugin symlink and its source directory name the SAME store and must be deduplicated. The free-agent repo initially had zero learned recipes/candidates. The bundled demo is a fixture, not an already learned deployment/test/cleanup workflow.

The new bridge lists learned recipes and review-only proposals, checks the operator-owned manifest's input fingerprints before calling native Replant, and accepts success only from a successful native replay receipt with `provider_calls: 0`. Changed/missing inputs, bad registries, ambiguous requests, reviews, timeouts and failures stop the loop; only an exact miss may go to a planner.

Promotion ladder:

1. A planner defines the task and observable acceptance criteria.
2. An implementation worker does the novel work in a bounded execute/verify loop.
3. A passing `--learn --artifact ...` run writes a review-only candidate, not an approved recipe. Standalone `workflows propose` does not certify that its check was run.
4. The operator reviews complete deterministic actions, independent checks, executable identities, input/dependency hashes, workdir, side effects and rollback. A candidate containing only an LLM's edits/check string is NOT automatically a complete deterministic recipe.
5. The operator records the reviewed registry/recipe using the supported native interface. No agent can approve its own candidate through this CLI.
6. Repeated matching work uses `--role runner` or replay-first `loop --workflow-manifest ...`. Actions and checks run again; model calls are skipped, not cached success asserted.
7. Drift blocks replay; re-review/re-record deliberately. Do not “repair” fingerprint sidecars or re-prompt a model merely to evade the gate.

The real bundled demo was exercised in scratch: one record/two replays, then our bridge replayed with zero provider calls. A changed guard input blocked before its action ran. A CLI loop configured with an intentionally failing `--check false` still exited via the successful Replant branch before dispatching any planner: the native independent checks, not the unrelated model-loop check, own that branch. Separately, a live NVIDIA worker wrote `multiply.py`, its Python assertion passed, and a review-only candidate was emitted. No production recipe was auto-approved.

Same-user-writable manifests, registries and hash sidecars are mutable configuration, not an authenticated security boundary against that user. Fingerprint known dependencies and disclose untracked interpreter/environment dependencies; pinning a wrapper alone does not pin the interpreter or the whole machine.

## Verification and next access steps

* Working inference/harness checks: NVIDIA Pi and OpenRouter Pi. Working live edit/check/proposal: NVIDIA Pi.
* Catalog checks only: Groq, Cerebras, Mistral. No inference/billing claims inferred from these reads.
* Gemini API needs a valid selected key. Gemini CLI headless readiness remains unverified after a failing local check; use official Google login rather than treating API and OAuth entitlements as interchangeable.
* Jules installed, official help/version checked; login/repository consent and remote job completion remain separate user-controlled steps.
* Cloudflare needs account-scoped setup; SambaNova needs a selected key/model. HF/Mistral are not labelled free API inference just because old catalogs said so.
* Repository unit tests and real CLI/provider/Replant acceptance checks are recorded in the task's verification notes. GitHub Actions is still subject to the account's previously reported billing/spending-limit block; local green tests do not imply remote CI ran.

## Sources

[1] https://geminicli.com/docs/resources/quota-and-pricing — gemini-cli
[2] https://jules.google/docs/usage-limits — jules
[3] https://www.atlassian.com/software/rovo-dev/pricing — rovo
[4] https://platform.iflow.cn/en/cli/changelog — iflow
[5] https://raw.githubusercontent.com/bytonylee/free-router/main/README.md — free-router
[6] https://raw.githubusercontent.com/tokkkie/free-model-router/main/README.md — free-model-router
[7] https://raw.githubusercontent.com/cheaper-inference/OmniRoute/main/README.md — omniroute
[8] https://raw.githubusercontent.com/0xzr/freellmpool/main/README.md — freellmpool
[9] https://docs.litellm.ai/docs/routing — litellm
[10] https://raw.githubusercontent.com/maximhq/bifrost/main/README.md — bifrost
[11] https://raw.githubusercontent.com/router-for-me/CLIProxyAPI/main/README.md — cliproxy
[12] https://x.ai/bot/guides/grok-bot-101 — grok-bot
[13] https://raw.githubusercontent.com/HAEGONG/grok-bot-profiles/main/README.md — grok-profiles
[14] https://openrouter.ai/docs/guides/routing/routers/free-router — openrouter
[15] https://docs.api.nvidia.com/nim/docs/overview — nvidia
[16] https://console.groq.com/docs/rate-limits — groq
[17] https://inference-docs.cerebras.ai/support/rate-limits — cerebras
[18] https://aistudio.google.com/docs/rate-limits — gemini-api
[19] https://developers.cloudflare.com/workers-ai/platform/pricing — cloudflare
[20] https://huggingface.co/docs/inference-providers/pricing — huggingface
[22] https://docs.mistral.ai/admin/billing-usage/usage-limits — mistral-usage
[23] https://docs.sambanova.ai/docs/en/models/rate-limits — sambanova-limits
[24] https://docs.sambanova.ai/docs/en/get-started/api-keys-urls — sambanova-endpoints
