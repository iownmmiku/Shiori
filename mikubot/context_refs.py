"""Pack optional prompt references without cutting serialized records in half."""
from __future__ import annotations

import json


REFERENCE_GROUPS = ('memories', 'dictionary', 'expressions', 'persona_knowledge')


def pack_references(references, limit=16000):
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))

    if size(references) <= limit:
        return references
    packed = {key: ([] if key in REFERENCE_GROUPS else value) for key, value in references.items()}
    omitted = {key: len(references.get(key, [])) for key in REFERENCE_GROUPS}
    packed['omitted_references'] = omitted
    # Plans and completed command receipts remain whole. The overall input
    # budget still applies if this necessary metadata alone exceeds the cap.
    if size(packed) > limit:
        return packed
    # Start with each kind's best-ranked item before filling later ranks. An
    # oversized record must not prevent shorter, useful records from fitting.
    for index in range(max(omitted.values(), default=0)):
        for key in REFERENCE_GROUPS:
            candidates = references.get(key, [])
            if index >= len(candidates):
                continue
            packed.setdefault(key, []).append(candidates[index])
            omitted[key] -= 1
            if size(packed) > limit:
                packed[key].pop()
                omitted[key] += 1
    return packed
