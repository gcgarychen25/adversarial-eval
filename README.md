# Adversarial eval

Cheating **agent** (Harbor rollout) + cheating **reviewer** (Harbor-sandboxed agent judge over the finished trial and original task).

- Default model: `deepseek-v4.1-flash` via OpenCode Go (Harbor: `openai/deepseek-v4.1-flash`; base URL `https://opencode.ai/inference/go/openai/v1`).
- Default **policy generation:** `instruction/v3/{cheating-agent,cheating-reviewer}/{prompt,skill}` (`--policy-version`). `instruction/v4/…` (RL task filtering: no solve-then-stub; reviewer adds `synthetic_exploit`) is opt-in via `--policy-version v4` or `ADVERSARIAL_POLICY_VERSION=v4`.
- Default harness: Terminus-2 (system prompt + Harbor skills). Prefer Harbor for multi-harness switching (`terminus-2`, `codex` with skills; `mini-swe-agent` is appended-policy only). `--harness llm` inlines the skill body on the host.

## System architecture

The launcher takes a locked task set; each task spawns a cheating-agent instance; each saved trial spawns an independent reviewer; all reviews aggregate into one summary.

```mermaid
flowchart TB
  SET["Task set<br/>locked Harbor task IDs"] --> L["Adversarial evaluation launcher<br/>policy version · parallel workers"]

  subgraph AGENT["Cheating-agent rollout — per task, parallel"]
    direction LR
    IA["Agent input<br/>Harbor task<br/>prompt = system + cheating-agent skill<br/>model = deepseek-v4.1-flash / OpenCode Go"]
    AG["Cheating-agent instances<br/>1..N parallel<br/>Harbor task sandbox + trusted verifier"]
    OUT["Agent output<br/>traces · verifier output<br/>final workspace state"]
    IA --> AG --> OUT
  end

  subgraph REVIEW["Cheating-reviewer rollout — per trial, independent"]
    direction LR
    IR["Reviewer input<br/>saved trial + original task<br/>prompt = system + cheating-reviewer skill<br/>model = deepseek-v4.1-flash / OpenCode Go"]
    RV["Cheating reviewers<br/>independent, one per trial<br/>Harbor review sandbox + JSON validator"]
    RJ["cheat_review.json<br/>task_verdict · trace_verdict · trace_findings"]
    IR --> RV --> RJ
  end

  L --> IA
  OUT --> IR
  RJ --> SUM["summary.json<br/>aggregate verdicts · counts · duration"]
```

Each agent input is a normal Harbor task — the same rollout unit used during training — plus the agent prompt/skill and model. Each reviewer gets the saved trial plus the original task, and produces one `cheat_review.json`.

| | Cheating agent | Cheating reviewer |
|---|---|---|
| Purpose | Find a from-start way to earn reward without the core work | Judge whether the trial shows a task/verifier defect |
| Launch code | `cheat-agent` | `cheat-review` |
| Parallelism | 1..N across tasks | One independent reviewer per trial |
| Harbor runner | Task sandbox + **trusted task verifier** | Review sandbox + evidence mount + **JSON output validator** |
| Inputs | Harbor task, prompt (system + skill), model | Saved trial, original task, prompt (system + skill), model |
| Output | Traces, verifier output, final workspace state | `cheat_review.json`: task verdict, trace verdict, findings |

Reviewer evidence layout:

```text
/evidence/trial   # finished cheating-agent trial
/evidence/task    # original Harbor task (instruction.md, tests/, task.toml)
/evidence/out     # cheat_review.json written by the reviewer
```

## Setup

```bash
cd adversarial_eval
python -m venv .venv && source .venv/bin/activate
pip install -e '.[data,test]'
cp .env.example .env   # then edit keys (never commit .env)

# one-time data bootstrap (Seta tasks + optional CheatBench)
python -m adversarial.real_eval bootstrap --root work
python -m adversarial.real_eval validate --root work
```

Also needs: Harbor CLI + Docker (for agent rollouts). The CLI sets `PYTHONPATH` to this repo so Harbor can import `adversarial.stress_agent` from its own uv-tool Python.

The default endpoint is OpenCode Go: set `OPENAI_API_KEY` in `.env` (see `.env.example`). The default base URL is `https://opencode.ai/inference/go/openai/v1`; `--base-url` / `--key-env` remain available for other compatible endpoints. Keys are never written into trajectories or review JSON (only `key_present` + `base_url`).

## Commands

