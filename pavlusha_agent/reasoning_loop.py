"""Deterministic lexical observation of one Worker reasoning stream; no project semantics."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import re

SHINGLE_WORDS = 4
WINDOW_WORDS = 240
STRIDE_WORDS = 40
MIN_LAG_WORDS = 480
JACCARD_THRESHOLD = 0.70
CONSECUTIVE_MATCHES = 2
_WORD_RUNS = re.compile(r"\w+|\W+", re.UNICODE)


@dataclass(frozen=True)
class LoopSignal:
    word_count: int
    window_start: int
    matched_window_start: int | None
    similarity: float
    repetition_distance: int | None
    consecutive_matches: int
    confirmed: bool

    def as_dict(self) -> dict:
        return asdict(self)


class ReasoningLoopDetector:
    """Each delimited Unicode word is consumed once; evaluate only at window strides.

    Positions are zero-based word offsets. Unfinished words span arbitrary deltas.
    Only the last window's words and older shingle sets are retained, never raw reasoning.
    After first confirmation the observer stops work for this generation.
    """
    def __init__(self) -> None:
        self.word_count = 0
        self._partial: list[str] = []
        self._words: deque[str] = deque(maxlen=WINDOW_WORDS)
        self._windows: list[tuple[int, set[tuple[str, ...]]]] = []
        self._consecutive = 0
        self.confirmation: LoopSignal | None = None

    def feed(self, text: str) -> list[LoopSignal]:
        signals = []
        if self.confirmation is not None:
            return signals
        for match in _WORD_RUNS.finditer(text):
            run = match.group()
            if run[0].isalnum() or run[0] == "_":
                self._partial.append(run)
            elif self._partial:
                signal = self._finish_word()
                if signal is not None:
                    signals.append(signal)
                    if signal.confirmed:
                        break
        return signals

    def finish(self) -> list[LoopSignal]:
        """Flush a final undelimited word for offline/end-of-stream observation."""
        if self.confirmation is not None or not self._partial:
            return []
        signal = self._finish_word()
        return [signal] if signal is not None else []

    def _finish_word(self) -> LoopSignal | None:
        self._words.append("".join(self._partial).lower())
        self._partial.clear()
        self.word_count += 1
        if self.word_count < WINDOW_WORDS or (self.word_count - WINDOW_WORDS) % STRIDE_WORDS:
            return None
        start = self.word_count - WINDOW_WORDS
        words = list(self._words)
        shingles = {tuple(words[i:i + SHINGLE_WORDS])
                    for i in range(WINDOW_WORDS - SHINGLE_WORDS + 1)}
        best, matched = 0.0, None
        for old_start, old_shingles in self._windows:
            if start - old_start < MIN_LAG_WORDS:
                break
            similarity = len(shingles & old_shingles) / len(shingles | old_shingles)
            if similarity > best:
                best, matched = similarity, old_start
        self._windows.append((start, shingles))
        self._consecutive = self._consecutive + 1 if best >= JACCARD_THRESHOLD else 0
        signal = LoopSignal(self.word_count, start, matched, best,
                            start - matched if matched is not None else None,
                            self._consecutive, self._consecutive >= CONSECUTIVE_MATCHES)
        if signal.confirmed:
            self.confirmation = signal
        return signal


RECOVERY_MESSAGE = {
    "role": "user",
    "content": (
        "REASONING RECOVERY\n"
        "The previous reasoning generation was stopped because a deterministic repetition "
        "detector found sustained repeated text. No action from that interrupted generation "
        "was executed. Continue the current task from the existing project state and evidence. "
        "Do not reconstruct or summarize the interrupted reasoning. "
        "Choose the next concrete action needed to make progress."
    ),
}
