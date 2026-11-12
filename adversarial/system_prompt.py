"""Policy generations under ``instruction/vN/<role>/{prompt,skill}``.

Layout::

    instruction/
      v3/
        cheating-agent/
          prompt/system.md
          skill/SKILL.md
          skill/scripts/
        cheating-reviewer/
          prompt/system.md
          skill/SKILL.md
          skill/scripts/

Bump ``DEFAULT_VERSION`` when adding the next generation (v4, …).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTRUCTION_DIR = ROOT / 'instruction'

CHEATING_AGENT_NAME = 'cheating-agent'
CHEATING_REVIEWER_NAME = 'cheating-reviewer'
ROLE_NAMES = (CHEATING_AGENT_NAME, CHEATING_REVIEWER_NAME)

# Bump when the next generation is ready to be the default.
DEFAULT_VERSION = 'v3'
POLICY_VERSION_ENV = 'ADVERSARIAL_POLICY_VERSION'

_VERSION_RE = re.compile(r'^v(\d+)$')
_FRONTMATTER = re.compile(r'^---\n.*?\n---\n*', re.DOTALL)


def _version_key(version: str) -> int:
    match = _VERSION_RE.match(version)
    if not match:
        raise ValueError(f'Policy version must look like vN, got {version!r}')
    return int(match.group(1))


def generation_dir(version: str | None = None) -> Path:
    version = version or active_version()
    _version_key(version)
    return INSTRUCTION_DIR / version


def role_dir(role: str, version: str | None = None) -> Path:
    if role not in ROLE_NAMES:
        raise ValueError(f'Unknown role {role!r}; expected one of {ROLE_NAMES}')
    return generation_dir(version) / role


def prompt_path(role: str, version: str | None = None) -> Path:
    return role_dir(role, version) / 'prompt' / 'system.md'


def skill_dir(role: str, version: str | None = None) -> Path:
    return role_dir(role, version) / 'skill'


def list_policy_versions() -> tuple[str, ...]:
    """Discover generations that contain both roles with prompt + skill."""
    if not INSTRUCTION_DIR.is_dir():
        return (DEFAULT_VERSION,)
    found: list[str] = []
    for child in INSTRUCTION_DIR.iterdir():
        if not child.is_dir() or not _VERSION_RE.match(child.name):
            continue
        ok = True
        for role in ROLE_NAMES:
            if not (child / role / 'prompt' / 'system.md').is_file():
                ok = False
                break
            if not (child / role / 'skill' / 'SKILL.md').is_file():
                ok = False
                break
        if ok:
            found.append(child.name)
    return tuple(sorted(found, key=_version_key)) or (DEFAULT_VERSION,)


# Back-compat aliases
SKILL_VERSIONS = list_policy_versions()
DEFAULT_SKILL_VERSION = DEFAULT_VERSION


def active_version() -> str:
    return os.environ.get(POLICY_VERSION_ENV, DEFAULT_VERSION)


def agent_system_prompt_path(version: str | None = None) -> Path:
    return prompt_path(CHEATING_AGENT_NAME, version)


def reviewer_system_prompt_path(version: str | None = None) -> Path:
    return prompt_path(CHEATING_REVIEWER_NAME, version)


SYSTEM_PROMPT = agent_system_prompt_path(DEFAULT_VERSION)
REVIEWER_SYSTEM_PROMPT = reviewer_system_prompt_path(DEFAULT_VERSION)

# Archived convenience aliases (v1 appended attack / CheatBench auditor).
AGENT_V1_APPENDED = prompt_path(CHEATING_AGENT_NAME, 'v1')
AGENT_V2_FULL = prompt_path(CHEATING_AGENT_NAME, 'v2')
REVIEWER_V1_CHEATBENCH = prompt_path(CHEATING_REVIEWER_NAME, 'v1')
REVIEWER_V2_FULL = prompt_path(CHEATING_REVIEWER_NAME, 'v2')


def skill_version_dir(name: str, version: str | None = None) -> Path:
    """Return ``instruction/<version>/<name>/skill`` containing SKILL.md."""
    path = skill_dir(name, version)
    if not (path / 'SKILL.md').is_file():
        raise FileNotFoundError(f'Skill version missing SKILL.md: {path}')
    return path


CHEATING_AGENT_SKILL = skill_version_dir(CHEATING_AGENT_NAME, DEFAULT_VERSION)
CHEATING_REVIEWER_SKILL = skill_version_dir(CHEATING_REVIEWER_NAME, DEFAULT_VERSION)


def skill_body(skill_directory: Path) -> str:
    """Return SKILL.md body with YAML frontmatter stripped."""
    path = skill_directory / 'SKILL.md'
    text = path.read_text()
    return _FRONTMATTER.sub('', text, count=1).strip() + '\n'


def reviewer_policy_text(version: str | None = None) -> str:
    """Full reviewer policy for host llm / appended fallbacks."""
    version = version or active_version()
    try:
        return skill_body(skill_version_dir(CHEATING_REVIEWER_NAME, version))
    except FileNotFoundError:
        return REVIEWER_V2_FULL.read_text()


def stage_skill_for_harbor(
    name: str,
    staging_root: Path,
    *,
    version: str | None = None,
) -> Path:
    """Copy ``instruction/<ver>/<name>/skill`` → ``staging_root/<name>`` for Harbor.

    Harbor uses the directory basename as the skill name, so the staged folder
    must be ``cheating-agent`` / ``cheating-reviewer`` (not ``v3``).
    Includes optional ``scripts/`` beside SKILL.md.
    """
    version = version or active_version()
    source = skill_version_dir(name, version)
    staging_root = staging_root.resolve()
    staging_root.mkdir(parents=True, exist_ok=True)
    dest = staging_root / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    return dest


def ensure_generation(version: str, *, role: str = 'agent') -> None:
    """Fail fast if this generation is missing prompt or skill files."""
    harbor_role = CHEATING_AGENT_NAME if role == 'agent' else CHEATING_REVIEWER_NAME
    if role == 'reviewer':
        harbor_role = CHEATING_REVIEWER_NAME
    elif role != 'agent':
        raise ValueError(f'Unknown role {role!r}')
    prompt = prompt_path(harbor_role, version)
    skill = skill_version_dir(harbor_role, version)
    if not prompt.is_file():
        raise FileNotFoundError(f'Missing prompt for generation {version}: {prompt}')
    if not (skill / 'SKILL.md').is_file():
        raise FileNotFoundError(f'Missing skill for generation {version}: {skill}')


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
        record = {'system_prompt_sha256': hashlib.sha256(self.system_prompt.encode()).hexdigest(),
                  'messages': history + [{'role': 'user', 'content': prompt}]}
        (self.audit_dir / f'request_{self.calls:04d}.json').write_text(json.dumps(record, indent=2)+'\n')
        return await self.inner.call(prompt=prompt, message_history=history, **kwargs)
