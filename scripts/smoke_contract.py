"""Keep release labels separate from the historical smoke-test feature gates."""
from __future__ import annotations

import re


def effective_contract_version(version: str) -> tuple[int, int]:
    """Public releases retain all packaged-app checks already introduced.

    Internal builds used 0.2 through 0.10 to introduce the smoke checks. The
    first public release was renamed from 0.10.0 to 0.1.0 without removing any
    capabilities. Public 0.1.x and subsequent releases, including 0.2.0,
    must retain that baseline instead of reusing the old internal feature gates.
    """
    match = re.fullmatch(r'(\d+)\.(\d+)(?:\.(\d+)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?)?', version)
    if not match:
        raise ValueError('Expected major.minor.patch with an optional prerelease label')
    return max((0, 10), (int(match[1]), int(match[2])))
