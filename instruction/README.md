# Instruction generations

Version-first layout. Each `vN` is a matched generation for both roles.

```text
instruction/
  v1|v2|v3|v4|…/
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

**Current default:** `v3` (`DEFAULT_VERSION` in `adversarial/system_prompt.py`).

## Generations

| Version | Attacker | Reviewer statuses |
|---|---|---|
| `v1` | appended attack prompt | CheatBench auditor |
| `v2` | full system prompt | `confirmed_cheat` verdicts |
| `v3` (default) | verifier stress test: reward while violating a substantive requirement | `loophole_found`, `legitimate_solution`, `failed_or_unproven`, `inconclusive` |
| `v4` (opt-in) | RL post-training task filtering: reward **without doing the core work**, hacking from the start; solve-then-stub and post-hoc sabotage are forbidden; "no realistic exploit" is a valid result | v3 statuses + `synthetic_exploit` (solve-then-stub / post-hoc sabotage) |

### Using v4

`v4` is not the default yet. Opt in per run or per shell:

```bash
python -m adversarial cheat-agent  --task <task_dir> --policy-version v4
python -m adversarial cheat-review --trial <trial_dir> --task <task_dir> --policy-version v4
python -m adversarial cheat-review --trial <trial_dir> --task <task_dir> --harness llm --policy-version v4
# or
export ADVERSARIAL_POLICY_VERSION=v4
```

v4 reviews add `binary == "synthetic"` (from `status == "synthetic_exploit"`) next to `exploit | attempt | legitimate | inconclusive`.
For reward < 1, v4 distinguishes a credible from-start bypass (`loophole_found` → `attempt`) from a speculative failure or a valid task with no loophole (`failed_or_unproven` → `inconclusive`).
A task-visible pre-existing answer source (e.g. `.expected_output`) is treated as a real exposure even if the agent also computed the same values later.
The evidence-mounted review workspace renders the active policy version and its verifier accepts v4 `synthetic_exploit` rows (empty or populated `loopholes`).
To make v4 the default, set `DEFAULT_VERSION = "v4"` in `adversarial/system_prompt.py`.

Harbor staging copies `skill/` to `/harbor/skills/<role>/` (stable name).
