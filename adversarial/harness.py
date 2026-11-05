"""Execute genuine `harbor run` CLI trials and locate their real artifacts."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


def run_task(task: Path, jobs: Path, *, agent: str, model: str | None,
             label: str, env_name: str = 'docker', dry_run: bool = False,
             timeout: int = 3600, agent_kwargs: dict | None = None) -> dict:
    task = task.resolve()
    if not (task / 'task.toml').is_file() or not (task / 'instruction.md').is_file():
        raise ValueError(f'Not a Harbor task directory: {task}')
    jobs.mkdir(parents=True, exist_ok=True)
    name = re.sub(r'[^a-zA-Z0-9_-]+', '-', label)[:65]+'-'+str(time.time_ns())
    args = ['harbor', 'run', '--path', str(task), '--agent', agent,
            '--n-concurrent', '1', '--n-attempts', '1', '--env', env_name,
            '--jobs-dir', str(jobs.resolve()), '--job-name', name]
    if model:
        args.extend(['--model', model])
    for key, value in (agent_kwargs or {}).items():
        args.extend(['--agent-kwarg', f'{key}={json.dumps(value)}'])
    entry = {'task': task.name, 'agent': agent, 'model': model,
             'job_name': name, 'command': args, 'status': 'dry_run' if dry_run else 'running'}
    if dry_run:
        return entry
    if shutil.which('harbor') is None:
        raise RuntimeError('Harbor CLI missing. Install with: uv tool install harbor  (or pip install harbor)')
    log = jobs / f'{name}.console.log'
    # Harbor is usually a uv-tool install with its own interpreter. Custom agents
    # like ``adversarial.stress_agent:StressTerminus2`` must be importable there.
    env = os.environ.copy()
    project_root = str(Path(__file__).resolve().parent.parent)
    prev_pp = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = project_root + (os.pathsep + prev_pp if prev_pp else '')
    entry['pythonpath'] = env['PYTHONPATH']
    try:
        with log.open('w') as file:
            proc = subprocess.run(
                args, stdout=file, stderr=subprocess.STDOUT, text=True,
                timeout=timeout, check=False, env=env,
            )
        entry['exit_code'] = proc.returncode
    except subprocess.TimeoutExpired:
        entry['status'] = 'timeout'
        entry['console_log'] = str(log)
        return entry
    entry['console_log'] = str(log)
    job = jobs / name
    # Harbor can vary directory layout by version; search all within the dedicated
    # job path, never silently use an unrelated previous job.
    search_root = job if job.exists() else jobs
    trials = find_trials(search_root, name=name if search_root == jobs else None)
    if len(trials) == 1:
        trial = trials[0]
        entry['trial_dir'] = str(trial)
        result_file = trial / 'result.json'
        if result_file.is_file():
            result = json.loads(result_file.read_text())
            if result.get('exception_info'):
                entry['status'] = 'error'
                entry['exception_info'] = result['exception_info']
        entry['reward'] = read_reward(trial)
        entry['trajectory'] = str(trial / 'agent' / 'trajectory.json') if (trial / 'agent' / 'trajectory.json').exists() else None
    elif trials:
        entry['status'] = 'ambiguous'
        entry['warning'] = f'Found {len(trials)} possible trial directories: {trials}'
    entry['status'] = entry.get('status', 'running')
    if entry['status'] == 'running':
        entry['status'] = 'ok' if proc.returncode == 0 and len(trials) == 1 and entry.get('reward') is not None else 'error'
    if not trials:
        entry['warning'] = 'No per-trial artifact found; inspect console log and Harbor version.'
    return entry


def find_trials(folder: Path, name: str | None = None) -> list[Path]:
    if not folder.exists():
        return []
    candidates: list[Path] = []
    for p in folder.rglob('result.json'):
        if name and name not in str(p):
            continue
        d = p.parent
        if (d / 'agent').exists() or (d / 'verifier').exists() or (d / 'trial.log').exists() or (d / 'trajectory.json').exists():
            candidates.append(d)
    if not candidates:
        # When a crashed trial wrote verifier output but not result.json.
        for p in folder.rglob('reward.txt'):
            if p.parent.name == 'verifier' and (not name or name in str(p)):
                candidates.append(p.parent.parent)
    return sorted(set(candidates))


def read_reward(trial: Path) -> float | None:
    """Prefer Harbor result or verifier reward; never conflate a missing reward with zero."""
    for filename in ('verifier/reward.json', 'verifier/reward.txt', 'reward.txt'):
        p = trial / filename
        if not p.is_file():
            continue
        try:
            if p.suffix == '.txt':
                return float(p.read_text().strip())
            value = json.loads(p.read_text())
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, dict):
                for key in ('reward', 'score', 'accuracy'):
                    if isinstance(value.get(key), (int, float)):
                        return float(value[key])
                # Multiple reward metrics cannot be reduced safely.
        except (ValueError, TypeError):
            continue
    result = trial / 'result.json'
    if result.exists():
        try:
            obj = json.loads(result.read_text())
            ver = obj.get('verifier_result') or {}
            rewards = ver.get('rewards') if isinstance(ver, dict) else None
            if isinstance(rewards, dict):
                for k in ('reward', 'score', 'accuracy'):
                    if isinstance(rewards.get(k), (int, float)):
                        return float(rewards[k])
            if isinstance(obj.get('reward'), (int, float)):
                return float(obj['reward'])
        except (ValueError, TypeError):
            pass
    return None


def text_evidence(trial: Path, max_chars: int = 110_000) -> str:
    choices = [
        ('Agent trajectory', trial / 'agent' / 'trajectory.json'),
        ('ATIF trajectory', trial / 'trajectory.json'),
        ('Agent text log', trial / 'agent.log'),
        ('Terminal log', trial / 'trial.log'),
        ('Verifier stdout', trial / 'verifier' / 'test-stdout.txt'),
        ('Verifier stderr', trial / 'verifier' / 'test-stderr.txt'),
    ]
    parts = []
    remaining = max_chars
    for title, file in choices:
        if not file.is_file() or remaining < 100:
            continue
        content = file.read_text(errors='replace')
        if len(content) > remaining:
            # The beginning and the final trace steps contain complementary
            # evidence. Mark truncation explicitly for reviewer confidence.
            a = remaining // 3
            b = remaining - a
            content = content[:a] + '\n\n[TRUNCATED MIDDLE OF FILE]\n\n' + content[-b:]
        parts.append(f'### {title} ({file.relative_to(trial)})\n{content}')
        remaining -= len(content)
    if not parts:
        return '(No trajectory or logs found.)'
    return '\n\n'.join(parts)
