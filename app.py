"""
app.py — Streamlit front-end for the Tata Steel Narrow Complex Mill Planner
============================================================================
Upload your WIP file → pick a date → download the formatted plan.
Optionally upload a corrected plan to train the learning DB.
"""

import io
import json
import os
import sys
import tempfile
from datetime import date

import streamlit as st

# ── path fix so sibling modules resolve correctly ──────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from generator import generate_daily_plan
from db        import load_db, save_db, is_supabase_connected, get_storage_mode
from constants import SECTION_SHORT_NAME

# ── New pipeline-aware planning engine ────────────────────────────────────
from crm import config as C
from crm import pipeline as PIPE
from crm import health   as HLTH
from crm import scoring  as SCORE
from crm import campaign as CAMP
from crm import twin     as TWIN
from crm import planner  as PLAN


# ── Shared WIP uploader — one upload serves every page ────────────────────
def wip_gate(key: str = "wip_global"):
    """Ensure a WIP file is loaded. Returns (tmp_path, enriched_df) or stops."""
    import tempfile as _tf, os as _os
    if st.session_state.get("wip_path") and st.session_state.get("wip_df") is not None:
        with st.expander("📂 WIP file loaded — upload a different one?"):
            up = st.file_uploader("Replace WIP", type=["xlsx"], key=key + "_r")
            if up:
                up.seek(0)
                with _tf.NamedTemporaryFile(suffix=".xlsx", delete=False) as t:
                    t.write(up.read()); p = t.name
                st.session_state.wip_path = p
                st.session_state.wip_df   = PIPE.load_pipeline(p)
                st.session_state.pop("plan_result", None)
                st.rerun()
        return st.session_state.wip_path, st.session_state.wip_df

    st.info("👋 Upload today's WIP file to begin.")
    up = st.file_uploader("WIP file (.xlsx)", type=["xlsx"], key=key)
    if not up:
        st.stop()
    up.seek(0)
    with _tf.NamedTemporaryFile(suffix=".xlsx", delete=False) as t:
        t.write(up.read()); p = t.name
    st.session_state.wip_path = p
    st.session_state.wip_df   = PIPE.load_pipeline(p)
    st.rerun()


HICON = {"CRITICAL": "🔴", "ATTENTION": "🟡", "HEALTHY": "🟢", "EXCESS": "🟠"}

# ──────────────────────────────────────────────────────────────────────────
# Page config
# ──────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title  = "CRM Narrow Complex — Mill Planner",
    page_icon   = "🏭",
    layout      = "wide",
    initial_sidebar_state = "expanded",
)

# ──────────────────────────────────────────────────────────────────────────
# Sidebar — branding + navigation
# ──────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🏭 Mill Planner")
    st.markdown("**Tata Steel CRM Sahibabad**  \nNarrow Complex — Rolling")
    st.divider()

    page = st.radio(
        "Navigate",
        ["📋 Generate Plan", "🏭 Pipeline Overview",
         "🩺 Stage Health", "🎯 Plan Builder",
         "🔮 Digital Twin", "🧠 Learn", "📊 Stats"],
        label_visibility="collapsed",
    )

    st.divider()

    # Storage status badge
    storage_mode = get_storage_mode()
    st.caption(f"Storage: {storage_mode}")

    st.divider()

    # ── Test Mode toggle ──────────────────────────────────
    from db import set_test_mode, is_test_mode
    if "test_mode" not in st.session_state:
        st.session_state["test_mode"] = False

    test_on = st.toggle(
        "🧪 Test Mode",
        value=st.session_state["test_mode"],
        help="When ON: all saves go to TEST_ keys. Real data is untouched.",
        key="test_toggle",
    )
    if test_on != st.session_state["test_mode"]:
        st.session_state["test_mode"] = test_on
        set_test_mode(test_on)
        st.rerun()
    set_test_mode(test_on)

    if test_on:
        st.warning("🧪 TEST MODE ON\nData saves to TEST_ keys only.")

    st.divider()
    db = load_db()
    st.metric("Sessions learned",    db.get("total_sessions", 0))
    acc = db.get("cumulative_accuracy", 0.0)
    st.metric("Cumulative accuracy", f"{acc*100:.1f}%" if db.get("total_sessions") else "—")
    st.metric("Learned rules",       len(db.get("grade_routing", {})))


# ══════════════════════════════════════════════════════════════════════════
# PAGE 1 — GENERATE
# ══════════════════════════════════════════════════════════════════════════
if page == "📋 Generate Plan":
    st.title("📋 Generate Mill Plan")
    st.caption("Upload the daily WIP coil staging export → get a formatted rolling plan.")

    col1, col2 = st.columns([2, 1])
    with col1:
        wip_file = st.file_uploader(
            "Upload WIP file  (any filename — must contain the standard WIP columns)",
            type=["xlsx"],
            key="wip_upload",
        )
    with col2:
        plan_date = st.date_input("Planning date", value=date.today())

        use_db    = st.checkbox("Apply learned rules", value=True)

    if wip_file and st.button("🚀 Generate Plan", type="primary", use_container_width=True):
        with st.spinner("Analysing WIP data and building plan…"):
            try:
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                    tmp.write(wip_file.read())
                    wip_path = tmp.name

                out_path    = f"/tmp/mill_plan_{plan_date.strftime('%d-%m-%Y')}.xlsx"
                learning_db = load_db() if use_db else None

                import contextlib
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    result = generate_daily_plan(
                        wip_file    = wip_path,
                        plan_date   = plan_date.strftime("%Y-%m-%d"),
                        output_file = out_path,
                        days        = 1,
                        learning_db = learning_db,
                        verbose     = True,
                    )
                console_out = buf.getvalue()

                sections     = result["sections"]
                total_coils  = sum(len(s["coils_df"]) for s in sections)
                total_weight = sum(s["coils_df"]["Input Coil Weight"].sum() for s in sections)

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Total coils",  total_coils)
                m2.metric("Total weight", f"{total_weight:.1f} MT")
                m3.metric("Sections",     len(sections))
                m4.metric("Excluded",     result["excluded_count"])

                st.success("Plan generated successfully!")

                import pandas as pd
                st.subheader("Section Breakdown")
                rows = [{
                    "Section":     SECTION_SHORT_NAME.get(s["section_key"], s["section_key"]),
                    "Mill":        s["mill"],
                    "Coils":       len(s["coils_df"]),
                    "Weight (MT)": round(s["coils_df"]["Input Coil Weight"].sum(), 2),
                } for s in sections]
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                with open(out_path, "rb") as f:
                    xlsx_bytes = f.read()
                st.download_button(
                    label     = f"⬇️  Download  mill_plan_{plan_date.strftime('%d-%m-%Y')}.xlsx",
                    data      = xlsx_bytes,
                    file_name = f"mill_plan_{plan_date.strftime('%d-%m-%Y')}.xlsx",
                    mime      = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )

                with st.expander("Console log"):
                    st.code(console_out, language="text")

                os.unlink(wip_path)

            except Exception as e:
                st.error(f"Error: {e}")
                import traceback; st.code(traceback.format_exc())

    elif not wip_file:
        st.info("👆 Upload a WIP file to get started.")


