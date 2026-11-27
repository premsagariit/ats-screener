"""Self-check for retries (max 3, backoff + jitter, retry-after) and the per-minute rate limiter.
No network, no API key:  python tests/test_resilience.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

import ats.llm as llm  # noqa: E402

OK = {"content": [{"type": "text", "text": '{"criteria": {}}'}], "usage": {"input_tokens": 100, "output_tokens": 50}}


def client(responses, **cfg):
    """LLMClient whose HTTP calls return `responses` in order; records request bodies."""
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        status, body, headers = responses[min(len(seen) - 1, len(responses) - 1)]
        return httpx.Response(status, json=body, headers=headers)

    c = llm.LLMClient(llm.LLMConfig(provider="anthropic", model="claude-sonnet-5-5", api_key="k", **cfg))
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c, seen


async def main():
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(d):
        sleeps.append(d)
        await real_sleep(0)

    llm.asyncio.sleep = fake_sleep
    try:
        # transient errors are retried, then succeed; retry-after is the minimum wait
        c, seen = client([(429, {"error": "rate"}, {"retry-after": "7"}), (503, {}, {}), (200, OK, {})])
        assert await c.complete("s", "u") == '{"criteria": {}}'
        assert len(seen) == 3 and sleeps[0] >= 7 and 4 <= sleeps[1] <= 6, (len(seen), sleeps)

        # max 3 retries: 4 attempts, then the error is raised
        sleeps.clear()
        c, seen = client([(500, {}, {})])
        try:
            await c.complete("s", "u")
            raise AssertionError("expected LLMError")
        except llm.LLMError:
            pass
        assert len(seen) == 4 and len(sleeps) == 3, (len(seen), sleeps)
        assert all(2 * 2 ** i <= d <= 3 * 2 ** i for i, d in enumerate(sleeps)), sleeps  # exponential + jitter

        # permanent errors are not retried
        c, seen = client([(400, {"error": "bad"}, {})])
        try:
            await c.complete("s", "u")
            raise AssertionError("expected LLMError")
        except llm.LLMError:
            pass
        assert len(seen) == 1

        # malformed JSON is retried with a corrective note, through the same retry budget
        bad = {"content": [{"type": "text", "text": "not json"}], "usage": {"input_tokens": 1, "output_tokens": 1}}
        c, seen = client([(200, bad, {}), (200, OK, {})])
        assert await c.complete("s", "u", parse=json.loads) == {"criteria": {}}
        assert len(seen) == 2 and "not valid JSON" in seen[1]["messages"][0]["content"]
    finally:
        llm.asyncio.sleep = real_sleep

    # rate limiter: 2 requests/min -> the third waits
    lim = llm.RateLimiter(rpm=2, itpm=0, otpm=0)
    await lim.acquire(10)
    await lim.acquire(10)
    try:
        await asyncio.wait_for(lim.acquire(10), 0.3)
        raise AssertionError("third request should wait")
    except asyncio.TimeoutError:
        pass

    # output-token budget: settled usage counts against the window
    lim = llm.RateLimiter(rpm=0, itpm=0, otpm=1000)
    e = await lim.acquire(10)
    lim.settle(e, 10, 990)
    try:
        await asyncio.wait_for(lim.acquire(10), 0.3)
        raise AssertionError("output budget should be exhausted")
    except asyncio.TimeoutError:
        pass

    # empty window always admits one request, even if it is larger than the limit
    lim = llm.RateLimiter(rpm=0, itpm=5, otpm=0)
    await asyncio.wait_for(lim.acquire(10_000), 0.3)
    print("resilience checks passed")


if __name__ == "__main__":
    asyncio.run(main())
