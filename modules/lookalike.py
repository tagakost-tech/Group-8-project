"""How much a company looks like Omega's best customers.

The search finds companies by keywords; this reads what each company says
about itself and compares it with the kinds of company that buy from Omega
today (client/omega.json, "best_customers"), plus every company a reviewer
approved. Companies a reviewer rejected count against: a new company close to
a rejected one scores lower. So the measure learns from people's decisions.

Text is compared as overlapping 4-letter pieces of words (TF-IDF over
character n-grams, cosine similarity), which works across Italian, German
and English without a language model or a download. A sentence-embedding
model can replace `_vectors` later; the score keeps its meaning.
"""

import math
import re
from collections import Counter

from modules.client_profile import PROFILE

N = 4
_WORDS = re.compile(r"[a-zà-ÿß0-9]+")


def _grams(text: str) -> Counter:
    grams = Counter()
    for w in _WORDS.findall((text or "").lower()):
        w = f" {w} "
        for i in range(max(1, len(w) - N + 1)):
            grams[w[i : i + N]] += 1
    return grams


def _vectors(texts: list[str]) -> list[dict]:
    """TF-IDF weighted, length-normalised character n-gram vectors."""
    counts = [_grams(t) for t in texts]
    df = Counter(g for c in counts for g in c)
    n = len(texts)
    vectors = []
    for c in counts:
        v = {
            g: (1 + math.log(tf)) * math.log((1 + n) / (1 + df[g])) + 1e-9
            for g, tf in c.items()
        }
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vectors.append({g: x / norm for g, x in v.items()})
    return vectors


def _cos(a: dict, b: dict) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(g, 0.0) for g, x in a.items())


def describe(contact: dict) -> str:
    return " ".join(
        str(contact.get(k) or "") for k in ("organization", "qualification_rationale")
    ).strip()


class Lookalike:
    """Scores companies against the best customers, approvals and rejections."""

    def __init__(self, positives: list[str], negatives: list[str] | None = None):
        self.positives = [p for p in positives if p.strip()]
        self.negatives = [n for n in (negatives or []) if n.strip()]

    @classmethod
    def from_db(cls, db) -> "Lookalike":
        positives = list(PROFILE.best_customers)
        negatives = []
        try:
            rows = db.conn.execute(
                """SELECT c.organization, c.qualification_rationale, r.status
                   FROM review_queue r JOIN contacts c ON c.id = r.contact_id
                   WHERE r.stage = 'first_email' AND r.status IN ('approved', 'sent', 'rejected')
                     AND c.status != 'erased'"""
            ).fetchall()
        except Exception:  # noqa: BLE001 - no review history yet is a normal state
            rows = []
        for org, rationale, status in rows:
            text = f"{org or ''} {rationale or ''}"
            (negatives if status == "rejected" else positives).append(text)
        return cls(positives, negatives)

    def score(self, texts: list[str]) -> list[dict]:
        """0-100 per text: its closeness to the nearest positive, less half its
        closeness to the nearest rejected company, with the nearest of each."""
        if not texts or not self.positives:
            return [{"score": None, "like": None, "unlike": None} for _ in texts]
        corpus = self.positives + self.negatives + texts
        vecs = _vectors(corpus)
        pos = vecs[: len(self.positives)]
        neg = vecs[len(self.positives) : len(self.positives) + len(self.negatives)]
        out = []
        for v in vecs[len(self.positives) + len(self.negatives) :]:
            p_sims = [_cos(v, p) for p in pos]
            best_p = max(range(len(pos)), key=lambda i: p_sims[i])
            n_sim, best_n = 0.0, None
            if neg:
                n_sims = [_cos(v, n) for n in neg]
                best_n = max(range(len(neg)), key=lambda i: n_sims[i])
                n_sim = n_sims[best_n]
            raw = p_sims[best_p] - 0.5 * n_sim
            out.append(
                {
                    "score": max(0, min(100, round(raw * 160))),
                    "like": self.positives[best_p][:90],
                    "unlike": self.negatives[best_n][:90]
                    if best_n is not None and n_sim > 0.15
                    else None,
                }
            )
        return out
