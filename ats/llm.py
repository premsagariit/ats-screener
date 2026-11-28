"""Provider-agnostic async LLM client. Every provider returns raw text; JSON parsing happens in scorer.py.

Providers:
  anthropic  - Claude via api.anthropic.com
  openai     - OpenAI via api.openai.com
  gemini     - Google Gemini via generativelanguage.googleapis.com
  compatible - any OpenAI-compatible server (Ollama, vLLM, LM Studio, Groq, Together, Azure-style gateways)
  mock       - offline keyword heuristic for demos/tests, no API key, no network
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import httpx

PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "model": "claude-haiku-5-5", "base_url": "https://api.anthropic.com"},
    "openai": {"label": "OpenAI", "model": "gpt-4.1-mini", "base_url": "https://api.openai.com/v1"},
    "gemini": {"label": "Google Gemini", "model": "gemini-2.5-flash",
               "base_url": "https://generativelanguage.googleapis.com/v1beta"},
    "compatible": {"label": "OpenAI-compatible (Ollama, vLLM, Groq...)", "model": "llama3.1:8b",
                   "base_url": "http://localhost:11434/v1"},
    "mock": {"label": "Offline demo (keyword heuristic, no AI)", "model": "mock", "base_url": ""},
}


class LLMError(Exception):
    def __init__(self, msg: str, retryable: bool = False, retry_after: float | None = None):
        super().__init__(msg)
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass
class LLMConfig:
    provider: str = "anthropic"
    model: str = PROVIDERS["anthropic"]["model"]
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.0
    max_tokens: int = 4096  # 9 criteria + evidence overflowed 1500 and truncated the JSON
    timeout: float = 90.0
    samples: int = 1  # LLM calls per resume; >1 takes the per-criterion median
    seed: int = 7
    effort: str = "low"  # Anthropic: low|medium|high|xhigh|max; low = least reasoning
    max_retries: int = 3  # retries per call after the first attempt (so up to 4 attempts)
    rpm: int = 0   # account rate limits per minute; 0 = no pacing
    itpm: int = 0
    otpm: int = 0

    def resolved_base_url(self) -> str:
        return (self.base_url or PROVIDERS[self.provider]["base_url"]).rstrip("/")


def env_limits() -> dict:
    """Per-minute account limits from .env. Defaults are this account's Claude Sonnet 5.5 limits."""
    return {"rpm": int(os.environ.get("LLM_RPM", 1000)), "itpm": int(os.environ.get("LLM_ITPM", 500_000)),
            "otpm": int(os.environ.get("LLM_OTPM", 80_000))}


# ---------------------------------------------------------------- rate limiting
class RateLimiter:
    """Rolling 60-second window over requests, input tokens and output tokens.

    A reply's output size is unknown until it arrives, so each request reserves an estimate (a running average of
    real replies) and is corrected from the API's reported usage afterwards. Input is estimated from prompt length.
    ponytail: one limiter per server process; running several processes would need a shared store (e.g. Redis).
    """

    def __init__(self, rpm: int, itpm: int, otpm: int):
        self.rpm, self.itpm, self.otpm = rpm, itpm, otpm
        self.window: deque[list] = deque()  # [start time, input tokens, output tokens]
        self.avg_out = 800.0

    async def acquire(self, est_in: int) -> list:
        while True:
            now = time.monotonic()
            while self.window and now - self.window[0][0] >= 60:
                self.window.popleft()
            used_in = sum(e[1] for e in self.window)
            used_out = sum(e[2] for e in self.window)
            fits = ((not self.rpm or len(self.window) < self.rpm)
                    and (not self.itpm or used_in + est_in <= self.itpm)
                    and (not self.otpm or used_out + self.avg_out <= self.otpm))
            if fits or not self.window:  # an empty window always admits one request, however large
                entry = [now, est_in, self.avg_out]
                self.window.append(entry)
                return entry
            await asyncio.sleep(min(1.0, max(0.05, 60 - (now - self.window[0][0]))))

    def settle(self, entry: list, used_in: int | None, used_out: int | None):
        if used_in is not None:
            entry[1] = used_in
        if used_out is not None:
            entry[2] = used_out
            self.avg_out = 0.8 * self.avg_out + 0.2 * used_out


_LIMITERS: dict[tuple, RateLimiter] = {}


def _limiter(c: LLMConfig) -> RateLimiter | None:
    if c.provider == "mock" or not (c.rpm or c.itpm or c.otpm):
        return None
    key = (c.provider, c.model, c.rpm, c.itpm, c.otpm)
    return _LIMITERS.setdefault(key, RateLimiter(c.rpm, c.itpm, c.otpm))


