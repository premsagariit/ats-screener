"""Scoring rubric: weighted criteria the LLM scores 0-10; the final score is computed in Python."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_JD = (Path(__file__).parent / "default_jd.txt").read_text(encoding="utf-8")


@dataclass
class Criterion:
    key: str
    name: str
    weight: float
    description: str


# Fixed rubric (100 pts). Every criterion is judged on what the candidate BUILT (projects, internships,
# research), not on keywords: skills-section-only mentions score low, project/experience evidence scores high.
DEFAULT_RUBRIC: list[Criterion] = [
    Criterion("llm_genai", "LLM & GenAI hands-on", 20,
              "Real use of LLMs, RAG, embeddings, prompting, fine-tuning not just making API calls instead a system that uses it to create impact." 
             "Infer it from what the projects/work actually do (e.g. a document Q&A app implies retrieval + embeddings, vector embeddings and LLM API via RAG), even if not named."),
    Criterion("experience", "Experience", 20,
              "Any role or title (not just 'AI Engineer Intern') counts if the work actually done is AI/ML/GenAI-relevant; judge the work, not the title. Includes LLM finetuning, LLM research, AI application, Hackathons, open source, papers."),
    Criterion("agents", "Agent / orchestration frameworks", 15,
              "Projects that involve agentic ai frameworks like LangChain/LangGraph, LlamaIndex, Semantic kernal, Bedrock, Vertex, tool calling, fine-tuning small models. Credit projects whose design needs orchestration/tool use, not just a listed library."),
    Criterion("credibility", "Project credibility & logical coherence", 14,
              "Do projects hold together technically? Are metrics appropriate for the task, clearly defined (what, on what data, vs what baseline) and realistic rather than just a big number? Are evaluations present? "
              "Is the claimed work feasible in the stated duration given the employer's scale (big firms: narrow scoped intern work; startups: broad ownership) and modern AI coding tools? Implausible scope/metrics score low."),
    Criterion("mlops", "Deployment & engineering practice", 12,
              "Docker, cloud, Git, live demo links [Not mandatory] - shown by how projects were shipped."),
    Criterion("fundamentals_ml", "CS & ML fundamentals", 8,
              "DSA, ML/DL coursework and projects"),
    Criterion("development", "Python & full-stack build ability", 6,
              "Any programmaing language (like Python) and an API layer (Rest API, FASTAPI, etc) actually built in a project or job. A programming language listed in the skills section OR used in any work/project already earns a higher score here (a skills-section mention is enough)."),
    Criterion("preferred", "Preferred extras", 5,
              "Multimodal, inference optimisation, health/insurance domain."),
]


def rubric_to_records(rubric: list[Criterion]) -> list[dict]:
    return [asdict(c) for c in rubric]


def records_to_rubric(records: list[dict]) -> list[Criterion]:
    out = []
    for i, r in enumerate(records):
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        key = str(r.get("key") or "").strip() or f"c{i+1}"
        out.append(Criterion(key, name, float(r.get("weight") or 0), str(r.get("description") or "")))
    return out


def rubric_fingerprint(rubric: list[Criterion], jd: str) -> str:
    import hashlib

    blob = json.dumps({"r": rubric_to_records(rubric), "jd": jd.strip()}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


RUBRIC_FROM_JD_PROMPT = """You design resume-screening rubrics. Read the job description and produce 5-8 \
weighted criteria that a recruiter would use to judge SKILLS and EXPERIENCE fit. Must-have requirements get \
the highest weights; nice-to-haves get small weights. Weights are integers summing to 100. Do not include \
criteria about age, gender, religion, caste, nationality, marital status, photos, or college prestige.

Return ONLY JSON: {"criteria": [{"key": "snake_case", "name": "short name", "weight": 20, \
"description": "what evidence earns a high score"}]}

JOB DESCRIPTION:
<<<
%s
>>>"""
