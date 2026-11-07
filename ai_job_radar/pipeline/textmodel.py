"""A tiny text classifier trained on the EMSCAD fake-job dataset, evaluated without scikit-learn.

The model is TF-IDF (unigrams + bigrams, sublinear tf, l2 norm) + logistic regression, exported to
plain JSON. It is one weak signal among many: postings that read like the scams in that dataset
(2012-2014, mostly US/UK) get a lower legitimacy score. It knows nothing about Indian wording,
which the regex signals cover.
"""
from __future__ import annotations

import gzip
import json
import math
import re
from functools import lru_cache
from importlib import resources

_TOKEN = re.compile(r"(?u)\b\w\w+\b")


@lru_cache(maxsize=1)
def _model() -> dict | None:
    try:
        path = resources.files("ai_job_radar") / "data" / "scam_textmodel.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as fh:  # type: ignore[arg-type]
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def fraud_probability(text: str | None) -> float | None:
    """0..1 probability that the text resembles a fraudulent posting; None when the model is missing."""
    m = _model()
    if not m or not text:
        return None
    toks = _TOKEN.findall(text.lower())
    if not toks:
        return None
    counts: dict[int, int] = {}
    vocab = m["vocab"]
    for i, t in enumerate(toks):
        j = vocab.get(t)
        if j is not None:
            counts[j] = counts.get(j, 0) + 1
        if i + 1 < len(toks):
            j = vocab.get(t + " " + toks[i + 1])
            if j is not None:
                counts[j] = counts.get(j, 0) + 1
    if not counts:
        return None
    idf, coef = m["idf"], m["coef"]
    vals = {j: (1.0 + math.log(c)) * idf[j] for j, c in counts.items()}
    norm = math.sqrt(sum(v * v for v in vals.values())) or 1.0
    z = m["intercept"] + sum(v / norm * coef[j] for j, v in vals.items())
    return 1.0 / (1.0 + math.exp(-z))
