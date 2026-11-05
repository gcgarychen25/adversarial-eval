"""Bounded, resumable-by-artifact multi-model Harbor stress-test pilot."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from adversarial.real_eval import save_json
from adversarial.system_prompt import SYSTEM_PROMPT


def _done_keys(results: list[dict]) -> set[tuple[str, str]]:
    return {(r['model'], r['task_id']) for r in results if r.get('status') in ('ok', 'dry_run')}


def _log(obj) -> None:
    print(json.dumps(obj, default=str), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--models', nargs='+', default=['qwen3.5-plus', 'qwen3-coder-flash', 'deepseek-v4.1-flash'])
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--resume', action='store_true',
                    help='Continue an existing output dir; skip model/task cells already status ok/dry_run')
    ap.add_argument('--max-concurrent', type=int, default=None,
                    help='Harbor cells in parallel (default: 1 on --resume, else min(3, n_models))')
    args = ap.parse_args()
    output = args.output.resolve()
    if output.exists() and not args.resume:
        raise SystemExit('Choose a new output directory to preserve prior runs, or pass --resume.')
    from adversarial.env import apply_openai_compat, redact_config
    try:
        cfg = apply_openai_compat(require_key=not args.dry_run)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    source = json.loads((PROJECT / 'work/attacker/cases.json').read_text())
    cases = {x['task_id']: x for x in source}
    output.mkdir(parents=True, exist_ok=True)

    comparison_path = output / 'comparison.json'
    heartbeat_path = output / 'heartbeat.json'
    if args.resume and comparison_path.exists():
        prior = json.loads(comparison_path.read_text())
        plan = prior['plan']
        results = list(prior.get('results') or [])
        plan['base_url'] = cfg.base_url
        plan['endpoint'] = redact_config(cfg)
        plan['system_prompt_sha256'] = hashlib.sha256(SYSTEM_PROMPT.read_bytes()).hexdigest()
        plan['dry_run'] = args.dry_run
        plan['models'] = args.models
        plan['note'] = 'Resume of adaptive stress-test pilot; no task-specific exploit hints or mid-grid prompt changes.'
    else:
        results = []
        plan = {
            'models': args.models,
            'task_ids': ['1012', '1018', '1025'],
            'attempts_per_cell': 1,
            'attack_mode': 'system-stress',
            'max_turns': 20,
            'outer_timeout_sec': 1500,
            'dry_run': args.dry_run,
            'base_url': cfg.base_url,
            'endpoint': redact_config(cfg),
            'system_prompt_sha256': hashlib.sha256(SYSTEM_PROMPT.read_bytes()).hexdigest(),
            'note': 'Adaptive stress-test pilot; no task-specific exploit hints or prompt changes between cells.',
        }
    default_conc = 1 if args.resume else min(3, len(args.models))
    plan['max_concurrent'] = max(1, args.max_concurrent or default_conc)
    save_json(output / 'plan.json', plan)
    (output / 'system_prompt.md').write_bytes(SYSTEM_PROMPT.read_bytes())
    done = _done_keys(results)
    lock = threading.Lock()

    def record(row):
        with lock:
            results[:] = [r for r in results if not (r.get('model') == row.get('model') and r.get('task_id') == row.get('task_id'))]
            results.append(row)
            save_json(comparison_path, {'plan': plan, 'results': results})
            save_json(heartbeat_path, {
                'ts': time.time(),
                'pid': os.getpid(),
                'n_results': len(results),
                'last': {k: row.get(k) for k in ('model', 'task_id', 'status', 'reward', 'error')},
            })
            _log({k: row.get(k) for k in ('model', 'task_id', 'status', 'reward', 'error')})

    def preflight(model: str) -> None:
        if args.dry_run:
            return
        safe = model.replace('/', '_')
        req = Request(
            os.environ['OPENAI_BASE_URL'].rstrip('/') + '/chat/completions',
            data=json.dumps({
                'model': model,
                'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
                'max_tokens': 16,
            }).encode(),
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY'],
            },
        )
        with urlopen(req, timeout=90) as response:
            probe = json.load(response)
        save_json(output / f'{safe}_preflight.json', probe)
        if not probe.get('choices'):
            raise ValueError('Preflight response missing choices')

    def run_cell(model: str, tid: str) -> dict:
        safe = model.replace('/', '_')
        cell = output / 'cells' / safe / tid
        save_json(heartbeat_path, {
            'ts': time.time(), 'pid': os.getpid(),
            'phase': 'start_cell', 'model': model, 'task_id': tid,
        })
        _log({'event': 'start_cell', 'model': model, 'task_id': tid})
        if cell.exists():
            shutil.rmtree(cell)
        save_json(cell / 'attacker/cases.json', [cases[tid]])
        cmd = [
            sys.executable, '-m', 'adversarial.real_eval', 'attack',
            '--root', str(cell),
            '--model', 'openai/' + model,
            '--task', tid,
            '--attack-mode', 'system-stress',
            '--timeout', '1500',
        ]
        if args.dry_run:
            cmd.append('--dry-run')
        save_json(cell / 'command.json', cmd)
        with (cell / 'execution.log').open('w') as log:
            proc = subprocess.run(cmd, cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT, check=False)
        report = cell / 'results' / ('seta_attacks_dry_run.json' if args.dry_run else 'seta_attacks.json')
        if report.exists():
            row = json.loads(report.read_text())['results'][-1]
        else:
            row = {'status': 'launcher_error', 'error': 'No attack result; see execution.log'}
        row.update({'model': model, 'task_id': tid, 'cell_dir': str(cell), 'launcher_exit_code': proc.returncode})
        if not args.dry_run and row.get('trial_dir'):
            audits = list((Path(row['trial_dir']) / 'agent/request_audit').glob('request_*.json'))
            row['audited_llm_calls'] = len(audits)
            row['system_role_verified'] = bool(audits) and all(
                json.loads(p.read_text())['messages'][0] == {'role': 'system', 'content': SYSTEM_PROMPT.read_text()}
                for p in audits
            )
        return row

    pending = [(m, t) for m in args.models for t in plan['task_ids'] if (m, t) not in done]
    _log({
        'resume': bool(args.resume),
        'pid': os.getpid(),
        'max_concurrent': plan['max_concurrent'],
        'already_done': sorted(f'{m}/{t}' for m, t in done),
        'pending': [f'{m}/{t}' for m, t in pending],
    })

    # One preflight per model that still has work.
    models_needed = sorted({m for m, _ in pending})
    for model in models_needed:
        try:
            preflight(model)
            _log({'event': 'preflight_ok', 'model': model})
        except Exception as exc:
            for tid in [t for m, t in pending if m == model]:
                record({'model': model, 'task_id': tid, 'status': 'preflight_error', 'error': str(exc)})
            pending = [(m, t) for m, t in pending if m != model]

    with ThreadPoolExecutor(max_workers=plan['max_concurrent']) as pool:
        futures = {pool.submit(run_cell, m, t): (m, t) for m, t in pending}
        for fut in as_completed(futures):
            m, t = futures[fut]
            try:
                record(fut.result())
            except Exception as exc:
                record({'model': m, 'task_id': t, 'status': 'launcher_error', 'error': str(exc)})

    save_json(heartbeat_path, {'ts': time.time(), 'pid': os.getpid(), 'phase': 'finished', 'n_results': len(results)})
    _log({'event': 'finished', 'n_results': len(results)})
    return 0 if all(x.get('status') in ('ok', 'dry_run') for x in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
