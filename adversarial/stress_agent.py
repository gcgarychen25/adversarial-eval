"""Thin Harbor Terminus2 adapter for the authorized stress-test system prompt.

Harbor is an optional runtime dependency, imported only by live Harbor execution.
Reads ``instruction/<ver>/cheating-agent/prompt/system.md``; full policy is the
staged Harbor skill from ``instruction/<ver>/cheating-agent/skill/``.
"""
from harbor.agents.terminus_2.terminus_2 import Terminus2

from .system_prompt import SystemPromptLLM, agent_system_prompt_path


class StressTerminus2(Terminus2):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._llm = SystemPromptLLM(
            self._llm,
            agent_system_prompt_path().read_text(),
            self.logs_dir / 'request_audit',
        )