def _usage(data: dict) -> tuple[int | None, int | None]:
    """(input tokens, output tokens) as reported by the provider, when it reports them."""
    u = data.get("usage") or {}
    if "input_tokens" in u:  # Anthropic (input_tokens excludes cache reads, as the rate limit does)
        return u.get("input_tokens"), u.get("output_tokens")
    if "prompt_tokens" in u:  # OpenAI-style
        return u.get("prompt_tokens"), u.get("completion_tokens")
    g = data.get("usageMetadata") or {}  # Gemini
    return g.get("promptTokenCount"), g.get("candidatesTokenCount")


def _anthropic_reasoning(c: LLMConfig) -> dict:
    """Minimise reasoning. Per Anthropic docs (thinking / effort pages): Claude 5 models accept output_config.effort;
    Haiku 5.x can switch thinking off ("disabled", effort <= high), Sonnet 5.5 uses "between_tools" instead, and
    Opus 5.x / Fable cannot disable it at all (so only low effort applies)."""
    m = c.model.lower()
    if not re.search(r"(haiku|sonnet|opus|fable|mythos)-5", m):
        return {}  # older models: no effort param
    out: dict = {"output_config": {"effort": c.effort}}
    if "haiku-5" in m:
        out["thinking"] = {"type": "disabled"}
    elif "sonnet-5-5" in m:
        out["thinking"] = {"type": "between_tools"}
    elif "sonnet-5" in m:  # Sonnet 5 (not 5.5) still accepts "disabled"
        out["thinking"] = {"type": "disabled"}
    return out


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._http = httpx.AsyncClient(timeout=cfg.timeout)
        self._limiter = _limiter(cfg)

    async def aclose(self):
        await self._http.aclose()

    async def complete(self, system: str, user: str, parse: Callable[[str], Any] | None = None,
                       images: list[bytes] | None = None, schema: dict | None = None) -> Any:
        """One logical call with up to cfg.max_retries retries, exponential backoff and jitter.

        Retried: rate limits (429/529), server errors (5xx), timeouts/network errors, and, when `parse` is given,
        a reply it rejects with ValueError (malformed JSON). Permanent errors (400, 401, 403, 404...) are raised at
        once. A `retry-after` header from the API is honoured as the minimum wait.
        """
        retries = max(0, self.cfg.max_retries)
        prompt = user
        for attempt in range(retries + 1):
            wait = 0.0
            try:
                raw = await self._complete_once(system, prompt, images or [], schema)
                return parse(raw) if parse else raw
            except LLMError as exc:
                if not exc.retryable or attempt == retries:
                    raise
                wait = exc.retry_after or 0.0
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == retries:
                    raise LLMError(f"Network error after {attempt + 1} attempts: {exc}") from exc
            except ValueError as exc:  # parse() rejected the reply
                if attempt == retries:
                    raise ValueError(f"Unparseable model output after {attempt + 1} attempts: {exc}") from exc
                prompt = user + "\n\nYour previous reply was not valid JSON. Reply with JSON only."
            backoff = min(60.0, 2.0 * 2 ** attempt)  # 2s, 4s, 8s ...
            await asyncio.sleep(max(wait, backoff) + random.uniform(0, backoff / 2))
        raise LLMError("unreachable")

    async def _post(self, url: str, headers: dict, payload: dict, params: dict | None = None) -> dict:
        r = await self._http.post(url, headers=headers, json=payload, params=params)
        if r.status_code in (408, 429, 529) or r.status_code >= 500:
            try:
                after = float(r.headers.get("retry-after", ""))
            except ValueError:
                after = None
            raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}", retryable=True, retry_after=after)
        if r.status_code >= 400:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:400]}")
        return r.json()

    async def _complete_once(self, system: str, user: str, images: list[bytes], schema: dict | None = None) -> str:
        c, base = self.cfg, self.cfg.resolved_base_url()
        if c.provider == "mock":
            return _mock_response(user)

        entry = await self._limiter.acquire((len(system) + len(user)) // 3 + 1600 * len(images)) if self._limiter else None
        data, text = await self._request(c, base, system, user, images, schema)
        if entry is not None:
            self._limiter.settle(entry, *_usage(data))
        return text

    async def _request(self, c: LLMConfig, base: str, system: str, user: str, images: list[bytes],
                       schema: dict | None = None) -> tuple[dict, str]:
        b64 = [base64.b64encode(i).decode() for i in images]  # page renders are JPEG
        if c.provider == "anthropic":
            body = {"model": c.model, "max_tokens": c.max_tokens,  # no temperature: rejected on Claude 5 models
                    "system": system, "messages": [{"role": "user", "content": [
                        *({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": d}} for d in b64),
                        {"type": "text", "text": user}]}]}
            body.update(_anthropic_reasoning(c))
            if schema:  # structured outputs: reply is guaranteed to match the schema (no malformed-JSON retries)
                body["output_config"] = {**body.get("output_config", {}),
                                         "format": {"type": "json_schema", "schema": schema}}
            headers = {"x-api-key": c.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
            try:
                data = await self._post(f"{base}/v1/messages", headers, body)
            except LLMError as exc:  # model/account without structured outputs: fall back to prompt-only JSON
                if not (schema and "HTTP 400" in str(exc) and ("format" in str(exc) or "schema" in str(exc))):
                    raise
                body["output_config"].pop("format")
                data = await self._post(f"{base}/v1/messages", headers, body)
            return data, "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

        if c.provider in ("openai", "compatible"):
            headers = {"content-type": "application/json"}
            if c.api_key:
                headers["authorization"] = f"Bearer {c.api_key}"
            payload = {"model": c.model, "temperature": c.temperature, "max_tokens": c.max_tokens, "seed": c.seed,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": [
                           *({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + d}} for d in b64),
                           {"type": "text", "text": user}]}],
                       "response_format": {"type": "json_object"}}
            try:
                data = await self._post(f"{base}/chat/completions", headers, payload)
            except LLMError as exc:  # some compatible servers reject response_format
                if c.provider == "compatible" and "response_format" in str(exc):
                    payload.pop("response_format")
                    data = await self._post(f"{base}/chat/completions", headers, payload)
                else:
                    raise
            return data, data["choices"][0]["message"]["content"] or ""

        if c.provider == "gemini":
            data = await self._post(
                f"{base}/models/{c.model}:generateContent",
                {"content-type": "application/json", "x-goog-api-key": c.api_key},
                {"systemInstruction": {"parts": [{"text": system}]},
                 "contents": [{"role": "user", "parts": [
                     *({"inlineData": {"mimeType": "image/jpeg", "data": d}} for d in b64), {"text": user}]}],
                 "generationConfig": {"temperature": c.temperature, "seed": c.seed, "maxOutputTokens": c.max_tokens,
                                      "responseMimeType": "application/json"}},
            )
            cands = data.get("candidates") or []
            if not cands:
                raise LLMError(f"Gemini returned no candidates: {json.dumps(data)[:300]}")
            return data, "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))

        raise LLMError(f"Unknown provider '{c.provider}'")


