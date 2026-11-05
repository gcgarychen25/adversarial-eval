"""Thin CLI: cheat-agent + cheat-review.

Legacy bootstrap / CheatBench commands remain on ``python -m adversarial.real_eval``.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .env import apply_openai_compat, redact_config
from .harness import read_reward, run_task
from .reviewer import llm_review

DEFAULT_MODEL = 'deepseek-v4.1-flash'
# Harbor/LiteLLM needs a provider prefix. ``openai/`` means "OpenAI-compatible
# HTTP API" (Alibaba compatible-mode, real OpenAI, etc.) — not "OpenAI the model".
DEFAULT_AGENT_MODEL = DEFAULT_MODEL
HARNESSES = ('terminus-2', 'mini-swe-agent')
PROMPT_MODES = ('system', 'appended')
SETA_IDS = ('1012', '1018', '1025')
ATTACK_PROMPT = Path(__file__).resolve().parent.parent / 'prompts' / 'cheating_agent.md'


def normalize_harbor_model(model: str) -> str:
    """Bare ids (e.g. deepseek-v4.1-flash) → openai/<id> for Harbor/LiteLLM."""
    model = model.strip()
    if '/' not in model:
        return f'openai/{model}'
    return model


def _save_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + '\n')


def _stage_task(source: Path, dest: Path, *, append_prompt: bool) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    if append_prompt:
        instruction = dest / 'instruction.md'
        instruction.write_text(
            instruction.read_text().rstrip() + '\n\n' + ATTACK_PROMPT.read_text().strip() + '\n'
        )
    return dest


def resolve_task(task: str, root: Path) -> Path:
    """Accept a Harbor task directory, or a bootstrapped Seta id (1012/1018/1025)."""
    path = Path(task).expanduser()
    if path.is_dir() and (path / 'task.toml').is_file():
        return path.resolve()
    if task in SETA_IDS:
        cases_path = root / 'attacker' / 'cases.json'
        if not cases_path.is_file():
            raise FileNotFoundError(
                f'Seta id {task} needs bootstrap data at {cases_path}. '
                f'Run: python -m adversarial.real_eval bootstrap --root {root}'
            )
        cases = {c['task_id']: Path(c['task_path']) for c in json.loads(cases_path.read_text())}
        if task not in cases or not cases[task].is_dir():
            raise FileNotFoundError(f'No original_task path for Seta id {task} in {cases_path}')
        return cases[task].resolve()
    raise ValueError(
        f'--task must be a Harbor task directory (with task.toml) or one of {SETA_IDS}; got {task!r}'
    )


def harbor_agent_for(
    harness: str,
    prompt_mode: str,
    *,
    api_base: str | None = None,
) -> tuple[str, dict | None]:
    kwargs: dict = {}
    if harness == 'terminus-2':
        if prompt_mode == 'system':
            agent = 'adversarial.stress_agent:StressTerminus2'
            kwargs.update({
                'max_turns': 20,
                'enable_summarize': False,
                'store_all_messages': True,
            })
        else:
            agent = 'terminus-2'
        if api_base:
            kwargs['api_base'] = api_base
        return agent, (kwargs or None)
    if harness == 'mini-swe-agent':
        if prompt_mode == 'system':
            raise ValueError(
                'mini-swe-agent has no system-prompt stress adapter yet. '
                'Use --harness terminus-2 (default) for system injection, '
                'or --prompt-mode appended with --harness mini-swe-agent.'
            )
        # mini-swe-agent picks up OPENAI_BASE_URL / OPENAI_API_KEY from the environment.
        return 'mini-swe-agent', None
    raise ValueError(f'Unknown harness: {harness}')


def cmd_cheat_agent(args: argparse.Namespace) -> int:
    cfg = apply_openai_compat(base_url=args.base_url, key_env=args.key_env, require_key=not args.dry_run)
    root = args.root.resolve()
    source = resolve_task(args.task, root)
    agent, agent_kwargs = harbor_agent_for(
        args.harness, args.prompt_mode, api_base=cfg.base_url,
    )
    stage_name = source.name if source.name not in ('original_task',) else Path(args.task).name
    if args.task in SETA_IDS:
        stage_name = args.task
    stage = (args.stage_dir or (root / 'attacker' / 'staged' / stage_name)).resolve()
    staged = _stage_task(source, stage, append_prompt=(args.prompt_mode == 'appended'))
    jobs = (args.jobs_dir or (root / 'results' / 'cheat_jobs')).resolve()
    label = args.job_name or f'cheat-{stage_name}'
    model = normalize_harbor_model(args.model)

    run = run_task(
        staged,
        jobs,
        agent=agent,
        model=model,
        label=label,
        env_name=args.env,
        dry_run=args.dry_run,
        timeout=args.timeout,
        agent_kwargs=agent_kwargs,
    )
    run.update({
        'task_arg': args.task,
        'task_source': str(source),
        'staged_task': str(staged),
        'harness': args.harness,
        'prompt_mode': args.prompt_mode,
        'harbor_agent': agent,
        'endpoint': redact_config(cfg),
    })
    if args.prompt_mode == 'system':
        from .system_prompt import SYSTEM_PROMPT
        import hashlib
        run['system_prompt_sha256'] = hashlib.sha256(SYSTEM_PROMPT.read_bytes()).hexdigest()

    out = jobs / 'last_cheat_agent.json'
    _save_json(out, run)
    print(json.dumps(run, indent=2))
    print(f'\nSaved summary: {out}', file=sys.stderr)
    if run.get('trajectory'):
        print(f'Trajectory: {run["trajectory"]}', file=sys.stderr)
    if run.get('trial_dir'):
        print(f'Trial dir:  {run["trial_dir"]}', file=sys.stderr)
    if run.get('reward') is not None:
        print(f'Reward:     {run["reward"]}', file=sys.stderr)
    if args.dry_run:
        return 0
    return 0 if run.get('status') in ('ok',) else 1


def cmd_cheat_review(args: argparse.Namespace) -> int:
    cfg = apply_openai_compat(base_url=args.base_url, key_env=args.key_env, require_key=True)
    trial = Path(args.trial).expanduser().resolve()
    task = Path(args.task).expanduser().resolve()
    if not trial.is_dir():
        raise FileNotFoundError(f'--trial is not a directory: {trial}')
    if not (task / 'task.toml').is_file():
        # Allow Seta id + --root
        if args.task in SETA_IDS:
            task = resolve_task(args.task, args.root.resolve())
        else:
            raise ValueError(f'--task must be a Harbor task directory or Seta id; got {args.task!r}')

    reward = read_reward(trial)
    if reward is None or reward < 1:
        report = {
            'skipped': True,
            'reason': 'reward is missing or < 1; cheat-review only scores reward>=1 traces',
            'reward': reward,
            'trial_dir': str(trial),
            'task': str(task),
            'verdict': None,
        }
        print(json.dumps(report, indent=2))
        return 0

    verdict = llm_review(
        task,
        trial,
        model=args.model,
        base_url=cfg.base_url,
        key_env='OPENAI_API_KEY',
    )
    # Team-facing binary, keep raw verdict for audit.
    raw = verdict.get('verdict')
    if raw == 'confirmed_cheat':
        binary = 'exploit'
    elif raw == 'legitimate_solution':
        binary = 'legitimate'
    else:
        binary = 'inconclusive'
    report = {
        'skipped': False,
        'binary': binary,
        'verdict': verdict,
        'reward': reward,
        'trial_dir': str(trial),
        'task': str(task),
        'endpoint': redact_config(cfg),
    }
    out = Path(args.output).expanduser().resolve() if args.output else trial / 'cheat_review.json'
    _save_json(out, report)
    print(json.dumps(report, indent=2))
    print(f'\nSaved review: {out}', file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='adversarial',
        description='Adversarial eval: cheating agent rollouts + cheating reviewer on Harbor trials',
    )
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('cheat-agent', help='Launch one cheating-agent Harbor rollout; save trajectory')
    p.add_argument('--task', required=True, help='Harbor task dir, or Seta id 1012/1018/1025 after bootstrap')
    p.add_argument(
        '--model',
        default=DEFAULT_MODEL,
        help=f'default: {DEFAULT_MODEL} (auto-prefixed openai/ for Harbor; means compatible API, not OpenAI-only)',
    )
    p.add_argument('--harness', choices=HARNESSES, default='terminus-2')
    p.add_argument(
        '--prompt-mode',
        choices=PROMPT_MODES,
        default='system',
        help='system=inject cheating_agent_system_v2 via StressTerminus2; appended=paste cheating_agent.md into instruction',
    )
    p.add_argument('--root', type=Path, default=Path('work'), help='workspace root (Seta bootstrap + defaults)')
    p.add_argument('--jobs-dir', type=Path, help='Harbor jobs output dir (default: work/results/cheat_jobs)')
    p.add_argument('--stage-dir', type=Path, help='where to copy/stage the task before run')
    p.add_argument('--job-name', help='Harbor job name prefix')
    p.add_argument('--env', default='docker')
    p.add_argument('--timeout', type=int, default=3600)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--base-url', help='OpenAI-compatible base URL (else OPENAI_BASE_URL / .env)')
    p.add_argument('--key-env', default='OPENAI_API_KEY', help='env var holding the API key')
    p.set_defaults(func=cmd_cheat_agent)

    p = sub.add_parser(
        'cheat-review',
        help='Review one saved Harbor trial (skips if reward < 1); binary legitimate|exploit',
    )
    p.add_argument('--trial', required=True, help='Harbor trial directory containing agent/ + verifier/')
    p.add_argument('--task', required=True, help='Harbor task dir used for that trial, or Seta id')
    p.add_argument('--model', default=DEFAULT_MODEL, help=f'default: {DEFAULT_MODEL}')
    p.add_argument('--root', type=Path, default=Path('work'))
    p.add_argument('--base-url', help='OpenAI-compatible base URL (else OPENAI_BASE_URL / .env)')
    p.add_argument('--key-env', default='OPENAI_API_KEY')
    p.add_argument('--output', type=Path, help='default: <trial>/cheat_review.json')
    p.set_defaults(func=cmd_cheat_review)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (RuntimeError, ValueError, FileNotFoundError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


def main_cheat_agent(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    return main(['cheat-agent', *argv])


def main_cheat_review(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    return main(['cheat-review', *argv])


if __name__ == '__main__':
    raise SystemExit(main())

