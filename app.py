"""LessonForge - AI-powered Lesson Creation Tool (Streamlit UI)."""
import difflib
import time

import pandas as pd
import streamlit as st

import core

st.set_page_config(page_title="LessonForge", page_icon="🎓", layout="wide")

SAMPLES = {
    "Test 1: HTTP": ("HTTP Requests and Responses",
                     "Explain GET and POST methods and distinguish between 2xx, 4xx, and 5xx response status categories."),
    "Test 2: Git": ("Introduction to Git Branching",
                    "Create, switch, and merge branches in Git, and explain why branching helps teams work in parallel."),
}


def set_sample(name):
    st.session_state["in_title"], st.session_state["in_obj"] = SAMPLES[name]


# ---------------- state (autosaved to disk) ----------------
if "S" not in st.session_state:
    st.session_state.S = core.load_state() or core.new_state()
S = st.session_state.S
for k, d in (("in_title", S["title"]), ("in_obj", S["objective"])):
    st.session_state.setdefault(k, d)


def sync_editor():
    """Pull the instructor's live edits from the text boxes into state + autosave."""
    if not S.get("lesson"):
        return
    changed = False
    for i, sec in enumerate(S["lesson"]["sections"]):
        for fld, key in (("content", f"c_{i}_{S['version']}"), ("heading", f"h_{i}_{S['version']}"),
                         ("minutes", f"m_{i}_{S['version']}")):
            if key in st.session_state and st.session_state[key] != sec[fld]:
                sec[fld] = st.session_state[key]
                changed = True
    if changed:
        S["edited"] = True
        core.save_state(S)


sync_editor()


# ---------------- pipeline ----------------
def run_step(step: str) -> bool:
    """Run one step. Failures never touch saved work; they are stored so the user can retry."""
    try:
        if step == "research":
            S["sources"] = core.research(S["title"], S["objective"])
        elif step == "generate":
            if not S["sources"]:
                raise core.StepError("Run research first.")
            S["lesson"] = core.generate_lesson(S["title"], S["objective"], S["sources"])
            S["version"] += 1
        elif step == "qc1":
            if not S["lesson"]:
                raise core.StepError("Generate the lesson first.")
            S["qc1"] = core.run_qc1(S["title"], S["objective"], S["lesson"], S["sources"])
        elif step == "qc2":
            if not S["lesson"]:
                raise core.StepError("Generate the lesson first.")
            S["qc2"] = core.run_qc2(S["title"], S["objective"], S["lesson"])
        S["status"][step] = "done"
        S["errors"].pop(step, None)
        ok = True
    except Exception as e:
        S["status"][step] = "failed"
        S["errors"][step] = str(e)
        ok = False
    core.save_state(S)
    return ok


def run_pipeline():
    for step in core.STEPS:
        if S["status"][step] != "done":
            with st.spinner(f"{core.STEP_LABELS[step]}..."):
                if not run_step(step):
                    break


# ---------------- sidebar ----------------
with st.sidebar:
    st.title("🎓 LessonForge")
    st.caption("Research → Lesson → 2 QC layers → Edit → Export")
    p = core.provider()
    if p:
        st.success(f"AI: {p} / {core.model_name('main')}\n\nQC model: {core.model_name('qc')}")
    else:
        st.error("No API key found. Add one to .env and restart.")
    st.markdown("**Load a test input**")
    for name in SAMPLES:
        st.button(name, on_click=set_sample, args=(name,), use_container_width=True)
    if S.get("saved_at"):
        st.caption(f"Autosaved: {S['saved_at']}")

# ---------------- input ----------------
st.header("1. Lesson input")
title = st.text_input("Lesson Title", key="in_title")
objective = st.text_area("Learning Objective", key="in_obj", height=90)
has_work = bool(S.get("lesson"))
overwrite = True
if has_work:
    overwrite = st.checkbox("Start a NEW lesson (your current lesson and edits are backed up first)")

