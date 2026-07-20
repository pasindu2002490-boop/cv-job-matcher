from __future__ import annotations

import re
import zipfile
from html import unescape
from pathlib import Path
from xml.etree import ElementTree

from .models import CandidateProfile


SKILL_LEXICON = {
    "accounting",
    "agile",
    "android",
    "angular",
    "api",
    "aws",
    "azure",
    "business analysis",
    "c#",
    "c++",
    "ci/cd",
    "cloud",
    "communication",
    "css",
    "customer service",
    "data analysis",
    "data engineering",
    "devops",
    "django",
    "docker",
    "excel",
    "fastapi",
    "figma",
    "finance",
    "flutter",
    "git",
    "go",
    "graphql",
    "html",
    "ios",
    "java",
    "javascript",
    "kotlin",
    "kubernetes",
    "langchain",
    "leadership",
    "linux",
    "llm",
    "machine learning",
    "marketing",
    "mlops",
    "node",
    "nlp",
    "php",
    "postgresql",
    "power bi",
    "product management",
    "project management",
    "python",
    "rag",
    "react",
    "rest",
    "ruby",
    "sales",
    "scrum",
    "sql",
    "swift",
    "tableau",
    "typescript",
    "ux",
}

TITLE_TERMS = {
    "accountant",
    "ai engineer",
    "ai/ml engineer",
    "analyst",
    "architect",
    "assistant",
    "consultant",
    "designer",
    "developer",
    "engineer",
    "machine learning engineer",
    "manager",
    "marketer",
    "nurse",
    "operator",
    "product manager",
    "project manager",
    "sales",
    "scientist",
    "software engineer",
    "specialist",
    "technician",
}

SECTION_HEADINGS = {
    "about",
    "certifications",
    "contact",
    "education",
    "experience",
    "projects",
    "skills",
    "summary",
}


def read_cv(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix == ".docx":
        return _read_docx(path)
    raise ValueError(f"Unsupported CV type: {suffix}. Use .txt, .md, .pdf, or .docx.")


def parse_cv(text: str) -> CandidateProfile:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    email = _first_match(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    phone = _first_match(r"(?:\+?\d[\d\s().-]{7,}\d)", text)
    links = tuple(dict.fromkeys(re.findall(r"https?://\S+|www\.\S+", text, re.I)))
    name = _guess_name(lines, email)
    skills = tuple(sorted(_extract_terms(text, SKILL_LEXICON)))
    likely_titles = tuple(sorted(_extract_title_terms(lines)))
    experience_lines = tuple(line for line in lines if line.startswith(("-", "*")) or _looks_like_role_line(line))
    return CandidateProfile(
        raw_text=text,
        name=name,
        email=email,
        phone=phone,
        links=links,
        skills=skills,
        likely_titles=likely_titles,
        experience_lines=experience_lines[:30],
    )


def _read_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    texts = []
    for node in root.iter():
        if node.tag.endswith("}t") and node.text:
            texts.append(node.text)
    return unescape("\n".join(texts))


def _read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return _repair_spaced_pdf_text("\n".join(pages))


def _repair_spaced_pdf_text(text: str) -> str:
    repaired = []
    for line in text.splitlines():
        if _looks_letter_spaced(line):
            words = re.split(r"\s{2,}", line.strip())
            repaired.append(" ".join(word.replace(" ", "") for word in words if word))
        else:
            repaired.append(line)
    return "\n".join(repaired)


def _looks_letter_spaced(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 6:
        return False
    tokens = [token for token in stripped.split(" ") if token]
    if len(tokens) < 3:
        return False
    short_tokens = [token for token in tokens if len(token) <= 2]
    return len(short_tokens) / len(tokens) > 0.7


def _first_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(0).strip() if match else ""


def _guess_name(lines: list[str], email: str) -> str:
    for line in lines[:5]:
        lower = line.lower()
        if email and email.lower() in lower:
            continue
        if "http" in lower or "linkedin" in lower or "@" in lower:
            continue
        if 1 < len(line.split()) <= 5:
            return line
    for line in lines[:60]:
        lower = line.lower()
        words = line.split()
        if lower in SECTION_HEADINGS:
            continue
        if email and email.lower() in lower:
            continue
        if any(char.isdigit() for char in line) or "@" in line or "http" in lower:
            continue
        if 1 < len(words) <= 4 and line.upper() == line:
            return line.title()
    return ""


def _extract_terms(text: str, terms: set[str]) -> set[str]:
    normalized = re.sub(r"[^a-z0-9+#/]+", " ", text.lower())
    found = set()
    for term in terms:
        escaped = re.escape(term.lower()).replace(r"\ ", r"\s+")
        if re.search(rf"(?<![a-z0-9+#]){escaped}(?![a-z0-9+#])", normalized):
            found.add(term)
    return found


def _extract_title_terms(lines: list[str]) -> set[str]:
    candidate_lines = lines[:25] + [line for line in lines if _looks_like_title_candidate(line)]
    text = "\n".join(candidate_lines)
    return _extract_terms(text, TITLE_TERMS)


def _looks_like_title_candidate(line: str) -> bool:
    lower = line.lower()
    if len(line.split()) > 14:
        return False
    context_terms = (
        "engineer",
        "developer",
        "scientist",
        "architect",
        "analyst",
        "assistant",
        "manager",
        "consultant",
        "specialist",
        "technician",
    )
    return any(term in lower for term in context_terms)


def _looks_like_role_line(line: str) -> bool:
    lower = line.lower()
    return any(term in lower for term in TITLE_TERMS) and len(line.split()) <= 14
