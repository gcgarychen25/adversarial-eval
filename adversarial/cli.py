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
from .review_harness import REVIEW_HARNESSES, run_harbor_review
from .reviewer import binary_label, llm_review
from .system_prompt import (
    CHEATING_AGENT_NAME,
    DEFAULT_VERSION,
    agent_system_prompt_path,
    ensure_generation,
    list_policy_versions,
    stage_skill_for_harbor,
)

DEFAULT_MODEL = 'deepseek-v4-pro-0813'
# Harbor/LiteLLM needs a provider prefix. ``openai/`` means "OpenAI-compatible
# HTTP API" (Alibaba compatible-mode, real OpenAI, etc.) — not "OpenAI the model".
DEFAULT_AGENT_MODEL = DEFAULT_MODEL
HARNESSES = ('terminus-2', 'mini-swe-agent', 'codex')
# Harnesses that accept Harbor ``--skill`` (capabilities.skills=True).
SKILL_HARNESSES = frozenset({'terminus-2', 'codex'})
# Harnesses without a Terminus system-prompt adapter: thin prompt goes into instruction.md.
INSTRUCTION_INJECT_HARNESSES = frozenset({'codex'})
PROMPT_MODES = ('system', 'appended')
SETA_IDS = ('1012', '1018', '1025')
# TB2 tasks analyzed for Terminal Wrench but not in the 331 hackable set.
CLEAN_IDS = ('cancel-async-tasks', 'openssl-selfsigned-cert', 'nginx-request-logging')
BOOTSTRAP_IDS = SETA_IDS + CLEAN_IDS
ATTACK_PROMPT = (
    Path(__file__).resolve().parent.parent
    / 'instruction' / 'v1' / 'cheating-agent' / 'prompt' / 'system.md'
)


def normalize_harbor_model(model: str) -> str:
    """Bare ids (e.g. deepseek-v4-pro-0813) → openai/<id> for Harbor/LiteLLM."""
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
    """Accept a Harbor task directory, or a bootstrapped id (vulnerable Seta / clean TB2)."""
    path = Path(task).expanduser()
    if path.is_dir() and (path / 'task.toml').is_file():
        return path.resolve()
    if task in BOOTSTRAP_IDS:
        cases_path = root / 'attacker' / 'cases.json'
        if not cases_path.is_file():
            raise FileNotFoundError(
                f'Task id {task} needs bootstrap data at {cases_path}. '
                f'Run: python -m adversarial.real_eval bootstrap --root {root} '
                f'(or bootstrap-clean for TB2 negatives)'
            )
        cases = {c['task_id']: Path(c['task_path']) for c in json.loads(cases_path.read_text())}
        if task not in cases or not cases[task].is_dir():
            raise FileNotFoundError(f'No task_path for id {task} in {cases_path}')
        return cases[task].resolve()
    raise ValueError(
        f'--task must be a Harbor task directory (with task.toml) or one of {BOOTSTRAP_IDS}; got {task!r}'
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
                'mini-swe-agent has no system-prompt stress adapter and '
                'capabilities.skills=False. Prefer --harness codex (skills) or '
                'terminus-2 (system+skills), or --prompt-mode appended with mini-swe-agent.'
            )
        # mini-swe-agent picks up OPENAI_BASE_URL / OPENAI_API_KEY from the environment.
        return 'mini-swe-agent', None
    if harness == 'codex':
        # Codex supports Harbor --skill (copies into ~/.agents/skills; mount stays
        # /harbor/skills). No StressTerminus adapter — thin prompt is injected into
        # instruction.md in cmd_cheat_agent when prompt_mode=system.
        # Auth/base URL via OPENAI_API_KEY + OPENAI_BASE_URL (Harbor ModelConnection).
        return 'codex', None
    raise ValueError(f'Unknown harness: {harness}')


