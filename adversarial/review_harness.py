"""Harness-agnostic cheating reviewer: fixed evidence layout, pluggable agent launcher."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .harness import read_reward, run_task
from .reviewer import parse_verdict
from .system_prompt import (
    CHEATING_REVIEWER_NAME,
    DEFAULT_VERSION,
    reviewer_policy_text,
    stage_skill_for_harbor,
)

TEMPLATE_ROOT = Path(__file__).resolve().parent / 'review_templates' / 'workspace'

REVIEW_HARNESSES = ('llm', 'terminus-2', 'mini-swe-agent', 'codex')
# Harbor agents that declare capabilities.skills=True and accept --skill.
SKILL_REVIEW_HARNESSES = frozenset({'terminus-2', 'codex'})

# Skip bulky / low-value binaries when packing the review image context.
_SKIP_NAME_SUFFIXES = ('.cast',)
_SKIP_NAMES = {'recording.cast'}

_TASK_FILES = (
    'instruction.md',
    'task.toml',
    'tests/test_outputs.py',
    'tests/test.sh',
    'tests/test.py',
)


def template_root() -> Path:
    if not (TEMPLATE_ROOT / 'task.toml').is_file():
        raise FileNotFoundError(f'Review workspace template missing: {TEMPLATE_ROOT}')
    return TEMPLATE_ROOT


def _should_skip(path: Path) -> bool:
    if path.name in _SKIP_NAMES:
        return True
    return any(path.name.endswith(suf) for suf in _SKIP_NAME_SUFFIXES)


def _copy_tree_filtered(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.rglob('*'):
        if not item.is_file():
            continue
        if _should_skip(item):
            continue
        rel = item.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def _copy_task_evidence(task: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for rel in _TASK_FILES:
        src = task / rel
        if src.is_file():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    # Prefer whole tests/ when present (covers extra helpers).
    tests = task / 'tests'
    if tests.is_dir():
        for item in tests.rglob('*'):
            if item.is_file():
                target = dest / 'tests' / item.relative_to(tests)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)


def prepare_review_workspace(
    trial: Path,
    task: Path,
    dest: Path,
    *,
    append_policy: bool = False,
    policy_version: str = DEFAULT_VERSION,
) -> Path:
    """Build a thin Harbor review task with /evidence populated from host paths.

    Layout inside the container (via Dockerfile COPY):
      /evidence/trial  — finished cheating-agent trial
      /evidence/task   — original task definition
      /evidence/out    — writable output for cheat_review.json
    """
    trial = trial.resolve()
    task = task.resolve()
    if not trial.is_dir():
        raise FileNotFoundError(f'Trial directory missing: {trial}')
    if not (task / 'task.toml').is_file():
        raise ValueError(f'Not a Harbor task directory: {task}')

    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(template_root(), dest)

    evidence = dest / 'environment' / 'evidence'
    _copy_tree_filtered(trial, evidence / 'trial')
    _copy_task_evidence(task, evidence / 'task')
    (evidence / 'out').mkdir(parents=True, exist_ok=True)
    (evidence / 'out' / '.gitkeep').write_text('')

    # Ensure Docker always has a COPY source even if trial was empty-ish.
    (evidence / 'trial').mkdir(parents=True, exist_ok=True)
    (evidence / 'task').mkdir(parents=True, exist_ok=True)

    instruction = dest / 'instruction.md'
    body = instruction.read_text()
    if append_policy:
        body = (
            body.rstrip()
            + '\n\n--- reviewer policy (inline fallback; prefer /harbor/skills/cheating-reviewer/SKILL.md) ---\n'
            + reviewer_policy_text(policy_version).strip()
            + '\n'
        )
        instruction.write_text(body)

    test_sh = dest / 'tests' / 'test.sh'
    test_sh.chmod(test_sh.stat().st_mode | 0o111)
    return dest.resolve()


def harbor_review_agent_for(
    harness: str,
    *,
    api_base: str | None = None,
) -> tuple[str, dict | None]:
    """Map review --harness to a Harbor agent id + kwargs. ``llm`` is host-only."""
    if harness == 'llm':
        raise ValueError('harness=llm does not use Harbor; call llm_review on the host')
    kwargs: dict = {}
    if harness == 'terminus-2':
        agent = 'adversarial.review_agent:ReviewTerminus2'
        kwargs.update({
            'max_turns': 25,
            'enable_summarize': False,
            'store_all_messages': True,
        })
        if api_base:
            kwargs['api_base'] = api_base
        return agent, kwargs
    if harness == 'mini-swe-agent':
        # No system-prompt adapter yet; prepare_review_workspace(..., append_policy=True).
        return 'mini-swe-agent', None
    if harness == 'codex':
        # Skills via Harbor --skill; thin reviewer prompt lives in review instruction.md
        # (no ReviewTerminus2). Auth via OPENAI_API_KEY / OPENAI_BASE_URL.
        return 'codex', None
    raise ValueError(f'Unknown review harness: {harness!r}; expected one of {REVIEW_HARNESSES}')


def find_review_json(review_trial: Path) -> Path | None:
    """Locate cheat_review.json written by the reviewer agent inside a Harbor review trial."""
    candidates = [
        review_trial / 'agent' / 'cheat_review.json',
        review_trial / 'artifacts' / 'cheat_review.json',
        review_trial / 'cheat_review.json',
    ]
    for path in candidates:
        if path.is_file():
            return path
    # Fallback: search under the review trial.
    matches = sorted(review_trial.rglob('cheat_review.json'))
    return matches[0] if matches else None


def load_and_parse_review(path: Path, *, reward: float | None) -> dict:
    raw = path.read_text(encoding='utf-8')
    # Allow a wrapper report or bare verdict object.
    obj = json.loads(raw)
    if isinstance(obj, dict) and 'verdict' in obj and isinstance(obj['verdict'], dict):
        # Our host report shape: {"verdict": {...}, "binary": ...}
        inner = obj['verdict']
        if isinstance(inner, dict) and 'verdict' in inner:
            return parse_verdict(json.dumps(inner), reward)
    if isinstance(obj, dict) and (
        obj.get('status') in {
            'loophole_found', 'legitimate_solution', 'failed_or_unproven', 'inconclusive',
        }
        or obj.get('verdict') in {
            'confirmed_cheat', 'legitimate_solution', 'failed_or_unproven', 'inconclusive',
        }
    ):
        return parse_verdict(json.dumps(obj), reward)
    return parse_verdict(raw, reward)


def run_harbor_review(
    trial: Path,
    task: Path,
    *,
    harness: str,
    model: str,
    jobs_dir: Path,
    workspace_dir: Path,
    api_base: str | None = None,
    env_name: str = 'docker',
    dry_run: bool = False,
    timeout: int = 3600,
    job_name: str | None = None,
    policy_version: str = DEFAULT_VERSION,
    skill_stage_dir: Path | None = None,
) -> dict:
    """Prepare evidence workspace and launch a Harbor review agent."""
    # mini-swe: no skills support → inline full policy. Codex: skills + thin instruction
    # already points at /harbor/skills/cheating-reviewer/SKILL.md.
    append_policy = harness == 'mini-swe-agent'
    workspace = prepare_review_workspace(
        trial, task, workspace_dir, append_policy=append_policy,
        policy_version=policy_version,
    )
    agent, agent_kwargs = harbor_review_agent_for(harness, api_base=api_base)
    label = job_name or f'review-{trial.name}'
    skills: list[Path] | None = None
    staged_skill: Path | None = None
    if harness in SKILL_REVIEW_HARNESSES:
        stage_root = Path(skill_stage_dir) if skill_stage_dir else (workspace_dir.parent / 'skills_staging')
        staged_skill = stage_skill_for_harbor(
            CHEATING_REVIEWER_NAME, stage_root, version=policy_version,
        )
        skills = [staged_skill]
    run = run_task(
        workspace,
        jobs_dir,
        agent=agent,
        model=model,
        label=label,
        env_name=env_name,
        dry_run=dry_run,
        timeout=timeout,
        agent_kwargs=agent_kwargs,
        skills=skills,
        policy_version=policy_version,
    )
    run.update({
        'review_workspace': str(workspace),
        'source_trial': str(trial.resolve()),
        'source_task': str(task.resolve()),
        'harness': harness,
        'harbor_agent': agent,
        'skill': str(staged_skill) if staged_skill else None,
        'policy_version': policy_version,
        'skill_name': CHEATING_REVIEWER_NAME if staged_skill else None,
        'policy_delivery': 'harbor-skill' if staged_skill else 'appended-instruction',
    })
    if dry_run:
        return run

    review_trial = Path(run['trial_dir']) if run.get('trial_dir') else None
    if review_trial is None:
        run['status'] = 'error'
        run['warning'] = run.get('warning') or 'Review Harbor job produced no trial_dir'
        return run

    found = find_review_json(review_trial)
    if found is None:
        run['status'] = 'error'
        run['warning'] = 'Reviewer did not write cheat_review.json under the review trial'
        return run

    # Use the *original* cheating trial reward for verdict consistency checks.
    cheat_reward = read_reward(trial)
    verdict = load_and_parse_review(found, reward=cheat_reward)
    run['review_json_path'] = str(found)
    run['parsed_verdict'] = verdict
    return run
