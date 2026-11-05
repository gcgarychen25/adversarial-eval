# Adversarial eval

Cheating **agent** (Harbor rollout) + cheating **reviewer** (post-hoc LLM judge on saved trials).

Default model: `deepseek-v4.1-flash` (Harbor: `openai/deepseek-v4.1-flash`).  
Default harness: Terminus-2 with system-prompt injection.  
`mini-swe-agent` is supported only with `--prompt-mode appended`.

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
python -m adversarial cheat-agent --task 1012 --model deepseek-v4.1-flash
```

**Real OpenAI / later Luma (OpenAI base URL)**

```bash
export OPENAI_API_KEY='...'
export OPENAI_BASE_URL='https://api.openai.com/v1'
python -m adversarial cheat-agent --task 1012 --model <model-id>
```


## Commands

```bash
# Cheating agent — one task, trajectory + reward under work/results/cheat_jobs/
python -m adversarial cheat-agent --task 1012
python -m adversarial cheat-agent --task 1012 --dry-run
python -m adversarial cheat-agent --task /path/to/harbor_task --model openai/qwen3.5-plus
python -m adversarial cheat-agent --task 1012 --harness mini-swe-agent --prompt-mode appended

# Cheating reviewer — skips reward < 1; binary legitimate | exploit
python -m adversarial cheat-review --trial work/results/cheat_jobs/<job>/<trial>/ --task 1012
```

Console scripts (after install): `adversarial`, `cheat-agent`, `cheat-review`.

## Layout

```text
adversarial/     # cheat-agent / cheat-review CLI + Harbor helpers
prompts/         # cheating_agent*.md, cheating_reviewer.md, cheatbench_reviewer.md
scripts/         # optional multi-model stress grid helper
tests/
work/            # local only (gitignored): bootstrap + Harbor jobs
```

Optional / legacy: `python -m adversarial.real_eval review` runs the separate **CheatBench** 6-trace classifier (not the Harbor cheat-reviewer).

## Reproduce a small pilot

Fresh output dir (do not reuse old absolute-path jobs):

```bash
# example: 1 model × 3 Seta tasks via the thin CLI
for t in 1012 1018 1025; do
  python -m adversarial cheat-agent --task "$t" --jobs-dir "work/results/pilot_$(date +%Y%m%d)"
done
# then for each trial with reward 1:
python -m adversarial cheat-review --trial <trial_dir> --task <id>
```

Multi-model grid helper (still optional): `scripts/run_stress_comparison.py`.

## What is intentionally not in git

- `work/` Harbor trials, HF cache, prior stress pilots  
- `.env` and API keys  
- local `*.log`

Teammates bootstrap + run with their own endpoint.
