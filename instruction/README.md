# Instruction generations

Version-first layout. Each `vN` is a matched generation for both roles.

```text
instruction/
  v1|v2|v3|…/
    cheating-agent/
      prompt/system.md    # system / appended activator text for this generation
      skill/
        SKILL.md          # full Harbor skill policy
        scripts/          # optional helper scripts used by the skill
    cheating-reviewer/
      prompt/system.md
      skill/
        SKILL.md
        scripts/
```

**Current default:** `v3`. Next round: add `instruction/v4/...` then set `DEFAULT_VERSION = "v4"`.

Harbor staging copies `skill/` to `/harbor/skills/<role>/` (stable name).
