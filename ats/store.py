"""Permanent SQLite store of screening runs, candidates and scores (separate from the score cache)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .rubric import Criterion
from .scorer import ScoreResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, started REAL, finished REAL, status TEXT, model TEXT,
  jd TEXT, rubric_json TEXT, total INTEGER, fingerprint TEXT);
CREATE TABLE IF NOT EXISTS candidates (          -- one row per distinct resume text
  sha256 TEXT PRIMARY KEY, name TEXT, email TEXT, phone TEXT, role TEXT, experience_years REAL,
  filename TEXT, first_seen REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS results (             -- one row per candidate per run
  run_id TEXT, sha256 TEXT, filename TEXT, status TEXT, score REAL, normalized_score REAL, rank INTEGER,
  summary TEXT, strengths TEXT, gaps TEXT, red_flags TEXT, error TEXT, cached INTEGER, details TEXT,
  PRIMARY KEY (run_id, sha256, filename));
CREATE TABLE IF NOT EXISTS criterion_scores (    -- per-criterion points and evidence
  run_id TEXT, sha256 TEXT, filename TEXT, key TEXT, name TEXT, weight REAL, score REAL, evidence TEXT,
  PRIMARY KEY (run_id, sha256, filename, key));
CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id, rank);
"""


class Store:
    def __init__(self, path: str | Path = "ats_data.sqlite"):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        for table, col in (("runs", "fingerprint"), ("results", "details")):  # columns added after the first release
            if col not in {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                self.conn.commit()

    def save_run(self, run_id: str, job: dict, ranked: list[dict]):
        """ranked = with_ranks(results): result dicts that carry a 'rank'. Safe to call again for the same run."""
        now, rubric = time.time(), job["rubric"]
        top = max((r["score"] for r in ranked if r.get("score") is not None), default=0)
        with self._lock, self.conn:
            c = self.conn
            c.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                      (run_id, job["t0"], now, job["status"], job["model"], job["jd"],
                       json.dumps([vars(x) for x in rubric]), job["total"], job["fp"]))
            for r in ranked:
                sha, fn = r["sha256"], r["filename"]
                if r["status"] == "scored":
                    c.execute("""INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)
                                 ON CONFLICT(sha256) DO UPDATE SET name=excluded.name, email=excluded.email,
                                 phone=excluded.phone, role=excluded.role,
                                 experience_years=excluded.experience_years, filename=excluded.filename,
                                 last_seen=excluded.last_seen""",
                              (sha, r["candidate_name"], r["email"], r["phone"], r["role"],
                               r["experience_years"], fn, now, now))
                norm = round(r["score"] / top * 100, 1) if r.get("score") is not None and top > 0 else None
                c.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (run_id, sha, fn, r["status"], r["score"], norm, r["rank"], r["summary"],
                           json.dumps(r["strengths"]), json.dumps(r["gaps"]), json.dumps(r["red_flags"]),
                           r["error"], int(r["cached"]), json.dumps(r.get("details") or {})))
                for k in rubric:
                    cr = (r["criteria"] or {}).get(k.key)
                    if cr is not None:
                        c.execute("INSERT OR REPLACE INTO criterion_scores VALUES (?,?,?,?,?,?,?,?)",
                                  (run_id, sha, fn, k.key, k.name, k.weight, cr.get("score"), cr.get("evidence", "")))

    # ------------------------------------------------------------------ reading
    def list_runs(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("""
                SELECT r.id, r.started, r.finished, r.status, r.model, r.total, r.jd,
                       COUNT(x.sha256), SUM(x.status='scored'), MAX(x.score)
                FROM runs r LEFT JOIN results x ON x.run_id = r.id GROUP BY r.id ORDER BY r.started DESC""").fetchall()
        return [{"id": i, "started": s, "finished": f, "status": st, "model": m, "total": t, "jd": (jd or "")[:120],
                 "rows": n, "scored": sc or 0, "top": top} for i, s, f, st, m, t, jd, n, sc, top in rows]

    def _results(self, where: str, args: tuple) -> list[ScoreResult]:
        """ScoreResults for results rows matching 'where', newest run first, with criteria filled in. Caller locks."""
        q = f"""SELECT x.run_id, x.sha256, x.filename, x.status, x.score, x.summary, x.strengths, x.gaps, x.red_flags,
                       x.error, x.cached, c.name, c.email, c.phone, c.role, c.experience_years, x.details
                FROM results x JOIN runs r ON r.id = x.run_id LEFT JOIN candidates c ON c.sha256 = x.sha256
                WHERE {where} ORDER BY r.started DESC, x.score DESC"""
        out = []
        for (rid, sha, fn, st, sc, sm, stg, gp, rf, er, ca, nm, em, ph, ro, ey, dt) in self.conn.execute(q, args).fetchall():
            crit = {k: {"score": s, "evidence": ev or ""} for k, s, ev in self.conn.execute(
                "SELECT key, score, evidence FROM criterion_scores WHERE run_id=? AND sha256=? AND filename=?",
                (rid, sha, fn))}
            out.append(ScoreResult(fn, sha, st, sc, crit, nm or "", em or "", ph or "", ro or "", ey, sm or "",
                                   json.loads(stg or "[]"), json.loads(gp or "[]"), json.loads(rf or "[]"),
                                   er or "", bool(ca), json.loads(dt or "{}")))
        return out

    def load_run(self, run_id: str) -> dict | None:
        """A finished run in the same shape server.py keeps for live jobs, or None."""
        with self._lock:
            run = self.conn.execute(
                "SELECT status, model, jd, rubric_json, total, started, finished FROM runs WHERE id=?",
                (run_id,)).fetchone()
            if not run:
                return None
            results = self._results("x.run_id = ?", (run_id,))
        status, model, jd, rub, total, t0, t1 = run
        counts = {k: sum(r.status == k for r in results) for k in ("scored", "error", "unreadable")}
        counts["cached"] = sum(r.cached for r in results)
        return {"status": status, "total": total, "done": total, "counts": counts, "model": model, "error": "",
                "secs": (t1 or t0) - t0, "t0": t0, "results": results, "jd": jd,
                "rubric": [Criterion(**d) for d in json.loads(rub)]}

    def find_scores(self, shas: list[str], fingerprint: str) -> dict[str, ScoreResult]:
        """Latest stored 'scored' result per resume hash, from runs with the same rubric + job description."""
        found: dict[str, ScoreResult] = {}
        with self._lock:
            for i in range(0, len(shas), 500):  # stay under SQLite's variable limit
                part = shas[i:i + 500]
                marks = ",".join("?" * len(part))
                for r in self._results(f"r.fingerprint = ? AND x.status = 'scored' AND x.sha256 IN ({marks})",
                                       (fingerprint, *part)):
                    found.setdefault(r.sha256, r)  # rows are newest-first
        return found