# ══════════════════════════════════════════════════════════════════════════
# PAGE 2 — LEARN
# ══════════════════════════════════════════════════════════════════════════
elif page == "🧠 Learn":
    st.title("🧠 Learn from Planner Corrections")
    st.caption(
        "Upload the WIP file + generated plan + planner's corrected version. "
        "Routing rules AND the ML model update automatically in one step."
    )

    # ══════════════════════════════════════════════════════════════════
    # FIX 3 — TRAINING MODE: generate → correct inline → learn (no Excel)
    # ══════════════════════════════════════════════════════════════════
    with st.expander("🏋️ Training Mode — generate a plan and correct it "
                     "here (no Excel editing needed)", expanded=False):
        import pandas as pd

        wip_train  = st.file_uploader("WIP file for training",
                                      type=["xlsx"], key="wip_train")
        train_date = st.date_input("Plan date ", value=date.today(),
                                   key="train_date")

        if wip_train and st.button("⚙️ Generate training plan",
                                   type="primary", use_container_width=True,
                                   key="gen_train_btn"):
            with st.spinner("Generating plan…"):
                try:
                    wip_train.seek(0)
                    with tempfile.NamedTemporaryFile(
                            suffix=".xlsx", delete=False) as t:
                        t.write(wip_train.read()); tp = t.name
                    gen_out = tp.replace(".xlsx", "_gen.xlsx")
                    import contextlib, io as _io
                    with contextlib.redirect_stdout(_io.StringIO()):
                        res = generate_daily_plan(
                            wip_file=tp,
                            plan_date=train_date.strftime("%Y-%m-%d"),
                            output_file=gen_out,
                            days=1, learning_db=load_db(), verbose=False)
                    st.session_state.train_res      = res
                    st.session_state.train_gen_path = gen_out
                    st.session_state.train_wip_path = tp
                    st.session_state.pop("train_editor_df", None)
                    st.success(
                        f"Plan generated — {res['eligible_count']} coils in "
                        f"{len(res['sections'])} sections. Correct below.")
                except Exception as e:
                    st.error(f"Generation failed: {e}")

        if "train_res" in st.session_state:
            res       = st.session_state.train_res
            sec_opts  = list(SECTION_SHORT_NAME.keys())
            mill_opts = ["CRM04", "CRM06"]

            if "train_editor_df" not in st.session_state:
                rows = []
                for s in res["sections"]:
                    for _, r in s["coils_df"].iterrows():
                        rows.append({
                            "Coil":     str(r.get("Coil Number", "")),
                            "Width":    r.get("Actual Width", ""),
                            "Thick":    r.get("Actual Thick", ""),
                            "RT":       r.get("Plan Rolling Thick 1", ""),
                            "MT":       round(float(
                                r.get("Input Coil Weight", 0) or 0), 3),
                            "Quality":  r.get("Actual Quality", ""),
                            "Customer": str(r.get("Customer Desc", ""))[:20],
                            "Section":  s["section_key"],
                            "Mill":     s["mill"],
                        })
                st.session_state.train_editor_df = pd.DataFrame(rows)

            st.markdown("**Edit the Section / Mill cells to correct routing:**")
            edited = st.data_editor(
                st.session_state.train_editor_df,
                use_container_width=True, hide_index=True, height=400,
                column_config={
                    "Section": st.column_config.SelectboxColumn(
                        "Section", options=sec_opts, required=True),
                    "Mill": st.column_config.SelectboxColumn(
                        "Mill", options=mill_opts, required=True),
                },
                disabled=["Coil", "Width", "Thick", "RT", "MT",
                          "Quality", "Customer"],
                key="train_editor")

            base    = st.session_state.train_editor_df
            changed = edited[(edited["Section"] != base["Section"]) |
                             (edited["Mill"]    != base["Mill"])]
            st.info(f"✏️ {len(changed)} coil(s) corrected.")

            if len(changed) and st.button(
                    "🧠 Run Learning Session with these corrections",
                    type="primary", use_container_width=True,
                    key="train_learn_btn"):
                with st.spinner("Building corrected plan and retraining…"):
                    try:
                        from openpyxl import Workbook as _WB
                        from generator import write_sheet as _ws
                        from learner import (diff_plans, extract_and_update,
                                             calculate_accuracy,
                                             build_session_entry)
                        # 1. Sections with the planner's corrections applied
                        corr = {str(r["Coil"]): (r["Section"], r["Mill"])
                                for _, r in edited.iterrows()}
                        bucket: dict = {}
                        for s in res["sections"]:
                            for _, r in s["coils_df"].iterrows():
                                cn = str(r.get("Coil Number", ""))
                                sec, mill = corr.get(
                                    cn, (s["section_key"], s["mill"]))
                                bucket.setdefault((sec, mill), []).append(r)
                        corrected = [
                            {"section_key": k[0], "mill": k[1],
                             "label": k[0].replace("_", " ").title(),
                             "coils_df": pd.DataFrame(v)}
                            for k, v in bucket.items()]

                        # 2. Corrected plan in the standard format
                        act_out = st.session_state.train_wip_path.replace(
                            ".xlsx", "_corrected.xlsx")
                        wb = _WB(); wb.remove(wb.active)
                        _ws(wb, train_date, corrected, load_db())
                        wb.save(act_out)

                        # 3. Standard learning pipeline
                        clog, gplan, aplan = diff_plans(
                            st.session_state.train_gen_path,
                            act_out, train_date)
                        cur   = load_db()
                        n_act = sum(len(x["coils"])
                                    for x in aplan["sections"])
                        added, reinf, confl = extract_and_update(
                            clog, cur, gplan, aplan)
                        acc = calculate_accuracy(clog, n_act)
                        cur["session_log"].append(build_session_entry(
                            train_date, gplan, aplan, clog, acc,
                            added, reinf, confl))
                        ns   = cur.get("total_sessions", 0) + 1
                        prev = cur.get("cumulative_accuracy", 0.0)
                        cur["cumulative_accuracy"] = round(
                            (prev * (ns - 1) +
                             acc["overall_accuracy"]) / ns, 4)
                        cur["total_sessions"] = ns
                        save_db(cur)
                        st.success(
                            f"✅ Learned from {len(changed)} correction(s) — "
                            f"{added} rule(s) added, {reinf} reinforced. "
                            f"Accuracy {acc['overall_accuracy']*100:.1f}%.")
                    except Exception as e:
                        st.error(f"Learning failed: {e}")
                        import traceback
                        st.code(traceback.format_exc())

    st.divider()

    col1, col2, col3 = st.columns(3)
    with col1:
        wip_learn = st.file_uploader(
            "WIP file (same one used to generate)", type=["xlsx"], key="wip_learn")
    with col2:
        gen_file = st.file_uploader(
            "Generated plan (system output)", type=["xlsx"], key="gen")
    with col3:
        act_file = st.file_uploader(
            "Corrected plan (planner version)", type=["xlsx"], key="act")

    learn_date = st.date_input("Plan date", value=date.today())

    all_uploaded = wip_learn and gen_file and act_file
    if not all_uploaded:
        missing = []
        if not wip_learn: missing.append("WIP file")
        if not gen_file:  missing.append("Generated plan")
        if not act_file:  missing.append("Corrected plan")
        st.info(f"👆 Still needed: {', '.join(missing)}")

    if all_uploaded and st.button("🧠 Run Learning Session",
                                   type="primary", use_container_width=True):
        with st.spinner("Comparing plans, updating rules and retraining ML model…"):
            try:
                # Save uploaded files to temp paths
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tw:
                    tw.write(wip_learn.read()); wip_path = tw.name
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tg:
                    tg.write(gen_file.read()); gen_path = tg.name
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as ta:
                    ta.write(act_file.read()); act_path = ta.name

                # ── Step 1: Rule-based diff + DB update ───────────────
                from learner import diff_plans, extract_and_update, \
                                    calculate_accuracy, build_session_entry

                correction_log, gen_plan, act_plan = diff_plans(
                    gen_path, act_path, learn_date)

                current_db = load_db()
                n_act = sum(len(s["coils"]) for s in act_plan["sections"])
                added, reinforced, conflicts = extract_and_update(
                    correction_log, current_db, gen_plan, act_plan)

                acc     = calculate_accuracy(correction_log, n_act)
                session = build_session_entry(
                    learn_date, gen_plan, act_plan,
                    correction_log, acc, added, reinforced, conflicts)

                current_db["session_log"].append(session)
                n_sess  = current_db.get("total_sessions", 0) + 1
                prev    = current_db.get("cumulative_accuracy", 0.0)
                cum_acc = (prev * (n_sess - 1) + acc["overall_accuracy"]) / n_sess
                current_db["cumulative_accuracy"] = round(cum_acc, 4)
                current_db["total_sessions"]      = n_sess
                save_db(current_db)

                # ── Step 2: ML model retrain ───────────────────────────
                ml_status = ""
                ml_accuracy = None
                try:
                    from ml_trainer    import train_from_pair
                    from ml_classifier import SectionClassifier

                    model_path = os.path.join(
                        os.path.dirname(__file__), "models", "section_clf.pkl")
                    os.makedirs(os.path.dirname(model_path), exist_ok=True)

                    clf = train_from_pair(
                        wip_path    = wip_path,
                        actual_path = act_path,
                        model_path  = model_path,
                        verbose     = False,
                    )

                    if clf.training_log:
                        last = clf.training_log[-1]
                        ml_accuracy = last.get("cv_accuracy")
                        n_actual    = last.get("n_actual", 0)
                        n_total     = last.get("n_total", 0)
                        ml_status = (
                            f"✅ ML model retrained — "
                            f"{n_actual} actual + {n_total - n_actual} synthetic samples"
                            + (f", CV accuracy: **{ml_accuracy*100:.1f}%**"
                               if ml_accuracy else "")
                        )

                        # Invalidate cached classifier so next generation uses new model
                        import sectioning as _sec
                        _sec._clf_cache = None

                except Exception as ml_err:
                    ml_status = f"⚠️ ML retrain skipped: {ml_err}"

                # ── Display results ────────────────────────────────────
                st.success(
                    f"Learning complete! Rule accuracy: **{acc['overall_accuracy']*100:.1f}%**  "
                    + ("☁️ Saved to Supabase" if is_supabase_connected()
                       else "💾 Saved locally")
                )
                if ml_status:
                    st.info(ml_status)

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Section accuracy",   f"{acc['section_accuracy']*100:.1f}%")
                c2.metric("Ordering accuracy",  f"{acc['ordering_accuracy']*100:.1f}%")
                c3.metric("Inclusion accuracy", f"{acc['inclusion_accuracy']*100:.1f}%")
                c4.metric("Overall accuracy",   f"{acc['overall_accuracy']*100:.1f}%")

                if ml_accuracy:
                    st.metric("ML CV accuracy", f"{ml_accuracy*100:.1f}%",
                              help="Cross-validated accuracy of the XGBoost model on actual labels")

                import pandas as pd
                ct = session["corrections_by_type"]
                ct_df = pd.DataFrame([
                    {"Correction type": k.replace("_"," ").title(), "Count": v}
                    for k, v in ct.items() if v > 0
                ])
                if not ct_df.empty:
                    st.subheader("Corrections by Type")
                    st.dataframe(ct_df, use_container_width=True, hide_index=True)

                r1, r2, r3 = st.columns(3)
                r1.metric("New rules",   added)
                r2.metric("Reinforced",  reinforced)
                r3.metric("Conflicts",   conflicts)

                os.unlink(wip_path)
                os.unlink(gen_path)
                os.unlink(act_path)

            except Exception as e:
                st.error(f"Learning error: {e}")
                import traceback; st.code(traceback.format_exc())


