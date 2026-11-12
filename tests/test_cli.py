"""Smoke tests for the thin cheat-agent / cheat-review CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from adversarial.cli import build_parser, harbor_agent_for, resolve_task


def test_parser_defaults():
    args = build_parser().parse_args(['cheat-agent', '--task', '1012'])
    assert args.model == 'deepseek-v4-pro-0813'
    assert args.harness == 'terminus-2'
    assert args.prompt_mode == 'system'


def test_normalize_harbor_model():
    from adversarial.cli import normalize_harbor_model
    assert normalize_harbor_model('deepseek-v4-pro-0813') == 'openai/deepseek-v4-pro-0813'
    assert normalize_harbor_model('openai/deepseek-v4-pro-0813') == 'openai/deepseek-v4-pro-0813'


def test_codex_system_ok():
    agent, kwargs = harbor_agent_for('codex', 'system')
    assert agent == 'codex'
    assert kwargs is None


def test_harbor_agent_system_terminus():
    agent, kwargs = harbor_agent_for('terminus-2', 'system', api_base='https://example/v1')
    assert agent == 'adversarial.stress_agent:StressTerminus2'
    assert kwargs['max_turns'] == 20
    assert kwargs['api_base'] == 'https://example/v1'


def test_mini_system_rejected():
    with pytest.raises(ValueError, match='mini-swe-agent'):
        harbor_agent_for('mini-swe-agent', 'system')


def test_mini_appended_ok():
    agent, kwargs = harbor_agent_for('mini-swe-agent', 'appended')
    assert agent == 'mini-swe-agent'
    assert kwargs is None


def test_resolve_seta(tmp_path: Path):
    task = tmp_path / 'original_task'
    task.mkdir()
    (task / 'task.toml').write_text('[task]\nname="x"\n')
    (task / 'instruction.md').write_text('do it\n')
    root = tmp_path / 'work'
    (root / 'attacker').mkdir(parents=True)
    (root / 'attacker' / 'cases.json').write_text(json.dumps([
        {'task_id': '1012', 'task_path': str(task)},
        {'task_id': 'cancel-async-tasks', 'task_path': str(task), 'known_exploitable': False},
    ]))
    assert resolve_task('1012', root) == task.resolve()
    assert resolve_task('cancel-async-tasks', root) == task.resolve()
    assert resolve_task(str(task), root) == task.resolve()
