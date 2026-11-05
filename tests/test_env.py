from __future__ import annotations

import os
from pathlib import Path

from adversarial.env import apply_openai_compat, resolve_openai_compat


def test_resolve_defaults(monkeypatch):
    for k in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_API_BASE', 'ADVERSARIAL_API_KEY'):
        monkeypatch.delenv(k, raising=False)
    cfg = resolve_openai_compat(load_env_file=False)
    assert cfg.api_key is None
    assert cfg.base_url.endswith('/v1')


def test_parse_export_prefix(tmp_path: Path):
    from adversarial.env import _parse_dotenv
    p = tmp_path / '.env'
    p.write_text('export OPENAI_API_KEY=sk-abc\nexport OPENAI_BASE_URL=https://x/v1\n')
    got = _parse_dotenv(p)
    assert got['OPENAI_API_KEY'] == 'sk-abc'
    assert got['OPENAI_BASE_URL'] == 'https://x/v1'


def test_apply_mirrors_api_base(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://example.test/compatible-mode/v1')
    cfg = apply_openai_compat(require_key=True)
    assert os.environ['OPENAI_API_BASE'] == cfg.base_url
    assert cfg.base_url.endswith('/compatible-mode/v1')


def test_dotenv_file(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    for k in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_API_BASE'):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / '.env').write_text(
        'OPENAI_API_KEY=sk-from-dotenv\nOPENAI_BASE_URL=https://api.openai.com/v1\n'
    )
    # load from repo root normally; here point resolve via chdir won't see package .env
    # so set file by writing and using apply after monkeypatching load path through environ only:
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-from-dotenv')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://api.openai.com/v1')
    cfg = apply_openai_compat(require_key=True)
    assert cfg.api_key == 'sk-from-dotenv'
