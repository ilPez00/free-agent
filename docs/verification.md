# Verification evidence

Local checks for the provider/profile/Replant extension, 2026-11-12. These are observed results, not fabricated example outputs. Live acceptance repositories/stores are under Hermes scratch and are not shipped as approved production recipes.

## Unit/regression suite

`python3 -m unittest discover -s tests -q` completed with 141 tests, all passing.

Coverage includes credential alias selection/redaction; duplicate/blank env assignments; literal and quoted `#`; no shell evaluation; argv placeholder validation; private atomic config writes; public listings omitting argv/env values; malformed-config management/help recovery; independent API-provider policy; free-only finite-zero OpenRouter prices; isolated Pi configuration; process-group timeout handling; role dispatch; pretty-printed wrapper JSON vs JSONL; no user/startup-log echoes; and Replant input freshness, receipt/status mapping and approval boundaries.

A live reviewer-role smoke check exposed a pretty-printed JSON wrapper parsing defect after the earlier suite passed. A regression test was first observed failing, the parser was fixed to support a single JSON document as well as JSONL, then the complete 141-test suite and live wrapper check passed. This is why unit coverage and actual end-to-end execution are reported separately.

## Live model/harness execution

* NVIDIA Pi returned `NVIDIA_PI_OK`, and later wrote `multiply.py`. Its independent Python assertion checked `multiply(6, 7) == 42` and printed `NVIDIA_EDIT_CHECK_OK`. The loop completed in one iteration, then wrote a candidate with `approved: false`, `replayable: false`. NVIDIA cost remains unknown.
* OpenRouter Pi returned `OPENROUTER_PI_OK`, then wrote `subtract.py`. Its independent Python assertion checked `subtract(9, 4) == 5` and printed `OPENROUTER_EDIT_CHECK_OK`. The loop completed in one iteration with reported cost `0`, using the catalog-checked free router.
* Installed `nvidia-reviewer` role wrapper returned `ROLE_WRAPPER_OK` through role selection, a nested provider CLI invocation and result parsing. The read-only harness allowlist was enabled; this is not an OS sandbox assertion.
* Groq Pi returned `GROQ_PI_OK`. The route remains explicit/manual because provider free-plan entitlement is account/model dependent.
* Groq/Cerebras/Mistral/SambaNova catalog HTTP checks are NOT presented as free billing tests. Cerebras inference returned HTTP 402 and SambaNova chat inference HTTP 401. Jules help/version worked; login/repository consent/remote job execution were not attempted.

## Native Replant execution

* The supported bundled demo tool recorded one scratch recipe and replayed it twice. Its counter advanced on actual actions and its independent check passed each time; receipt `provider_calls` was `0`.
* Our bridge replayed the real scratch record with the manifest-owned context intact and `provider_calls: 0`.
* Modifying a fingerprinted guard input produced `blocked`; the action counter was unchanged. Missing/invalid fingerprints fail closed in unit tests as well.
* Replay-first `loop` exited through `stage: replay`, `provider_calls: 0` before planner/executor dispatch, even when the unrelated model-branch `--check` was `false`. Native actions/checks, not the model-branch command, decide replay success.
* `free-agent run 'physis replant demo' --role runner --workflow-manifest ...` completed a real native replay with zero provider calls.
* The canonical non-scratch host inventory has one unique indexed learned demo recipe. Plugin/source symlink aliases were deduplicated. The repository itself has zero approved production workflows. See `learned-workflows.json`.

## Security and publishing gates

The task's repository files were scanned against actual locally parsed credential strings without printing them: no matches. Python AST checks found no `eval`, `exec` or `shell=True` use. `git diff --check` passed. Runtime state, candidates, env files and private registries/manifests are ignored/not committed.

These checks do not establish an OS isolation boundary, complete provider billing guarantees, or whole-machine deterministic provenance. Explicit operator checks use `/bin/sh -c` by design; do not pass untrusted verification commands. Same-user configuration and hash sidecars remain mutable. Read the documented limits before unattended use.

The previously observed GitHub Actions account billing/spending-limit block is external to local test execution. No remote CI success is implied by this report.
