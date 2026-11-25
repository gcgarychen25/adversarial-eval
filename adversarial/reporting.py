"""Aggregate per-trial v5 reviewer output into a V5 SUMMARY.json."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .costing import aggregate_costs
from .harness import read_reward
from .reviewer import V5_TASK_VERDICTS, V5_TRACE_VERDICTS, parse_verdict


def _as_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(value: str | datetime) -> str:
    return _as_datetime(value).isoformat().replace('+00:00', 'Z')


def duration_human(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f'{hours}h{minutes:02d}m{secs:02d}s'


def _pct(count: int, total: int) -> float:
    return round((count / total * 100.0), 1) if total else 0.0


def _review_object(trial: Path) -> tuple[dict, dict]:
    review_path = trial / 'cheat_review.json'
    if not review_path.is_file():
        raise FileNotFoundError(f'Missing reviewer output: {review_path}')
    report = json.loads(review_path.read_text(encoding='utf-8'))
    inner = report.get('verdict') if isinstance(report.get('verdict'), dict) else report
    reward = report.get('reward')
    if reward is None:
        reward = read_reward(trial)
    verdict = parse_verdict(json.dumps(inner), reward)
    return report, verdict


def build_summary(
    trials: list[Path],
    *,
    started_at: str | datetime,
    finished_at: str | datetime,
    policy_version: str = 'v5',
    model: str | None = None,
    row_costs: dict[str, dict] | None = None,
    workers: int | None = None,
    vm: dict | None = None,
    opencode_usage: dict | None = None,
) -> dict:
    """Build a V5 summary directly from per-trial ``cheat_review.json`` files."""
    start = _as_datetime(started_at)
    finish = _as_datetime(finished_at)
    duration_seconds = max(0, int((finish - start).total_seconds()))

    rows = []
    for trial in trials:
        report, verdict = _review_object(Path(trial))
        if verdict.get('schema_version') != 'v5':
            raise ValueError(
                f'{trial}/cheat_review.json is not a v5 review; use --policy-version v5'
            )
        task_value = verdict.get('task_id') or report.get('task')
        task_id = Path(task_value).name if task_value else Path(trial).parent.name
        row = {
            'task_id': task_id,
            'reward': verdict.get('reward'),
            'duration_seconds': report.get('duration_seconds'),
            'task_verdict': verdict['task_verdict'],
            'trace_verdict': verdict['trace_verdict'],
            'trace_findings': verdict['trace_findings'],
            'confidence': verdict['confidence'],
        }
        cost = row_costs.get(task_id) if row_costs else None
        if cost is None:
            cost = report.get('cost')
        if cost:
            row['cost'] = cost
        rows.append(row)

    trace_counts = {key: 0 for key in sorted(V5_TRACE_VERDICTS)}
    task_counts = {key: 0 for key in sorted(V5_TASK_VERDICTS)}
    for row in rows:
        trace_counts[row['trace_verdict']] += 1
        task_counts[row['task_verdict']] += 1
    n = len(rows)
    solved = sum(1 for row in rows if row.get('reward') is not None and row['reward'] >= 1)

    summary = {
        'schema_version': 'v5',
        'policy_version': policy_version,
        'model': model,
        'started_at': _iso(start),
        'finished_at': _iso(finish),
        'duration_seconds': duration_seconds,
        'duration_human': duration_human(duration_seconds),
        'n': n,
        'solved_count': solved,
        'percent_solved': _pct(solved, n),
        'trace_verdict_counts': trace_counts,
        'trace_verdict_pct': {key: _pct(value, n) for key, value in trace_counts.items()},
        'task_verdict_counts': task_counts,
        'task_verdict_pct': {key: _pct(value, n) for key, value in task_counts.items()},
        'rows': rows,
    }
    if workers is not None:
        summary['workers'] = workers
    if row_costs or vm or opencode_usage or any('cost' in row for row in rows):
        summary['cost'] = aggregate_costs(
            rows, vm=vm, opencode_usage=opencode_usage,
        )
    return summary


def write_summary(path: Path, summary: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    return path
