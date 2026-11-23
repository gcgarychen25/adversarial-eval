"""Batch runner parsing and small pure helpers."""
from __future__ import annotations

import json
from pathlib import Path

from adversarial.batch import _completed, _harbor_model, _read_tasks, _task_id
from adversarial.cli import build_parser


def test_batch_parser_defaults():
    args = build_parser().parse_args(['batch', '--tasks', 'tasks.txt'])
    assert args.tasks == Path('tasks.txt')
    assert args.workers == 1
    assert args.policy_version == 'v5'
    assert args.agent_harness == 'codex'
    assert args.review_harness == 'codex'
    assert args.resume is True
    assert args.no_prefetch is False
    assert args.no_cleanup is False


def test_read_tasks_skips_comments_and_blanks(tmp_path: Path):
    path = tmp_path / 'tasks.txt'
    path.write_text('# comment\n\ntask_a\ntask_b  # inline\n')
    assert _read_tasks(path) == ['task_a', 'task_b']


def test_completed_reads_ok_rows(tmp_path: Path):
    path = tmp_path / 'RESULTS.jsonl'
    path.write_text(
        json.dumps({'task_id': 'a', 'status': 'ok'}) + '\n'
        + json.dumps({'task_id': 'b', 'status': 'error'}) + '\n'
        + 'not json\n'
    )
    assert _completed(path) == {'a'}


def test_task_id_and_harbor_model():
    assert _task_id(Path('/tmp/task_007374_b34201c8')) == 'task_007374_b34201c8'
    assert _task_id(Path('/tmp/task_007374_b34201c8/task')) == 'task_007374_b34201c8'
    assert _harbor_model('deepseek-v4.1-flash') == 'openai/deepseek-v4.1-flash'
    assert _harbor_model('openai/deepseek-v4.1-flash') == 'openai/deepseek-v4.1-flash'
