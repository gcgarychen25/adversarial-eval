"""Bounded parallel task runner: one Harbor task per worker, agent then reviewer."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .costing import (
    fetch_opencode_usage,
    is_peak,
    model_cost,
    read_usage,
    usage_delta,
    vm_cost,
)
from .env import apply_openai_compat
from .harness import read_reward
from .reporting import build_summary, write_summary
from .review_harness import load_and_parse_review

_BUILD_LOCK = threading.Lock()
_RESULTS_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace('+00:00', 'Z')


def _parse_dt(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _harbor_model(model: str) -> str:
    return model if '/' in model else f'openai/{model}'


def _read_tasks(path: Path) -> list[str]:
    items = []
    for line in path.read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            items.append(line)
    return items


def _run(args: list[str], *, cwd: Path, env: dict, timeout: int, log_path: Path) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        args, cwd=cwd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        f'$ {" ".join(args)}\n\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n'
    )
    return proc


def _resolve_task(raw: str, root: Path, cwd: Path) -> Path:
    candidate = Path(raw).expanduser()
    if candidate.is_dir() and (candidate / 'task.toml').is_file():
        return candidate.resolve()

    local = root / 'attacker' / 'tmax' / 'tasks' / raw
    if (local / 'task.toml').is_file():
        return local.resolve()
    if (local / 'task' / 'task.toml').is_file():
        return (local / 'task').resolve()

    cases_path = root / 'attacker' / 'cases.json'
    if cases_path.is_file():
        cases = json.loads(cases_path.read_text())
        for case in cases:
            if case.get('task_id') == raw:
                path = Path(case['task_path'])
                if path.is_dir() and (path / 'task.toml').is_file():
                    return path.resolve()

    if raw.startswith('task_'):
        target = root / 'attacker' / 'tmax' / 'tasks'
        target.mkdir(parents=True, exist_ok=True)
        cmd = ['harbor', 'download', f'tmax/{raw}', '-o', str(target), '--overwrite']
        proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
        if proc.returncode == 0:
            if (target / raw / 'task.toml').is_file():
                return (target / raw).resolve()
            if (target / raw / 'task' / 'task.toml').is_file():
                return (target / raw / 'task').resolve()

    raise FileNotFoundError(f'Cannot resolve task {raw!r}; pass a Harbor task directory')


def _task_id(task: Path) -> str:
    return task.parent.name if task.name == 'task' else task.name


def _docker_lines(args: list[str]) -> list[str]:
    try:
        return subprocess.check_output(args, text=True).splitlines()
    except (OSError, subprocess.CalledProcessError):
        return []


def _cleanup_task_images(task_id: str) -> None:
    prefix = f'{task_id}__'
    for line in _docker_lines(['docker', 'ps', '-a', '--format', '{{.ID}} {{.Image}}']):
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].split(':', 1)[0].startswith(prefix):
            subprocess.run(['docker', 'rm', '-f', parts[0]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for line in _docker_lines(['docker', 'images', '--format', '{{.Repository}}:{{.Tag}} {{.ID}}']):
        parts = line.split()
        if len(parts) == 2 and parts[0].startswith(prefix):
            subprocess.run(['docker', 'rmi', '-f', parts[1]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _prefetch(task: Path, task_id: str, *, worker_dir: Path, model: str, agent_harness: str,
              env: dict, timeout: int) -> None:
    prefetch_dir = worker_dir / 'prefetch'
    shutil.rmtree(prefetch_dir, ignore_errors=True)
    cmd = [
        'harbor', 'run', '--path', str(task),
        '--agent', agent_harness,
        '--model', _harbor_model(model),
        '--env', 'docker',
        '--install-only', '--no-delete',
        '--n-concurrent', '1',
        '--jobs-dir', str(prefetch_dir),
        '--job-name', f'prefetch-{task_id}-{int(time.time())}',
    ]
    with _BUILD_LOCK:
        proc = _run(cmd, cwd=task.parent, env=env, timeout=timeout,
                    log_path=worker_dir / 'prefetch.log')
    shutil.rmtree(prefetch_dir, ignore_errors=True)
    if proc.returncode != 0:
        raise RuntimeError(f'prefetch failed for {task_id}; see {worker_dir / "prefetch.log"}')


def _agent(task: Path, task_id: str, *, worker_dir: Path, jobs_dir: Path, policy_version: str,
           model: str, agent_harness: str, timeout: int, env: dict, cwd: Path) -> dict:
    job_name = f'batch-{task_id}-{int(time.time())}'
    args = [
        sys.executable, '-m', 'adversarial', 'cheat-agent',
        '--task', str(task),
        '--model', model,
        '--harness', agent_harness,
        '--prompt-mode', 'system',
        '--policy-version', policy_version,
        '--jobs-dir', str(jobs_dir),
        '--job-name', job_name,
        '--stage-dir', str(worker_dir / 'staged-task'),
        '--skill-stage-dir', str(worker_dir / 'agent-skill'),
        '--timeout', str(timeout),
    ]
    proc = _run(args, cwd=cwd, env=env, timeout=timeout + 60,
                log_path=worker_dir / 'agent.log')
    if proc.returncode != 0:
        raise RuntimeError(f'agent failed for {task_id}; see {worker_dir / "agent.log"}')
    try:
        summary = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'agent emitted non-JSON summary for {task_id}') from exc
    if not summary.get('trial_dir'):
        raise RuntimeError(f'agent produced no trial for {task_id}')
    return summary


def _review(task: Path, task_id: str, trial: Path, *, worker_dir: Path, policy_version: str,
            model: str, review_harness: str, timeout: int, env: dict, cwd: Path) -> dict:
    output = trial / 'cheat_review.json'
    args = [
        sys.executable, '-m', 'adversarial', 'cheat-review',
        '--trial', str(trial),
        '--task', str(task),
        '--harness', review_harness,
        '--policy-version', policy_version,
        '--model', model,
        '--jobs-dir', str(worker_dir / 'review-jobs'),
        '--workspace-dir', str(worker_dir / 'review-workspace'),
        '--skill-stage-dir', str(worker_dir / 'reviewer-skill'),
        '--job-name', f'batch-review-{task_id}-{int(time.time())}',
        '--output', str(output),
        '--timeout', str(timeout),
    ]
    proc = _run(args, cwd=cwd, env=env, timeout=timeout + 60,
                log_path=worker_dir / 'review.log')
    if proc.returncode != 0 or not output.is_file():
        raise RuntimeError(f'review failed for {task_id}; see {worker_dir / "review.log"}')
    report = json.loads(output.read_text(encoding='utf-8'))
    return {
        'verdict': load_and_parse_review(output, reward=read_reward(trial)),
        'review_trial_dir': report.get('review_trial_dir'),
        'report': report,
    }


def _write_row(path: Path, row: dict) -> None:
    with _RESULTS_LOCK:
        with path.open('a') as handle:
            handle.write(json.dumps(row) + '\n')


def _read_results(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _completed(path: Path) -> set[str]:
    return {row.get('task_id') for row in _read_results(path) if row.get('status') == 'ok'}


def _run_one(raw: str, *, root: Path, jobs_dir: Path, policy_version: str, model: str,
             agent_harness: str, review_harness: str, timeout: int,
             prefetch_timeout: int, do_prefetch: bool, do_cleanup: bool,
             workers: int, vm_hourly_rate: float, vm_disk_gb: float,
             vm_disk_monthly_rate: float | None, vm_machine_type: str | None,
             vm_disk_type: str,
             env: dict, cwd: Path, results_path: Path, completed: set[str]) -> dict:
    started = _now()
    row = {'task_id': raw, 'status': 'error', 'started_at': _iso(started)}
    task = None
    task_id = raw
    worker_dir = None
    trial = None
    try:
        task = _resolve_task(raw, root, cwd)
        task_id = _task_id(task)
        row['task_id'] = task_id
        worker_dir = jobs_dir / task_id
        worker_dir.mkdir(parents=True, exist_ok=True)
        if task_id in completed:
            row['status'] = 'skipped'
            return row
        if do_prefetch:
            _prefetch(task, task_id, worker_dir=worker_dir, model=model,
                      agent_harness=agent_harness, env=env, timeout=prefetch_timeout)
        agent_summary = _agent(task, task_id, worker_dir=worker_dir, jobs_dir=jobs_dir,
                               policy_version=policy_version, model=model,
                               agent_harness=agent_harness, timeout=timeout,
                               env=env, cwd=cwd)
        trial = Path(agent_summary['trial_dir'])
        review_result = _review(task, task_id, trial, worker_dir=worker_dir,
                                policy_version=policy_version, model=model,
                                review_harness=review_harness, timeout=timeout,
                                env=env, cwd=cwd)
        verdict = review_result['verdict']
        if verdict.get('schema_version') != 'v5':
            raise RuntimeError(f'reviewer did not emit v5 for {task_id}')
        finished = _now()
        duration = int((finished - started).total_seconds())

        agent_usage = read_usage(trial)
        review_trial_dir = review_result.get('review_trial_dir')
        review_usage = read_usage(Path(review_trial_dir)) if review_trial_dir else {
            'available': False, 'input_tokens': 0, 'cached_tokens': 0,
            'output_tokens': 0, 'total_tokens': 0,
        }
        peak = is_peak(started) and is_peak(finished)
        agent_cost = model_cost(
            model, agent_usage['input_tokens'], agent_usage['cached_tokens'],
            agent_usage['output_tokens'], peak=peak,
        )
        review_cost = model_cost(
            model, review_usage['input_tokens'], review_usage['cached_tokens'],
            review_usage['output_tokens'], peak=peak,
        )
        allocated_seconds = duration / max(1, workers)
        vm_share = vm_cost(
            machine_type=vm_machine_type, seconds=allocated_seconds,
            disk_gb=vm_disk_gb, disk_type=vm_disk_type,
            hourly_rate=vm_hourly_rate, disk_monthly_rate=vm_disk_monthly_rate,
        )
        model_list = (
            float(agent_cost.get('list_cost_usd') or 0)
            + float(review_cost.get('list_cost_usd') or 0)
        )
        model_allowance = (
            float(agent_cost.get('estimated_allowance_usd') or 0)
            + float(review_cost.get('estimated_allowance_usd') or 0)
        )
        vm_share_cost = float(vm_share.get('estimated_total_cost_usd') or 0)
        row['cost'] = {
            'agent': agent_cost,
            'reviewer': review_cost,
            'model': {
                'list_cost_usd': round(model_list, 8),
                'estimated_allowance_usd': round(model_allowance, 8),
            },
            'vm': {
                'allocated_seconds': round(allocated_seconds, 1),
                'estimated_cost_usd': round(vm_share_cost, 8),
            },
            'estimated_total_cost_usd': round(model_allowance + vm_share_cost, 8),
        }

        review_path = trial / 'cheat_review.json'
        report = json.loads(review_path.read_text())
        report['duration_seconds'] = duration
        report['cost'] = row['cost']
        review_path.write_text(json.dumps(report, indent=2) + '\n')
        row.update({
            'status': 'ok',
            'finished_at': _iso(finished),
            'duration_seconds': duration,
            'reward': verdict.get('reward'),
            'task_verdict': verdict.get('task_verdict'),
            'trace_verdict': verdict.get('trace_verdict'),
            'trace_findings': verdict.get('trace_findings'),
            'confidence': verdict.get('confidence'),
            'trial_dir': str(trial),
            'review_trial_dir': review_trial_dir,
        })
        return row
    except Exception as exc:
        row.update({
            'status': 'error',
            'finished_at': _iso(_now()),
            'error': str(exc),
            'trial_dir': str(trial) if trial else None,
        })
        return row
    finally:
        if do_cleanup and worker_dir is not None:
            _cleanup_task_images(task_id)
            shutil.rmtree(worker_dir / 'staged-task', ignore_errors=True)
            shutil.rmtree(worker_dir / 'agent-skill', ignore_errors=True)
            shutil.rmtree(worker_dir / 'reviewer-skill', ignore_errors=True)
            shutil.rmtree(worker_dir / 'review-workspace', ignore_errors=True)
        _write_row(results_path, row)


def run_batch(args) -> int:
    cwd = Path.cwd()
    root = Path(args.root).expanduser().resolve()
    jobs_dir = Path(args.jobs_dir).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    tasks = _read_tasks(Path(args.tasks).expanduser())
    if not tasks:
        raise ValueError(f'No tasks in {args.tasks}')
    workers = max(1, int(args.workers))
    jobs_dir.mkdir(parents=True, exist_ok=True)
    apply_openai_compat(require_key=True)
    env = os.environ.copy()
    usage_before = None if args.no_opencode_usage else fetch_opencode_usage()
    results_path = jobs_dir / 'RESULTS.jsonl'
    prior_rows = _read_results(results_path) if args.resume else []
    completed = _completed(results_path) if args.resume else set()
    started = _now()
    vm_started = _parse_dt(args.vm_started_at, started)
    print(json.dumps({
        'event': 'batch_start', 'n_tasks': len(tasks), 'workers': workers,
        'policy_version': args.policy_version, 'model': args.model,
        'agent_harness': args.agent_harness, 'review_harness': args.review_harness,
        'jobs_dir': str(jobs_dir),
        'vm_hourly_rate': args.vm_hourly_rate,
        'vm_disk_gb': args.vm_disk_gb,
        'opencode_usage': usage_before is not None,
    }), flush=True)

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _run_one, raw, root=root, jobs_dir=jobs_dir,
                policy_version=args.policy_version, model=args.model,
                agent_harness=args.agent_harness, review_harness=args.review_harness,
                timeout=args.timeout, prefetch_timeout=args.prefetch_timeout,
                do_prefetch=not args.no_prefetch, do_cleanup=not args.no_cleanup,
                workers=workers, vm_hourly_rate=args.vm_hourly_rate,
                vm_disk_gb=args.vm_disk_gb,
                vm_disk_monthly_rate=args.vm_disk_monthly_rate,
                vm_machine_type=args.vm_machine_type,
                vm_disk_type=args.vm_disk_type,
                env=env, cwd=cwd, results_path=results_path, completed=completed,
            ): raw for raw in tasks
        }
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(json.dumps({k: row.get(k) for k in (
                'task_id', 'status', 'reward', 'task_verdict', 'trace_verdict', 'duration_seconds'
            )}), flush=True)

    usage_after = None if args.no_opencode_usage else fetch_opencode_usage()
    opencode_usage = usage_delta(
        usage_before, usage_after, monthly_limit_usd=args.opencode_monthly_limit,
    )
    finished = _now()
    vm = None
    if args.vm_machine_type or args.vm_hourly_rate or args.vm_disk_gb:
        vm = vm_cost(
            machine_type=args.vm_machine_type, seconds=(finished - vm_started).total_seconds(),
            disk_gb=args.vm_disk_gb, disk_type=args.vm_disk_type,
            hourly_rate=args.vm_hourly_rate,
            disk_monthly_rate=args.vm_disk_monthly_rate,
        )
        vm['started_at'] = _iso(vm_started)
        vm['finished_at'] = _iso(finished)
    row_costs = {
        row['task_id']: row['cost']
        for row in prior_rows + rows
        if row.get('status') == 'ok' and row.get('cost')
    }
    successful = [Path(r['trial_dir']) for r in prior_rows + rows
                  if r.get('status') == 'ok' and r.get('trial_dir')]
    if successful:
        summary = build_summary(
            successful,
            started_at=started,
            finished_at=finished,
            policy_version=args.policy_version,
            model=args.model,
            row_costs=row_costs,
            workers=workers,
            vm=vm,
            opencode_usage=opencode_usage,
        )
        write_summary(output, summary)
    else:
        summary = None
    if not args.keep_build_cache:
        subprocess.run(['docker', 'builder', 'prune', '-af'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if args.quit_docker:
        subprocess.run(['osascript', '-e', 'quit app "Docker"'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(json.dumps({
        'event': 'batch_done',
        'ok': sum(1 for r in rows if r.get('status') == 'ok'),
        'error': sum(1 for r in rows if r.get('status') == 'error'),
        'summary': str(output) if summary else None,
        'duration_seconds': int((finished - started).total_seconds()),
        'cost': (summary or {}).get('cost'),
    }, indent=2), flush=True)
    return 0 if all(r.get('status') in ('ok', 'skipped') for r in rows) else 1
