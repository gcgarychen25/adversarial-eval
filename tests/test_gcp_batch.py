"""Small pure checks for the GCP batch wrapper."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'gcp_batch.py'


def _load():
    spec = importlib.util.spec_from_file_location('gcp_batch', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_run_defaults_to_auto_delete():
    args = _load().build_parser().parse_args(['run'])
    assert args.no_auto_delete is False


def test_run_can_disable_auto_delete():
    args = _load().build_parser().parse_args(['run', '--no-auto-delete'])
    assert args.no_auto_delete is True


def test_startup_script_installs_docker_and_harbor():
    script = _load()._startup_script('ubuntu')
    assert 'docker-ce' in script
    assert 'harbor==0.24.0' in script
    assert 'adv-batch-ready' in script


def test_auto_delete_wrapper_calls_finish_script():
    script = _load()._batch_wrapper_script(
        remote='adversarial_eval', jobs_dir='work/runs/test',
        batch_cmd='.venv/bin/python -m adversarial batch --tasks tasks.txt',
        auto_delete=True, bucket='bucket', prefix='runs/vm/run',
        project='project', zone='us-central1-a', instance='vm',
    )
    assert 'PIPESTATUS' in script
    assert 'scripts/gcp_finish.sh' in script
    assert 'EXIT_CODE' in script


def test_no_auto_delete_wrapper_skips_finish_script():
    script = _load()._batch_wrapper_script(
        remote='adversarial_eval', jobs_dir='work/runs/test',
        batch_cmd='.venv/bin/python -m adversarial batch --tasks tasks.txt',
        auto_delete=False, bucket='bucket', prefix='runs/vm/run',
        project='project', zone='us-central1-a', instance='vm',
    )
    assert 'gcp_finish.sh' not in script