# ══════════════════════════════════════════════════════════════════════════
# PAGE 3 — STATS
# ══════════════════════════════════════════════════════════════════════════
elif page == "📊 Stats":
    st.title("📊 Stats & Rules")

    db = load_db()

    # ── Connection status banner ──────────────────────────────────────
    if is_supabase_connected():
        st.success("☁️  Connected to Supabase — rules persist across sessions")
    else:
        st.warning(
            "💾  Running in local mode — rules reset on server restart.  \n"
            "Add `SUPABASE_URL` and `SUPABASE_KEY` to Streamlit secrets to enable persistence."
        )

    n_sessions  = db.get("total_sessions", 0)
    cum_acc     = db.get("cumulative_accuracy", 0.0)
    grade_rules = db.get("grade_routing", {})
    coil_ovr    = db.get("coil_overrides", {})
    conflicts   = [c for c in db.get("conflict_log", []) if not c.get("resolved")]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sessions",      n_sessions)
    c2.metric("Accuracy",      f"{cum_acc*100:.1f}%" if n_sessions else "—")
    c3.metric("Grade rules",   len(grade_rules))
    c4.metric("Conflicts",     len(conflicts), delta_color="inverse")

    # ── Accuracy trend ────────────────────────────────────────────────
    sessions = db.get("session_log", [])
    if sessions:
        import pandas as pd
        trend = pd.DataFrame([{
            "Date":     s.get("session_date", ""),
            "Overall":  round(s.get("overall_accuracy",  0) * 100, 1),
            "Section":  round(s.get("section_accuracy",  0) * 100, 1),
            "Ordering": round(s.get("sort_accuracy",     0) * 100, 1),
        } for s in sessions])
        st.subheader("Accuracy Trend")
        st.line_chart(trend.set_index("Date")[["Overall", "Section", "Ordering"]])

    st.divider()

    # ── Grade routing rules ───────────────────────────────────────────
    st.subheader("Grade Routing Rules")
    if grade_rules:
        import pandas as pd
        rules_df = pd.DataFrame([{
            "Key":        k,
            "Section":    v.get("section", ""),
            "Mill":       v.get("mill", ""),
            "Confidence": v.get("confidence", 0),
            "Source":     v.get("source", ""),
            "Last seen":  v.get("last_seen", ""),
        } for k, v in grade_rules.items()]).sort_values("Confidence", ascending=False)

        min_conf = st.select_slider("Min confidence", [1, 2, 3, 5, 10], value=1)
        st.dataframe(rules_df[rules_df["Confidence"] >= min_conf],
                     use_container_width=True, hide_index=True)
    else:
        st.info("No learned rules yet.")

    st.divider()

    # ── Conflicts ─────────────────────────────────────────────────────
    if conflicts:
        st.subheader(f"⚠️ Unresolved Conflicts ({len(conflicts)})")
        import pandas as pd
        st.dataframe(pd.DataFrame([{
            "Key":           c.get("key", ""),
            "Existing rule": c.get("existing_rule", ""),
            "New evidence":  c.get("new_evidence", ""),
            "Confidence":    c.get("existing_confidence", 0),
            "Date":          c.get("date", ""),
        } for c in conflicts]), use_container_width=True, hide_index=True)

        st.subheader("Resolve a Conflict")
        with st.form("resolve_form"):
            key_in  = st.text_input("Routing key (Quality|TDC|ProdCode|NextStage)")
            sec_in  = st.selectbox("Correct section", list(SECTION_SHORT_NAME.keys()))
            mill_in = st.selectbox("Correct mill", ["CRM04", "CRM06", "CRM04/06"])
            conf_in = st.slider("Confidence", 1, 10, 5)
            if st.form_submit_button("✅ Save Rule", type="primary") and key_in:
                db["grade_routing"][key_in] = {
                    "section": sec_in, "mill": mill_in,
                    "confidence": conf_in, "observations": conf_in,
                    "overrides": 0, "last_seen": date.today().isoformat(),
                    "source": "manual_ui",
                }
                for c in db.get("conflict_log", []):
                    if c.get("key") == key_in and not c.get("resolved"):
                        c["resolved"] = True
                        c["resolution"] = f"UI → {sec_in}|{mill_in}"
                save_db(db)
                st.success(f"Saved: {key_in} → {sec_in} @ {mill_in}")
                st.rerun()

    st.divider()

    # ── ML Model Status ──────────────────────────────────────────────────
    st.subheader("🤖 ML Model Status")
    from db import get_model_info, load_model_from_supabase, save_model_to_supabase
    import os as _os

    model_path = _os.path.join(_os.path.dirname(__file__), 'models', 'section_clf.pkl')
    local_exists = _os.path.exists(model_path)
    cloud_info   = get_model_info()

    mc1, mc2, mc3 = st.columns(3)
    mc1.metric("Local model",  "✅ Loaded" if local_exists else "❌ Missing")
    mc2.metric("Cloud model",  "☁️ Available" if cloud_info.get('exists') else "❌ Not uploaded")
    if cloud_info.get('saved_at'):
        mc3.metric("Last trained", cloud_info['saved_at'][:10])
    if cloud_info.get('size_original'):
        st.caption(
            f"Model size: {cloud_info['size_original']/1024/1024:.1f} MB original → "
            f"{cloud_info['size_compressed']/1024/1024:.1f} MB compressed in Supabase")

    col_ml1, col_ml2 = st.columns(2)
    with col_ml1:
        if cloud_info.get('exists') and not local_exists:
            if st.button("⬇️ Download model from Supabase", use_container_width=True):
                with st.spinner("Downloading…"):
                    ok = load_model_from_supabase(model_path)
                    if ok:
                        import sectioning as _sec; _sec._clf_cache = None
                        st.success("Model downloaded and ready.")
                        st.rerun()
                    else:
                        st.error("Download failed.")
    with col_ml2:
        if local_exists:
            if st.button("☁️ Upload local model to Supabase", use_container_width=True):
                with st.spinner("Uploading…"):
                    ok = save_model_to_supabase(model_path)
                    st.success("Uploaded." if ok else "Upload failed.")
                    st.rerun()

    st.divider()

    # ── DB Management ─────────────────────────────────────────────────
    st.subheader("🗄️ Database Management")
    from db import get_db_stats, clear_test_data, is_test_mode

    stats = get_db_stats()
    if "error" not in stats:
        dm1, dm2, dm3 = st.columns(3)
        dm1.metric("Total records",  stats["total"])
        dm2.metric("Production",     stats["prod"])
        dm3.metric("Test records",   stats["test"],
                   delta_color="off")

        if stats["test"] > 0:
            st.warning(f"🧪 {stats['test']} test record(s) in database: "
                       f"{', '.join(stats['test_keys'][:5])}"
                       f"{'…' if len(stats['test_keys']) > 5 else ''}")
            if st.button("🗑️ Clear ALL test data", type="secondary",
                          use_container_width=False):
                n, msg = clear_test_data()
                st.success(f"✅ {msg}")
                st.rerun()
        else:
            st.success("✅ No test records — database is clean.")
    else:
        st.info(f"DB stats: {stats.get('error','Not connected')}")

    st.divider()

    # ── Export / Import ───────────────────────────────────────────────
    st.subheader("Export / Import Learning DB")
    col_exp, col_imp = st.columns(2)
    with col_exp:
        st.download_button(
            "⬇️ Download learning_db.json",
            data      = json.dumps(db, indent=2, default=str),
            file_name = "learning_db.json",
            mime      = "application/json",
            use_container_width=True,
        )
    with col_imp:
        up = st.file_uploader("Upload learning_db.json", type=["json"], key="db_up")
        if up and st.button("↩️ Restore DB", use_container_width=True):
            new_db = json.loads(up.read())
            save_db(new_db)
            st.success("DB restored.")
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════
# PAGE 4 — ROLL OPTIMISER  (appended to existing app.py)
# ══════════════════════════════════════════════════════════════════════════


    # ══════════════════════════════════════════════════════════════════
    # DAILY OUTCOME LOG — 5 fields only (SPEC §09)
    # ══════════════════════════════════════════════════════════════════
    st.divider()
    st.subheader("📓 Daily Outcome Log")
    st.caption("2 minutes a day. After 30 days these replace the assumed "
               "consumption numbers with measured reality.")

    try:
        from outcome_logger import save_outcome, load_outcomes
        _outlog_ok = True
    except ImportError:
        _outlog_ok = False
        st.warning("⚠️ outcome_logger.py not found in the repo — "
                   "outcome logging disabled. Add the file to enable it.")
    import pandas as _olpd

    if not _outlog_ok:
        st.stop()
    with st.form("stats_outcome_form"):
        oc1, oc2, oc3 = st.columns(3)
        ol_date  = oc1.date_input("Date", value=_olpd.Timestamp.now().date())
        ol_mt04  = oc2.number_input("CRM-04 MT rolled", 0.0, 500.0, 0.0, 1.0)
        ol_mt06  = oc3.number_input("CRM-06 MT rolled", 0.0, 500.0, 0.0, 1.0)
        oc4, oc5 = st.columns(2)
        ol_ht    = oc4.checkbox("H&T starved today?")
        ol_crs   = oc5.checkbox("CRS starved today?")
        ol_notes = st.text_input("Notes", placeholder="Optional")
        ol_save  = st.form_submit_button("💾 Save", type="primary",
                                          use_container_width=True)
    if ol_save:
        ok = save_outcome({
            "log_date":        str(ol_date),
            "shift_no":        0,
            "crm04_mt_rolled": ol_mt04,
            "crm06_mt_rolled": ol_mt06,
            "total_mt_rolled": round(ol_mt04 + ol_mt06, 1),
            "ht_starved":      ol_ht,
            "crs_starved":     ol_crs,
            "notes":           ol_notes,
        })
        st.success(f"✅ Saved for {ol_date}") if ok else \
            st.error("Save failed — check Supabase connection")

    ol_hist = load_outcomes(30)
    if not ol_hist.empty:
        st.markdown(f"**Last 30 days — {len(ol_hist)} records**")
        show = [c for c in ["log_date","crm04_mt_rolled","crm06_mt_rolled",
                             "total_mt_rolled","ht_starved","crs_starved",
                             "notes"] if c in ol_hist.columns]
        st.dataframe(ol_hist[show], use_container_width=True,
                     hide_index=True, height=240)
        if len(ol_hist) >= 30:
            _tot = _olpd.to_numeric(ol_hist["total_mt_rolled"],
                                     errors="coerce").dropna()
            if len(_tot):
                st.info(f"📐 30-day measured average: "
                        f"{_tot.mean():.0f} MT/day combined. "
                        f"Consider updating capacity defaults in the "
                        f"Priority Advisor setup.")



