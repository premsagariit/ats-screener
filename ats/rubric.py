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
              "Must have worked with LLMs, RAG, embeddings, prompting, fine-tuning not just making API calls instead a system that uses it to create impact. " 
             "Infer it from what the projects/work actually do (e.g. a document Q&A app implies retrieval + embeddings, RAG implies vector embeddings and LLM API), even if not named."),
    Criterion("experience", "Experience", 20,
              "Mandatory: Any internship role or title (not just 'AI Engineer Intern') counts if the work actually done is AI/ML/GenAI-relevant; judge the work, not the title. Includes LLM finetuning, Research level project, LLM research, AI application, Hackathons, open source, papers. AI role based internship weighs more than other role based internships like AI > SDE > Other Tech > non-tech."),
    Criterion("agents", "Agent / orchestration frameworks", 15,
              "Must have projects/work that involve agentic ai frameworks like LangChain/LangGraph, LlamaIndex, AutoGen, CrewAI, Semantic kernal, Bedrock, Vertex, tool calling, fine-tuning small models. Credit projects whose design needs orchestration/tool use, not just a listed library."),
    Criterion("credibility", "Project credibility & logical coherence", 14,
              "Do projects hold together technically? Are metrics appropriate for the task, clearly defined (what, on what data, vs what baseline) and realistic? Are evaluations present? "
              "Is the claimed work feasible in the stated duration given the employer's scale and modern AI coding tools? Only clearly implausible scope or metrics score low; be lenient with unusual but possible claims."),
    Criterion("mlops", "Deployment & engineering practice", 12,
              "Good to have: Docker, Git"),
    Criterion("fundamentals_ml", "CS & ML fundamentals", 8,
              "ML/DL coursework and projects, ML Certifications, DSA. Evidence of understanding ML fundamentals and not just using LLMs, ML related Projects / Work"),
    Criterion("development", "Python & full-stack build ability", 6,
              "Good to have: Any programmaing language (like Python, Rust, Go, etc) and an API layer (Rest API, FASTAPI, etc) actually built in a project or job. A programming language listed in the skills section OR used in any work/project already earns a higher score here (a skills-section mention is enough)."),
    Criterion("preferred", "Preferred extras", 5,
              "Good to have: Multimodal, inference optimisation. Any mention, as a skill or in work, earns a high score."),
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
the highest weights; nice-to-haves get small weights, and their description must start with "Good to have:" (any mention earns a high score). Weights are integers summing to 100. Do not include \
criteria about age, gender, religion, caste, nationality, marital status, photos, or college prestige.

Return ONLY JSON: {"criteria": [{"key": "snake_case", "name": "short name", "weight": 20, \
"description": "what evidence earns a high score"}]}

JOB DESCRIPTION:
<<<
%s
>>>"""
