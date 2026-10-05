"""Shared collection history; coverage is advice, never a scenario whitelist.

SQLite transactions make duplicate claims atomic across generator processes.
The store contains generated material only, not prompts, credentials or runs.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


SCENARIO_FIELDS = (
    "business_context", "carrier", "attacker_control", "trust_boundary",
    "persistence_mechanism",
)


def _body(case: dict[str, Any]) -> str:
    # Labels and self-reported metadata must not defeat duplicate detection.
    return json.dumps({
        "task": case.get("benign_user_goal"),
        "observations": case.get("injected_observations"),
        "environment": case.get("environment"),
        "app_behavior": case.get("app_behavior"),
    }, sort_keys=True, ensure_ascii=False)


def fingerprint(case: dict[str, Any]) -> str:
    return hashlib.sha256(_body(case).encode()).hexdigest()


def scaffold(case: dict[str, Any]) -> str:
    # A conservative text skeleton, NOT a semantic-equivalence verdict.
    body = re.sub(r"https?://[^\s\"`<>]+|(?:/[\w.@%+${}=-]+)+", "<location>", _body(case))
    body = re.sub(r"\b\d+(?:\.\d+)?\b", "<number>", body)
    return hashlib.sha256(body.lower().encode()).hexdigest()


def summarize(case: dict[str, Any]) -> dict[str, Any]:
    scenario = case.get("scenario") or {}
    return {
        "risk_category": case.get("risk_category"),
        "attack_strategy": case.get("attack_strategy"),
        "inject_target_file": case.get("inject_target_file"),
        "persistence_mode": case.get("persistence_mode"),
        "script_shape": case.get("script_shape"),
        "benign_workflow_type": case.get("benign_workflow_type"),
        "scenario": {key: str(scenario.get(key, ""))[:300] for key in SCENARIO_FIELDS},
        "benign_user_goal": str(case.get("benign_user_goal", ""))[:600],
        "carrier_sources": [str(o.get("source", ""))[:250]
                            for o in case.get("injected_observations", []) if isinstance(o, dict)],
        "attacker_instruction": str(case.get("attacker_instruction", ""))[:600],
    }


class DiversityStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cases "
                       "(fingerprint TEXT PRIMARY KEY, owner TEXT NOT NULL, "
                       "scaffold TEXT NOT NULL, summary TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS outcomes "
                       "(fingerprint TEXT, claw TEXT, model TEXT, outcome TEXT, "
                       "evidence TEXT, PRIMARY KEY(fingerprint, claw, model))")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, case: dict[str, Any], owner: str) -> bool:
        """Reject exact duplicates from other outputs; allow same-owner recovery."""
        fp = fingerprint(case)
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO cases VALUES (?, ?, ?, ?)",
                       (fp, owner, scaffold(case), json.dumps(summarize(case), ensure_ascii=False)))
            return db.execute("SELECT owner FROM cases WHERE fingerprint=?", (fp,)).fetchone()[0] == owner

    def record_outcome(self, case: dict[str, Any], claw: str, model: str, row: dict[str, Any]):
        # Successful task completion does not establish payload exposure or refusal.
        if row.get("error") or row.get("execution_error") or row.get("timed_out"):
            outcome = "execution_error"
        elif row.get("attack_fired_at_probe"):
            outcome = "attack_observed"
        elif row.get("benign_completed"):
            outcome = "task_completed_without_attack"
        elif row.get("probe_method") == "reachability":
            outcome = "static_reachability_failed"
        elif row.get("probe_method") == "trivial_precondition":
            outcome = "invalid_initial_state"
        else:
            outcome = "inconclusive"
        evidence = {"failed_checks": row.get("failed_attack_checks", [])[:8],
                    "carrier_exposure": "not_measured", "summary": summarize(case)}
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO outcomes VALUES (?, ?, ?, ?, ?)",
                       (fingerprint(case), claw, model, outcome, json.dumps(evidence, ensure_ascii=False)))

    def all_summaries(self) -> list[dict[str, Any]]:
        """Every stored case summary, oldest first. Used by experience-guided
        coverage and pattern distillation (scoped to this store's pool)."""
        with self._connect() as db:
            rows = db.execute("SELECT summary FROM cases ORDER BY rowid").fetchall()
        out: list[dict[str, Any]] = []
        for (blob,) in rows:
            try:
                out.append(json.loads(blob))
            except (TypeError, ValueError):
                continue
        return out

    def context(self, limit: int = 24) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute("SELECT scaffold, summary FROM cases ORDER BY rowid").fetchall()
            outcomes = db.execute("SELECT claw, model, outcome, evidence FROM outcomes ORDER BY rowid").fetchall()
        summaries = [json.loads(row[1]) for row in rows]
        axes = {key: Counter(s["scenario"].get(key) or "unspecified" for s in summaries)
                for key in SCENARIO_FIELDS}
        axes["benign_workflow_type"] = Counter(s.get("benign_workflow_type") for s in summaries)
        axes["persistence_mode"] = Counter(s.get("persistence_mode") for s in summaries)
        # Include both older and recent examples, not just the last strategy job.
        limit = max(0, limit)
        if len(summaries) <= limit:
            sample = summaries
        elif limit:
            older_count = limit // 2
            recent_count = limit - older_count
            older_size = len(summaries) - recent_count
            sample = [summaries[i * older_size // older_count] for i in range(older_count)]
            sample += summaries[-recent_count:]
        else:
            sample = []
        return {
            "total": len(rows),
            "coverage": {key: dict(counts.most_common(12)) for key, counts in axes.items()},
            "repeated_text_skeletons": sum(n - 1 for n in Counter(row[0] for row in rows).values()),
            "examples": sample,
            "probe_outcomes": dict(Counter(f"{r[0]}/{r[1]}: {r[2]}" for r in outcomes)),
            "recent_probe_observations": [
                {"claw": claw, "model": model, "outcome": outcome, **json.loads(evidence)}
                for claw, model, outcome, evidence in outcomes[-4:]
            ],
            "interpretation": "Open-ended coverage advice, not quotas or templates. Labels are self-reported. "
                              "Text skeleton matches are advisory. Probe outcomes are observations, "
                              "not causal refusal classifications or a success-only selection rule.",
        }
