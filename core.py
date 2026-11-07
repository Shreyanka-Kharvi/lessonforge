"""LessonForge core: research, generation, 2 independent QC layers, export.

No UI code here, so every function can be tested on its own.
"""
import io
import json
import os
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

TOTAL_MINUTES = 45
STATE_DIR = "saved"
STATE_FILE = os.path.join(STATE_DIR, "current_lesson.json")
STEPS = ["research", "generate", "qc1", "qc2"]
STEP_LABELS = {
    "research": "Research",
    "generate": "Lesson generation",
    "qc1": "QC 1: Facts & sources",
    "qc2": "QC 2: Instructional quality",
}


class StepError(Exception):
    """A pipeline step failed in a way we can explain to the user."""


# --------------------------------------------------------------------------
# LLM adapter (Anthropic / Gemini / OpenAI) - pick via .env
# --------------------------------------------------------------------------
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5-5",
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
}


def provider() -> str:
    p = os.getenv("LLM_PROVIDER", "").strip().lower()
    if p:
        return p
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return "gemini"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return ""


def model_name(role: str = "main") -> str:
    """role = 'main' (writer) or 'qc' (auditor). QC can use a different model
    (QC_MODEL) so the checker is independent from the writer."""
    p = provider()
    if role == "qc" and os.getenv("QC_MODEL"):
        return os.getenv("QC_MODEL")
    return os.getenv("MODEL") or DEFAULT_MODELS.get(p, "")


def llm(system: str, user: str, temperature: float = 0.4, role: str = "main",
        max_tokens: int = 6000) -> str:
    p = provider()
    if not p:
        raise StepError("No API key found. Copy .env.example to .env and add a key.")
    model = model_name(role)
    last = None
    for attempt in range(3):
        try:
            if p == "anthropic":
                import anthropic
                c = anthropic.Anthropic()
                r = c.messages.create(
                    model=model, max_tokens=max_tokens, temperature=temperature,
                    system=system, messages=[{"role": "user", "content": user}])
                return "".join(b.text for b in r.content if getattr(b, "text", None))
            if p == "gemini":
                from google import genai
                key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
                c = genai.Client(api_key=key)
                r = c.models.generate_content(
                    model=model, contents=system + "\n\n" + user,
                    config={"temperature": temperature, "max_output_tokens": max_tokens})
                return r.text or ""
            if p == "openai":
                from openai import OpenAI
                c = OpenAI()
                r = c.chat.completions.create(
                    model=model, temperature=temperature, max_tokens=max_tokens,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}])
                return r.choices[0].message.content or ""
            raise StepError(f"Unknown LLM_PROVIDER '{p}'")
        except StepError:
            raise
        except Exception as e:  # network, rate limit, auth...
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise StepError(f"AI call failed after 3 attempts: {last}")


def parse_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("no JSON object found")
    return json.loads(t[a:b + 1])


def llm_json(system, user, temperature=0.4, role="main", max_tokens=6000) -> dict:
    out = llm(system, user, temperature, role, max_tokens)
    try:
        return parse_json(out)
    except Exception:
        fix = llm(system + "\nReturn ONLY one valid JSON object. No prose, no code fences.",
                  user, 0.0, role, max_tokens)
        try:
            return parse_json(fix)
        except Exception as e:
            raise StepError(f"AI did not return valid JSON: {e}")


# --------------------------------------------------------------------------
# Research: websites (DuckDuckGo), books (Google Books), videos (YouTube)
# --------------------------------------------------------------------------
HIGH_DOMAINS = ("developer.mozilla.org", "mozilla.org", "rfc-editor.org", "ietf.org",
                "w3.org", "python.org", "docs.python.org", "nih.gov", "nasa.gov",
                "who.int", "oracle.com/java", "kubernetes.io", "postgresql.org",
                "learn.microsoft.com", "cloud.google.com", "aws.amazon.com", "git-scm.com")
MEDIUM_DOMAINS = ("wikipedia.org", "britannica.com", "geeksforgeeks.org", "w3schools.com",
                  "freecodecamp.org", "khanacademy.org", "ibm.com", "cloudflare.com",
                  "redhat.com", "digitalocean.com", "github.com", "stackoverflow.com")


