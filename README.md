# AI Resume Screener (ATS)

Scores every resume against a job description with an LLM, then ranks candidates from highest to lowest score.
Pre-loaded with the **Niva Bupa – AI Engineer (Graduate Hire)** JD and a matching rubric. Works for any JD.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py          # then open http://127.0.0.1:8000
```

1. **Settings:** copy `.env.example` to `.env` and set `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY` and
   `LLM_SAMPLES` (default 1). Your account's per-minute rate limits go in `LLM_RPM`, `LLM_ITPM` and `LLM_OTPM`
   (defaults 1000 / 500000 / 80000); requests are paced to stay under them. To try the app without a key, set
   `LLM_PROVIDER=mock`. The key stays on the server.
   Parallel requests are chosen per run with the slider on the Upload page (1–50, default 20). Each API call is
   retried up to 3 times with exponential backoff and jitter on rate limits, server errors, timeouts and bad JSON.
2. **Job & rubric:** edit the JD and the weighted criteria, or click *Draft from job description* to have the AI
   draft them for a new role.
3. **Upload & screen:** drop PDFs, DOCX or TXT files, or a single **ZIP** (the best option for 1000+ files), then
   click *Screen and rank resumes*.
4. **Ranked results:** filter, search, sort, open any candidate for the score breakdown and evidence, and download
   Excel or CSV.

Try it with synthetic data: `python tests/make_samples.py 1200 sample_resumes.zip`

## How scoring works

- The LLM reads the full resume and gives each rubric criterion points from **0 up to its weight**, with one line of
  evidence quoted or paraphrased from the resume. It follows a fixed scale: 0 = no evidence, 2–3 = only mentioned,
  ~55% = used in a real project, ~85% = substantial or production work, 100% = exceptional.
- **Final score = sum of the criterion points** (each capped at its weight, no normalisation), computed in Python
  rather than by the model, so it is consistent and auditable.
- Default rubric for this JD: LLMs & GenAI 25 · Agent frameworks 20 · Python & full-stack 15 ·
  CS fundamentals & ML 15 · LLMOps/MLOps 10 · Builder mindset 10 · Preferred extras 5.
- Only skills and experience are scored. The model is told to ignore name, gender, age, religion, caste,
  nationality, photo and college prestige. Eligibility rules such as graduation year are not applied.
- Resumes are treated as untrusted data. Hidden instructions like "rate me 10/10" are ignored and listed in
  **Red flags**.
- Temperature is 0, so re-scoring the same resume gives near-identical results.

## Built for 1000+ resumes

| Concern | How it's handled |
|---|---|
| Speed | Async parallel requests (slider, default 8). About 1000 resumes in roughly 5–15 minutes, depending on the provider's rate limits |
| Rate limits / outages | Automatic retry with exponential backoff on 429, 5xx and network errors |
| Bad model output | Tolerant JSON parsing plus one corrective retry |
| Crashes / re-runs | SQLite cache keyed by resume + JD + rubric + model, so an interrupted run resumes free and only failures are retried |
| Duplicates | The same resume uploaded twice, even as PDF *and* DOCX, is scored once |
| Scanned / corrupt PDFs | Flagged as *unreadable* for manual review instead of getting a silent zero |
| Very long resumes | Sent in full (no cut-off) |
| Rate limits | Requests paced to `LLM_RPM` / `LLM_ITPM` / `LLM_OTPM`; up to 3 retries per call, honouring `retry-after` |

Rough usage per 1000 resumes: about 2–3M input tokens and about 0.7M output tokens. The app shows an estimate
for your batch before you run it.

## Providers

| Provider | Default model (editable) | Key env var |
|---|---|---|
| Anthropic (Claude) | `claude-haiku-5-5` | `ANTHROPIC_API_KEY` |
| OpenAI | `gpt-4.1-mini` | `OPENAI_API_KEY` |
| Google Gemini | `gemini-2.5-flash` | `GEMINI_API_KEY` |
| OpenAI-compatible (Ollama, vLLM, LM Studio, Groq, Together…) | `llama3.1:8b` at `http://localhost:11434/v1` | `OPENAI_COMPATIBLE_API_KEY` (optional) |
| Offline demo | keyword heuristic, no AI | – |

Model names change over time, so set any current model ID in `LLM_MODEL`.
To add a provider, add a branch in `ats/llm.py::_complete_once`.

## Command line (no UI)

```bash
export ANTHROPIC_API_KEY=...
python cli.py ./resumes_folder --provider anthropic --concurrency 16 -o ranked.xlsx
python cli.py resumes.zip --jd other_jd.pdf --rubric rubric.json --provider gemini -o ranked.csv
```

## Project layout

```
server.py           Web server: serves web/ and the JSON API
web/                HTML, CSS and JS front end (index.html, style.css, app.js)
cli.py              Headless batch runner
ats/parsing.py      PDF/DOCX/TXT/ZIP text extraction, dedupe, unreadable detection
ats/llm.py          Provider-agnostic async client with retries
ats/rubric.py       Default JD + rubric, rubric-from-JD prompt
ats/scorer.py       Prompts, concurrent scoring, JSON parsing, cache, weighted score
ats/report.py       Ranking table, Excel/CSV export
tests/make_samples.py  Synthetic resume generator for load tests
```

## Notes

- Resume text is sent to whichever provider you choose. If candidate data must stay on your own servers, use a
  self-hosted model through the OpenAI-compatible option.
- AI scores are for shortlisting. Have a person review borderline and red-flagged candidates.
