"""Headless batch screening, e.g. for a folder of 1000+ resumes on a server.

Examples:
  python cli.py resumes/ --provider anthropic --model claude-haiku-5-5 -o ranked.xlsx
  python cli.py resumes.zip --provider compatible --base-url http://localhost:11434/v1 --model llama3.1:8b
  python cli.py resumes/ --provider mock          # offline dry run, no API key
API keys are read from ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / OPENAI_COMPATIBLE_API_KEY.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from ats.llm import PROVIDERS, LLMConfig, env_limits
from ats.parsing import SUPPORTED_EXT, dedupe, load_files
from ats.report import to_dataframe, to_excel
from ats.rubric import DEFAULT_JD, DEFAULT_RUBRIC, records_to_rubric
from ats.scorer import Cache, ScoreResult, score_all

load_dotenv()
ENV_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
            "compatible": "OPENAI_COMPATIBLE_API_KEY", "mock": ""}


def collect(paths: list[str]) -> list[tuple[str, bytes]]:
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += [(f.name, f.read_bytes()) for f in sorted(p.rglob("*"))
                      if f.is_file() and f.suffix.lower() in SUPPORTED_EXT | {".zip"}]
        elif p.is_file():
            files.append((p.name, p.read_bytes()))
        else:
            sys.exit(f"Not found: {p}")
    return files


def main():
    ap = argparse.ArgumentParser(description="AI resume screening: score and rank resumes against a JD")
    ap.add_argument("inputs", nargs="+", help="Resume files, folders, or ZIPs")
    ap.add_argument("--jd", help="Job description file (txt/pdf/docx). Default: bundled Niva Bupa AI Engineer JD")
    ap.add_argument("--rubric", help="Rubric JSON: [{key,name,weight,description}, ...]. Default: built-in")
    ap.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", "anthropic"), choices=list(PROVIDERS))
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL"))
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", ""))
    ap.add_argument("--concurrency", type=int, default=20, help="Parallel requests, 1-50 (default 20)")
    ap.add_argument("-o", "--output", default="ranked_candidates.xlsx", help=".xlsx or .csv")
    ap.add_argument("--cache", default=".ats_cache.sqlite")
    a = ap.parse_args()
    a.concurrency = min(50, max(1, a.concurrency))

    jd = DEFAULT_JD
    if a.jd:
        r = load_files([(Path(a.jd).name, Path(a.jd).read_bytes())])[0]
        jd = r.text or sys.exit(f"Could not read JD: {r.error}")
    rubric = records_to_rubric(json.loads(Path(a.rubric).read_text())) if a.rubric else DEFAULT_RUBRIC

    key_env = ENV_KEYS[a.provider]
    cfg = LLMConfig(provider=a.provider, model=a.model or PROVIDERS[a.provider]["model"],
                    api_key=(os.environ.get("LLM_API_KEY") or os.environ.get(key_env, "")) if key_env else "", base_url=a.base_url, samples=max(1, int(os.environ.get("LLM_SAMPLES", 1))), **env_limits(),
                    effort=os.environ.get("LLM_EFFORT", "low"))
    if a.provider in ("anthropic", "openai", "gemini") and not cfg.api_key:
        sys.exit(f"Set {key_env} first")

    resumes, dups = dedupe(load_files(collect(a.inputs)))
    print(f"{len(resumes)} resumes ({sum(r.ok for r in resumes)} readable), {len(dups)} duplicates skipped")

    t0 = time.time()

    def progress(done, total, res):
        if done % 25 == 0 or done == total:
            print(f"  {done}/{total}  {done / (time.time() - t0):.1f}/s", flush=True)

    results = asyncio.run(score_all(resumes, jd, rubric, cfg, a.concurrency, Cache(a.cache), progress))
    results += [ScoreResult(d.filename, d.sha256, "duplicate", error="duplicate") for d in dups]
    df = to_dataframe(results, rubric)
    out = Path(a.output)
    if out.suffix.lower() == ".csv":
        df.to_csv(out, index=False)
    else:
        out.write_bytes(to_excel(df, rubric, jd))
    errs = (df["Status"] == "error").sum()
    print(f"Done in {(time.time() - t0) / 60:.1f} min -> {out}  ({errs} errors; re-run to retry them)")
    print(df[["Rank", "Score", "Candidate", "File"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
