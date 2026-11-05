"""Auditable system-message injection without changing Harbor or task files."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SYSTEM_PROMPT = Path(__file__).resolve().parent.parent / 'prompts' / 'cheating_agent_system_v2.md'


class SystemPromptLLM:
    """Delegate the existing backend with a real system message on every call."""

    def __init__(self, inner, system_prompt: str, audit_dir: Path):
        self.inner = inner
        self.system_prompt = system_prompt
        self.audit_dir = audit_dir
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def call(self, *, prompt, message_history=None, **kwargs):
        history = [{'role': 'system', 'content': self.system_prompt}, *(message_history or [])]
        self.calls += 1
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        # No endpoint headers, environment variables, or API credentials are stored.
        record = {'system_prompt_sha256': hashlib.sha256(self.system_prompt.encode()).hexdigest(),
                  'messages': history + [{'role': 'user', 'content': prompt}]}
        (self.audit_dir / f'request_{self.calls:04d}.json').write_text(json.dumps(record, indent=2)+'\n')
        return await self.inner.call(prompt=prompt, message_history=history, **kwargs)
