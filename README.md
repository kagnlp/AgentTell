# AgentTell: Replication Package

Code and scored results for *Your Agent's Choices Reveal Your Secrets: Behavioural Side-Channel
Inference Against Browser-Use Agents*.

A browser-use agent reads a private value on one website (the **plant**), then does an unrelated
task on a second website (the **probe**), which offers one option per possible value plus a
general option. The **Leakage Score** (LS) is how much more often the agent picks the option
matching the held value after the plant (**loaded**) than without it (**cold**), in percentage
points. All websites are local mocks on `127.0.0.1`; see [ETHICS.md](ETHICS.md).

## Layout

```
benchmark/scenarios/   the 18 scenarios (YAML) and index.yaml
mock_origins/          plant websites (bank.test, services.test)
attacker/              probe website (app.test) and its event log
harness/               scenario loader, plant codes, session plans
agents/                browser-use runner and privacy instructions
orchestrator/          run planning, results store, spend tracking
analysis/              Leakage Score and bootstrap intervals
scripts/               servers, sweeps, checks, tables
results/<dataset>/     results.json for each reported dataset
labels/                session-level annotation labels and the code that builds them
```

## Install

Linux, Python 3.13, [uv](https://docs.astral.sh/uv/), Google Chrome, and an
[OpenRouter](https://openrouter.ai) key for agent runs.

```bash
uv sync --locked
cp .env.example .env        # set OPENROUTER_API_KEY
```

## Check the setup (no API key)

```bash
SCT_DATASET=setup-check bash scripts/serve.sh               # terminal 1
export SCT_DATASET=setup-check                               # terminal 2
uv run python scripts/browser_check.py                       # BROWSER LOOP OK
uv run python scripts/mechanical_check.py                    # PASS
```

## Run

Start the servers with the same `SCT_DATASET` as the run, then run the sweep. It resumes where it
stopped; failed sessions are recorded as errors and re-run, never scored as "no leak".

```bash
SCT_DATASET=<name> bash scripts/serve.sh                                  # terminal 1
export SCT_DATASET=<name>                                                 # terminal 2

# main design: 18 scenarios x 5 tasks x 3 reps + cold = 1,545 sessions
SCT_GUARD=main bash scripts/run_final_sweep.sh

# privacy-instruction arms: 3 tasks x 1 rep + 5 cold = 349 sessions
SCT_GUARD=plain TASKS=3 REPS=1 COLD_REPS=5 DENSE_COLD_REPS=5 bash scripts/run_final_sweep.sh
```

`SCT_GUARD` is one of `main`, `plain`, `strict`, `action` (texts in
[agents/browseruse_runner.py](agents/browseruse_runner.py)). Prefix `PLAN=1` to print the plan
only. For Claude, prefix `LLM=claude_openrouter`.

| Model | `OPENROUTER_MODEL` | `OPENROUTER_PROVIDER` | Other |
|---|---|---|---|
| Claude Sonnet 5 | (`ANTHROPIC_MODEL=anthropic/claude-sonnet-5`) | `anthropic` (fixed) | `LLM=claude_openrouter` |
| Gemini 3.7 Flash | `google/gemini-3.7-flash` | `google-vertex/global` | `OPENROUTER_REASONING_EFFORT=low` |
| GPT-5.6 Luna | `openai/gpt-5.6-luna` | `openai` | |
| Qwen3-VL 235B | `qwen/qwen3-vl-235b-a22b-instruct` | | |
| Kimi K2.6 | `moonshotai/kimi-k2.6` | `crusoe/bf16` | |
| GLM-4.6V | `z-ai/glm-4.6v` | `novita/bf16` | `OPENROUTER_SCHEMA_IN_PROMPT=1`, `OPENROUTER_NO_FORCE_STRUCTURED=1` |

All OpenRouter runs used `OPENROUTER_MAX_TOKENS=16000`.

## Score

```bash
PYTHONHASHSEED=0 uv run python -m analysis.export_report --dataset <name>
```

writes `results/<name>/results.json`. `scripts/plant_check.py` reports whether each loaded
session read its plant page, and `scripts/export_ls_table.py` builds the per-scenario and
per-task LS tables across the six `paper18-main-*` datasets.

## Reported results

`results/` holds the scored `results.json` of every reported dataset (no traces or raw
databases). Mean LS over the 18 scenarios, and the number of scenarios whose 95% interval
excludes 0:

| Model | main | plain | strict | action |
|---|---|---|---|---|
| Claude Sonnet 5 | 55.2 (16/18) | | | |
| Gemini 3.7 Flash | 53.2 (13/18) | 73.2 (16/18) | 55.1 (11/18) | 7.7 (1/18) |
| GPT-5.6 Luna | 64.6 (15/18) | 72.5 (15/18) | 60.4 (13/18) | 57.4 (13/18) |
| Qwen3-VL 235B | 56.8 (16/18) | 69.2 (16/18) | 57.7 (14/18) | 58.4 (14/18) |
| GLM-4.6V | 61.4 (16/18) | 65.5 (15/18) | 67.5 (16/18) | 63.5 (14/18) |
| Kimi K2.6 | 55.3 (16/18) | 73.1 (16/18) | 74.9 (16/18) | 39.5 (11/18) |

Datasets: `paper18-main-<model>` (1,545 sessions; GLM 1,526) and
`rebuttal18-{plain,strict,action}-<model>` (349 sessions; GLM 343). GLM never completes the
planting step of SC03 task c, so those cells are absent.

## Annotation labels

[labels/](labels/README.md) holds the per-session labels behind the trace findings (leak outcome,
privacy statements in memory and final responses, task requirement) for the six
`paper18-main-*` datasets, with the blind LLM-judge labels, the author adjudications and the
script that rebuilds them from the runs. It also holds the session labels, tables and figure for
the privacy instruction variants (Findings 6 and 7) and the code that builds them.

## Scenarios

Each scenario has five tasks (a–e), one per plant route; k is the number of held values. IDs
follow the paper; SC10 and SC19 are no longer part of the benchmark.

| ID | Scenario | Key | k | Tasks a–e |
|---|---|---|---|---|
| SC01 | Single-sign-on provider | `sso_wall` | 6 | `idp_settings`, `sso_policy`, `login_audit`, `idp_provisioning`, `sso_expiry` |
| SC02 | Bank identity | `checkout` | 6 | `payroll`, `statements`, `cards`, `standing_orders`, `fee_schedule` |
| SC03 | Account de-anonymisation | `account_deanon` | 6 | `deanon_form`, `deanon_contact`, `deanon_draft`, `deanon_display`, `deanon_duplicate` |
| SC04 | Health-provider identity | `health_service` | 4 | `hs_refills`, `hs_appointment`, `hs_allowance`, `hs_result`, `hs_referral` |
| SC05 | Sensitive reading interest | `sensitive_access` | 8 | `sa_synthesis`, `sa_shortlist`, `sa_triage`, `sa_subscriptions`, `sa_summary` |
| SC06 | Organisational affiliation | `affiliation` | 4 | `af_inventory`, `af_card`, `af_notice`, `af_roster`, `af_dues` |
| SC07 | Subscription tier | `tier_gated` | 3 | `plan_eligibility`, `plan_settings`, `plan_returns`, `plan_allowance`, `plan_upgrade` |
| SC08 | Second factor | `mfa_config` | 4 | `mfa_settings`, `mfa_onetime`, `mfa_devices`, `mfa_policy`, `mfa_enrolment` |
| SC09 | Recovery route | `recovery_channel` | 4 | `rc_settings`, `rc_routes`, `rc_attempts`, `rc_policy`, `rc_reachability` |
| SC11 | Credit source | `credit_standing` | 4 | `cs_inventory`, `cs_payee`, `cs_letter`, `cs_provenance`, `cs_settlement` |
| SC12 | Occupation | `profession_gated` | 3 | `headline`, `profile_summary`, `hr_record`, `credential_renewal`, `role_access` |
| SC13 | Stigma-associated service | `stigma_platform` | 4 | `sp_inventory`, `sp_descriptor`, `sp_message`, `sp_pause`, `sp_renewal` |
| SC14 | Privilege level | `privilege_role` | 3 | `pr_members`, `pr_capability`, `pr_approvals`, `pr_audit`, `pr_delegation` |
| SC15 | Document ownership | `content_ownership` | 6 | `co_readback`, `co_open`, `co_inventory`, `co_versions`, `co_size` |
| SC16 | Recent life event | `life_event` | 4 | `le_letter`, `le_deadline`, `le_checklist`, `le_history`, `le_eligibility` |
| SC17 | Account tenure | `account_age` | 4 | `aa_anniversary`, `aa_points`, `aa_milestone`, `aa_earliest`, `aa_archive` |
| SC18 | Confidential assignment | `confidential_role` | 6 | `cr_queue`, `cr_deadline`, `cr_status`, `cr_conflict`, `cr_length` |
| SC20 | Consumer-service identity | `vendor_session` | 4 | `vs_cart`, `vs_addresses`, `vs_connections`, `vs_queue`, `vs_storage` |