c1, c2 = st.columns([1, 3])
if c1.button("🚀 Generate lesson", type="primary"):
    if not title.strip() or not objective.strip():
        st.error("Please enter both a Lesson Title and a Learning Objective.")
    elif has_work and not overwrite:
        st.warning("You already have a lesson. Tick 'Start a NEW lesson' to replace it "
                   "(a backup is saved), or keep editing below.")
    elif not core.provider():
        st.error("No API key configured. See README.")
    else:
        if has_work:
            core.backup_state(S)
        S.clear()
        S.update(core.new_state())
        S["title"], S["objective"] = title.strip(), objective.strip()
        core.save_state(S)
        run_pipeline()
        st.rerun()
if S["lesson"] is None and any(v != "pending" for v in S["status"].values()):
    if c2.button("▶ Resume / retry pipeline"):
        run_pipeline()
        st.rerun()

# ---------------- status + retry ----------------
if any(v != "pending" for v in S["status"].values()):
    st.subheader("Pipeline status")
    cols = st.columns(4)
    icons = {"pending": "⚪", "done": "✅", "failed": "❌"}
    for col, step in zip(cols, core.STEPS):
        col.markdown(f"{icons[S['status'][step]]} **{core.STEP_LABELS[step]}**")
        if S["status"][step] == "failed":
            col.error(S["errors"].get(step, "failed")[:300])
            if col.button("Retry", key=f"retry_{step}"):
                with st.spinner("Retrying..."):
                    run_step(step)
                st.rerun()

if not S.get("lesson"):
    st.info("Enter a title and objective, then click **Generate lesson**.")
    st.stop()

lesson = S["lesson"]
total = sum(int(s["minutes"]) for s in lesson["sections"])

tabs = st.tabs(["✏️ Edit lesson", "🔎 QC 1: Facts", "🎯 QC 2: Instruction",
                "📚 Sources", "👁 Preview", "⬇️ Export"])

# ---------------- edit ----------------
with tabs[0]:
    st.subheader("Review and edit")
    (st.success if total == core.TOTAL_MINUTES else st.warning)(
        f"Time budget: {total} / {core.TOTAL_MINUTES} minutes")
    st.bar_chart(pd.DataFrame({"minutes": [int(s["minutes"]) for s in lesson["sections"]]},
                              index=[s["heading"] for s in lesson["sections"]]))
    for i, sec in enumerate(lesson["sections"]):
        with st.expander(f'{i}. {sec["heading"]} ({sec["minutes"]} min)', expanded=(i == 0)):
            a, b = st.columns([4, 1])
            a.text_input("Heading", value=sec["heading"], key=f"h_{i}_{S['version']}")
            b.number_input("Minutes", value=int(sec["minutes"]), min_value=1, max_value=45,
                           key=f"m_{i}_{S['version']}")
            st.text_area("Content (Markdown)", value=sec["content"], height=280,
                         key=f"c_{i}_{S['version']}")
    if st.button("💾 Save edits"):
        sync_editor()
        S["edited"] = True
        core.save_state(S)
        st.success("Saved. (Edits are also autosaved automatically.)")
    st.caption("Edited the lesson? Re-run QC so the audits match your latest version.")
    r1, r2, _ = st.columns([1, 1, 3])
    if r1.button("🔁 Re-run QC 1"):
        with st.spinner("QC 1..."):
            run_step("qc1")
        st.rerun()
    if r2.button("🔁 Re-run QC 2"):
        with st.spinner("QC 2..."):
            run_step("qc2")
        st.rerun()


