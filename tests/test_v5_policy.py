"""v5 task/trace verdict schema, review workspace, and summary aggregation."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from adversarial import cli, reviewer
from adversarial.reporting import build_summary
from adversarial.review_harness import prepare_review_workspace
from adversarial.reviewer import binary_label, llm_review, parse_verdict
from adversarial.system_prompt import (
    CHEATING_AGENT_NAME,
    CHEATING_REVIEWER_NAME,
    DEFAULT_VERSION,
    ensure_generation,
    list_policy_versions,
    prompt_path,
    reviewer_policy_text,
    skill_body,
    skill_version_dir,
    stage_skill_for_harbor,
)

FINDING = {
    'type': 'output_only_verification',
    'failure_mode': 'At steps 7-9 the agent wrote the log directly; the verifier never checked the build or RPC.',
    'component': 'Verifier',
    'ease': 'Easy',
}


def _make_task(tmp: Path) -> Path:
    task = tmp / 'task'
    tests = task / 'tests'
    tests.mkdir(parents=True)
    (task / 'task.toml').write_text('[metadata]\nname="demo"\n')
    (task / 'instruction.md').write_text('Write process.py that cleans the sensor CSV.\n')
    (tests / 'test_final_state.py').write_text('def test_final_state():\n    assert True\n')
    (tests / 'test.sh').write_text('#!/bin/bash\npython -m pytest /tests/test_final_state.py\n')
    return task


def _make_trial(tmp: Path, reward: str = '1') -> Path:
    trial = tmp / 'trial'
    (trial / 'agent').mkdir(parents=True)
    (trial / 'verifier').mkdir(parents=True)
    (trial / 'agent' / 'trajectory.json').write_text(json.dumps({'steps': [{'command': 'ls'}]}))
    (trial / 'verifier' / 'reward.txt').write_text(reward + '\n')
    return trial


def _v5(task='defect', trace='attempt', findings=None, confidence='high') -> dict:
    return {
        'task_verdict': task,
        'trace_verdict': trace,
        'trace_findings': [FINDING] if findings is None else findings,
        'confidence': confidence,
    }


# 1. Generation discovery -----------------------------------------------------------


def test_v5_discovered_and_default():
    assert 'v5' in list_policy_versions()
    assert 'v4' in list_policy_versions()
    assert DEFAULT_VERSION == 'v5'


@pytest.mark.parametrize('role', ['agent', 'reviewer'])
def test_ensure_v5_generation(role):
    ensure_generation('v5', role=role)


@pytest.mark.parametrize('name', [CHEATING_AGENT_NAME, CHEATING_REVIEWER_NAME])
def test_stage_v5_skill(tmp_path: Path, name: str):
    staged = stage_skill_for_harbor(name, tmp_path / 'stage', version='v5')
    assert staged.name == name
    assert (staged / 'SKILL.md').is_file()
    assert (staged / 'SKILL.md').read_text().startswith('---\n')


def test_v5_prompts_and_skills_content():
    agent_thin = prompt_path(CHEATING_AGENT_NAME, 'v5').read_text()
    assert '/harbor/skills/cheating-agent/SKILL.md' in agent_thin
    assert 'solve-then-stub' in agent_thin.lower()
    assert 'Trajectory contract' in skill_body(skill_version_dir(CHEATING_AGENT_NAME, 'v5'))

    reviewer_thin = prompt_path(CHEATING_REVIEWER_NAME, 'v5').read_text()
    assert '/harbor/skills/cheating-reviewer/SKILL.md' in reviewer_thin
    assert '/evidence/out/cheat_review.json' in reviewer_thin
    assert 'task_verdict' in reviewer_thin
    assert 'trace_verdict' in reviewer_thin
    reviewer_body = reviewer_policy_text('v5')
    assert 'trace_findings' in reviewer_body
    assert 'answer_exposure' in reviewer_body
    assert 'insufficient_evidence' in reviewer_body
    assert 'Decision order' in reviewer_body


# 2. parse_verdict -----------------------------------------------------------------


def test_v5_valid_json():
    parsed = parse_verdict(json.dumps(_v5(task='valid', trace='no_exploit', findings=[])), reward=1.0)
    assert parsed['schema_version'] == 'v5'
    assert parsed['task_verdict'] == 'valid'
    assert parsed['trace_verdict'] == 'no_exploit'
    assert parsed['status'] == 'legitimate_solution'
    assert binary_label(parsed, 1.0) == 'legitimate'


def test_v5_failed_no_exploit_maps_to_inconclusive():
    parsed = parse_verdict(json.dumps(_v5(task='valid', trace='no_exploit', findings=[])), reward=0.0)
    assert parsed['status'] == 'failed_or_unproven'
    assert binary_label(parsed, 0.0) == 'inconclusive'


@pytest.mark.parametrize('trace,reward,expected', [
    ('exploit', 1.0, 'exploit'),
    ('attempt', 0.0, 'attempt'),
    ('synthetic', 1.0, 'synthetic'),
])
def test_v5_defect_trace_labels(trace, reward, expected):
    parsed = parse_verdict(json.dumps(_v5(trace=trace)), reward=reward)
    assert parsed['task_verdict'] == 'defect'
    assert binary_label(parsed, reward) == expected


def test_v5_inconclusive_json():
    finding = {
        'type': 'insufficient_evidence',
        'failure_mode': 'The verifier stdout is missing, so the result cannot be established.',
        'component': 'Unknown',
        'ease': 'Unknown',
    }
    parsed = parse_verdict(json.dumps(_v5(task='inconclusive', trace='inconclusive', findings=[finding])), reward=None)
    assert parsed['task_verdict'] == 'inconclusive'
    assert parsed['trace_verdict'] == 'inconclusive'
    assert binary_label(parsed, None) == 'inconclusive'


@pytest.mark.parametrize('payload,reward', [
    (_v5(task='valid', trace='no_exploit', findings=[FINDING]), 1.0),
    (_v5(task='defect', trace='attempt', findings=[]), 0.0),
    (_v5(task='defect', trace='exploit'), 0.0),
    (_v5(task='defect', trace='attempt'), 1.0),
    (_v5(task='valid', trace='exploit'), 1.0),
])
def test_v5_invalid_combinations(payload, reward):
    with pytest.raises(ValueError):
        parse_verdict(json.dumps(payload), reward=reward)


# 3. Review workspace validator -----------------------------------------------------

VALIDATOR = (
    Path(__file__).resolve().parent.parent
    / 'adversarial' / 'review_templates' / 'workspace' / 'tests' / 'validate_review.py'
)


def _run_validator(tmp_path: Path, payload: dict) -> subprocess.CompletedProcess:
    review = tmp_path / 'cheat_review.json'
    review.write_text(json.dumps(payload))
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(review)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize('payload', [
    _v5(task='valid', trace='no_exploit', findings=[]),
    _v5(task='defect', trace='exploit'),
    _v5(
        task='inconclusive',
        trace='inconclusive',
        findings=[{
            'type': 'insufficient_evidence',
            'failure_mode': 'Missing verifier output.',
            'component': 'Unknown',
            'ease': 'Unknown',
        }],
    ),
])
def test_v5_validator_accepts(tmp_path: Path, payload):
    result = _run_validator(tmp_path, payload)
    assert result.returncode == 0, result.stderr


def test_v5_validator_rejects_valid_with_findings(tmp_path: Path):
    result = _run_validator(tmp_path, _v5(task='valid', trace='no_exploit', findings=[FINDING]))
    assert result.returncode == 1


def test_prepare_v5_instruction(tmp_path: Path):
    task = _make_task(tmp_path)
    trial = _make_trial(tmp_path)
    dest = tmp_path / 'workspace'
    prepare_review_workspace(trial, task, dest, policy_version='v5')
    text = (dest / 'instruction.md').read_text()
    assert 'Policy generation: `v5`' in text
    assert 'task_verdict' in text
    assert 'trace_findings' in text
    assert '{{' not in text


# 4. llm_review / summary ----------------------------------------------------------


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_llm_review_uses_v5_skill(tmp_path: Path, monkeypatch):
    task = _make_task(tmp_path)
    trial = _make_trial(tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
    sent = {}

    def fake_urlopen(request, timeout=None):
        sent['body'] = json.loads(request.data.decode())
        content = json.dumps(_v5(task='valid', trace='no_exploit', findings=[]))
        return _FakeResp(json.dumps({'choices': [{'message': {'content': content}}]}).encode())

    monkeypatch.setattr(reviewer, 'urlopen', fake_urlopen)
    out = llm_review(task, trial, model='m', base_url='https://example/v1', policy_version='v5')
    assert sent['body']['messages'][0]['content'] == reviewer_policy_text('v5')
    assert out['task_verdict'] == 'valid'
    assert out['policy_version'] == 'v5'


def test_build_summary_v5(tmp_path: Path):
    t1 = _make_trial(tmp_path / 'one', reward='1')
    t2 = _make_trial(tmp_path / 'two', reward='0')
    (t1 / 'cheat_review.json').write_text(json.dumps({
        'task': str(tmp_path / 'task_one'),
        'reward': 1.0,
        'verdict': _v5(task='valid', trace='no_exploit', findings=[]),
    }))
    (t2 / 'cheat_review.json').write_text(json.dumps({
        'task': str(tmp_path / 'task_two'),
        'reward': 0.0,
        'verdict': _v5(task='defect', trace='attempt'),
    }))
    summary = build_summary(
        [t1, t2],
        started_at='2026-11-23T02:00:00Z',
        finished_at='2026-11-23T03:00:00Z',
        policy_version='v5',
        model='deepseek-v4.1-flash',
    )
    assert summary['schema_version'] == 'v5'
    assert summary['n'] == 2
    assert summary['solved_count'] == 1
    assert summary['percent_solved'] == 50.0
    assert summary['duration_seconds'] == 3600
    assert summary['duration_human'] == '1h00m00s'
    assert summary['trace_verdict_counts']['no_exploit'] == 1
    assert summary['trace_verdict_counts']['attempt'] == 1
    assert summary['task_verdict_counts']['valid'] == 1
    assert summary['task_verdict_counts']['defect'] == 1


def test_cli_summarize_v5(tmp_path: Path, monkeypatch):
    t1 = _make_trial(tmp_path / 'one', reward='1')
    (t1 / 'cheat_review.json').write_text(json.dumps({
        'task': str(tmp_path / 'task_one'),
        'reward': 1.0,
        'verdict': _v5(task='valid', trace='no_exploit', findings=[]),
    }))
    out = tmp_path / 'SUMMARY.json'
    args = cli.build_parser().parse_args([
        'summarize', '--trial', str(t1),
        '--started-at', '2026-11-23T02:00:00Z',
        '--finished-at', '2026-11-23T02:30:00Z',
        '--output', str(out),
    ])
    assert args.func(args) == 0
    summary = json.loads(out.read_text())
    assert summary['schema_version'] == 'v5'
    assert summary['n'] == 1
    assert summary['duration_human'] == '0h30m00s'
