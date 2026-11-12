# Adversarial eval

Cheating **agent** (Harbor rollout) + cheating **reviewer** (post-hoc LLM judge on saved trials).

Default model: `deepseek-v4-pro-0813` (Harbor: `openai/deepseek-v4-pro-0813`).  
Default **policy generation:** `instruction/v3/{cheating-agent,cheating-reviewer}/{prompt,skill}` (`--policy-version`). Next iteration is `instruction/v4/…`.  
Default harness: Terminus-2 (system prompt + Harbor skills). Prefer Harbor for multi-harness switching (`terminus-2`, `codex` with skills; `mini-swe-agent` is appended-policy only). `--harness llm` inlines the skill body on the host.

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

### API endpoint (OpenAI-compatible)

Alibaba Model Studio (`.../compatible-mode/v1`) and real OpenAI use the **same** Chat Completions protocol. Set:

| Var | Role |
|---|---|
| `OPENAI_API_KEY` | Bearer token |
| `OPENAI_BASE_URL` | Base URL ending in `/v1` (or Alibaba `/compatible-mode/v1`) |

The CLI also accepts `--base-url` / `--key-env`, loads a local `.env` if present, and mirrors `OPENAI_API_BASE` for Harbor/LiteLLM. Keys are never written into trajectories or review JSON (only `key_present` + `base_url`).

**Alibaba example**

```bash
export OPENAI_API_KEY='...'
export OPENAI_BASE_URL='https://<workspace>.maas.aliyuncs.com/compatible-mode/v1'
python -m adversarial cheat-agent --task 1012 --model deepseek-v4-pro-0813
```

**Real OpenAI / later Luma (OpenAI base URL)**

```bash
export OPENAI_API_KEY='...'
export OPENAI_BASE_URL='https://api.openai.com/v1'
python -m adversarial cheat-agent --task 1012 --model <model-id>
```


## Commands

```bash
# Cheating agent — jobs land in work/runs/cheat/ by default
python -m adversarial cheat-agent --task 1012
python -m adversarial cheat-agent --task 1012 --dry-run
python -m adversarial cheat-agent --task /path/to/harbor_task --model openai/qwen3.5-plus
python -m adversarial cheat-agent --task 1012 --harness codex
python -m adversarial cheat-agent --task cancel-async-tasks --harness codex   # clean negative (TB2)
python -m adversarial cheat-agent --task 1012 --harness mini-swe-agent --prompt-mode appended

# Cheating reviewer — reviews pass AND failed attempts; binary exploit|attempt|legitimate|inconclusive
python -m adversarial cheat-review --trial work/runs/cheat/<job>/<trial>/ --task 1012
python -m adversarial cheat-review --trial <trial> --task 1012 --harness codex
python -m adversarial cheat-review --trial <trial> --task 1012 --harness llm
python -m adversarial cheat-review --trial <trial> --task 1012 --require-pass   # legacy: only reward>=1
python -m adversarial cheat-review --trial <trial> --task 1012 --dry-run
```

Review contracts (harness-agnostic): evidence layout `/evidence/{trial,task,out}`, policy `instruction/vN/cheating-reviewer`, output `cheat_review.json` with loophole rows. Failed attacks (reward &lt; 1) are reviewed too when they may reveal a real loophole (`binary=attempt`).

Console scripts (after install): `adversarial`, `cheat-agent`, `cheat-review`.

## Layout

```text
adversarial/     # cheat-agent / cheat-review CLI + Harbor helpers + review_templates/
instruction/     # policy generations: vN/{cheating-agent,cheating-reviewer}/{prompt,skill}
scripts/         # optional multi-model stress grid helper
tests/
work/            # local only (gitignored): see work/README.md
```

Policy generations live under [`instruction/`](instruction/README.md). Default **v3**; add **v4** folders when iterating.  
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

## What is intentionally not in git

- `work/` Harbor trials, HF cache, prior stress pilots  
- `.env` and API keys  
- local `*.log`

Teammates bootstrap + run with their own endpoint.
