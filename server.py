"""Resume screener web app: serves web/ and a small JSON API around the scoring code.

Run:  python server.py   (then open http://127.0.0.1:8000)
Model settings come from .env (see .env.example); the API key never reaches the browser.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from ats.llm import PROVIDERS, LLMConfig, env_limits  # noqa: E402
from ats.parsing import dedupe, load_files  # noqa: E402
from ats.report import to_dataframe, to_excel  # noqa: E402
from ats.rubric import DEFAULT_JD, DEFAULT_RUBRIC, records_to_rubric, rubric_fingerprint, rubric_to_records  # noqa: E402
from ats.store import Store  # noqa: E402
from ats.scorer import Cache, ScoreResult, generate_rubric, score_all  # noqa: E402

ENV_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
            "compatible": "OPENAI_COMPATIBLE_API_KEY"}
WEB = Path(__file__).parent / "web"

app = FastAPI(title="Resume Screener")
cache = Cache(os.environ.get("ATS_CACHE_PATH", ".ats_cache.sqlite"))
store = Store(os.environ.get("ATS_DB_PATH", "ats_data.sqlite"))  # permanent record of runs, candidates, scores
BATCHES: dict[str, list] = {}  # batch id -> parsed Resume objects (single-user local tool: kept in memory)
JOBS: dict[str, dict] = {}


def make_cfg() -> LLMConfig:
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    if provider not in PROVIDERS:
        raise HTTPException(500, f"LLM_PROVIDER must be one of: {', '.join(PROVIDERS)}")
    key = "" if provider == "mock" else os.environ.get("LLM_API_KEY") or os.environ.get(ENV_KEYS[provider], "")
    return LLMConfig(
        provider=provider, model=os.environ.get("LLM_MODEL") or PROVIDERS[provider]["model"], api_key=key,
        base_url=os.environ.get("LLM_BASE_URL") or (PROVIDERS[provider]["base_url"] if provider == "compatible" else ""),
        samples=max(1, int(os.environ.get("LLM_SAMPLES", 1))), effort=os.environ.get("LLM_EFFORT", "low"),
        **env_limits())


CONCURRENCY = {"min": 1, "max": 50, "default": 20}  # parallel requests; the UI slider uses the same range


@app.get("/api/config")
def config():
    c = make_cfg()
    return {"provider": c.provider, "label": PROVIDERS[c.provider]["label"], "model": c.model, "samples": c.samples,
            "ready": c.provider in ("mock", "compatible") or bool(c.api_key), "can_generate": c.provider != "mock",
            "jd": DEFAULT_JD, "rubric": rubric_to_records(DEFAULT_RUBRIC), "concurrency": CONCURRENCY}


# ---------------------------------------------------------------- job description + rubric
@app.post("/api/jd")
async def read_jd(file: UploadFile = File(...)):
    r = load_files([(file.filename or "jd.txt", await file.read())])[0]
    if not r.text:
        raise HTTPException(400, r.error or "Could not read the file")
    return {"text": r.text}


@app.post("/api/rubric/generate")
async def gen_rubric(body: dict = Body(...)):
    try:
        return {"criteria": rubric_to_records(await generate_rubric(str(body.get("jd", "")), make_cfg()))}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Could not generate rubric: {exc}") from exc


# ---------------------------------------------------------------- resumes
def batch_stats(bid: str) -> dict:
    parsed = BATCHES[bid]
    resumes, dups = dedupe(parsed)
    readable = sum(r.ok for r in resumes)
    return {"batch": bid, "found": len(parsed), "readable": readable, "unreadable": len(resumes) - readable,
            "duplicates": len(dups),
            "bad": [{"file": r.filename, "problem": r.error} for r in resumes if not r.ok]}


@app.post("/api/resumes")
async def add_resumes(files: list[UploadFile] = File(...), batch: str | None = Form(None)):
    """Parses uploads and adds them to a batch (created on first call). The client sends files in chunks."""
    pairs = [(f.filename or "resume", await f.read()) for f in files]
    parsed = await asyncio.to_thread(load_files, pairs)
    bid = batch if batch in BATCHES else uuid.uuid4().hex
    BATCHES.setdefault(bid, []).extend(parsed)
    return batch_stats(bid)


@app.get("/api/resumes/{bid}/file")
def resume_file(bid: str, name: str, sha: str = ""):
    """Serves an uploaded resume as-is (PDFs/text open in the browser, DOCX downloads)."""
    r = next((x for x in BATCHES.get(bid, []) if x.filename == name and x.raw and (not sha or x.sha256 == sha)), None)
    if not r:
        raise HTTPException(404, "Resume file not found (uploads are kept in memory until the server restarts)")
    ctype = {".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8"}.get(Path(name).suffix.lower(), "application/octet-stream")
    return Response(r.raw, media_type=ctype, headers={"Content-Disposition": f'inline; filename="{uuid.uuid4().hex[:8]}{Path(name).suffix}"',
                                                       "X-Content-Type-Options": "nosniff"})


@app.delete("/api/resumes/{bid}")
def drop_batch(bid: str):
    BATCHES.pop(bid, None)
    return {"ok": True}


# ---------------------------------------------------------------- screening jobs
def with_ranks(results: list[ScoreResult]) -> list[dict]:
    scores = [r.score for r in results if r.status == "scored" and r.score is not None]
    out = []
    for r in results:
        d = asdict(r)
        d["rank"] = 1 + sum(s > r.score for s in scores) if r.status == "scored" and r.score is not None else None
        out.append(d)
    return out


async def run_job(jid: str, job: dict, resumes, dups, rubric, jd: str, cfg: LLMConfig, concurrency: int,
                  reuse: bool = False):
    t0 = time.time()
    reused: list[ScoreResult] = []
    base = 0  # resumes already settled from the database, counted before the model-scored ones

    def on_progress(done: int, total: int, res: ScoreResult):
        c = job["counts"]
        c[res.status] = c.get(res.status, 0) + 1
        c["cached"] += int(res.cached)
        job["done"] = base + done

    try:
        if reuse:  # opt-in: take scores from earlier runs with the same job description and rubric
            hits = await asyncio.to_thread(store.find_scores, [r.sha256 for r in resumes if r.ok], job["fp"])
            todo = []
            for r in resumes:
                hit = hits.get(r.sha256) if r.ok else None
                if hit:
                    hit.filename, hit.cached = r.filename, True
                    reused.append(hit)
                    job["counts"]["scored"] += 1
                    job["counts"]["cached"] += 1
                else:
                    todo.append(r)
            resumes, base = todo, len(reused)
            job["done"] = base
        results = reused + await score_all(resumes, jd, rubric, cfg, concurrency=concurrency, cache=cache,
                                           on_progress=on_progress, should_stop=lambda: job["stop"])
        results += [ScoreResult(d.filename, d.sha256, "duplicate", error="Same text as another uploaded resume")
                    for d in dups]
        job["results"] = results
        job["status"] = "stopped" if job["stop"] else "done"
    except Exception as exc:  # noqa: BLE001
        job["status"], job["error"] = "error", str(exc)[:300]
    job["secs"] = time.time() - job["t0"] if "t0" in job else time.time() - t0
    if job["results"]:
        try:
            await asyncio.to_thread(store.save_run, jid, job, with_ranks(job["results"]))
        except Exception as exc:  # noqa: BLE001  a storage problem must not lose the on-screen results
            job["error"] = f"Results shown but not saved to the database: {exc}"[:300]


@app.post("/api/screen")
async def screen(body: dict = Body(...)):
    bid = body.get("batch")
    if bid not in BATCHES:
        raise HTTPException(400, "Upload resumes first")
    cfg = make_cfg()
    if cfg.provider not in ("mock", "compatible") and not cfg.api_key:
        raise HTTPException(400, "Set LLM_API_KEY in .env to start screening")
    rubric = records_to_rubric(body.get("rubric") or [])
    keys = [c.key for c in rubric]
    if not rubric or sum(max(c.weight, 0) for c in rubric) <= 0:
        raise HTTPException(400, "Rubric weights must add up to more than 0")
    if len(set(keys)) != len(keys):
        raise HTTPException(400, "Rubric keys must be unique")
    jd = str(body.get("jd") or "")
    try:
        conc = int(body.get("concurrency", CONCURRENCY["default"]))
    except (TypeError, ValueError):
        conc = CONCURRENCY["default"]
    conc = min(CONCURRENCY["max"], max(CONCURRENCY["min"], conc))
    resumes, dups = dedupe(BATCHES[bid])
    jid = uuid.uuid4().hex
    job = {"status": "running", "total": len(resumes), "done": 0, "stop": False, "t0": time.time(), "secs": 0,
           "counts": {"scored": 0, "error": 0, "unreadable": 0, "cached": 0}, "results": [], "rubric": rubric,
           "jd": jd, "fp": rubric_fingerprint(rubric, jd), "model": f"{PROVIDERS[cfg.provider]['label']} · {cfg.model}", "error": ""}
    JOBS[jid] = job
    job["task"] = asyncio.create_task(run_job(jid, job, resumes, dups, rubric, jd, cfg, conc,
                                    bool(body.get("reuse"))))
    return {"job": jid}


def get_job(jid: str) -> dict | None:
    """A live job from memory, else a finished run from the database (so past runs open and export too)."""
    return JOBS.get(jid) or store.load_run(jid)


@app.get("/api/runs")
def list_runs():
    return store.list_runs()


@app.get("/api/jobs/{jid}")
def job_state(jid: str):
    job = get_job(jid)
    if not job:
        raise HTTPException(404, "Unknown job")
    out = {k: job[k] for k in ("status", "total", "done", "counts", "model", "error")}
    out["elapsed"] = time.time() - job["t0"] if job["status"] == "running" else job["secs"]
    if job["status"] != "running":
        out["results"] = with_ranks(job["results"])
        out["rubric"] = rubric_to_records(job["rubric"])
    return out


@app.post("/api/jobs/{jid}/stop")
def stop_job(jid: str):
    if jid in JOBS:
        JOBS[jid]["stop"] = True
    return {"ok": True}


@app.get("/api/jobs/{jid}/export.{ext}")
def export(jid: str, ext: str):
    job = get_job(jid)
    if not job or job["status"] == "running" or ext not in ("xlsx", "csv"):
        raise HTTPException(404, "Nothing to export")
    df = to_dataframe(job["results"], job["rubric"])
    if ext == "csv":
        body, mime = df.to_csv(index=False).encode("utf-8-sig"), "text/csv"
    else:
        body, mime = to_excel(df, job["rubric"], job["jd"]), \
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="ranked_candidates.{ext}"'})


@app.post("/api/cache/clear")
def clear_cache():
    cache.clear()
    return {"ok": True}


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 8000)))