def rate_reliability(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    if host.endswith(".edu") or host.endswith(".gov") or ".ac." in host:
        return "high"
    if any(host == d or host.endswith("." + d) or host.endswith(d) for d in HIGH_DOMAINS):
        return "high"
    if any(host.endswith(d) for d in MEDIUM_DOMAINS):
        return "medium"
    return "low"


def _ddgs():
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    return DDGS()


def search_web(query: str, n: int = 6) -> list:
    out = []
    try:
        for r in _ddgs().text(query, max_results=n) or []:
            url = r.get("href") or r.get("url")
            if url:
                out.append({"type": "website", "title": r.get("title", url), "url": url,
                            "snippet": r.get("body", ""), "reliability": rate_reliability(url)})
    except Exception:
        pass
    return out


def search_books(query: str, n: int = 4) -> list:
    out = []
    try:
        r = requests.get("https://www.googleapis.com/books/v1/volumes",
                         params={"q": query, "maxResults": n, "printType": "books"}, timeout=10)
        r.raise_for_status()
        for it in r.json().get("items", []):
            v = it.get("volumeInfo", {})
            authors = ", ".join(v.get("authors", [])) or "Unknown author"
            title = f'{v.get("title", "Untitled")} - {authors} ({v.get("publishedDate", "n.d.")[:4]})'
            out.append({"type": "book", "title": title,
                        "url": v.get("infoLink") or it.get("selfLink", ""),
                        "snippet": (v.get("description") or "")[:500],
                        "reliability": "medium"})
    except Exception:
        pass
    return out


def search_videos(query: str, n: int = 4) -> list:
    out = []
    key = os.getenv("YOUTUBE_API_KEY")
    if key:
        try:
            r = requests.get("https://www.googleapis.com/youtube/v3/search", params={
                "part": "snippet", "q": query, "type": "video", "maxResults": n,
                "videoEmbeddable": "true", "relevanceLanguage": "en", "key": key}, timeout=10)
            r.raise_for_status()
            for it in r.json().get("items", []):
                sn = it["snippet"]
                out.append({"type": "video",
                            "title": f'{sn["title"]} ({sn["channelTitle"]})',
                            "url": "https://www.youtube.com/watch?v=" + it["id"]["videoId"],
                            "snippet": sn.get("description", ""), "reliability": "unverified"})
        except Exception:
            pass
    if not out:  # fallback without API key
        try:
            for r in _ddgs().videos(query + " youtube", max_results=n) or []:
                url = r.get("content") or r.get("url")
                if url and ("youtube.com" in url or "youtu.be" in url):
                    out.append({"type": "video",
                                "title": f'{r.get("title", url)} ({r.get("uploader", "YouTube")})',
                                "url": url, "snippet": r.get("description", ""),
                                "reliability": "unverified"})
        except Exception:
            pass
    return out


def fetch_excerpt(url: str, limit: int = 2500) -> str:
    """Download a page and return readable text so the AI is grounded in real content."""
    try:
        from bs4 import BeautifulSoup
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 LessonForge"})
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            t.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
        return text[:limit]
    except Exception:
        return ""


def check_link(url: str):
    try:
        h = {"User-Agent": "Mozilla/5.0 LessonForge"}
        r = requests.head(url, timeout=6, allow_redirects=True, headers=h)
        if r.status_code >= 400:
            r = requests.get(url, timeout=8, allow_redirects=True, headers=h, stream=True)
        return r.status_code < 400
    except Exception:
        return False


def research(title: str, objective: str) -> list:
    q = f"{title} {objective}"[:200]
    web = search_web(q, 6)
    seen = {w["url"] for w in web}
    for w in search_web(f"{title} documentation tutorial explained", 4):
        if w["url"] not in seen:
            web.append(w)
            seen.add(w["url"])
    # prefer reliable sources first, keep 6
    order = {"high": 0, "medium": 1, "low": 2}
    web.sort(key=lambda s: order.get(s["reliability"], 3))
    web = web[:6]
    books = search_books(title, 4)[:3]
    videos = search_videos(f"{title} tutorial", 4)[:3]
    sources = web + books + videos
    if not sources:
        raise StepError("Research returned nothing. Check your internet connection and retry.")

    # excerpts for websites (parallel), link validation for all
    with ThreadPoolExecutor(max_workers=8) as ex:
        for s, ex_text in zip(web, ex.map(lambda s: fetch_excerpt(s["url"]), web)):
            s["excerpt"] = ex_text
        for s, ok in zip(sources, ex.map(lambda s: check_link(s["url"]) if s["url"] else False, sources)):
            s["link_ok"] = ok
    sources = [s for s in sources if s.get("link_ok") or s["type"] == "book"]
    for i, s in enumerate(sources, 1):
        s["id"] = i
    if not sources:
        raise StepError("All found links were unreachable. Retry the research step.")
    return sources


def sources_for_prompt(sources: list) -> str:
    lines = []
    for s in sources:
        body = s.get("excerpt") or s.get("snippet") or ""
        lines.append(f'[{s["id"]}] ({s["type"]}, reliability: {s["reliability"]}) '
                     f'{s["title"]} | {s["url"]}\n    Content: {body[:1800]}')
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Lesson generation
# --------------------------------------------------------------------------
GEN_SYSTEM = """You are an expert instructional designer and subject-matter researcher.
You write accurate, engaging, well-structured lessons grounded ONLY in the provided sources
plus well-established general knowledge. Never invent facts, statistics, quotes or URLs.
Cite sources inline as [1], [2] using the source numbers given. Respond with ONE JSON object only."""


def generate_lesson(title: str, objective: str, sources: list) -> dict:
    user = f"""Lesson title: {title}
Learning objective: {objective}

SOURCES (cite by number):
{sources_for_prompt(sources)}

Create a lesson that a learner can finish in EXACTLY {TOTAL_MINUTES} minutes (self-study or
instructor-led), covering explanations, examples, an activity and knowledge checks.
Section minutes MUST add up to {TOTAL_MINUTES}. Use these sections (rename headings sensibly):
 1. Hook & objective (about 3 min)
 2. Core concepts (about 15 min)
 3. Worked examples (about 10 min)
 4. Hands-on activity (about 8 min)
 5. Knowledge check (about 6 min; 4-5 questions WITH answers and short explanations)
 6. Recap & next steps (about 3 min)
Write each section's "content" in Markdown (code blocks allowed). Keep content realistic for the
minutes allotted. Add inline citations [n] next to claims that come from sources.
From the VIDEO sources only, recommend 1-3 supplementary videos (optional viewing, NOT counted
in the 45 minutes) and say why each is useful.

Return JSON exactly in this shape:
{{"overview": "2-3 sentence summary",
  "sections": [{{"heading": "...", "minutes": 3, "content": "markdown..."}}],
  "recommended_videos": [{{"source_id": 7, "why": "..."}}]}}"""
    data = llm_json(GEN_SYSTEM, user, temperature=0.5, role="main", max_tokens=8000)
    secs = data.get("sections")
    if not isinstance(secs, list) or not secs:
        raise StepError("AI returned no lesson sections. Retry the step.")
    clean = []
    for s in secs:
        try:
            m = int(round(float(s.get("minutes", 0))))
        except Exception:
            m = 0
        clean.append({"heading": re.sub(r"\s*\(\d+\s*min\)\s*$", "", re.sub(r"^\s*\d+[.)]\s*", "", str(s.get("heading", "Section")))).strip(),
                      "minutes": max(m, 1), "content": str(s.get("content", "")).strip()})
    diff = TOTAL_MINUTES - sum(x["minutes"] for x in clean)
    if diff != 0:  # force the 45-minute budget onto the largest section
        big = max(range(len(clean)), key=lambda i: clean[i]["minutes"])
        clean[big]["minutes"] = max(1, clean[big]["minutes"] + diff)
    valid_ids = {s["id"] for s in sources}
    vids = []
    for v in data.get("recommended_videos", []) or []:
        try:
            sid = int(v.get("source_id"))
        except Exception:
            continue
        if sid in valid_ids:
            vids.append({"source_id": sid, "why": str(v.get("why", ""))})
    return {"overview": str(data.get("overview", "")), "sections": clean, "recommended_videos": vids}


# --------------------------------------------------------------------------
# QC layers (two separate prompts, personas, temperatures, optional model)
# --------------------------------------------------------------------------
def lesson_text_for_qc(lesson: dict) -> str:
    parts = [f"OVERVIEW: {lesson.get('overview', '')}"]
    for i, s in enumerate(lesson["sections"]):
        parts.append(f'### SECTION {i} | {s["heading"]} | {s["minutes"]} min\n{s["content"]}')
    return "\n\n".join(parts)


QC1_SYSTEM = """You are a meticulous fact-checker and research auditor. You do NOT judge teaching style.
Your job: verify factual correctness against the provided sources and your expert knowledge,
find unsupported/misleading/incorrect claims, assess source reliability and relevance, and find
gaps or inconsistencies in the content. Be strict. Respond with ONE JSON object only."""

QC2_SYSTEM = """You are a senior instructional-design reviewer. You do NOT fact-check sources.
Your job: check alignment with the learning objective, clarity, structure, relevance, whether
explanations/examples/practical applications/knowledge checks are sufficient, and whether the
45-minute timing is realistic for the amount of content. Respond with ONE JSON object only."""

ISSUE_RULES = """For every issue, "original_text" MUST be an exact, copy-pasted substring (max ~300 chars)
of the lesson section so a replacement can be applied automatically; use "" only if the issue is a
missing item, and then put the text to ADD in "suggested_text" (it will be appended to the section).
"suggested_text" is the improved replacement text. Keep suggested_text concise."""


def _tag_issues(report: dict, prefix: str) -> dict:
    issues = report.get("issues") or []
    for i, it in enumerate(issues, 1):
        it["id"] = f"{prefix}-{i}"
        it["status"] = "open"
        try:
            it["section_index"] = int(it.get("section_index", 0))
        except Exception:
            it["section_index"] = 0
    report["issues"] = issues
    report["created"] = datetime.now().isoformat(timespec="seconds")
    return report


def run_qc1(title, objective, lesson, sources) -> dict:
    user = f"""Lesson title: {title}
Learning objective: {objective}

SOURCES:
{sources_for_prompt(sources)}

LESSON TO AUDIT:
{lesson_text_for_qc(lesson)}

{ISSUE_RULES}
Issue types: unsupported_claim | misleading | factual_error | source_problem | gap | inconsistency.

Return JSON:
{{"score": 0-100 factual reliability score,
  "summary": "short verdict",
  "source_assessment": [{{"source_id": 1, "reliability": "high|medium|low", "relevance": "high|medium|low", "comment": "..."}}],
  "issues": [{{"type": "...", "severity": "high|medium|low", "section_index": 0, "original_text": "...",
              "problem": "...", "suggested_text": "..."}}]}}"""
    return _tag_issues(llm_json(QC1_SYSTEM, user, 0.1, "qc", 6000), "F")


def run_qc2(title, objective, lesson) -> dict:
    user = f"""Lesson title: {title}
Learning objective: {objective}
Target duration: {TOTAL_MINUTES} minutes

LESSON TO REVIEW:
{lesson_text_for_qc(lesson)}

{ISSUE_RULES}
Issue types: objective_alignment | clarity | structure | missing_example | missing_activity |
weak_knowledge_check | timing | relevance.

Return JSON:
{{"score": 0-100 instructional quality score,
  "summary": "short verdict",
  "objective_alignment": "how well each part of the objective is covered",
  "timing_assessment": "is {TOTAL_MINUTES} min realistic? why",
  "issues": [{{"type": "...", "severity": "high|medium|low", "section_index": 0, "original_text": "...",
              "problem": "...", "suggested_text": "..."}}]}}"""
    return _tag_issues(llm_json(QC2_SYSTEM, user, 0.3, "qc", 6000), "I")


def apply_issue(lesson: dict, issue: dict):
    """Apply one QC suggestion. Returns (ok, message, before, after)."""
    idx = issue.get("section_index", 0)
    if not (0 <= idx < len(lesson["sections"])):
        return False, "Section no longer exists - edit manually.", "", ""
    sec = lesson["sections"][idx]
    orig, new = (issue.get("original_text") or ""), (issue.get("suggested_text") or "")
    if not new:
        return False, "No suggested text to apply.", "", ""
    if orig:
        if orig in sec["content"]:
            sec["content"] = sec["content"].replace(orig, new, 1)
            return True, "Applied.", orig, new
        # also search other sections (AI may point to the wrong index)
        for s in lesson["sections"]:
            if orig in s["content"]:
                s["content"] = s["content"].replace(orig, new, 1)
                return True, "Applied (found in a different section).", orig, new
        return False, "Original text was edited or not found - apply manually.", orig, new
    sec["content"] = sec["content"].rstrip() + "\n\n" + new
    return True, "Added to the end of the section.", "", new


# --------------------------------------------------------------------------
# State + autosave (never lose instructor work)
# --------------------------------------------------------------------------
def new_state() -> dict:
    return {"title": "", "objective": "", "status": {s: "pending" for s in STEPS},
            "errors": {}, "sources": [], "lesson": None, "qc1": None, "qc2": None,
            "edited": False, "version": 0, "saved_at": ""}


def save_state(state: dict, path: str = STATE_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state["saved_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)  # atomic: a crash can't corrupt the saved file


def load_state(path: str = STATE_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        base = new_state()
        base.update(st)
        return base
    except Exception:
        return None


def backup_state(state: dict):
    if state.get("lesson"):
        save_state(state, os.path.join(STATE_DIR, "backups",
                                       datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"))


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
def lesson_markdown(state: dict) -> str:
    l = state["lesson"]
    md = [f'# {state["title"]}', f'**Learning objective:** {state["objective"]}', "",
          f'_Duration: {sum(s["minutes"] for s in l["sections"])} minutes_', "", l.get("overview", ""), ""]
    for s in l["sections"]:
        md += [f'## {s["heading"]} ({s["minutes"]} min)', "", s["content"], ""]
    if l.get("recommended_videos"):
        by_id = {s["id"]: s for s in state["sources"]}
        md += ["## Supplementary videos (optional)", ""]
        for v in l["recommended_videos"]:
            s = by_id.get(v["source_id"])
            if s:
                md.append(f'- [{s["title"]}]({s["url"]}) - {v["why"]}')
        md.append("")
    md += ["## References", ""]
    for s in state["sources"]:
        md.append(f'[{s["id"]}] {s["title"]} - {s["url"]}')
    return "\n".join(md)


def references_markdown(state: dict) -> str:
    md = [f'# References for: {state["title"]}', ""]
    for s in state["sources"]:
        ok = {True: "link OK", False: "link unreachable"}.get(s.get("link_ok"), "not checked")
        md.append(f'{s["id"]}. **{s["title"]}**  \n   Type: {s["type"]} | Reliability: {s["reliability"]} | {ok}  \n   {s["url"]}')
    return "\n".join(md)


def qc_markdown(report: dict, name: str) -> str:
    if not report:
        return f"# {name}\n\nNot run yet."
    md = [f"# {name}", f'Score: **{report.get("score", "n/a")}/100**', "", report.get("summary", ""), ""]
    for k in ("objective_alignment", "timing_assessment"):
        if report.get(k):
            md += [f'**{k.replace("_", " ").title()}:** {report[k]}', ""]
    if report.get("source_assessment"):
        md += ["## Source assessment", ""]
        for a in report["source_assessment"]:
            md.append(f'- [{a.get("source_id")}] reliability: {a.get("reliability")}, '
                      f'relevance: {a.get("relevance")} - {a.get("comment", "")}')
        md.append("")
    md += ["## Issues", ""]
    for it in report.get("issues", []):
        md += [f'### {it["id"]} [{it.get("severity", "")}] {it.get("type", "")} (status: {it["status"]})',
               f'- **Section:** {it.get("section_index")}',
               f'- **Problem:** {it.get("problem", "")}',
               f'- **Original:** {it.get("original_text", "")}',
               f'- **Suggested:** {it.get("suggested_text", "")}', ""]
    return "\n".join(md)


def build_docx(state: dict) -> bytes:
    from docx import Document
    d = Document()
    d.add_heading(state["title"], 0)
    d.add_paragraph("Learning objective: " + state["objective"])
    l = state["lesson"]
    d.add_paragraph(l.get("overview", ""))
    for s in l["sections"]:
        d.add_heading(f'{s["heading"]} ({s["minutes"]} min)', 1)
        for line in s["content"].splitlines():
            if line.startswith("#"):
                d.add_heading(line.lstrip("# ").strip(), 2)
            elif line.strip().startswith(("- ", "* ")):
                d.add_paragraph(line.strip()[2:], style="List Bullet")
            elif line.strip():
                d.add_paragraph(line.replace("**", "").replace("`", ""))
    d.add_heading("References", 1)
    for s in state["sources"]:
        d.add_paragraph(f'[{s["id"]}] {s["title"]} - {s["url"]}')
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def build_zip(state: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("lesson.md", lesson_markdown(state))
        z.writestr("references.md", references_markdown(state))
        z.writestr("qc1_factual_report.md", qc_markdown(state.get("qc1"), "QC Layer 1: Research & Factual Accuracy"))
        z.writestr("qc2_instructional_report.md", qc_markdown(state.get("qc2"), "QC Layer 2: Instructional Quality"))
        z.writestr("lesson.docx", build_docx(state))
        z.writestr("full_state.json", json.dumps(state, indent=2, ensure_ascii=False))
    return buf.getvalue()
