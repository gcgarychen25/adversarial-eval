"""Thin Harbor Terminus2 adapter that injects the cheating-reviewer system prompt.

Reads ``instruction/<ver>/cheating-reviewer/prompt/system.md``; full policy is
the staged Harbor skill from ``instruction/<ver>/cheating-reviewer/skill/``.
"""
from harbor.agents.terminus_2.terminus_2 import Terminus2

from .system_prompt import SystemPromptLLM, reviewer_system_prompt_path


class ReviewTerminus2(Terminus2):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._llm = SystemPromptLLM(
            self._llm,
            reviewer_system_prompt_path().read_text(),
            self.logs_dir / 'request_audit',
        )