def cmd_cheat_agent(args: argparse.Namespace) -> int:
    cfg = apply_openai_compat(base_url=args.base_url, key_env=args.key_env, require_key=not args.dry_run)
    root = args.root.resolve()
    source = resolve_task(args.task, root)
    agent, agent_kwargs = harbor_agent_for(
        args.harness, args.prompt_mode, api_base=cfg.base_url,
    )
    stage_name = source.name if source.name not in ('original_task',) else Path(args.task).name
    if args.task in BOOTSTRAP_IDS:
        stage_name = args.task
    stage = (args.stage_dir or (root / 'attacker' / 'staged' / stage_name)).resolve()
    # Appended mode always pastes v1 attack prompt. Codex system mode also needs the
    # thin prompt in instruction.md (no StressTerminus2 adapter).
    append_legacy = args.prompt_mode == 'appended'
    staged = _stage_task(source, stage, append_prompt=append_legacy)
    jobs = (args.jobs_dir or (root / 'runs' / 'cheat')).resolve()
    label = args.job_name or f'cheat-{stage_name}'
    model = normalize_harbor_model(args.model)

    # System mode: instruction/vN/cheating-agent/{prompt,skill}
    skills = None
    skill_staged = None
    policy_version = args.policy_version
    if args.prompt_mode == 'system':
        ensure_generation(policy_version, role='agent')
        if args.harness in SKILL_HARNESSES:
            skill_stage_root = (args.skill_stage_dir or (root / 'staging' / 'skills' / 'agent')).resolve()
            skill_staged = stage_skill_for_harbor(
                CHEATING_AGENT_NAME, skill_stage_root, version=policy_version,
            )
            skills = [skill_staged]
        if args.harness in INSTRUCTION_INJECT_HARNESSES:
            thin = agent_system_prompt_path(policy_version).read_text().strip()
            instruction = staged / 'instruction.md'
            instruction.write_text(
                instruction.read_text().rstrip()
                + '\n\n--- adversarial thin prompt (system mode; activate Harbor skill) ---\n'
                + thin
                + '\n'
            )
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
        skills=skills,
        policy_version=policy_version if args.prompt_mode == 'system' else None,
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
        import hashlib
        prompt_path = agent_system_prompt_path(policy_version)
        run['system_prompt_sha256'] = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
        run['system_prompt'] = str(prompt_path)
        run['skill'] = str(skill_staged) if skill_staged else None
        run['policy_version'] = policy_version
        run['skill_name'] = CHEATING_AGENT_NAME if skill_staged else None
        run['policy_delivery'] = (
            'harbor-skill+instruction-inject' if args.harness in INSTRUCTION_INJECT_HARNESSES
            else ('harbor-skill' if skill_staged else 'none')
        )

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
    need_key = not (args.harness != 'llm' and args.dry_run)
    cfg = apply_openai_compat(base_url=args.base_url, key_env=args.key_env, require_key=need_key)
    trial = Path(args.trial).expanduser().resolve()
    root = args.root.resolve()
    if not trial.is_dir():
        raise FileNotFoundError(f'--trial is not a directory: {trial}')
    task_arg = args.task
    task_path = Path(task_arg).expanduser()
    if task_path.is_dir() and (task_path / 'task.toml').is_file():
        task = task_path.resolve()
    elif task_arg in BOOTSTRAP_IDS:
        task = resolve_task(task_arg, root)
    else:
        raise ValueError(f'--task must be a Harbor task directory or bootstrap id; got {task_arg!r}')

    reward = read_reward(trial)
    has_trace = (
        (trial / 'agent' / 'trajectory.json').is_file()
        or (trial / 'trajectory.json').is_file()
        or (trial / 'trial.log').is_file()
        or (trial / 'agent.log').is_file()
    )
    if not has_trace:
        report = {
            'skipped': True,
            'reason': 'no trajectory/logs found under the trial; nothing to review',
            'reward': reward,
            'trial_dir': str(trial),
            'task': str(task),
            'harness': args.harness,
            'verdict': None,
        }
        print(json.dumps(report, indent=2))
        return 0
    if args.require_pass and (reward is None or reward < 1):
        report = {
            'skipped': True,
            'reason': '--require-pass set; reward is missing or < 1',
            'reward': reward,
            'trial_dir': str(trial),
            'task': str(task),
            'harness': args.harness,
            'verdict': None,
        }
        print(json.dumps(report, indent=2))
        return 0

    out = Path(args.output).expanduser().resolve() if args.output else trial / 'cheat_review.json'

    if args.harness == 'llm':
        verdict = llm_review(
            task,
            trial,
            model=args.model,
            base_url=cfg.base_url,
            key_env='OPENAI_API_KEY',
        )
        report = {
            'skipped': False,
            'binary': binary_label(verdict, reward),
            'verdict': verdict,
            'reward': reward,
            'attack_passed': reward is not None and reward >= 1,
            'trial_dir': str(trial),
            'task': str(task),
            'harness': 'llm',
            'endpoint': redact_config(cfg),
        }
        _save_json(out, report)
        print(json.dumps(report, indent=2))
        print(f'\nSaved review: {out}', file=sys.stderr)
        return 0

    # Evidence-mounted Docker reviewer (terminus-2 / mini-swe-agent / future harnesses).
    jobs = (args.jobs_dir or (root / 'runs' / 'review')).resolve()
    workspace = (
        args.workspace_dir
        or (root / 'staging' / 'review_workspaces' / f'{trial.name}-{args.harness}')
    ).resolve()
    model = normalize_harbor_model(args.model)
    ensure_generation(args.policy_version, role='reviewer')
    skill_stage_root = (args.skill_stage_dir or (root / 'staging' / 'skills' / 'reviewer')).resolve()
    run = run_harbor_review(
        trial,
        task,
        harness=args.harness,
        model=model,
        jobs_dir=jobs,
        workspace_dir=workspace,
        api_base=cfg.base_url,
        env_name=args.env,
        dry_run=args.dry_run,
        timeout=args.timeout,
        job_name=args.job_name,
        policy_version=args.policy_version,
        skill_stage_dir=skill_stage_root,
    )
    if args.dry_run:
        print(json.dumps(run, indent=2))
        return 0

    verdict = run.get('parsed_verdict')
    if not verdict:
        print(json.dumps(run, indent=2))
        print('ERROR: harness review failed to produce a parsed verdict', file=sys.stderr)
        return 1

    report = {
        'skipped': False,
        'binary': binary_label(verdict, reward),
        'verdict': verdict,
        'reward': reward,
        'attack_passed': reward is not None and reward >= 1,
        'trial_dir': str(trial),
        'task': str(task),
        'harness': args.harness,
        'harbor_agent': run.get('harbor_agent'),
        'review_workspace': run.get('review_workspace'),
        'review_trial_dir': run.get('trial_dir'),
        'endpoint': redact_config(cfg),
    }
    _save_json(out, report)
    _save_json(jobs / 'last_cheat_review.json', {**run, 'report_path': str(out)})
    print(json.dumps(report, indent=2))
    print(f'\nSaved review: {out}', file=sys.stderr)
    return 0 if run.get('status') in ('ok',) else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='adversarial',
        description='Adversarial eval: cheating agent rollouts + cheating reviewer on Harbor trials',
    )
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('cheat-agent', help='Launch one cheating-agent Harbor rollout; save trajectory')
    p.add_argument(
        '--task',
        required=True,
        help='Harbor task dir, or bootstrap id (vulnerable: 1012/1018/1025; clean: cancel-async-tasks/openssl-selfsigned-cert/nginx-request-logging)',
    )
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
        help='system=matched instruction/vN generation (prompt + skill); appended=paste instruction/v1/.../prompt',
    )
    p.add_argument(
        '--policy-version',
        choices=list_policy_versions(),
        default=DEFAULT_VERSION,
        help=f'policy generation under instruction/vN (default: {DEFAULT_VERSION})',
    )
    p.add_argument('--skill-stage-dir', type=Path, help='where to stage Harbor skill upload dir')
    p.add_argument('--root', type=Path, default=Path('work'), help='workspace root (Seta bootstrap + defaults)')
    p.add_argument('--jobs-dir', type=Path, help='Harbor jobs output dir (default: work/runs/cheat)')
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
        help='Review one saved Harbor trial (pass or failed attempt); binary exploit|attempt|legitimate|inconclusive',
    )
    p.add_argument('--trial', required=True, help='Harbor trial directory containing agent/ + verifier/')
    p.add_argument('--task', required=True, help='Harbor task dir used for that trial, or bootstrap id')
    p.add_argument('--model', default=DEFAULT_MODEL, help=f'default: {DEFAULT_MODEL}')
    p.add_argument(
        '--harness',
        choices=REVIEW_HARNESSES,
        default='terminus-2',
        help='llm=host one-shot; terminus-2/codex=evidence-mounted Docker + Harbor --skill; mini-swe-agent=appended policy (no skills)',
    )
    p.add_argument(
        '--policy-version',
        choices=list_policy_versions(),
        default=DEFAULT_VERSION,
        help=f'policy generation under instruction/vN (default: {DEFAULT_VERSION})',
    )
    p.add_argument('--skill-stage-dir', type=Path, help='where to stage Harbor skill upload dir')
    p.add_argument('--root', type=Path, default=Path('work'))
    p.add_argument('--jobs-dir', type=Path, help='Harbor review jobs dir (default: work/runs/review)')
    p.add_argument('--workspace-dir', type=Path, help='where to materialize the evidence-mounted review task')
    p.add_argument('--job-name', help='Harbor review job name prefix')
    p.add_argument('--env', default='docker')
    p.add_argument('--timeout', type=int, default=3600)
    p.add_argument('--dry-run', action='store_true', help='prepare/print Harbor review command without running')
    p.add_argument(
        '--require-pass',
        action='store_true',
        help='legacy gate: skip trials unless trusted reward >= 1 (default reviews failed attempts too)',
    )
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

