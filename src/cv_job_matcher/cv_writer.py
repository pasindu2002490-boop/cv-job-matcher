from __future__ import annotations

from collections import Counter

from .models import CandidateProfile, MatchResult

CORE_SKILL_PRIORITY = (
    "machine learning",
    "python",
    "llm",
    "nlp",
    "rag",
    "mlops",
    "langchain",
    "data engineering",
    "data analysis",
    "fastapi",
    "postgresql",
    "docker",
    "kubernetes",
    "aws",
    "azure",
    "ci/cd",
    "cloud",
    "react",
    "typescript",
    "javascript",
    "sql",
)

LOW_SIGNAL_SKILLS = {"agile", "communication", "excel", "finance", "leadership", "sales", "scrum"}


def build_tailored_cv(profile: CandidateProfile, matches: list[MatchResult], country: str) -> str:
    top_skills = _top_matched_skills(matches) or list(profile.skills[:12])
    target_titles = _top_titles(matches)
    lines = []
    lines.append(f"# {profile.name or 'Candidate'}")
    contact = " | ".join(item for item in [profile.email, profile.phone, *profile.links] if item)
    if contact:
        lines.append(contact)
    lines.append("")
    lines.append("## Professional Summary")
    title_phrase = ", ".join(target_titles[:3]) if target_titles else "relevant roles"
    skill_phrase = ", ".join(top_skills[:8]) if top_skills else "the required skills"
    lines.append(
        f"Candidate targeting {title_phrase} in {country}, with experience aligned to {skill_phrase}. "
        "This CV draft is based on the supplied CV and the strongest live job matches from the current search."
    )
    lines.append("")
    lines.append("## Core Skills")
    if top_skills:
        lines.append(", ".join(top_skills[:18]))
    else:
        lines.append("Add verified skills from the original CV here.")
    lines.append("")
    lines.append("## Experience Highlights")
    highlights = profile.experience_lines[:12]
    if highlights:
        for line in highlights:
            lines.append(_normalize_bullet(line))
    else:
        lines.append("- Add quantified achievements from the original CV.")
    lines.append("")
    lines.append("## Target Role Keywords")
    for title in target_titles[:8]:
        lines.append(f"- {title}")
    if not target_titles:
        lines.append("- Add target roles after reviewing job matches.")
    lines.append("")
    lines.append("## Integrity Check")
    lines.append("- Review this draft before sending. Do not add skills, credentials, or work authorization claims that are not true.")
    return "\n".join(lines).strip() + "\n"


def _top_matched_skills(matches: list[MatchResult]) -> list[str]:
    counter: Counter[str] = Counter()
    for match in matches[:25]:
        counter.update(skill for skill in match.matched_skills if skill not in LOW_SIGNAL_SKILLS)
    priority = {skill: index for index, skill in enumerate(CORE_SKILL_PRIORITY)}
    return [
        skill
        for skill, _ in sorted(
            counter.items(),
            key=lambda item: (-item[1], priority.get(item[0], 999), item[0]),
        )
    ]


def _top_titles(matches: list[MatchResult]) -> list[str]:
    counter: Counter[str] = Counter()
    for match in matches[:25]:
        title = match.job.title.strip()
        if title:
            counter[title] += 1
    return [title for title, _ in counter.most_common()]


def _normalize_bullet(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("*"):
        stripped = stripped[1:].strip()
    if stripped.startswith("-"):
        return stripped
    return f"- {stripped}"