# ---------------- QC rendering ----------------
def render_qc(key: str, label: str):
    rep = S.get(key)
    if not rep:
        st.info(f"{label} has not run yet.")
        return
    m1, m2 = st.columns([1, 4])
    m1.metric("Score", f'{rep.get("score", "n/a")}/100')
    m2.write(rep.get("summary", ""))
    for k in ("objective_alignment", "timing_assessment"):
        if rep.get(k):
            st.markdown(f"**{k.replace('_', ' ').title()}:** {rep[k]}")
    if rep.get("source_assessment"):
        with st.expander("Source reliability assessment"):
            for a in rep["source_assessment"]:
                st.markdown(f'- **[{a.get("source_id")}]** reliability **{a.get("reliability")}**, '
                            f'relevance **{a.get("relevance")}**: {a.get("comment", "")}')
    issues = rep.get("issues", [])
    st.markdown(f"### Issues ({sum(1 for i in issues if i['status'] == 'open')} open / {len(issues)})")
    sev = {"high": "🔴", "medium": "🟠", "low": "🟡"}
    for it in issues:
        head = f'{sev.get(it.get("severity"), "⚪")} {it["id"]} · {it.get("type", "")} · section {it["section_index"]} · {it["status"].upper()}'
        with st.expander(head):
            st.markdown(f'**Problem:** {it.get("problem", "")}')
            before, after = it.get("original_text", ""), it.get("suggested_text", "")
            if before:
                st.markdown("**Before → After (diff)**")
                diff = difflib.unified_diff(before.split(), after.split(), lineterm="", n=50)
                st.code("\n".join(diff) or after, language="diff")
            else:
                st.markdown("**Suggested addition:**")
                st.code(after)
            if it["status"] == "open":
                b1, b2, _ = st.columns([1, 1, 4])
                if b1.button("✅ Apply", key=f"ap_{it['id']}"):
                    sync_editor()
                    ok, msg, _, _ = core.apply_issue(lesson, it)
                    if ok:
                        it["status"] = "applied"
                        S["edited"] = True
                        S["version"] += 1  # refresh text boxes with new content
                        st.toast(msg)
                    else:
                        st.warning(msg)
                    core.save_state(S)
                    if ok:
                        st.rerun()
                if b2.button("🚫 Dismiss", key=f"dm_{it['id']}"):
                    it["status"] = "dismissed"
                    core.save_state(S)
                    st.rerun()


with tabs[1]:
    st.subheader("QC Layer 1: Research and Factual Accuracy")
    render_qc("qc1", "QC 1")
with tabs[2]:
    st.subheader("QC Layer 2: Instructional Quality")
    render_qc("qc2", "QC 2")

# ---------------- sources ----------------
with tabs[3]:
    st.subheader("Research references")
    for s in S["sources"]:
        link = {True: "🟢 link OK", False: "🔴 unreachable"}.get(s.get("link_ok"), "⚪ unchecked")
        st.markdown(f'**[{s["id"]}]** {s["title"]}  \n{s["type"]} · reliability: **{s["reliability"]}** · {link}  \n{s["url"]}')
    if lesson.get("recommended_videos"):
        st.subheader("Recommended videos")
        by_id = {s["id"]: s for s in S["sources"]}
        for v in lesson["recommended_videos"]:
            s = by_id.get(v["source_id"])
            if s:
                st.markdown(f'**{s["title"]}** - {v["why"]}')
                st.video(s["url"])

# ---------------- preview ----------------
with tabs[4]:
    st.markdown(core.lesson_markdown(S))

# ---------------- export ----------------
with tabs[5]:
    st.subheader("Export")
    d1, d2, d3 = st.columns(3)
    d1.download_button("Lesson (.md)", core.lesson_markdown(S), "lesson.md")
    d1.download_button("References (.md)", core.references_markdown(S), "references.md")
    d2.download_button("QC 1 report (.md)", core.qc_markdown(S.get("qc1"), "QC Layer 1: Research & Factual Accuracy"), "qc1_report.md")
    d2.download_button("QC 2 report (.md)", core.qc_markdown(S.get("qc2"), "QC Layer 2: Instructional Quality"), "qc2_report.md")
    try:
        d3.download_button("Lesson (.docx)", core.build_docx(S), "lesson.docx")
        d3.download_button("Everything (.zip)", core.build_zip(S), "lesson_package.zip", type="primary")
    except Exception as e:
        d3.error(f"Export failed: {e}")