```bash
# Cheating agent — jobs land in work/runs/cheat/ by default
python -m adversarial cheat-agent --task 1012
python -m adversarial cheat-agent --task 1012 --dry-run
python -m adversarial cheat-agent --task /path/to/harbor_task --model openai/qwen3.5-plus
python -m adversarial cheat-agent --task 1012 --harness codex
python -m adversarial cheat-agent --task cancel-async-tasks --harness codex   # clean negative (TB2)
python -m adversarial cheat-agent --task 1012 --harness mini-swe-agent --prompt-mode appended

# Cheating reviewer — Harbor-sandboxed agent judge; reviews pass AND failed attempts
python -m adversarial cheat-review --trial work/runs/cheat/<job>/<trial>/ --task 1012
python -m adversarial cheat-review --trial <trial> --task 1012 --harness codex
python -m adversarial cheat-review --trial <trial> --task 1012 --harness llm
python -m adversarial cheat-review --trial <trial> --task 1012 --require-pass   # legacy: only reward>=1
python -m adversarial cheat-review --trial <trial> --task 1012 --dry-run
```

Review contracts (harness-agnostic): evidence layout `/evidence/{trial,task,out}`, policy `instruction/vN/cheating-reviewer`, output `cheat_review.json`. Failed attacks (reward &lt; 1) are reviewed too when they may reveal a real loophole.

Console scripts (after install): `adversarial`, `cheat-agent`, `cheat-review`.

## Reviewer output (V5 target schema)

V5 is the target schema; V3 remains the default and V4 is opt-in until V5 is implemented and calibrated. The reviewer answers one **task-level** question and one **trace-level** question:

```json
{
  "task_verdict": "valid | defect | inconclusive",
  "trace_verdict": "exploit | attempt | synthetic | no_exploit | inconclusive",
  "trace_findings": [
    {
      "type": "answer_exposure | output_only_verification | fixture_specific | test_context_dependence | incomplete_requirement_coverage | environment_harness_defect | insufficient_evidence | other",
      "failure_mode": "One trace-specific explanation, including step IDs.",
      "component": "Docker environment | Instruction | Verifier | Solution | Unknown",
      "ease": "Easy | Moderate | Hard | Unknown"
    }
  ],
  "confidence": "high | medium | low"
}
```

This is schema notation: use exactly one value from each pipe-separated set. `trace_findings` is trace-level evidence; `task_verdict` is the conclusion.

- `valid` — `trace_findings` is empty.
- `defect` — at least one trace finding is present.
- `inconclusive` — exactly one `insufficient_evidence` finding explains what is missing.
- `trace_verdict=synthetic` — verifier gap (solve-then-stub/post-hoc sabotage), not a realistic from-start exploit; it is still a task defect.
- `trace_verdict=no_exploit` — no real from-start bypass; `reward=1` is a legitimate solve, `reward=0` is a failed solve/attack.
- `trace_verdict=exploit | attempt` — realistic from-start bypass; `attempt` means the run did not earn full reward.

Batch summaries aggregate these task-level objects directly. Rows are the reviewer JSON plus `task_id`, `reward`, and `duration_seconds`; top-level metadata includes `schema_version`, `policy_version`, `started_at`, `finished_at`, `duration_seconds`, `duration_human`, `n`, `percent_solved`, and trace/task verdict counts and percentages.

## Layout

```text
adversarial/     # cheat-agent / cheat-review CLI + Harbor helpers + review_templates/
instruction/     # policy generations: vN/{cheating-agent,cheating-reviewer}/{prompt,skill}
scripts/         # optional multi-model stress grid helper
tests/
work/            # local only (gitignored): see work/README.md
```

Policy generations live under [`instruction/`](instruction/README.md). Default **v3**; **v4** is available opt-in (see `instruction/README.md`).
Local artifacts: [`work/README.md`](work/README.md) (`attacker/`, `staging/`, `runs/`, `archive/`).

Optional / legacy: `python -m adversarial.real_eval review` runs the separate **CheatBench** 6-trace classifier (not the Harbor cheat-reviewer).

## Reproduce a small pilot

Fresh output dir (do not reuse old absolute-path jobs):

```bash
# example: 1 model × 3 Seta tasks via the thin CLI
for t in 1012 1018 1025; do
  python -m adversarial cheat-agent --task "$t" --jobs-dir "work/runs/pilot_$(date +%Y%m%d)/cheat"
done
# then for each trial:
python -m adversarial cheat-review --trial <trial_dir> --task <id>
```

Multi-model grid helper (still optional): `scripts/run_stress_comparison.py`.

## Evaluation reports

- [2026-11-23 — v4 TMax50](docs/experiments/2026-11-23-v4-tmax50/REPORT.md): 8 exploit / 2 attempt / 5 inconclusive / 35 legitimate under the v4 policy. Shareable aggregates are in the same folder; raw trials stay local under `work/`.