# ----------------------------------------------------------------------------------------------
# Offline mock: deterministic keyword heuristic so the whole app can be demoed / tested with no key.
_MOCK_KEYWORDS = {
    "llm_genai": ["llm", "gpt", "rag", "retrieval", "embedding", "prompt", "fine-tun", "lora", "hugging", "genai",
                  "generative", "vector", "faiss", "pinecone", "chroma"],
    "agents": ["langchain", "langgraph", "llamaindex", "llama index", "bedrock", "vertex", "agent", "crewai",
               "autogen", "tool calling", "function calling"],
    "python_fullstack": ["python", "react", "next.js", "nextjs", "fastapi", "flask", "django", "node", "express",
                         "streamlit", "typescript", "rest api"],
    "fundamentals_ml": ["data structures", "algorithm", "distributed", "pytorch", "tensorflow", "machine learning",
                        "deep learning", "transformer", "leetcode", "codeforces", "scikit"],
    "mlops": ["docker", "kubernetes", "ci/cd", "mlflow", "aws", "gcp", "azure", "github actions", "monitoring",
              "deployed"],
    "builder": ["built", "shipped", "launched", "hackathon", "open source", "publication", "users", "founded",
                "internship", "winner"],
    "preferred": ["multimodal", "multi-modal", "vision", "speech", "ocr", "whisper", "quantiz", "vllm", "onnx",
                  "tensorrt", "health", "insurance", "fintech", "claims"],
}


def _mock_response(user_prompt: str) -> str:
    m = re.search(r"<resume>(.*)</resume>", user_prompt, re.S)
    text = (m.group(1) if m else user_prompt).lower()
    weights = {k: float(w) for k, w in re.findall(r'"key": "([^"]+)", "name": "[^"]*", "weight": ([\d.]+)',
                                                  user_prompt.split("<resume>")[0])}
    keys = list(weights)
    scores = {}
    for k in keys:
        words = _MOCK_KEYWORDS.get(k, [])
        hits = [w for w in words if w in text]
        scores[k] = {"score": round(weights[k] * min(1, len(hits) / max(4, len(words) * 0.45))),
                     "evidence": ("Mentions: " + ", ".join(hits[:6])) if hits else "No evidence found"}
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    return json.dumps({
        "candidate_name": first_line.title()[:60],
        "email": email.group(0) if email else "",
        "phone": "",
        "current_or_latest_role": "",
        "total_experience_years": 0,
        "criteria": scores,
        "summary": "Offline demo score from keyword matching only (no AI model used).",
        "strengths": [], "gaps": [], "red_flags": [],
    })
