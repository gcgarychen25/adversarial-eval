"""Cost estimates for model tokens and the GCP VM that runs a batch."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .env import resolve_openai_compat

# Public list prices, USD per million tokens.  OpenCode Go counts a discounted
# share of this list cost against the monthly allowance; the factor below is
# calibrated from the API's own ``usage.opencode.allowance`` field.
MODEL_PRICES: dict[str, dict] = {
    'deepseek-v4.1-flash': {
        'input_per_million': 0.15,
        'cached_input_per_million': 0.003,
        'output_per_million': 0.60,
        'go_allowance_factor': 0.60,
    },
}

# Peak windows from the OpenCode/DeepSeek price sheet, UTC weekdays.
PEAK_WINDOWS = ((1, 4), (6, 10))

# On-demand list prices for common GCP machine types (USD/hour, us-central1).
VM_HOURLY_RATES = {
    'c4d-highcpu-2': 0.076667045,
    'c4d-highcpu-4': 0.157087438,
    'c4d-highcpu-8': 0.317928224,
    'c4d-highcpu-16': 0.635856448,
    'c4d-highcpu-32': 1.271712896,
    'e2-standard-4': 0.134000000,
    'e2-standard-8': 0.268000000,
}

DISK_MONTHLY_RATES = {
    'hyperdisk-balanced': 0.08,
    'pd-balanced': 0.10,
    'pd-ssd': 0.17,
    'pd-standard': 0.04,
}
HOURS_PER_MONTH = 730


def normalize_model(model: str | None) -> str:
    model = (model or '').strip()
    for prefix in ('openai/', 'opencode-go/', 'opencode/'):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def model_pricing(model: str | None) -> dict | None:
    return MODEL_PRICES.get(normalize_model(model))


def is_peak(value: datetime) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    if value.weekday() >= 5:
        return False
    return any(start <= value.hour < end for start, end in PEAK_WINDOWS)


def model_cost(
    model: str | None,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    *,
    peak: bool = False,
) -> dict:
    pricing = model_pricing(model)
    if not pricing:
        return {
            'model': model,
            'available': False,
            'input_tokens': int(input_tokens),
            'cached_tokens': int(cached_tokens),
            'output_tokens': int(output_tokens),
        }
    input_tokens = max(0, int(input_tokens))
    cached_tokens = min(input_tokens, max(0, int(cached_tokens)))
    output_tokens = max(0, int(output_tokens))
    uncached = input_tokens - cached_tokens
    list_cost = (
        uncached / 1_000_000 * pricing['input_per_million']
        + cached_tokens / 1_000_000 * pricing['cached_input_per_million']
        + output_tokens / 1_000_000 * pricing['output_per_million']
    )
    factor = float(pricing.get('go_allowance_factor', 1.0))
    if peak:
        list_cost *= 2.0
    allowance = list_cost * factor
    return {
        'model': normalize_model(model),
        'available': True,
        'input_tokens': input_tokens,
        'cached_tokens': cached_tokens,
        'output_tokens': output_tokens,
        'uncached_input_tokens': uncached,
        'peak': peak,
        'list_cost_usd': round(list_cost, 8),
        'estimated_allowance_usd': round(allowance, 8),
        'allowance_factor': factor,
    }


def _find_result(trial: Path) -> Path | None:
    for candidate in (trial / 'result.json', trial.parent / 'result.json'):
        if candidate.is_file():
            return candidate
    for parent in list(trial.parents)[:4]:
        candidate = parent / 'result.json'
        if candidate.is_file():
            return candidate
    return None


def read_usage(trial: Path) -> dict:
    """Read aggregate token usage for one Harbor trial.

    Harbor writes aggregate ``n_*_tokens`` fields in the job ``result.json``.
    Fall back to the ATIF trajectory when that file is absent.
    """
    trial = Path(trial)
    result_path = _find_result(trial)
    if result_path:
        try:
            data = json.loads(result_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        stats = data.get('stats') if isinstance(data.get('stats'), dict) else data
        input_tokens = int(stats.get('n_input_tokens') or 0)
        cached_tokens = int(stats.get('n_cache_tokens') or 0)
        output_tokens = int(stats.get('n_output_tokens') or 0)
        if input_tokens or output_tokens or cached_tokens:
            return {
                'available': True,
                'source': str(result_path),
                'input_tokens': input_tokens,
                'cached_tokens': cached_tokens,
                'output_tokens': output_tokens,
                'total_tokens': input_tokens + output_tokens,
                'harbor_cost_usd': stats.get('cost_usd'),
            }

    trajectory = trial / 'agent' / 'trajectory.json'
    if trajectory.is_file():
        try:
            final = json.loads(trajectory.read_text(encoding='utf-8')).get('final_metrics', {})
        except (OSError, ValueError):
            final = {}
        input_tokens = int(final.get('total_prompt_tokens') or 0)
        cached_tokens = int(final.get('total_cached_tokens') or 0)
        output_tokens = int(final.get('total_completion_tokens') or 0)
        return {
            'available': bool(input_tokens or output_tokens or cached_tokens),
            'source': str(trajectory),
            'input_tokens': input_tokens,
            'cached_tokens': cached_tokens,
            'output_tokens': output_tokens,
            'total_tokens': input_tokens + output_tokens,
            'steps': final.get('total_steps'),
            'harbor_cost_usd': None,
        }

    return {
        'available': False,
        'source': None,
        'input_tokens': 0,
        'cached_tokens': 0,
        'output_tokens': 0,
        'total_tokens': 0,
        'harbor_cost_usd': None,
    }


def vm_cost(
    *,
    machine_type: str | None,
    seconds: float,
    disk_gb: float = 0,
    disk_type: str = 'hyperdisk-balanced',
    hourly_rate: float | None = None,
    disk_monthly_rate: float | None = None,
) -> dict:
    rate = hourly_rate
    if rate is None:
        rate = VM_HOURLY_RATES.get(machine_type or '', 0.0)
    disk_rate = disk_monthly_rate
    if disk_rate is None:
        disk_rate = DISK_MONTHLY_RATES.get(disk_type, 0.0)
    compute = max(0.0, float(rate)) * max(0.0, float(seconds)) / 3600
    disk = (
        max(0.0, float(disk_gb))
        * max(0.0, float(disk_rate))
        * max(0.0, float(seconds))
        / (HOURS_PER_MONTH * 3600)
    )
    return {
        'machine_type': machine_type,
        'seconds': round(max(0.0, float(seconds)), 1),
        'hourly_rate_usd': rate,
        'disk_gb': disk_gb,
        'disk_type': disk_type,
        'disk_monthly_rate_usd': disk_rate,
        'estimated_compute_cost_usd': round(compute, 8),
        'estimated_disk_cost_usd': round(disk, 8),
        'estimated_total_cost_usd': round(compute + disk, 8),
        'note': 'List-price estimate; actual GCP invoice requires billing export.',
    }


def fetch_opencode_usage(*, timeout: int = 20) -> dict | None:
    """Fetch account-level OpenCode Go usage percentages, if configured."""
    cfg = resolve_openai_compat()
    if not cfg.api_key or 'opencode.ai' not in cfg.base_url:
        return None
    url = 'https://opencode.ai/inference/go/v1/usage'
    req = urllib.request.Request(
        url,
        headers={
            'Authorization': f'Bearer {cfg.api_key}',
            'User-Agent': 'adversarial-eval/usage',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (OSError, ValueError, urllib.error.HTTPError):
        return None
    usage = payload.get('usage') if isinstance(payload, dict) else None
    return usage if isinstance(usage, dict) else None


def usage_delta(before: dict | None, after: dict | None,
                *, monthly_limit_usd: float | None = None) -> dict | None:
    if not before or not after:
        return None
    delta = {}
    for window in ('rolling', 'weekly', 'monthly'):
        old = before.get(window) or {}
        new = after.get(window) or {}
        old_pct = old.get('percent')
        new_pct = new.get('percent')
        if isinstance(old_pct, (int, float)) and isinstance(new_pct, (int, float)):
            delta[window] = {
                'before_percent': old_pct,
                'after_percent': new_pct,
                'percent_delta': max(0, new_pct - old_pct),
                'resets_at': new.get('resetsAt'),
            }
    limits = {
        'rolling': (monthly_limit_usd * 0.20
                    if monthly_limit_usd is not None else None),
        'weekly': (monthly_limit_usd * 0.50
                   if monthly_limit_usd is not None else None),
        'monthly': monthly_limit_usd,
    }
    estimated: float | None = None
    estimate_window = None
    for window in ('rolling', 'weekly', 'monthly'):
        pct = delta.get(window, {}).get('percent_delta')
        if pct is not None and pct > 0 and limits[window] is not None:
            estimated = pct / 100 * limits[window]
            estimate_window = window
            break
    return {
        'windows': delta,
        'limits_usd': limits,
        'estimated_allowance_usd': (
            round(estimated, 8) if estimated is not None else None
        ),
        'estimate_window': estimate_window,
        'note': ('OpenCode usage percentages are rounded; the token-based '
                 'per-task estimate is more precise. A null value means the '
                 'rounded percentage did not change.'),
    }


def aggregate_costs(rows: list[dict], *, vm: dict | None = None,
                    opencode_usage: dict | None = None) -> dict:
    agent = {'input_tokens': 0, 'cached_tokens': 0, 'output_tokens': 0,
             'list_cost_usd': 0.0, 'estimated_allowance_usd': 0.0}
    reviewer = {'input_tokens': 0, 'cached_tokens': 0, 'output_tokens': 0,
                'list_cost_usd': 0.0, 'estimated_allowance_usd': 0.0}
    tasks_with_cost = 0
    vm_allocated = 0.0
    for row in rows:
        cost = row.get('cost') if isinstance(row, dict) else None
        if not isinstance(cost, dict):
            continue
        tasks_with_cost += 1
        for key, target in (('agent', agent), ('reviewer', reviewer)):
            part = cost.get(key) or {}
            for field in ('input_tokens', 'cached_tokens', 'output_tokens'):
                target[field] += int(part.get(field) or 0)
            target['list_cost_usd'] += float(part.get('list_cost_usd') or 0)
            target['estimated_allowance_usd'] += float(
                part.get('estimated_allowance_usd') or 0)
        vm_allocated += float((cost.get('vm') or {}).get('estimated_cost_usd') or 0)
    for part in (agent, reviewer):
        part['list_cost_usd'] = round(part['list_cost_usd'], 8)
        part['estimated_allowance_usd'] = round(part['estimated_allowance_usd'], 8)
    model_allowance = round(
        agent['estimated_allowance_usd'] + reviewer['estimated_allowance_usd'], 8)
    model_list = round(agent['list_cost_usd'] + reviewer['list_cost_usd'], 8)
    vm_total = round(float((vm or {}).get('estimated_total_cost_usd') or 0), 8)
    if not vm_total:
        vm_total = round(vm_allocated, 8)
    unallocated = round(max(0.0, vm_total - vm_allocated), 8)
    return {
        'model': {
            'agent': agent,
            'reviewer': reviewer,
            'list_cost_usd': model_list,
            'estimated_allowance_usd': model_allowance,
        },
        'vm': vm,
        'allocated_vm_cost_usd': round(vm_allocated, 8),
        'unallocated_vm_cost_usd': unallocated,
        'opencode_usage': opencode_usage,
        'tasks_with_cost': tasks_with_cost,
        'estimated_total_cost_usd': round(model_allowance + vm_total, 8),
    }
