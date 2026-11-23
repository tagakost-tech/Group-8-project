"""Made before: what Omega learned forming parts like this one.

The know-how that matters in a quote - which die cracked, which step was
saved, what a part really cost - sits in setters' heads and old job files.
This keeps it findable: each past job (client/omega_jobs.json, later King
plus setters' notes) and every approved quote is compared with a new part by
shape and by description, and the nearest come back with their notes. It
informs the estimator; it decides nothing.
"""

import json
import math
from pathlib import Path

from modules.lookalike import Lookalike

JOBS_PATH = Path(__file__).resolve().parent.parent / "client" / "omega_jobs.json"
SHAPE = ("length", "shank_d", "head_d", "collar_d")


def load_jobs(path: Path | None = None) -> list[dict]:
    p = Path(path or JOBS_PATH)
    if not p.is_file():
        return []
    return list(json.loads(p.read_text(encoding="utf-8")).get("jobs", []))


def _approved_quotes(db) -> list[dict]:
    if db is None:
        return []
    try:
        rows = db.conn.execute(
            "SELECT id, created_at, part, estimate, reviewer FROM quotes WHERE status = 'approved'"
        ).fetchall()
    except Exception:  # noqa: BLE001 - no quotes table yet is a normal state
        return []
    out = []
    for r in rows:
        part, est = json.loads(r["part"]), json.loads(r["estimate"])
        out.append(
            {
                "ref": f"Q-{r['id']}",
                "year": int(str(r["created_at"])[:4]),
                "part": f"Quoted part {part.get('thread') or ''} {part.get('length')} mm".strip(),
                "material": part.get("material"),
                **{k: part.get(k, 0) for k in SHAPE},
                "thread": part.get("thread", ""),
                "stations": est["formability"].get("stations"),
                "per_1000_eur": (est.get("cost") or {}).get("per_1000_eur"),
                "tooling_eur": (est.get("cost") or {}).get("tooling_once_eur"),
                "notes": f"Quote approved by {r['reviewer']}.",
            }
        )
    return out


def _shape_closeness(a: dict, b: dict) -> float:
    """1 for the same sizes, falling with the relative difference."""
    diffs = []
    for k in SHAPE:
        x, y = float(a.get(k) or 0), float(b.get(k) or 0)
        if x or y:
            diffs.append(abs(x - y) / max(x, y))
    if not diffs:
        return 0.0
    return math.exp(-3 * sum(diffs) / len(diffs))


def similar(part: dict, description: str = "", db=None, limit: int = 3) -> list[dict]:
    """The past jobs and quotes nearest this part, with how near and why."""
    jobs = load_jobs() + _approved_quotes(db)
    if not jobs:
        return []
    texts = [f"{j['part']} {j.get('notes', '')}" for j in jobs]
    words = (
        Lookalike([description or ""]).score(texts)
        if description.strip()
        else [{"score": 0}] * len(jobs)
    )
    ranked = []
    for j, w in zip(jobs, words):
        shape = _shape_closeness(part, j)
        same_material = 1.0 if j.get("material") == part.get("material") else 0.0
        same_thread = 1.0 if bool(j.get("thread")) == bool(part.get("thread")) else 0.0
        closeness = (
            0.6 * shape
            + 0.15 * same_material
            + 0.1 * same_thread
            + 0.15 * (w["score"] or 0) / 100
        )
        ranked.append({**j, "closeness": round(closeness * 100)})
    ranked.sort(key=lambda j: j["closeness"], reverse=True)
    return ranked[:limit]


def search(query: str, db=None, limit: int = 5) -> list[dict]:
    """Past jobs whose part or setters' notes read like the question."""
    jobs = load_jobs() + _approved_quotes(db)
    if not query.strip() or not jobs:
        return []
    scores = Lookalike([query]).score(
        [f"{j['part']} {j.get('notes', '')}" for j in jobs]
    )
    ranked = sorted(
        ({**j, "closeness": s["score"]} for j, s in zip(jobs, scores)),
        key=lambda j: j["closeness"],
        reverse=True,
    )
    return [j for j in ranked if j["closeness"]][:limit]
