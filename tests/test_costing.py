"""Cost estimation and usage-reading tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from adversarial.costing import (
    aggregate_costs,
    model_cost,
    read_usage,
    usage_delta,
    vm_cost,
)


def test_model_cost_uses_list_and_go_allowance():
    cost = model_cost('openai/deepseek-v4.1-flash', 1_000_000, 0, 1_000_000)
    assert cost['available']
    assert cost['list_cost_usd'] == 0.75
    assert cost['estimated_allowance_usd'] == 0.45
    assert cost['peak'] is False


def test_model_cost_cached_tokens_are_cheap():
    cost = model_cost('deepseek-v4.1-flash', 1_000_000, 1_000_000, 0)
    assert cost['list_cost_usd'] == 0.003
    assert cost['estimated_allowance_usd'] == 0.0018


def test_model_cost_peak_doubles_list():
    cost = model_cost('deepseek-v4.1-flash', 1_000_000, 0, 0, peak=True)
    assert cost['list_cost_usd'] == 0.3
    assert cost['estimated_allowance_usd'] == 0.18


def test_vm_cost_compute_and_disk():
    cost = vm_cost(machine_type='c4d-highcpu-8', seconds=3600, disk_gb=100)
    assert cost['estimated_compute_cost_usd'] == pytest.approx(0.317928224)
    assert cost['estimated_disk_cost_usd'] == pytest.approx(8 / 730)
    assert cost['estimated_total_cost_usd'] == pytest.approx(0.317928224 + 8 / 730)


def test_read_usage_from_parent_result(tmp_path: Path):
    trial = tmp_path / 'job' / 'staged-task__abc'
    trial.mkdir(parents=True)
    (tmp_path / 'job' / 'result.json').write_text(json.dumps({
        'stats': {
            'n_input_tokens': 100,
            'n_cache_tokens': 40,
            'n_output_tokens': 10,
            'cost_usd': None,
        },
    }))
    usage = read_usage(trial)
    assert usage['available']
    assert usage['input_tokens'] == 100
    assert usage['cached_tokens'] == 40
    assert usage['output_tokens'] == 10
    assert usage['total_tokens'] == 110


def test_read_usage_falls_back_to_trajectory(tmp_path: Path):
    trial = tmp_path / 'job' / 'trial'
    (trial / 'agent').mkdir(parents=True)
    (trial / 'agent' / 'trajectory.json').write_text(json.dumps({
        'final_metrics': {
            'total_prompt_tokens': 200,
            'total_cached_tokens': 50,
            'total_completion_tokens': 20,
            'total_steps': 4,
        },
    }))
    usage = read_usage(trial)
    assert usage['input_tokens'] == 200
    assert usage['cached_tokens'] == 50
    assert usage['output_tokens'] == 20
    assert usage['steps'] == 4


def test_usage_delta_converts_monthly_percent():
    before = {'monthly': {'percent': 10}, 'weekly': {'percent': 5}}
    after = {'monthly': {'percent': 12}, 'weekly': {'percent': 5}}
    delta = usage_delta(before, after, monthly_limit_usd=100)
    assert delta['windows']['monthly']['percent_delta'] == 2
    assert delta['estimate_window'] == 'monthly'
    assert delta['estimated_allowance_usd'] == 2.0


def test_usage_delta_prefers_rolling_window():
    before = {'rolling': {'percent': 1}, 'weekly': {'percent': 8},
              'monthly': {'percent': 8}}
    after = {'rolling': {'percent': 2}, 'weekly': {'percent': 8},
             'monthly': {'percent': 8}}
    delta = usage_delta(before, after, monthly_limit_usd=100)
    assert delta['estimate_window'] == 'rolling'
    assert delta['estimated_allowance_usd'] == 0.2


def test_usage_delta_is_none_when_rounded_percent_is_flat():
    before = {'rolling': {'percent': 2}, 'weekly': {'percent': 8},
              'monthly': {'percent': 8}}
    after = {'rolling': {'percent': 2}, 'weekly': {'percent': 8},
             'monthly': {'percent': 8}}
    delta = usage_delta(before, after, monthly_limit_usd=100)
    assert delta['estimated_allowance_usd'] is None
    assert delta['estimate_window'] is None


def test_aggregate_costs_sums_agent_reviewer_and_vm():
    rows = [
        {'cost': {
            'agent': {'input_tokens': 100, 'cached_tokens': 50, 'output_tokens': 10,
                      'list_cost_usd': 0.01, 'estimated_allowance_usd': 0.006},
            'reviewer': {'input_tokens': 200, 'cached_tokens': 100, 'output_tokens': 20,
                         'list_cost_usd': 0.02, 'estimated_allowance_usd': 0.012},
            'vm': {'estimated_cost_usd': 0.03},
        }},
    ]
    vm = {'estimated_total_cost_usd': 0.06}
    total = aggregate_costs(rows, vm=vm)
    assert total['model']['estimated_allowance_usd'] == 0.018
    assert total['allocated_vm_cost_usd'] == 0.03
    assert total['unallocated_vm_cost_usd'] == 0.03
    assert total['estimated_total_cost_usd'] == 0.078

    no_vm = aggregate_costs(rows)
    assert no_vm['estimated_total_cost_usd'] == 0.048
    assert no_vm['unallocated_vm_cost_usd'] == 0.0
