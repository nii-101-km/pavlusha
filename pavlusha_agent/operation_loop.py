"""Exact repetition of completed tool action/result pairs; no semantic inference."""
from collections import deque
from dataclasses import dataclass
import hashlib
import json


@dataclass(frozen=True)
class OperationLoop:
    cycle_length: int
    steps: tuple[int, ...]


class OperationLoopDetector:
    def __init__(self):
        self.history = deque(maxlen=9)

    def clear(self):
        self.history.clear()

    def feed(self, kind, action, result, *, step):
        # Unexecuted requests or withheld outcomes cannot establish identical executed results.
        if result.get('output_withheld') or result.get('error') in {
            'network_not_granted', 'unknown_function', 'invalid_arguments',
            'invalid_result', 'output_limit_exceeded',
        } or 'launch_error' in result or 'execution_error' in result:
            self.clear()
            return None
        normalized = dict(result)
        if kind == 'shell':
            normalized.pop('duration_seconds', None)
        payload = json.dumps([kind, action, normalized], sort_keys=True,
                             ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        self.history.append((digest, step))
        entries = list(self.history)
        for length in (1, 2, 3):
            if len(entries) < 3 * length:
                continue
            tail = entries[-3 * length:]
            hashes = [entry[0] for entry in tail]
            if hashes[:length] == hashes[length:2*length] == hashes[2*length:]:
                return OperationLoop(length, tuple(entry[1] for entry in tail))
        return None