# ══════════════════════════════════════════════════════════════════════════════
# PAGE — 🏭 PIPELINE OVERVIEW   (Guideline §1, §3, §9)
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🏭 Pipeline Overview":
    import pandas as pd
    st.title("🏭 Pipeline Overview")
    st.caption("Stage-wise WIP with Tube / OEM / H&T drill-down · "
               "material flow · inventory aging")

    _, df = wip_gate("wip_pipe")

    # ── Headline (planning scope — out-of-scope CRS excluded) ────────────
    dfs = PIPE.scoped(df)
    excl = len(df) - len(dfs)
    m = st.columns(4)
    m[0].metric("Total WIP (in scope)", f"{dfs['mt'].sum():,.0f} MT",
                f"{len(dfs)} coils" +
                (f" · {excl} CRS coils out of scope" if excl else ""))
    for i, cons in enumerate(("TUBE", "OEM", "H&T"), start=1):
        d = dfs[dfs["consumer"] == cons]
        cfg = C.CONSUMERS[cons]
        m[i].metric(f"{cfg['icon']} {cfg['label']}",
                    f"{d['mt'].sum():,.0f} MT",
                    f"{len(d)} coils · ask {cfg['daily_mt']:.0f} MT/day")

    st.divider()

    # ── §1 Stage-wise breakup ─────────────────────────────────────────────
    st.subheader("📊 Stage-wise WIP Breakup")
    all_stages = st.toggle("Show all stages", value=False)
    stages = None if all_stages else C.CORE_STAGES
    sb = PIPE.stage_breakup(df, stages)
    st.dataframe(
        sb.style.background_gradient(cmap="Blues", subset=["TOTAL"])
                .format(precision=1),
        use_container_width=True)
    st.bar_chart(sb[["TUBE", "OEM", "H&T"]])

    # ── §1 Drill-down ─────────────────────────────────────────────────────
    st.subheader("🔍 Drill-down")
    d1, d2 = st.columns(2)
    sel_cons  = d1.selectbox("Consumer", ["TUBE", "OEM", "H&T"], index=1)
    sel_stage = d2.selectbox("Stage (optional)",
        ["— all stages —"] +
        sorted(PIPE.scoped(df)["stage"].dropna().astype(str)
               .unique().tolist()))
    stg = None if sel_stage.startswith("—") else sel_stage
    cb  = PIPE.customer_breakup(df, sel_cons, stg)
    if cb.empty:
        st.info("No material for this combination.")
    else:
        st.dataframe(cb.rename(columns={
            "coils": "Coils", "mt": "MT", "avg_age": "Avg age (d)",
            "max_age": "Oldest (d)", "qual_risk": "Quality risk"}),
            use_container_width=True)
        st.caption(f"{cb['mt'].sum():.1f} MT across {int(cb['coils'].sum())} "
                   f"coils · {len(cb)} customers")

    st.divider()

    # ── §9 Material flow (Sankey-style edge table + chart) ────────────────
    st.subheader("🔀 Material Flow — where WIP is heading")
    edges = PIPE.flow_edges(df)
    if edges.empty:
        st.info("No flow edges above threshold.")
    else:
        top = edges.head(20).copy()
        top["Flow"] = top["stage"] + "  →  " + top["next"]
        st.dataframe(top[["Flow", "consumer", "mt", "coils"]].rename(
            columns={"consumer": "Consumer", "mt": "MT", "coils": "Coils"}),
            use_container_width=True, hide_index=True)
        st.bar_chart(top.set_index("Flow")["mt"])

    st.divider()

    # ── §3 Inventory aging ────────────────────────────────────────────────
    st.subheader("⏳ Inventory Aging Profile")
    ap = PIPE.aging_profile(df)
    ac1, ac2 = st.columns([2, 1])
    ac1.bar_chart(ap[["TUBE", "OEM", "H&T"]])
    ac2.dataframe(ap, use_container_width=True)
    old = df[df["coil_age"] >= 21]
    if len(old):
        st.warning(f"⏰ **{len(old)} coils / {old['mt'].sum():.0f} MT** are "
                   f"over 21 days old — rotate these into the plan.")
        with st.expander("Show aged coils"):
            st.dataframe(
                old[["coil", "stage", "consumer", "customer", "mt",
                     "coil_age", "stage_age", "age_band"]]
                .sort_values("coil_age", ascending=False)
                .rename(columns={"coil": "Coil", "stage": "Stage",
                                 "consumer": "Consumer", "customer": "Customer",
                                 "mt": "MT", "coil_age": "Coil age",
                                 "stage_age": "Stage age", "age_band": "Band"}),
                use_container_width=True, hide_index=True)

    # ── Stuck WIP ─────────────────────────────────────────────────────────
    sw = PIPE.stuck_wip(df)
    if not sw.empty:
        st.subheader("🚧 Stuck WIP (>21 days at one stage)")
        st.dataframe(sw.rename(columns={
            "coils": "Coils", "mt": "MT", "oldest": "Oldest (d)"}),
            use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — 🩺 STAGE HEALTH   (Guideline §4)
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🩺 Stage Health":
    import pandas as pd
    st.title("🩺 Stage Health Index")
    st.caption("Inventory · consumption rate · days of cover · "
               "starvation date · overload — for every stage and consumer")

    _, df = wip_gate("wip_health")

    with st.expander("⚙️ Daily demand (MT/day)"):
        dc = st.columns(3)
        demand = {}
        for col, k in zip(dc, ("TUBE", "OEM", "H&T")):
            demand[k] = col.number_input(
                C.CONSUMERS[k]["label"], 0.0, 600.0,
                float(C.CONSUMERS[k]["daily_mt"]), 5.0, key=f"dm_{k}")

    ch = HLTH.consumer_health(df, overrides=demand)
    sh = HLTH.stage_health(df)

    # ── Alerts first ──────────────────────────────────────────────────────
    al = HLTH.alerts(ch, sh)
    if al:
        st.subheader("🚨 Alerts")
        for a in al:
            msg  = a["msg"]
            lvl  = a["level"]
            cdf  = a.get("coil_df")
            if lvl == "CRITICAL":
                st.error(msg)
            elif lvl in ("ATTENTION", "STUCK"):
                st.warning(msg)
            else:
                st.info(msg)
            # FIX 4: show responsible coils under every alert
            if cdf is not None and len(cdf) > 0:
                with st.expander(f"🔍 View {len(cdf)} coil(s) responsible for this alert"):
                    disp_cols = [c for c in
                        ["coil","customer","consumer","mt","stage","storage",
                         "coil_age","stage_age","thick","width","rt",
                         "quality","qual_flags","age_band"]
                        if c in cdf.columns]
                    st.dataframe(
                        cdf[disp_cols].rename(columns={
                            "coil":"Coil","customer":"Customer",
                            "consumer":"Consumer","mt":"MT (t)",
                            "stage":"Stage","storage":"Storage",
                            "coil_age":"Age(d)","stage_age":"Stage Age(d)",
                            "thick":"Thick","width":"Width","rt":"RT",
                            "quality":"Grade","qual_flags":"Quality Flags",
                            "age_band":"Age Band"}),
                        use_container_width=True, hide_index=True)
    else:
        st.success("✅ All stages and consumers healthy.")

    st.divider()

    # ── Consumer health ───────────────────────────────────────────────────
    st.subheader("👥 Consumer Health — demand side")
    cc = st.columns(3)
    for col, x in zip(cc, ch.values()):
        col.metric(f"{x.icon} {x.label}", f"{x.days_cover:.1f} d cover",
                   f"{x.inventory_mt:.0f} MT buffer · {x.daily_rate:.0f} MT/day",
                   delta_color="off")
        if x.status == "CRITICAL":
            col.error(f"Starves {x.starvation_date}")
        elif x.status == "ATTENTION":
            col.warning(f"Needs {x.shortfall_mt:.0f} MT")
        elif x.status == "EXCESS":
            col.warning(f"{x.excess_mt:.0f} MT excess")
        else:
            col.success("Healthy")
    st.dataframe(HLTH.health_table(ch), use_container_width=True,
                 hide_index=True)

    st.divider()

    # ── Stage health ──────────────────────────────────────────────────────
    st.subheader("⚙️ Process Stage Health — supply side")
    st.caption("Days of cover = how long the stage can keep running on "
               "its current WIP. Low = starving. High = congested.")
    ht = HLTH.health_table(sh)
    st.dataframe(ht, use_container_width=True, hide_index=True)

    chart = pd.DataFrame({
        "Days cover": [x.days_cover for x in sh.values()],
    }, index=[x.label for x in sh.values()])
    st.bar_chart(chart)
    st.caption("🔴 <1 day = starving · 🟡 <2 days = attention · "
               "🟢 healthy · 🟠 >7 days = excess inventory")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — 🎯 PLAN BUILDER   (Guideline §2, §5, §6, §7, §10)
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🎯 Plan Builder":
    import pandas as pd
    from datetime import date as _date

    st.title("🎯 Plan Builder")
    st.caption("Score sections → choose what to run → optimise roll campaigns "
               "→ download the rolling plan")

    wip_path, df = wip_gate("wip_plan")

    # ── Setup ─────────────────────────────────────────────────────────────
    with st.expander("⚙️ Setup — mode · rolls · demand",
                     expanded="plan_result" not in st.session_state):
        s1, s2 = st.columns([3, 1])
        mode  = s1.selectbox("Planning mode", list(C.MODES.keys()),
                             format_func=lambda k: C.MODES[k])
        shift = s2.selectbox("Shift", [1, 2, 3], format_func=lambda x: f"Shift {x}")

        st.markdown("**Rolls currently mounted**")
        r1, r2 = st.columns(2)
        with r1:
            st.caption("CRM-04")
            rt04 = st.selectbox("Roll type", C.ROLL_TYPES, key="p_rt04",
                                index=C.ROLL_TYPES.index("Bright"))
            mu04 = st.number_input("MT already rolled on this roll",
                                   0.0, 400.0, 0.0, 5.0, key="p_mu04")
        with r2:
            st.caption("CRM-06")
            rt06 = st.selectbox("Roll type", C.ROLL_TYPES, key="p_rt06",
                                index=C.ROLL_TYPES.index("Light Matt"))
            mu06 = st.number_input("MT already rolled on this roll",
                                   0.0, 400.0, 0.0, 5.0, key="p_mu06")

        st.markdown("**Daily demand (MT/day)**")
        dcs = st.columns(3)
        demand = {k: c.number_input(C.CONSUMERS[k]["label"], 0.0, 600.0,
                                    float(C.CONSUMERS[k]["daily_mt"]), 5.0,
                                    key=f"pd_{k}")
                  for c, k in zip(dcs, ("TUBE", "OEM", "H&T"))}

        if st.button("🚀 Score Sections", type="primary",
                     use_container_width=True):
            with st.spinner("Routing coils (validated Mill Planner) and scoring…"):
                st.session_state.plan_result = PLAN.run_planning(
                    path=wip_path, mode=mode,
                    current_rolls={"CRM04": rt04, "CRM06": rt06},
                    mt_on_rolls={"CRM04": mu04, "CRM06": mu06},
                    demand=demand, db=load_db(),
                    full=df)   # reuse the already-loaded enriched WIP
                st.session_state.plan_cfg = dict(
                    mode=mode, shift=shift, rt04=rt04, rt06=rt06,
                    mu04=mu04, mu06=mu06, demand=demand)
                st.session_state.pop("plan_selected", None)
            st.rerun()

    if "plan_result" not in st.session_state:
        st.info("👆 Set your mode and rolls, then click **Score Sections**.")
        st.stop()

    R    = st.session_state.plan_result
    cfg  = st.session_state.plan_cfg
    ch   = R["consumer_health"]

    # ── Alerts ────────────────────────────────────────────────────────────
    for a in R["alerts"][:5]:
        msg = a["msg"] if isinstance(a, dict) else a
        (st.error if "CRITICAL" in (a.get("level","") if isinstance(a,dict) else msg)
         else st.warning)(msg)

    # ── Consumer status strip ─────────────────────────────────────────────
    cc = st.columns(3)
    for col, k in zip(cc, ("TUBE", "OEM", "H&T")):
        x = ch[k]
        col.metric(f"{x.icon} {x.label}", f"{x.days_cover:.1f}d",
                   f"{x.inventory_mt:.0f} MT buffer", delta_color="off")

    st.divider()

    # ── §2 Section scores (explainable) ───────────────────────────────────
    st.subheader("📋 Section Priority — tick what you will run today")
    if "plan_selected" not in st.session_state:
        st.session_state.plan_selected = {s.section_key + s.mill: True
                                          for s in R["scored"]}

    selected: list = []
    for mill in ("CRM04", "CRM06"):
        ms = sorted([s for s in R["scored"] if s.mill == mill],
                    key=lambda x: x.rank)
        if not ms:
            continue
        st.markdown(f"#### 🏭 {mill}")
        for s in ms:
            key = s.section_key + s.mill
            c1, c2 = st.columns([1, 11])
            on = c1.checkbox("", value=st.session_state.plan_selected.get(key, True),
                             key=f"cb_{key}")
            st.session_state.plan_selected[key] = on
            hi = "🔴" if s.score >= 60 else "🟡" if s.score >= 40 else "🟢"
            with c2.expander(
                f"{hi} **P{s.rank} · {s.section_key.replace('_',' ').title()}** — "
                f"score {s.score:.0f} · {s.n_coils} coils · {s.total_mt:.1f} MT · "
                f"[{s.roll_type}] → {C.CONSUMERS[s.consumer]['label']}",
                expanded=False):
                st.markdown(s.explanation)
                for w in s.warnings:
                    st.caption(w)
                fc1, fc2 = st.columns([1, 1])
                fc1.dataframe(SCORE.factor_table(s), hide_index=True,
                              use_container_width=True)
                fc2.markdown(
                    f"- **Roll life**: {s.roll_life_mt} MT\n"
                    f"- **Shift capacity**: {s.shift_cap_mt:.0f} MT "
                    f"({s.sec_type.replace('_',' ')})\n"
                    f"- **Oldest coil**: {s.max_age:.0f} days\n"
                    f"- **Avg quality risk**: {s.qual_risk:.0f}/100")
                if s.qual_risk >= 60:
                    fc2.warning("Quality-critical — schedule under stable "
                                "conditions, not straight after a roll change.")
            if on:
                selected.append(s)

    if not selected:
        st.warning("Select at least one section.")
        st.stop()

    # Re-plan campaigns using ONLY the ticked sections
    plans = CAMP.compare_plans(
        selected,
        {"CRM04": cfg["rt04"], "CRM06": cfg["rt06"]},
        {"CRM04": cfg["mu04"], "CRM06": cfg["mu06"]})

    st.divider()

    # ── §6 Roll campaign optimisation ─────────────────────────────────────
    st.subheader("🔩 Roll Campaign — Priority vs Minimum-Changeover")

    cmp_rows = []
    for mill, d in plans.items():
        for kind in ("priority", "alternate"):
            mp = d[kind]
            cmp_rows.append({
                "Mill": mill,
                "Plan": "⚡ Priority" if kind == "priority"
                        else "🔩 Min changeover",
                "Roll changes": mp.n_changes,
                "Downtime (min)": mp.downtime_min,
                "MT planned": mp.planned_mt,
                "Coils": mp.n_coils,
                "Utilisation %": mp.utilisation,
            })
    st.dataframe(pd.DataFrame(cmp_rows), use_container_width=True,
                 hide_index=True)

    choice = {}
    for mill, d in plans.items():
        sv = d["savings_min"]
        if sv > 0:
            rec = d["recommend"]
            opts = [f"⚡ Priority — {d['priority'].n_changes} change(s), "
                    f"{d['priority'].downtime_min} min",
                    f"🔩 Min changeover — {d['alternate'].n_changes} change(s), "
                    f"{d['alternate'].downtime_min} min · saves {sv} min"]
            idx = 1 if rec == "alternate" else 0
            pick = st.radio(f"**{mill}** — recommended: **{rec}**",
                            opts, index=idx, key=f"pick_{mill}")
            choice[mill] = "alternate" if pick.startswith("🔩") else "priority"
        else:
            st.success(f"✅ **{mill}** — priority order already minimises "
                       f"changeovers ({d['priority'].n_changes} change(s))")
            choice[mill] = "priority"

    # ── Chosen campaign detail ────────────────────────────────────────────
    for mill, d in plans.items():
        mp = d[choice[mill]]
        st.markdown(f"#### 🏭 {mill} — {mp.planned_mt} MT · "
                    f"{mp.n_coils} coils · {mp.n_changes} roll change(s) · "
                    f"{mp.downtime_min} min downtime · "
                    f"{mp.utilisation}% of {mp.capacity_mt:.0f} MT capacity")
        for i, camp in enumerate(mp.campaigns, 1):
            if camp.needs_change:
                st.markdown(f"🔄 **ROLL CHANGE: {camp.change_from} → "
                            f"{camp.roll_type}** · {C.ROLL_CHANGE_MIN} min")
            with st.expander(
                f"Campaign {i} · **{camp.roll_type}** — {camp.n_coils} coils · "
                f"{camp.total_mt:.1f} MT"
                + ("  ⚠️ exceeds roll life" if camp.over_life else ""),
                expanded=(i == 1)):
                st.progress(camp.life_pct,
                            text=f"Roll life {camp.mt_start:.0f} → "
                                 f"{camp.mt_end:.0f} MT of {camp.roll_life} MT")
                for w in camp.warnings:
                    st.warning(w)
                st.dataframe(pd.DataFrame([{
                    "Seq": j + 1, "Coil": c["coil"],
                    "Section": c["section"].replace("_", " ").title(),
                    "Width": c["width"], "Thick": c["thick"], "RT": c["rt"],
                    "MT": c["mt"], "Grade": c["quality"],
                    "Customer": c["customer"], "Age (d)": c["age"],
                    "Qual risk": c["qual_risk"],
                } for j, c in enumerate(camp.coils)]),
                    use_container_width=True, hide_index=True)
        if mp.deferred:
            st.info("⏭️ **Deferred** (beyond shift capacity): " +
                    " · ".join(mp.deferred))

    st.divider()

    # ── §5 Download the rolling plan ──────────────────────────────────────
    st.subheader("📄 Download Rolling Plan")
    st.caption("Standard Tata Steel format, sequenced exactly as chosen above.")
    xl = PLAN.export_excel(selected, plans, choice, load_db())
    st.download_button(
        "⬇️ Download Mill Plan (Excel)", data=xl,
        file_name=f"mill_plan_{_date.today().strftime('%d-%m-%Y')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary", use_container_width=True)

    # Persist for the Digital Twin page
    st.session_state.chosen_plans = {m: plans[m][choice[m]] for m in plans}
    st.session_state.plan_demand  = cfg["demand"]
    st.caption("✅ This plan is now loaded into the **Digital Twin** page.")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — 🔮 DIGITAL TWIN   (Guideline §8, §9)
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🔮 Digital Twin":
    import pandas as pd
    st.title("🔮 Digital Twin — Future Pipeline")
    st.caption("What will the pipeline look like if today's plan is executed?")

    _, df = wip_gate("wip_twin")

    plans  = st.session_state.get("chosen_plans")
    demand = st.session_state.get("plan_demand")

    tc1, tc2 = st.columns([1, 2])
    horizon = tc1.slider("Horizon (days)", 3, 14, 7)
    repeat  = tc2.toggle("Assume a similar plan runs every day (steady state)",
                         value=True)

    if plans:
        st.success("✅ Simulating **with** today's plan from Plan Builder.")
    else:
        st.warning("⚠️ No plan loaded — simulating **without** any new rolling. "
                   "Build a plan first to see its effect.")

    t = TWIN.simulate(df, plans, horizon=horizon,
                      overrides=demand, repeat_plan=repeat)

    # ── Predicted problems ────────────────────────────────────────────────
    st.subheader("🚨 Predicted Bottlenecks & Starvation")
    if t.bottlenecks:
        for b in t.bottlenecks:
            (st.error if b.startswith("🔴") else st.warning)(b)
    else:
        st.success("✅ No starvation or congestion predicted in this horizon.")

    st.divider()

    # ── Consumer buffer projection ────────────────────────────────────────
    st.subheader("📉 Consumer Buffer Projection")
    tf = TWIN.twin_frame(t)
    st.line_chart(tf)
    st.caption("A line reaching zero = that consumer starves on that date.")
    st.dataframe(tf, use_container_width=True)

    # ── Stage WIP projection ──────────────────────────────────────────────
    st.subheader("⚙️ Stage WIP Projection")
    sf = TWIN.stage_frame(t)
    st.line_chart(sf)
    st.caption("Rising line = WIP building up (congestion). "
               "Falling to zero = stage starving.")

    # ── Dispatch readiness ────────────────────────────────────────────────
    st.subheader("📦 Dispatch Readiness — MT arriving per day")
    dr = pd.DataFrame(
        {C.CONSUMERS[c]["label"]: t.dispatch_ready[c] for c in t.dispatch_ready},
        index=t.dates)
    st.bar_chart(dr)
    st.dataframe(dr, use_container_width=True)
