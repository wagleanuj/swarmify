"""Words heard in the audio matched to the text that was spoken: the text's spelling wins, the
times come from the audio. Kept free of the models so it is tested without them."""

import difflib
import re


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def match_words(
    text: str, heard: list[tuple[str, float, float]], duration: float
) -> list[tuple[str, float, float]]:
    want = text.split()
    sm = difflib.SequenceMatcher(
        a=[_norm(w) for w in want], b=[_norm(h[0]) for h in heard], autojunk=False
    )
    times: list[tuple[float, float] | None] = [None] * len(want)
    for a, b, n in sm.get_matching_blocks():
        for k in range(n):
            times[a + k] = heard[b + k][1:]
    spans: list[tuple[str, float, float]] = []
    i = 0
    while i < len(want):
        span = times[i]
        if span is not None:
            spans.append((want[i], *span))
            i += 1
            continue
        # A run of words not heard (or heard merged, "Goodnight") shares the gap between its
        # heard neighbours evenly.
        j = next((k for k in range(i, len(want)) if times[k] is not None), len(want))
        start = spans[-1][2] if spans else 0.0
        nxt = times[j] if j < len(want) else None
        end = nxt[0] if nxt else duration
        step = (end - start) / (j - i)
        spans += [
            (want[k], start + (k - i) * step, start + (k - i + 1) * step) for k in range(i, j)
        ]
        i = j
    return spans
