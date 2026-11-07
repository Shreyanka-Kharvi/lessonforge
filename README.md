# 🎓 LessonForge: AI-Powered Lesson Creation Tool

Enter a **Lesson Title + Learning Objective**. LessonForge researches books, websites and YouTube,
writes a **45-minute lesson**, audits it with **two independent QC layers**, lets the instructor
**review / edit / apply fixes / preview**, and **exports** everything.

## Quick start
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # add ONE API key (Anthropic, Gemini or OpenAI)
streamlit run app.py
```
Click a **Test 1 / Test 2** button in the sidebar to load the sample inputs.

## How it works
| Step | What happens |
|---|---|
| Research | DuckDuckGo (websites) + Google Books API + YouTube (API or fallback). Page text is downloaded as excerpts so the AI is grounded in real content. Every link is checked; dead links are dropped. Each source gets a reliability rating (high/medium/low/unverified) from its domain. |
| Generation | One AI call produces JSON: 6 timed sections that sum to exactly 45 min (enforced in code), inline citations `[n]`, knowledge check with answers, and 1-3 optional videos. |
| QC 1 (facts) | Separate prompt, "fact-checker" persona, temperature 0.1, optional different model (`QC_MODEL`). Flags unsupported / misleading / incorrect claims, rates sources, finds gaps. |
| QC 2 (instruction) | Separate prompt, "instructional designer" persona. Checks objective alignment, clarity, structure, examples, activities, timing realism. |
| Review | Edit every section, apply or dismiss each QC suggestion (with before/after diff), preview, re-run QC after edits. |
| Export | `.md`, `.docx`, references, both QC reports, or one `.zip`. |

## Error handling
- Empty inputs show a clear message; missing API key is detected.
- Each step is wrapped separately; a failure shows the reason and a **Retry** button for that step only.
- State autosaves atomically to `saved/current_lesson.json` after every step and every edit.
- Starting a new lesson first backs up the old one to `saved/backups/`.
- QC suggestions never overwrite text silently: if the original text was edited, the fix is refused with a message.

## Design decisions
- **Streamlit**: fastest path to a working review/edit UI; the brief prioritizes function over design.
- **Core/UI split** (`core.py`, `app.py`): logic is testable without the UI.
- **Provider-agnostic LLM adapter**: swap Anthropic/Gemini/OpenAI via `.env`.
- **QC independence**: different prompt, persona, temperature, and optionally a different model, and QC 1 also gets the raw source text.
- **Code enforces what prompts can't**: the 45-minute total, valid citation ids, valid JSON (one automatic repair retry).

## Limitations
- Source reliability is a domain heuristic plus the AI's assessment, not a guarantee. Always skim the QC 1 report.
- Video content is judged from title/description, not the transcript.

## Resources and AI acknowledgement
Built with Streamlit, DuckDuckGo search (`ddgs`), Google Books API, YouTube Data API, BeautifulSoup,
python-docx. AI assistants (Claude) were used for coding help and for the lesson/QC generation inside
the tool; I reviewed and can explain all of the code.
