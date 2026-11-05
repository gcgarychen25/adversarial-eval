"""OpenAI-compatible endpoint config for Harbor (LiteLLM) and the cheating reviewer.

Alibaba Model Studio ``.../compatible-mode/v1`` speaks the same Chat Completions
protocol as OpenAI. Real OpenAI and other LiteLLM-compatible hosts work the same way:

  export OPENAI_API_KEY=...
  export OPENAI_BASE_URL=https://api.openai.com/v1   # or Alibaba compatible-mode URL

Optional local file (never commit): ``.env`` in the repo root with the same keys.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = 'https://api.openai.com/v1'
ENV_KEY_NAMES = ('OPENAI_API_KEY', 'ADVERSARIAL_API_KEY')
ENV_BASE_NAMES = ('OPENAI_BASE_URL', 'OPENAI_API_BASE')


@dataclass(frozen=True)
class OpenAICompatConfig:
    api_key: str | None
    base_url: str
    key_env: str
    source: str  # 'environ' | 'dotenv' | 'missing'


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        # Allow shell-style ``export KEY=value`` lines people paste into .env.
        if line.startswith('export '):
            line = line[len('export '):].strip()
        key, _, val = line.partition('=')
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Load repo ``.env`` into os.environ only for keys that are currently unset."""
    path = path or (REPO_ROOT / '.env')
    loaded = _parse_dotenv(path)
    for key, val in loaded.items():
        if key not in os.environ or os.environ.get(key) == '':
            os.environ[key] = val
    return loaded


def resolve_openai_compat(
    *,
    base_url: str | None = None,
    key_env: str = 'OPENAI_API_KEY',
    load_env_file: bool = True,
) -> OpenAICompatConfig:
    source = 'environ'
    if load_env_file:
        before = {k: os.environ.get(k) for k in (*ENV_KEY_NAMES, *ENV_BASE_NAMES)}
        load_dotenv()
        if any(os.environ.get(k) != before.get(k) for k in before):
            source = 'dotenv'

    api_key = os.environ.get(key_env) or None
    used_key_env = key_env
    if not api_key:
        for name in ENV_KEY_NAMES:
            if os.environ.get(name):
                api_key = os.environ[name]
                used_key_env = name
                break

    resolved_base = (
        base_url
        or os.environ.get('OPENAI_BASE_URL')
        or os.environ.get('OPENAI_API_BASE')
        or DEFAULT_BASE_URL
    ).rstrip('/')

    if not api_key:
        source = 'missing'
    return OpenAICompatConfig(
        api_key=api_key,
        base_url=resolved_base,
        key_env=used_key_env,
        source=source,
    )


def apply_openai_compat(
    *,
    base_url: str | None = None,
    key_env: str = 'OPENAI_API_KEY',
    require_key: bool = True,
) -> OpenAICompatConfig:
    """Resolve config and mirror into env vars Harbor/LiteLLM commonly read."""
    cfg = resolve_openai_compat(base_url=base_url, key_env=key_env)
    os.environ['OPENAI_BASE_URL'] = cfg.base_url
    # LiteLLM / some Harbor agents also read OPENAI_API_BASE.
    os.environ['OPENAI_API_BASE'] = cfg.base_url
    if cfg.api_key:
        os.environ['OPENAI_API_KEY'] = cfg.api_key
        if cfg.key_env != 'OPENAI_API_KEY':
            os.environ[cfg.key_env] = cfg.api_key
    if require_key and not cfg.api_key:
        raise RuntimeError(
            'Missing API key. Export OPENAI_API_KEY (and optionally OPENAI_BASE_URL), '
            'or create a gitignored .env from .env.example. '
            'Alibaba Model Studio uses the OpenAI-compatible Chat Completions URL '
            '(.../compatible-mode/v1) with the same env vars.'
        )
    return cfg


def redact_config(cfg: OpenAICompatConfig) -> dict:
    """Safe summary for logs — never includes the key."""
    return {
        'base_url': cfg.base_url,
        'key_env': cfg.key_env,
        'key_present': bool(cfg.api_key),
        'source': cfg.source,
    }
