#!/usr/bin/env python3
"""Validate reviewer JSON written into the evidence-mounted review workspace."""
from __future__ import annotations

import json
import sys
from pathlib import Path

STATUS_VALID = {
    'loophole_found',
    'legitimate_solution',
    'failed_or_unproven',
    'inconclusive',
}
# Accept legacy v2 files still sitting in old trials.
LEGACY_VERDICT = {
    'confirmed_cheat',
    'legitimate_solution',
    'failed_or_unproven',
    'inconclusive',
}
HARBOR_COMPONENTS = {
    'Docker environment',
    'Instruction',
    'Verifier',
    'Solution',
}
EASE_LEVELS = {'Easy', 'Moderate', 'Hard', 'Unknown'}


def _loophole_ok(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    if not isinstance(row.get('loophole_category'), str) or not row['loophole_category'].strip():
        return False
    if not isinstance(row.get('failure_mode'), str) or not row['failure_mode'].strip():
        return False
    if row.get('harbor_component') not in HARBOR_COMPONENTS:
        return False
    if not isinstance(row.get('observed_trace'), str):
        return False
    if row.get('ease_of_exploitation') not in EASE_LEVELS:
        return False
    return True


def ok(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(obj, dict):
        return False
    if not isinstance(obj.get('rationale'), str) or not obj['rationale'].strip():
        return False
    if obj.get('confidence') not in ('high', 'medium', 'low'):
        return False

    # v3 loophole catalog
    if obj.get('status') in STATUS_VALID:
        loops = obj.get('loopholes')
        if not isinstance(loops, list):
            return False
        if obj['status'] == 'loophole_found' and not loops:
            return False
        if obj['status'] == 'legitimate_solution' and loops:
            return False
        return all(_loophole_ok(row) for row in loops)

    # legacy v2
    if obj.get('verdict') in LEGACY_VERDICT and isinstance(obj.get('evidence'), list):
        return True
    return False


def main(argv: list[str]) -> int:
    for raw in argv[1:]:
        if ok(Path(raw)):
            print(f'OK: {raw}')
            return 0
    print('FAIL: no valid cheat_review.json found', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
