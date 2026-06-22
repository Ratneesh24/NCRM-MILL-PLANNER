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
from datetime import date, datetime, timedelta

import streamlit as st

# ── path fix so sibling modules resolve correctly ──────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from generator import generate_daily_plan
from learner   import learn as learner_learn, calculate_accuracy
from db        import load_db, save_db, is_supabase_connected, get_storage_mode
from constants import SECTION_SHORT_NAME

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
        ["📋 Generate Plan", "🧠 Learn from Corrections",
         "📊 Stats & Rules", "⚙️ Roll Optimiser",
         "🔩 Roll Life Tracker", "📐 Width Programme",
         "🏭 Shift Execution", "🎯 Priority Advisor",
         "📓 Outcome Logger"],
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
elif page == "🧠 Learn from Corrections":
    st.title("🧠 Learn from Planner Corrections")
    st.caption(
        "Upload the WIP file + generated plan + planner's corrected version. "
        "Routing rules AND the ML model update automatically in one step."
    )

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
elif page == "📊 Stats & Rules":
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

elif page == "⚙️ Roll Optimiser":
    import pandas as pd
    from optimiser import optimise_plan, ROLL_TYPE, get_change_cost

    st.title("⚙️ Roll Change Optimiser")
    st.caption(
        "Upload today's WIP file — the optimiser analyses the planned section "
        "sequence and finds the ordering that minimises roll changes on both mills, "
        "maximising production MT."
    )

    col1, col2 = st.columns([2, 1])
    with col1:
        wip_opt = st.file_uploader(
            "Upload WIP file", type=["xlsx"], key="wip_opt"
        )
    with col2:
        opt_date = st.date_input("Plan date", value=date.today(), key="opt_date")

    if wip_opt and st.button("🔍 Analyse & Optimise", type="primary",
                              use_container_width=True):
        with st.spinner("Running roll change optimisation…"):
            try:
                from generator import load_wip, filter_rolling_coils, \
                                       assign_all, build_sections
                import tempfile, os

                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                    tmp.write(wip_opt.read())
                    wip_path = tmp.name

                df       = load_wip(wip_path)
                eligible = filter_rolling_coils(df)
                assigned = assign_all(eligible, load_db())
                sections = build_sections(assigned, load_db())
                result   = optimise_plan(sections)
                os.unlink(wip_path)

                cs = result['combined_summary']

                # ── Top summary banner ────────────────────────────────
                if cs['total_downtime_saved_min'] > 0:
                    st.success(
                        f"✅  Optimisation found **{cs['total_roll_changes_original'] - cs['total_roll_changes_optimised']} "
                        f"fewer roll change(s)** — saves **{cs['total_downtime_saved_min']} minutes** "
                        f"of downtime → **~{cs['total_extra_mt']} MT extra production possible today**"
                    )
                else:
                    st.success("✅  Current sequence is already optimal — no improvement possible.")

                # ── KPI metrics ───────────────────────────────────────
                k1, k2, k3, k4 = st.columns(4)
                k1.metric("Roll changes (current)",   cs['total_roll_changes_original'])
                k2.metric("Roll changes (optimised)", cs['total_roll_changes_optimised'],
                          delta=f"-{cs['total_roll_changes_original']-cs['total_roll_changes_optimised']}",
                          delta_color="inverse")
                k3.metric("Downtime saved",  f"{cs['total_downtime_saved_min']} min")
                k4.metric("Extra MT possible", f"~{cs['total_extra_mt']} MT",
                          delta="production gain", delta_color="normal")

                st.divider()

                # ── Hints ─────────────────────────────────────────────
                st.subheader("💡 Optimisation Hints")
                for h in result['hints']:
                    sev = h['severity']
                    icon = {'HIGH': '🔴', 'MEDIUM': '🟡',
                            'WARN': '🟠', 'OK': '🟢'}.get(sev, '⚪')
                    with st.expander(f"{icon}  {h['title']}", expanded=(sev == 'HIGH')):
                        st.code(h['detail'], language="text")
                        if h['action']:
                            st.info(f"**Action required:**\n{h['action']}")

                st.divider()

                # ── CRM04 sequence comparison ─────────────────────────
                st.subheader("CRM-04 — Section Sequence")
                a04 = result['crm04_analysis']
                col_orig, col_opt = st.columns(2)

                with col_orig:
                    st.markdown(f"**Current** — {a04['original_changes']} roll change(s), "
                                f"{a04['original_downtime_min']} min downtime")
                    rows04_orig = []
                    prev_roll = None
                    for i, sk in enumerate(a04['original_sequence']):
                        roll = ROLL_TYPE.get(sk, '?')
                        change = "⚠️ ROLL CHANGE" if (prev_roll and prev_roll != roll) else ""
                        rows04_orig.append({
                            "Pos": i+1, "Section": sk.replace('_',' ').title(),
                            "Roll Type": roll, "": change
                        })
                        prev_roll = roll
                    st.dataframe(pd.DataFrame(rows04_orig),
                                 use_container_width=True, hide_index=True)

                with col_opt:
                    st.markdown(f"**Optimised** — {a04['optimised_changes']} roll change(s), "
                                f"{a04['optimised_downtime_min']} min downtime  "
                                f"💾 saves {a04['saved_minutes']} min")
                    rows04_opt = []
                    prev_roll = None
                    for i, sk in enumerate(a04['optimised_sequence']):
                        roll = ROLL_TYPE.get(sk, '?')
                        change = "⚠️ ROLL CHANGE" if (prev_roll and prev_roll != roll) else ""
                        rows04_opt.append({
                            "Pos": i+1, "Section": sk.replace('_',' ').title(),
                            "Roll Type": roll, "": change
                        })
                        prev_roll = roll
                    st.dataframe(pd.DataFrame(rows04_opt),
                                 use_container_width=True, hide_index=True)

                st.divider()

                # ── CRM06 sequence comparison ─────────────────────────
                st.subheader("CRM-06 — Section Sequence")
                a06 = result['crm06_analysis']
                col_orig6, col_opt6 = st.columns(2)

                with col_orig6:
                    st.markdown(f"**Current** — {a06['original_changes']} roll change(s), "
                                f"{a06['original_downtime_min']} min downtime")
                    rows06_orig = []
                    prev_roll = None
                    for i, sk in enumerate(a06['original_sequence']):
                        roll = ROLL_TYPE.get(sk, '?')
                        change = "⚠️ ROLL CHANGE" if (prev_roll and prev_roll != roll) else ""
                        rows06_orig.append({
                            "Pos": i+1, "Section": sk.replace('_',' ').title(),
                            "Roll Type": roll, "": change
                        })
                        prev_roll = roll
                    st.dataframe(pd.DataFrame(rows06_orig),
                                 use_container_width=True, hide_index=True)

                with col_opt6:
                    st.markdown(f"**Optimised** — {a06['optimised_changes']} roll change(s), "
                                f"{a06['optimised_downtime_min']} min downtime  "
                                f"💾 saves {a06['saved_minutes']} min")
                    rows06_opt = []
                    prev_roll = None
                    for i, sk in enumerate(a06['optimised_sequence']):
                        roll = ROLL_TYPE.get(sk, '?')
                        change = "⚠️ ROLL CHANGE" if (prev_roll and prev_roll != roll) else ""
                        rows06_opt.append({
                            "Pos": i+1, "Section": sk.replace('_',' ').title(),
                            "Roll Type": roll, "": change
                        })
                        prev_roll = roll
                    st.dataframe(pd.DataFrame(rows06_opt),
                                 use_container_width=True, hide_index=True)

                st.divider()

                # ── Roll change event breakdown ───────────────────────
                st.subheader("Roll Change Events — Before vs After")
                all_events = []
                for evt in a04['change_events_original']:
                    all_events.append({
                        "Mill": "CRM04", "Status": "Current",
                        "From Section": evt['from_section'].replace('_',' ').title(),
                        "To Section":   evt['to_section'].replace('_',' ').title(),
                        "From Roll":    evt['from_roll'],
                        "To Roll":      evt['to_roll'],
                        "Downtime (min)": evt['minutes'],
                    })
                for evt in a04['change_events_optimised']:
                    all_events.append({
                        "Mill": "CRM04", "Status": "Optimised",
                        "From Section": evt['from_section'].replace('_',' ').title(),
                        "To Section":   evt['to_section'].replace('_',' ').title(),
                        "From Roll":    evt['from_roll'],
                        "To Roll":      evt['to_roll'],
                        "Downtime (min)": evt['minutes'],
                    })
                for evt in a06['change_events_original']:
                    all_events.append({
                        "Mill": "CRM06", "Status": "Current",
                        "From Section": evt['from_section'].replace('_',' ').title(),
                        "To Section":   evt['to_section'].replace('_',' ').title(),
                        "From Roll":    evt['from_roll'],
                        "To Roll":      evt['to_roll'],
                        "Downtime (min)": evt['minutes'],
                    })
                for evt in a06['change_events_optimised']:
                    all_events.append({
                        "Mill": "CRM06", "Status": "Optimised",
                        "From Section": evt['from_section'].replace('_',' ').title(),
                        "To Section":   evt['to_section'].replace('_',' ').title(),
                        "From Roll":    evt['from_roll'],
                        "To Roll":      evt['to_roll'],
                        "Downtime (min)": evt['minutes'],
                    })

                if all_events:
                    events_df = pd.DataFrame(all_events)
                    st.dataframe(events_df, use_container_width=True, hide_index=True)

                    # Bar chart: downtime current vs optimised
                    chart_data = pd.DataFrame({
                        "Mill":      ["CRM04 Current", "CRM04 Optimised",
                                      "CRM06 Current", "CRM06 Optimised"],
                        "Downtime (min)": [
                            a04['original_downtime_min'],
                            a04['optimised_downtime_min'],
                            a06['original_downtime_min'],
                            a06['optimised_downtime_min'],
                        ]
                    })
                    st.subheader("Downtime Comparison")
                    st.bar_chart(chart_data.set_index("Mill"))

            except Exception as e:
                st.error(f"Optimisation error: {e}")
                import traceback; st.code(traceback.format_exc())

    elif not wip_opt:
        st.info("👆 Upload today's WIP file to run the roll change optimisation.")

        # Show reference: roll change time matrix
        with st.expander("📖 Roll Change Time Reference (minutes)"):
            from optimiser import ROLL_CHANGE_MINUTES
            import pandas as pd
            roll_types = ['LIGHT_MATT', 'BRIGHT', 'SUPER_BRIGHT',
                          'CHROME_PLATED', 'HEAVY_MATT']
            matrix = {}
            for r1 in roll_types:
                matrix[r1] = {}
                for r2 in roll_types:
                    if r1 == r2:
                        matrix[r1][r2] = 0
                    else:
                        matrix[r1][r2] = ROLL_CHANGE_MINUTES.get(
                            frozenset([r1, r2]), 45)
            st.dataframe(pd.DataFrame(matrix), use_container_width=True)
            st.caption("Values in minutes. Source: CRM Sahibabad historical changeover data.")

# ══════════════════════════════════════════════════════════════════════════
# PAGE 5 — ROLL LIFE TRACKER
# ══════════════════════════════════════════════════════════════════════════
elif page == "🔩 Roll Life Tracker":
    import pandas as pd
    from roll_life import (analyse_roll_life, RollState,
                           DEFAULT_ROLL_LIFE, ROLL_TYPE)
    from db import (load_roll_state, save_roll_state,
                    record_roll_change, EMPTY_ROLL_STATE)

    st.title("🔩 Roll Life Tracker")
    st.caption(
        "Roll state is remembered across shifts. "
        "Update only what changed — the rest is pre-filled from yesterday."
    )

    # ── Load persisted state ──────────────────────────────────────────
    roll_state = load_roll_state()
    rs04 = roll_state.get("CRM04", dict(EMPTY_ROLL_STATE["CRM04"]))
    rs06 = roll_state.get("CRM06", dict(EMPTY_ROLL_STATE["CRM06"]))

    ROLL_TYPES_CRM04 = ["LIGHT_MATT", "BRIGHT", "SUPER_BRIGHT", "CHROME_PLATED"]
    ROLL_TYPES_CRM06 = ["LIGHT_MATT", "BRIGHT", "HEAVY_MATT"]

    # ── Last updated badge ────────────────────────────────────────────
    last_upd = rs04.get("last_plan_date") or rs04.get("last_updated", "")
    if last_upd:
        st.info(f"📅  Last saved: **{last_upd[:10]}**  |  "
                f"CRM04 roll: **{rs04.get('roll_type','-')}** "
                f"({rs04.get('mt_used',0):.1f} / {rs04.get('mt_life',0):.0f} MT used)  |  "
                f"CRM06 roll: **{rs06.get('roll_type','-')}** "
                f"({rs06.get('mt_used',0):.1f} / {rs06.get('mt_life',0):.0f} MT used)")
    else:
        st.warning("No saved roll state found — please enter today's roll details below.")

    st.divider()

    # ── Roll status input (pre-filled from saved state) ───────────────
    st.subheader("Current Roll Status")

    col04, col06 = st.columns(2)

    with col04:
        st.markdown("**CRM-04**")
        idx04 = ROLL_TYPES_CRM04.index(rs04.get("roll_type","LIGHT_MATT"))                 if rs04.get("roll_type") in ROLL_TYPES_CRM04 else 0
        r04_type = st.selectbox("Roll type", ROLL_TYPES_CRM04,
                                 index=idx04, key="r04t")
        r04_used = st.number_input("MT already used on this roll",
                                    min_value=0.0, max_value=1000.0,
                                    value=float(rs04.get("mt_used", 0.0)),
                                    step=5.0, key="r04u",
                                    help="Pre-filled from last session. Adjust if needed.")
        r04_life = st.number_input("Roll life (MT total)",
                                    min_value=10.0, max_value=2000.0,
                                    value=float(rs04.get("mt_life",
                                        DEFAULT_ROLL_LIFE["CRM04"].get(r04_type, 180))),
                                    step=10.0, key="r04l")
        r04_num  = st.text_input("Roll number", value=rs04.get("roll_number",""),
                                  key="r04n")

        # Roll change button
        with st.expander("🔄 Record a Roll Change on CRM04"):
            st.caption("Use this when you physically changed the roll on CRM04.")
            new04_type = st.selectbox("New roll type", ROLL_TYPES_CRM04, key="new04t")
            new04_life = st.number_input("New roll life (MT)",
                                          value=float(DEFAULT_ROLL_LIFE["CRM04"].get(
                                              new04_type, 180)),
                                          step=10.0, key="new04l")
            new04_num  = st.text_input("New roll number", key="new04n")
            if st.button("✅ Confirm CRM04 Roll Change", key="chg04"):
                record_roll_change("CRM04", new04_type, new04_life,
                                   new04_num, date.today().isoformat())
                st.success(f"CRM04 roll changed to {new04_type}. MT reset to 0.")
                st.rerun()

    with col06:
        st.markdown("**CRM-06**")
        idx06 = ROLL_TYPES_CRM06.index(rs06.get("roll_type","LIGHT_MATT"))                 if rs06.get("roll_type") in ROLL_TYPES_CRM06 else 0
        r06_type = st.selectbox("Roll type", ROLL_TYPES_CRM06,
                                 index=idx06, key="r06t")
        r06_used = st.number_input("MT already used on this roll",
                                    min_value=0.0, max_value=1000.0,
                                    value=float(rs06.get("mt_used", 0.0)),
                                    step=5.0, key="r06u",
                                    help="Pre-filled from last session. Adjust if needed.")
        r06_life = st.number_input("Roll life (MT total)",
                                    min_value=10.0, max_value=2000.0,
                                    value=float(rs06.get("mt_life",
                                        DEFAULT_ROLL_LIFE["CRM06"].get(r06_type, 160))),
                                    step=10.0, key="r06l")
        r06_num  = st.text_input("Roll number", value=rs06.get("roll_number",""),
                                  key="r06n")

        with st.expander("🔄 Record a Roll Change on CRM06"):
            st.caption("Use this when you physically changed the roll on CRM06.")
            new06_type = st.selectbox("New roll type", ROLL_TYPES_CRM06, key="new06t")
            new06_life = st.number_input("New roll life (MT)",
                                          value=float(DEFAULT_ROLL_LIFE["CRM06"].get(
                                              new06_type, 160)),
                                          step=10.0, key="new06l")
            new06_num  = st.text_input("New roll number", key="new06n")
            if st.button("✅ Confirm CRM06 Roll Change", key="chg06"):
                record_roll_change("CRM06", new06_type, new06_life,
                                   new06_num, date.today().isoformat())
                st.success(f"CRM06 roll changed to {new06_type}. MT reset to 0.")
                st.rerun()

    st.divider()

    wip_rl = st.file_uploader("Upload WIP file", type=["xlsx"], key="wip_rl")

    if wip_rl and st.button("🔍 Analyse & Save Roll State",
                             type="primary", use_container_width=True):
        with st.spinner("Simulating roll campaigns…"):
            try:
                import tempfile, os as _os
                from generator import load_wip, filter_rolling_coils,                                        assign_all, build_sections

                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                    tmp.write(wip_rl.read()); wip_path = tmp.name

                df       = load_wip(wip_path)
                eligible = filter_rolling_coils(df)
                assigned = assign_all(eligible, load_db())
                sections = build_sections(assigned, load_db())
                _os.unlink(wip_path)

                crm04_state = RollState(
                    mill="CRM04", roll_type=r04_type,
                    mt_used=r04_used, mt_life=r04_life,
                    roll_number=r04_num,
                )
                crm06_state = RollState(
                    mill="CRM06", roll_type=r06_type,
                    mt_used=r06_used, mt_life=r06_life,
                    roll_number=r06_num,
                )

                rl = analyse_roll_life(sections, crm04_state, crm06_state)
                s  = rl["summary"]

                # ── Save updated state to Supabase ────────────────────
                # Update the in-memory state with current form values
                roll_state["CRM04"].update({
                    "roll_type":   r04_type,
                    "mt_used":     r04_used,
                    "mt_life":     r04_life,
                    "roll_number": r04_num,
                })
                roll_state["CRM06"].update({
                    "roll_type":   r06_type,
                    "mt_used":     r06_used,
                    "mt_life":     r06_life,
                    "roll_number": r06_num,
                })

                # MT planned today (what will be rolled) per mill
                crm04_planned_mt = s.get("total_mt_crm04", 0.0)
                crm06_planned_mt = s.get("total_mt_crm06", 0.0)

                saved = save_roll_state(
                    roll_state,
                    plan_date       = opt_date.isoformat()
                                      if "opt_date" in dir() else
                                      date.today().isoformat(),
                    crm04_mt_rolled = 0.0,   # will increment when plan executed
                    crm06_mt_rolled = 0.0,
                )

                st.success(
                    f"✅ Roll state saved ({'☁️ Supabase' if is_supabase_connected() else '💾 Local'})  |  "
                    f"CRM04 planned: **{crm04_planned_mt:.1f} MT**  |  "
                    f"CRM06 planned: **{crm06_planned_mt:.1f} MT**"
                )

                # ── Status banner ─────────────────────────────────────
                STATUS_FN = {"CRITICAL": st.error,
                             "WARNING":  st.warning,
                             "OK":       st.success}
                STATUS_FN.get(s["status"], st.info)(
                    f"Roll Status: **{s['status']}** — "
                    f"{s['critical_count']} critical, {s['warning_count']} warnings"
                )

                k1, k2, k3, k4 = st.columns(4)
                k1.metric("CRM04 roll changes", s["crm04_roll_changes"])
                k2.metric("CRM04 total MT",     f"{s['total_mt_crm04']:.1f}")
                k3.metric("CRM06 roll changes", s["crm06_roll_changes"])
                k4.metric("CRM06 total MT",     f"{s['total_mt_crm06']:.1f}")

                st.divider()

                # ── Campaign tables ───────────────────────────────────
                for mill_label, campaigns in [
                    ("CRM-04", rl["crm04_campaigns"]),
                    ("CRM-06", rl["crm06_campaigns"]),
                ]:
                    st.subheader(f"{mill_label} — Roll Campaigns")
                    if not campaigns:
                        st.info("No campaigns found.")
                        continue
                    rows = []
                    for c in campaigns:
                        icon = {"CRITICAL":"🔴","WARNING":"🟡",
                                "MONITOR":"🟠","OK":"🟢"}.get(c["status"],"⚪")
                        rows.append({
                            "Status":        f"{icon} {c['status']}",
                            "Roll Type":     c["roll_type"],
                            "Sections":      ", ".join(
                                s2.replace("_"," ").title() for s2 in c["sections"]),
                            "MT Planned":    c["total_mt"],
                            "MT Used Start": c["start_mt_used"],
                            "MT Used End":   c["end_mt_used"],
                            "Roll Life":     c["mt_life"],
                            "% Consumed":    f"{c['pct_consumed']:.0f}%",
                            "Exhausts At":   c["exhausts_at"] or "—",
                        })
                    st.dataframe(pd.DataFrame(rows),
                                 use_container_width=True, hide_index=True)

                # ── Critical warnings ─────────────────────────────────
                criticals = [w for w in rl["all_warnings"]
                             if w.get("severity") == "CRITICAL"]
                if criticals:
                    st.divider()
                    st.subheader("🔴 Critical Warnings")
                    for w in criticals:
                        with st.expander(f"⚠️ {w['message'][:80]}", expanded=True):
                            st.error(w["message"])
                            if "recommendation" in w:
                                st.info(f"**Action:** {w['recommendation']}")
                            if w.get("mt_overrun", 0) > 0:
                                st.warning(
                                    f"Overrun: **{w['mt_overrun']:.1f} MT** beyond rated life.")

                # ── Roll usage history chart ──────────────────────────
                st.divider()
                st.subheader("📈 Roll Usage History")
                col_h04, col_h06 = st.columns(2)
                for col, mill, ms in [(col_h04, "CRM04", rs04),
                                       (col_h06, "CRM06", rs06)]:
                    with col:
                        hist = [h for h in ms.get("history", [])
                                if "mt_used_end" in h]
                        if hist:
                            h_df = pd.DataFrame(hist)
                            h_df["pct"] = (h_df["mt_used_end"] /
                                           ms.get("mt_life", 1) * 100).clip(0, 100)
                            st.markdown(f"**{mill}**")
                            st.line_chart(
                                h_df.set_index("date")["mt_used_end"],
                                height=180,
                            )
                        else:
                            st.markdown(f"**{mill}** — no history yet")

                with st.expander("📖 Default Roll Life Reference"):
                    ref = []
                    for mill2, types in DEFAULT_ROLL_LIFE.items():
                        for rt, life in types.items():
                            ref.append({"Mill": mill2,
                                        "Roll Type": rt,
                                        "Default Life (MT)": life})
                    st.dataframe(pd.DataFrame(ref),
                                 use_container_width=True, hide_index=True)

            except Exception as e:
                st.error(f"Error: {e}")
                import traceback; st.code(traceback.format_exc())

    elif not wip_rl:
        st.info("👆 Roll status pre-filled from last session. "
                "Adjust if needed, then upload WIP to analyse.")

# ══════════════════════════════════════════════════════════════════════════
# PAGE 6 — WIDTH PROGRAMME OPTIMISER
# ══════════════════════════════════════════════════════════════════════════
elif page == "📐 Width Programme":
    import pandas as pd
    from width_programme import analyse_width_programme

    st.title("📐 Width Programme Optimiser")
    st.caption(
        "Analyses the width profile across all sections — finds poor width "
        "transitions, cascade violations, and cross-section merge opportunities "
        "to reduce roll edge stress and extend roll life."
    )

    wip_wp = st.file_uploader("Upload WIP file", type=["xlsx"], key="wip_wp")

    if wip_wp and st.button("📐 Analyse Width Programme",
                             type="primary", use_container_width=True):
        with st.spinner("Analysing width programme…"):
            try:
                import tempfile, os
                from generator import load_wip, filter_rolling_coils, \
                                       assign_all, build_sections

                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                    tmp.write(wip_wp.read()); wip_path = tmp.name

                df       = load_wip(wip_path)
                eligible = filter_rolling_coils(df)
                assigned = assign_all(eligible, load_db())
                sections = build_sections(assigned, load_db())
                os.unlink(wip_path)

                wp = analyse_width_programme(sections)
                sm = wp['summary']

                # ── Programme score ───────────────────────────────────
                score  = wp['programme_score']
                rating = wp['programme_rating']
                COLOR  = {'EXCELLENT':'🟢','GOOD':'🟢','FAIR':'🟡','POOR':'🔴'}
                score_fn = st.success if score >= 65 else \
                           st.warning if score >= 45 else st.error
                score_fn(
                    f"{COLOR.get(rating,'⚪')} Width Programme Score: "
                    f"**{score}/100 ({rating})**"
                )

                k1, k2, k3, k4 = st.columns(4)
                k1.metric("Total transitions",    sm['n_transitions'])
                k2.metric("Poor transitions",     sm['poor_transitions'],
                          delta_color="inverse",
                          delta=f"-{sm['poor_transitions']}" if sm['poor_transitions'] else None)
                k3.metric("Cascade violations",   sm['cascade_violations'],
                          delta_color="inverse")
                k4.metric("Merge opportunities",  sm['merge_opportunities'],
                          delta_color="normal")

                st.divider()

                # ── Transition matrix heat-map ─────────────────────────
                st.subheader("Section Transition Matrix")
                st.caption("Each row = one section handoff. Lower score = better transition.")

                t_rows = []
                for t in wp['transitions']:
                    rating_icon = {
                        'POOR':'🔴','FAIR':'🟡',
                        'GOOD':'🟢','EXCELLENT':'🟢'
                    }.get(t['rating'],'⚪')
                    t_rows.append({
                        "From Section":    t['from_section'].replace('_',' ').title(),
                        "To Section":      t['to_section'].replace('_',' ').title(),
                        "Roll Change":     f"{t['roll_change_min']} min" if t['roll_change_min'] else "—",
                        "Width Gap (mm)":  t['width_gap_mm'],
                        "Direction":       t['width_direction'],
                        "Score":           t['total_score'],
                        "Rating":          f"{rating_icon} {t['rating']}",
                    })
                t_df = pd.DataFrame(t_rows)
                st.dataframe(t_df, use_container_width=True, hide_index=True)

                # ── Width profile chart per section ───────────────────
                st.subheader("Width Profile Across Sections")
                chart_rows = []
                for s in sections:
                    for _, row in s['coils_df'].iterrows():
                        chart_rows.append({
                            'Section': s['section_key'].replace('_',' ').title()[:15],
                            'Width':   float(row['Actual Width']),
                        })
                if chart_rows:
                    chart_df = pd.DataFrame(chart_rows)
                    import plotly.express as px
                    try:
                        fig = px.box(
                            chart_df, x='Section', y='Width',
                            color='Section',
                            title='Width Distribution by Section',
                            labels={'Width': 'Width (mm)'},
                        )
                        fig.update_layout(showlegend=False, height=400)
                        st.plotly_chart(fig, use_container_width=True)
                    except ImportError:
                        st.bar_chart(
                            chart_df.groupby('Section')['Width'].mean()
                        )

                st.divider()

                # ── Recommendations ───────────────────────────────────
                st.subheader("💡 Recommendations")
                sev_order = {'HIGH': 0, 'MEDIUM': 1, 'INFO': 2}
                recs = sorted(wp['recommendations'],
                              key=lambda r: sev_order.get(r['severity'], 3))

                if not recs:
                    st.success("No issues found — width programme is well-structured.")
                else:
                    for r in recs:
                        sev_icon = {'HIGH':'🔴','MEDIUM':'🟡','INFO':'🔵'}.get(
                            r['severity'],'⚪')
                        expanded = r['severity'] == 'HIGH'
                        with st.expander(f"{sev_icon}  {r['title']}", expanded=expanded):
                            st.code(r['detail'], language='text')
                            st.info(f"**Suggestion:** {r['suggestion']}")

                # ── Width band group analysis ─────────────────────────
                st.divider()
                st.subheader("Cross-Section Width Band Groups")
                st.caption(
                    "Sections that share the same width band AND roll type "
                    "are candidates for running as one continuous programme."
                )
                for band, groups in wp['band_groups'].items():
                    if not groups:
                        continue
                    with st.expander(f"**{band}** width band ({len(groups)} section(s))"):
                        g_df = pd.DataFrame([{
                            "Section":   g['section'].replace('_',' ').title(),
                            "Mill":      g['mill'],
                            "Roll Type": g['roll_type'],
                            "Coils":     g['coils'],
                            "MT":        g['mt'],
                            "Width Range": f"{g['min_width']:.0f}–{g['max_width']:.0f} mm",
                        } for g in groups])
                        st.dataframe(g_df, use_container_width=True, hide_index=True)

            except Exception as e:
                st.error(f"Error: {e}")
                import traceback; st.code(traceback.format_exc())

    elif not wip_wp:
        st.info("👆 Upload the WIP file to analyse the width programme.")

# ══════════════════════════════════════════════════════════════════════════
# PAGE 7 — SHIFT EXECUTION TRACKER
# ══════════════════════════════════════════════════════════════════════════
elif page == "🏭 Shift Execution":
    import pandas as pd
    from shift_tracker import (
        build_shift_record, load_shift, save_shift,
        confirm_coil, list_recent_shifts, get_shift_analytics,
        COIL_STATUS, SHIFT_NAMES
    )
    from db import load_roll_state, save_roll_state, EMPTY_ROLL_STATE
    from roll_life import DEFAULT_ROLL_LIFE

    st.title("🏭 Shift Execution Tracker")
    st.caption(
        "Live coil-by-coil confirmation. Tap ✅ as each coil finishes rolling — "
        "roll life and shift analytics update automatically."
    )

    # ── Top tabs ──────────────────────────────────────────────────────
    tab_live, tab_history, tab_analytics = st.tabs(
        ["▶️  Live Shift", "📋 Shift History", "📊 Analytics"])

    # ════════════════════════════════════════════════════════
    # TAB 1 — LIVE SHIFT
    # ════════════════════════════════════════════════════════
    with tab_live:

        # ── Shift setup ───────────────────────────────────
        col_s1, col_s2, col_s3, col_s4 = st.columns(4)
        with col_s1:
            shift_date = st.date_input("Plan date", value=date.today(), key="sd")
        with col_s2:
            shift_no = st.selectbox("Shift", [1, 2, 3],
                format_func=lambda x: SHIFT_NAMES[x], key="sno")
        with col_s3:
            shift_mill = st.selectbox("Mill", ["CRM04", "CRM06"], key="smil")
        with col_s4:
            operator_name = st.text_input("Operator name", key="sop",
                                           placeholder="optional")

        # Load saved shift or show setup
        shift_key_str = f"{shift_date.isoformat()}_{shift_mill}_S{shift_no}"
        shift = load_shift(shift_date.isoformat(), shift_mill, shift_no)

        if shift is None:
            st.info("No active shift found for this date/mill/shift. "
                    "Upload the WIP file to start a new shift.")
            wip_shift = st.file_uploader("Upload WIP file to start shift",
                                          type=["xlsx"], key="wip_shift")

            # Roll type for this shift
            rs = load_roll_state()
            mill_rs = rs.get(shift_mill, EMPTY_ROLL_STATE[shift_mill])
            RTYPES = (["LIGHT_MATT","BRIGHT","SUPER_BRIGHT","CHROME_PLATED"]
                      if shift_mill == "CRM04"
                      else ["LIGHT_MATT","BRIGHT","HEAVY_MATT"])
            curr_rt = mill_rs.get("roll_type", RTYPES[0])
            idx_rt  = RTYPES.index(curr_rt) if curr_rt in RTYPES else 0
            shift_roll = st.selectbox(
                f"Roll type on {shift_mill} at shift start",
                RTYPES, index=idx_rt, key="srt",
                help="Pre-filled from Roll Life Tracker. Change if roll was swapped.")

            if wip_shift and st.button("▶️ Start Shift", type="primary",
                                        use_container_width=True):
                import tempfile, os as _os
                from generator import load_wip, filter_rolling_coils, \
                                       assign_all, build_sections
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as t:
                    t.write(wip_shift.read()); wip_path = t.name
                df2      = load_wip(wip_path)
                elig2    = filter_rolling_coils(df2)
                asgn2    = assign_all(elig2, load_db())
                secs2    = build_sections(asgn2, load_db())
                _os.unlink(wip_path)

                # Filter sections for this mill
                mill_secs = [s for s in secs2 if s["mill"] == shift_mill]
                if not mill_secs:
                    st.error(f"No coils planned for {shift_mill} today.")
                else:
                    shift = build_shift_record(
                        shift_date.isoformat(), shift_no,
                        shift_mill, mill_secs, shift_roll)
                    shift["operator_notes"] = operator_name
                    save_shift(shift)
                    st.success(f"Shift started! {shift['total_coils']} coils, "
                               f"{shift['mt_target']:.1f} MT planned.")
                    st.rerun()

        else:
            # ── Active shift dashboard ─────────────────────────────
            mt_done    = shift.get("mt_rolled", 0)
            mt_target  = shift.get("mt_target", 1)
            pct_done   = min(100, mt_done / max(mt_target, 0.01) * 100)
            n_rolled   = shift.get("coils_rolled", 0)
            n_total    = shift.get("total_coils", 0)
            n_pending  = sum(1 for c in shift["coils"] if c["status"] == "PENDING")
            n_skipped  = shift.get("coils_skipped", 0)
            n_hold     = shift.get("coils_on_hold", 0)

            # ── KPI bar ───────────────────────────────────────────
            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric("MT Rolled",   f"{mt_done:.1f}")
            k2.metric("MT Target",   f"{mt_target:.1f}")
            k3.metric("Progress",    f"{pct_done:.1f}%")
            k4.metric("Coils Done",  f"{n_rolled}/{n_total}")
            k5.metric("Remaining",   n_pending,
                      delta=f"{n_skipped} skipped / {n_hold} hold",
                      delta_color="off")

            st.progress(pct_done / 100)

            # ── Shift info row ─────────────────────────────────────
            si1, si2, si3 = st.columns(3)
            si1.caption(f"🏭 **{shift_mill}**  |  {shift.get('shift_name')}")
            si2.caption(f"🔩 Roll: **{shift.get('roll_type')}**")
            si3.caption(f"🕐 Last update: {shift.get('last_updated','')[:16]}")

            st.divider()

            # ── Filter controls ────────────────────────────────────
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                show_status = st.multiselect(
                    "Show status", list(COIL_STATUS.keys()),
                    default=["PENDING","ON_HOLD"], key="fst")
            with fc2:
                show_section = st.multiselect(
                    "Filter section", list({c["section_key"]
                        for c in shift["coils"]}),
                    default=[], key="fsec")
            with fc3:
                search_coil = st.text_input("Search coil / SO", key="sch")

            # ── Coil list ──────────────────────────────────────────
            filtered = [
                c for c in shift["coils"]
                if c["status"] in (show_status or list(COIL_STATUS.keys()))
                and (not show_section or c["section_key"] in show_section)
                and (not search_coil or
                     search_coil.lower() in c["coil_number"].lower() or
                     search_coil.lower() in c["so_no"].lower())
            ]

            if not filtered:
                st.info("No coils match the current filter.")
            else:
                st.caption(f"Showing {len(filtered)} coils")

                # Render coil cards — group by section
                current_section = None
                for coil in filtered:
                    # Section divider
                    if coil["section_key"] != current_section:
                        current_section = coil["section_key"]
                        sec_coils  = [c for c in shift["coils"]
                                      if c["section_key"] == current_section]
                        sec_done   = sum(1 for c in sec_coils
                                         if c["status"] == "ROLLED")
                        sec_mt     = sum((c.get("actual_weight") or c["weight"])
                                        for c in sec_coils if c["status"] == "ROLLED")
                        sec_total  = sum(c["weight"] for c in sec_coils)
                        st.markdown(
                            f"#### {current_section.replace('_',' ').title()}"
                            f" — {sec_done}/{len(sec_coils)} coils"
                            f" | {sec_mt:.1f}/{sec_total:.1f} MT"
                        )

                    status = coil["status"]
                    icon   = {"PENDING":"⏳","ROLLED":"✅",
                              "SKIPPED":"⏭️","ON_HOLD":"🔴",
                              "PARTIAL":"⚠️"}.get(status,"❓")

                    # Card container
                    with st.container():
                        c1, c2, c3, c4, c5, c6 = st.columns(
                            [2.5, 1, 1, 1, 1, 3])

                        c1.markdown(
                            f"{icon} **{coil['coil_number']}**  \n"
                            f"<small>{coil['customer'][:14]} | "
                            f"{coil['quality']} | {coil['tdc']}</small>",
                            unsafe_allow_html=True)
                        c2.metric("Width", f"{coil['width']:.0f}mm",
                                  label_visibility="collapsed")
                        c2.caption(f"W: {coil['width']:.0f}mm")
                        c3.caption(f"T: {coil['thick']:.2f}→{coil['rt']:.2f}mm")
                        c4.caption(f"Wt: {coil['weight']:.3f}MT")
                        c5.caption(f"Age: {coil.get('age','-')}d")

                        with c6:
                            if status == "PENDING":
                                # Main action buttons
                                b1, b2, b3 = st.columns(3)
                                if b1.button("✅", key=f"roll_{coil['coil_number']}",
                                             help="Mark as Rolled",
                                             use_container_width=True):
                                    shift = confirm_coil(
                                        shift, coil["coil_number"],
                                        status="ROLLED",
                                        confirmed_by=operator_name)
                                    # Update roll life tracker
                                    rs = load_roll_state()
                                    save_roll_state(
                                        rs,
                                        plan_date=shift_date.isoformat(),
                                        **{f"{shift_mill.lower()}_mt_rolled":
                                           coil["weight"]})
                                    save_shift(shift)
                                    st.rerun()

                                if b2.button("⏭️", key=f"skip_{coil['coil_number']}",
                                             help="Skip this coil",
                                             use_container_width=True):
                                    shift = confirm_coil(
                                        shift, coil["coil_number"],
                                        status="SKIPPED",
                                        confirmed_by=operator_name)
                                    save_shift(shift)
                                    st.rerun()

                                if b3.button("🔴", key=f"hold_{coil['coil_number']}",
                                             help="Put on Hold",
                                             use_container_width=True):
                                    shift = confirm_coil(
                                        shift, coil["coil_number"],
                                        status="ON_HOLD",
                                        confirmed_by=operator_name)
                                    save_shift(shift)
                                    st.rerun()

                            elif status == "ROLLED":
                                st.caption(
                                    f"✅ Done  "
                                    f"{(coil.get('confirmed_at') or '')[:16]}")
                                if st.button("↩️ Undo",
                                             key=f"undo_{coil['coil_number']}",
                                             use_container_width=True):
                                    shift = confirm_coil(
                                        shift, coil["coil_number"],
                                        status="PENDING")
                                    # Deduct from roll life
                                    rs = load_roll_state()
                                    ms = rs.get(shift_mill, {})
                                    ms["mt_used"] = max(
                                        0, ms.get("mt_used",0) - coil["weight"])
                                    save_roll_state(rs)
                                    save_shift(shift)
                                    st.rerun()

                            else:
                                # Hold / Skipped — allow reset to pending
                                if st.button("↩️ Reset",
                                             key=f"rst_{coil['coil_number']}",
                                             use_container_width=True):
                                    shift = confirm_coil(
                                        shift, coil["coil_number"],
                                        status="PENDING")
                                    save_shift(shift)
                                    st.rerun()

                        st.divider()

            # ── Complete shift button ──────────────────────────────
            st.divider()
            col_end1, col_end2 = st.columns([2, 1])
            with col_end1:
                shift_notes = st.text_area(
                    "Shift-end notes (breakdowns, quality issues, etc.)",
                    value=shift.get("operator_notes",""), key="snotes",
                    height=80)
            with col_end2:
                if st.button("🏁 Complete Shift", type="primary",
                              use_container_width=True):
                    shift["status"]         = "COMPLETED"
                    shift["operator_notes"] = shift_notes
                    save_shift(shift)
                    st.success(
                        f"Shift completed!  "
                        f"{shift['coils_rolled']}/{shift['total_coils']} coils  |  "
                        f"{shift['mt_rolled']:.1f}/{shift['mt_target']:.1f} MT  |  "
                        f"{pct_done:.1f}% plan adherence")

                if st.button("💾 Save Progress", use_container_width=True):
                    shift["operator_notes"] = shift_notes
                    save_shift(shift)
                    st.toast("Progress saved ✅")

    # ════════════════════════════════════════════════════════
    # TAB 2 — SHIFT HISTORY
    # ════════════════════════════════════════════════════════
    with tab_history:
        st.subheader("Recent Shifts")
        recent = list_recent_shifts(days_back=30)
        if not recent:
            st.info("No shift records found yet.")
        else:
            h_df = pd.DataFrame(recent)
            h_df["Plan Adherence"] = h_df["adherence_pct"].astype(str) + "%"
            h_df["Coils"] = h_df["coils_rolled"].astype(str) + "/" + \
                            h_df["total_coils"].astype(str)
            h_df["MT"] = h_df["mt_rolled"].round(1).astype(str) + "/" + \
                         h_df["mt_target"].round(1).astype(str)
            display_cols = ["plan_date","shift_name","mill","roll_type",
                            "MT","Coils","Plan Adherence","status","last_updated"]
            st.dataframe(
                h_df[[c for c in display_cols if c in h_df.columns]],
                use_container_width=True, hide_index=True)

            # Drill into a specific shift
            st.subheader("View Shift Detail")
            shift_ids = [r["shift_id"] for r in recent]
            sel = st.selectbox("Select shift", shift_ids, key="sel_shift")
            if sel:
                sel_rec = next((r for r in recent if r["shift_id"] == sel), None)
                if sel_rec:
                    date_str, mill_str, sno_str = (
                        sel_rec["plan_date"],
                        sel_rec["mill"],
                        int(sel_rec["shift_id"].split("_S")[-1]))
                    full_shift = load_shift(date_str, mill_str, sno_str)
                    if full_shift and full_shift.get("coils"):
                        coil_df = pd.DataFrame([{
                            "Coil":      c["coil_number"],
                            "Section":   c["section_key"].replace("_"," ").title(),
                            "Customer":  c["customer"][:12],
                            "Width":     c["width"],
                            "Thick→RT":  f"{c['thick']:.2f}→{c['rt']:.2f}",
                            "Weight":    c["weight"],
                            "Status":    COIL_STATUS.get(c["status"], c["status"]),
                            "Done At":   (c.get("confirmed_at") or "")[:16],
                        } for c in full_shift["coils"]])
                        st.dataframe(coil_df, use_container_width=True,
                                     hide_index=True)
                        # Download
                        st.download_button(
                            "⬇️ Download shift report",
                            data=coil_df.to_csv(index=False),
                            file_name=f"shift_{sel}.csv",
                            mime="text/csv",
                        )

    # ════════════════════════════════════════════════════════
    # TAB 3 — ANALYTICS
    # ════════════════════════════════════════════════════════
    with tab_analytics:
        st.subheader("Shift Analytics")
        recent_full = list_recent_shifts(30)
        if not recent_full:
            st.info("No data yet — complete some shifts first.")
        else:
            date_from = st.date_input("From date",
                value=date.today() - __import__("datetime").timedelta(days=7),
                key="af")
            date_to   = st.date_input("To date", value=date.today(), key="at")
            mill_filter = st.multiselect("Mill", ["CRM04","CRM06"],
                                          default=["CRM04","CRM06"], key="amf")

            filtered_shifts = [
                r for r in recent_full
                if date_from.isoformat() <= r["plan_date"] <= date_to.isoformat()
                and r["mill"] in mill_filter
            ]

            if not filtered_shifts:
                st.info("No shifts in selected range.")
            else:
                # Load full records for analytics
                full_recs = []
                for r in filtered_shifts:
                    sno = int(r["shift_id"].split("_S")[-1])
                    fs  = load_shift(r["plan_date"], r["mill"], sno)
                    if fs:
                        full_recs.append(fs)

                analytics = get_shift_analytics(full_recs)

                a1, a2, a3, a4 = st.columns(4)
                a1.metric("Total MT Rolled",  f"{analytics['total_mt_rolled']:.1f}")
                a2.metric("Plan Adherence",   f"{analytics['plan_adherence']:.1f}%")
                a3.metric("Coils Rolled",     analytics["coils_rolled"])
                a4.metric("Coil Adherence",   f"{analytics['coil_adherence']:.1f}%")

                st.divider()

                col_r, col_s2 = st.columns(2)
                with col_r:
                    st.subheader("MT by Roll Type")
                    if analytics["roll_type_mt"]:
                        rt_df = pd.DataFrame(
                            analytics["roll_type_mt"].items(),
                            columns=["Roll Type","MT Rolled"])
                        st.bar_chart(rt_df.set_index("Roll Type"))

                with col_s2:
                    st.subheader("MT by Section")
                    if analytics["section_mt"]:
                        sec_df = pd.DataFrame(
                            analytics["section_mt"].items(),
                            columns=["Section","MT Rolled"])
                        sec_df["Section"] = sec_df["Section"].str.replace("_"," ").str.title()
                        st.bar_chart(sec_df.set_index("Section"))

                # Day-wise trend
                st.subheader("Day-wise MT Trend")
                day_mt = {}
                for r in filtered_shifts:
                    d = r["plan_date"]
                    day_mt[d] = day_mt.get(d, 0) + r["mt_rolled"]
                if day_mt:
                    trend_df = pd.DataFrame(
                        sorted(day_mt.items()), columns=["Date","MT Rolled"])
                    st.line_chart(trend_df.set_index("Date"))

                # Shift-wise adherence table
                st.subheader("Shift-wise Summary")
                sum_df = pd.DataFrame([{
                    "Date":        r["plan_date"],
                    "Shift":       r["shift_name"],
                    "Mill":        r["mill"],
                    "Roll Type":   r["roll_type"],
                    "MT Rolled":   round(r["mt_rolled"],1),
                    "MT Target":   round(r["mt_target"],1),
                    "Adherence %": r["adherence_pct"],
                    "Coils Done":  f"{r['coils_rolled']}/{r['total_coils']}",
                } for r in filtered_shifts])
                st.dataframe(sum_df, use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════
# PAGE 8 — PRIORITY ADVISOR
# ══════════════════════════════════════════════════════════════════════════
elif page == "🎯 Priority Advisor":
    import pandas as pd
    from priority_advisor import compute_priority, MODES, CONFIG

    st.title("🎯 Production Priority Advisor")
    st.caption(
        "Upload today's WIP → select planning mode → get scored priority "
        "sequence for CRM-04 and CRM-06 with shift briefing."
    )

    # ── Setup row ─────────────────────────────────────────────────
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        wip_pa = st.file_uploader("Upload WIP file", type=["xlsx"], key="wip_pa")
    with col2:
        mode = st.selectbox(
            "Planning mode",
            list(MODES.keys()),
            format_func=lambda k: MODES[k],
            index=0,
        )
        shift_no = st.selectbox("Shift", [1, 2, 3],
            format_func=lambda x: {1:"Shift 1 (06-14h)",
                                   2:"Shift 2 (14-22h)",
                                   3:"Shift 3 (22-06h)"}[x])
    with col3:
        from priority_advisor import CONFIG as _PA_CFG
        st.markdown("**Daily consumption rates (MT/day)** — adjust to actuals:")
        _demand_cols = st.columns(len(_PA_CFG['consumers']))
        _demand_vals = {}
        for _col, (_cname, _ccfg) in zip(
                _demand_cols, _PA_CFG['consumers'].items()):
            _demand_vals[_cname] = _col.number_input(
                _cname, value=float(_ccfg['daily_mt']),
                step=5.0, key=f"dem_{_cname}")

    # ── Tabs ─────────────────────────────────────────────────────────
elif page == "🎯 Priority Advisor":
    import pandas as pd
    from priority_advisor import (
        compute_priority, MODES, CONFIG,
        forecast_depletion, build_rolling_sheet, optimise_crs_sequence,
    )
    from roll_campaign_planner import (
        RollState, build_combined_plan, get_today_coil_shortlist,
        ROLL_TYPES, DEFAULT_ROLL_LIFE, ROLL_CHANGE_MINUTES,
    )

    st.title("🎯 Priority Advisor & Roll Campaign Planner")
    st.caption(
        "Upload today's WIP → configure rolls & capacity → one click gives "
        "priority scores, depletion forecast, CRS sequence and roll campaign plan.")

    # ══════════════════════════════════════════════════════════════
    # INPUTS — all at the top, always visible
    # ══════════════════════════════════════════════════════════════
    with st.expander("⚙️ Configure — WIP, Mode, Rolls & Capacity",
                     expanded=True):
        wip_pa = st.file_uploader("Today's WIP file", type=["xlsx"],
                                  key="wip_pa")

        col_mode, col_shift = st.columns([3, 1])
        with col_mode:
            mode = st.selectbox("Planning mode",
                list(MODES.keys()), format_func=lambda k: MODES[k])
        with col_shift:
            shift_no = st.selectbox("Shift", [1, 2, 3],
                format_func=lambda x: {1:"Shift 1",
                                       2:"Shift 2",3:"Shift 3"}[x])

        st.markdown("**Current Rolls on Mills**")
        rc1, rc2 = st.columns(2)
        with rc1:
            st.caption("CRM-04")
            roll_type_04 = st.selectbox("Roll type", ROLL_TYPES, key="rt04")
            mt_rem_04    = st.number_input(
                "MT remaining",
                min_value=0.0,
                max_value=float(DEFAULT_ROLL_LIFE.get(roll_type_04, 300)),
                value=float(DEFAULT_ROLL_LIFE.get(roll_type_04, 300)) * 0.5,
                step=10.0, key="mr04",
                help=f"Full life = {DEFAULT_ROLL_LIFE.get(roll_type_04,300)} MT")
            st.progress(
                int(mt_rem_04 / DEFAULT_ROLL_LIFE.get(roll_type_04, 300) * 100),
                text=f"{mt_rem_04 / DEFAULT_ROLL_LIFE.get(roll_type_04,300)*100:.0f}% life left")

        with rc2:
            st.caption("CRM-06")
            roll_type_06 = st.selectbox(
                "Roll type", ROLL_TYPES, key="rt06",
                index=ROLL_TYPES.index("Light Matt") if "Light Matt" in ROLL_TYPES else 0)
            mt_rem_06    = st.number_input(
                "MT remaining",
                min_value=0.0,
                max_value=float(DEFAULT_ROLL_LIFE.get(roll_type_06, 300)),
                value=float(DEFAULT_ROLL_LIFE.get(roll_type_06, 300)) * 0.5,
                step=10.0, key="mr06")
            st.progress(
                int(mt_rem_06 / DEFAULT_ROLL_LIFE.get(roll_type_06, 300) * 100),
                text=f"{mt_rem_06 / DEFAULT_ROLL_LIFE.get(roll_type_06,300)*100:.0f}% life left")

        st.markdown("**Mill Daily Capacity (MT/day)**")
        cc1, cc2 = st.columns(2)
        cap04 = cc1.number_input("CRM-04", min_value=50.0, max_value=300.0,
                                  value=165.0, step=5.0, key="cap04",
                                  help="150–180 MT/day typical")
        cap06 = cc2.number_input("CRM-06", min_value=50.0, max_value=300.0,
                                  value=185.0, step=5.0, key="cap06",
                                  help="170–200 MT/day typical")
        st.caption(f"Combined: {cap04+cap06:.0f} MT/day  (typical 220–300)")

        st.markdown("**Downstream Consumption Rates (MT/day)**")
        cons_cols = st.columns(len(CONFIG["consumers"]))
        cons_overrides = {}
        for col, (cname, ccfg) in zip(cons_cols, CONFIG["consumers"].items()):
            cons_overrides[cname] = col.number_input(
                cname, value=float(ccfg["daily_mt"]),
                step=5.0, key=f"dem_{cname}")

    if not wip_pa:
        st.info("👆 Upload today's WIP file above, then click **Run Analysis**.")
        st.stop()

    if st.button("🚀 Run Full Analysis", type="primary",
                 use_container_width=True):
        with st.spinner("Computing priority, coverage, campaigns and CRS sequence…"):
            try:
                import tempfile, os as _os
                from generator import load_wip, filter_rolling_coils,                                        assign_all, build_sections

                wip_pa.seek(0)
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as t:
                    t.write(wip_pa.read()); fp = t.name

                raw_df  = pd.read_excel(fp)
                wip_df  = load_wip(fp)
                secs    = build_sections(
                    assign_all(filter_rolling_coils(wip_df), load_db()),
                    load_db())
                _os.unlink(fp)

                # Core computations
                pr      = compute_priority(secs, wip_df=raw_df,
                                           mode=mode, shift_no=shift_no,
                                           downstream_demand=cons_overrides)
                fc      = forecast_depletion(raw_df, secs, cons_overrides)
                roll04  = RollState("CRM04", roll_type_04, mt_rem_04)
                roll06  = RollState("CRM06", roll_type_06, mt_rem_06)
                rp      = build_combined_plan(
                    roll_crm04=roll04, roll_crm06=roll06,
                    sections=secs,
                    capacity_crm04=cap04, capacity_crm06=cap06,
                    priority_result=pr)
                crs     = optimise_crs_sequence(secs, urgency_aware=True,
                                                coverage=pr.get("coverage"))
                sheets  = build_rolling_sheet(secs, pr)

                # ── Global warnings first ─────────────────────────────
                all_warnings = pr["warnings"] + rp["warnings"]
                for w in all_warnings:
                    st.warning(w)

                # ══════════════════════════════════════════════════════
                # SECTION A — CUSTOMER COVERAGE BOARD
                # ══════════════════════════════════════════════════════
                st.subheader("📊 A — Customer Demand Coverage")
                SICON = {"CRITICAL":"🔴","WARNING":"🟠","WATCH":"🟡","OK":"🟢"}
                cov_cols = st.columns(len(pr["coverage"]))
                for col, (cname, cov) in zip(
                        cov_cols,
                        sorted(pr["coverage"].items(),
                               key=lambda x: x[1].priority, reverse=True)):
                    col.metric(
                        f"{SICON[cov.status]} {cname}",
                        f"{cov.coverage_today:.1f}d cover",
                        f"Buffer {cov.ready_today_mt:.0f}MT "
                        f"+ Plan {cov.incoming_today_mt:.0f}MT",
                        delta_color="off")
                    if cov.required_today_mt > 0:
                        col.caption(
                            f"⚡ Need {cov.required_today_mt:.0f}MT "
                            f"today for 3-day cover")

                # 7-day projection chart
                with st.expander("📉 7-Day Buffer Projection", expanded=False):
                    proj_rows = []
                    for cname, r in fc.items():
                        for p in r["projection"]:
                            proj_rows.append({"Day": p["date"],
                                             "Consumer": cname,
                                             "Buffer MT": p["buffer_mt"]})
                    pivot = (pd.DataFrame(proj_rows)
                             .pivot(index="Day", columns="Consumer",
                                    values="Buffer MT"))
                    st.line_chart(pivot)
                    st.caption("Lines hitting zero = starvation date")

                st.divider()

                # ══════════════════════════════════════════════════════
                # SECTION B — ROLL CAMPAIGN PLAN (merged into priority)
                # ══════════════════════════════════════════════════════
                st.subheader("🔩 B — Roll Campaign Plan")
                rm1, rm2, rm3, rm4 = st.columns(4)
                rm1.metric("Total MT today", f"{rp['total_mt']} MT",
                           f"of {cap04+cap06:.0f} MT capacity")
                rm2.metric("Roll changes", rp["total_changes"],
                           f"{rp['total_change_min']} min downtime",
                           delta_color="inverse")
                rm3.metric("CRM-04 utilisation",
                           f"{rp['crm04'].utilisation_pct:.0f}%")
                rm4.metric("CRM-06 utilisation",
                           f"{rp['crm06'].utilisation_pct:.0f}%")

                col04r, col06r = st.columns(2)
                for col, mill_key, plan_obj in [
                        (col04r, "CRM-04", rp["crm04"]),
                        (col06r, "CRM-06", rp["crm06"])]:
                    with col:
                        rs = plan_obj.current_roll
                        st.markdown(f"#### 🏭 {mill_key}")
                        life_pct = rs.pct_remaining
                        st.progress(
                            int(life_pct),
                            text=f"{rs.roll_type} · {rs.mt_remaining:.0f}MT left · {life_pct:.0f}%")
                        for i, camp in enumerate(plan_obj.campaigns, 1):
                            if camp.preceded_by_change:
                                st.markdown(
                                    f"🔄 **Roll change → {camp.roll_type}** "
                                    f"({camp.change_cost_min} min)")
                            cst = SICON.get(camp.consumer_status, "⚪")
                            lbl = (f"Campaign {i}: **{camp.roll_type}** — "
                                   f"{camp.n_coils} coils · {camp.total_mt:.1f}MT")
                            with st.expander(lbl, expanded=(i == 1)):
                                if camp.primary_consumer:
                                    st.caption(
                                        f"{cst} Feeds **{camp.primary_consumer}** "
                                        f"({camp.consumer_status}) · "
                                        f"Priority score {camp.avg_priority_score:.0f}/100")
                                camp_rows = [
                                    {"Seq": j+1, "Coil": c["coil"],
                                     "Section": c["section"],
                                     "Width": c["width"], "Thick": c["thick"],
                                     "RT": c["rt"], "MT": c["mt"],
                                     "Customer": c["customer"],
                                     "Age(d)": c["age"]}
                                    for j, c in enumerate(camp.coils)]
                                st.dataframe(pd.DataFrame(camp_rows),
                                             use_container_width=True,
                                             hide_index=True)
                        if plan_obj.deferred_sections:
                            st.info("⏭️ Deferred: " +
                                    ", ".join(plan_obj.deferred_sections))

                st.divider()

                # ══════════════════════════════════════════════════════
                # SECTION C — PRIORITY SCORES
                # ══════════════════════════════════════════════════════
                st.subheader("🎯 C — Section Priority Scores")
                pc04, pc06 = st.columns(2)
                for col, mill, seq in [
                        (pc04, "CRM-04", pr["crm04_sequence"]),
                        (pc06, "CRM-06", pr["crm06_sequence"])]:
                    with col:
                        st.markdown(f"#### {mill}")
                        for s in seq:
                            rank = s.rank_crm04 if mill == "CRM-04" else s.rank_crm06
                            score = s.total_score
                            icon  = ("🔴" if score >= 75 else
                                     "🟡" if score >= 50 else "🟢")
                            with st.expander(
                                    f"#{rank} {icon} "
                                    f"{s.section_key.replace('_',' ').title()} "
                                    f"— {score:.0f}/100",
                                    expanded=(rank == 1)):
                                c1, c2 = st.columns(2)
                                c1.metric("MT", f"{s.total_mt:.1f}")
                                c2.metric("Consumer", s.consumer)
                                st.progress(int(score),
                                            text=f"Overall: {score:.0f}/100")
                                score_df = pd.DataFrame({
                                    "Factor": ["A Starvation","B Customer",
                                               "C Age","D Dispatch",
                                               "E Pipeline","F Efficiency",
                                               "G Setup"],
                                    "Score":  [s.A_starvation, s.B_customer,
                                               s.C_age, s.D_dispatch,
                                               s.E_pipeline, s.F_efficiency,
                                               s.G_setup],
                                })
                                st.dataframe(score_df, use_container_width=True,
                                             hide_index=True)
                                if s.warnings:
                                    for w in s.warnings:
                                        st.caption(w)
                                st.caption(s.explanation)

                st.divider()

                # ══════════════════════════════════════════════════════
                # SECTION D — CRS SEQUENCE
                # ══════════════════════════════════════════════════════
                st.subheader("🔧 D — CRS Setting Change Optimiser")
                if "error" in crs:
                    st.info(crs["error"])
                else:
                    xm1, xm2, xm3, xm4 = st.columns(4)
                    xm1.metric("CRS coils",      crs["total_coils"])
                    xm2.metric("Total MT",        f"{crs['total_mt']} MT")
                    xm3.metric("Changes (before)",crs["original_changes"])
                    xm4.metric("Changes (after)", crs["optimised_changes"],
                               delta=f"-{crs['changes_saved']}",
                               delta_color="inverse"
                               if crs["changes_saved"] > 0 else "off")
                    for rec in crs["recommendations"]:
                        st.success(rec) if rec.startswith("✅") else st.warning(rec)
                    with st.expander("CRS sequence table", expanded=False):
                        seq_rows = []
                        for i, c in enumerate(crs["optimised_sequence"], 1):
                            chg = next((e for e in crs["change_events"]
                                        if e["position"] == i-1), None)
                            seq_rows.append({
                                "Pos":       i,
                                "Coil":      c["coil_number"],
                                "Width":     c["width"],
                                "Thick":     c["thick"],
                                "Product":   c["product"],
                                "MT":        round(c["weight"], 3),
                                "Customer":  c["customer"][:15],
                                "⚠️ Change": " | ".join(chg["changes"]) if chg else "",
                            })
                        st.dataframe(pd.DataFrame(seq_rows),
                                     use_container_width=True, hide_index=True)

                st.divider()

                # ══════════════════════════════════════════════════════
                # SECTION E — ROLLING SHEETS
                # ══════════════════════════════════════════════════════
                st.subheader("📄 E — Ordered Rolling Sheets")
                sh04, sh06 = st.columns(2)
                for col, mill, items in [
                        (sh04, "CRM-04", sheets["CRM04"]),
                        (sh06, "CRM-06", sheets["CRM06"])]:
                    with col:
                        st.markdown(f"#### {mill}")
                        csv_rows = []
                        for item in items:
                            if item["type"] == "header":
                                st.markdown(
                                    f"**⬛ P{item['priority']}: "
                                    f"{item['section'].replace('_',' ').title()}** "
                                    f"— {item['coil_count']}c · {item['total_mt']}MT")
                                csv_rows.append({
                                    "Seq":"","Coil": f"=== P{item['priority']}: "
                                    f"{item['section']} ===",
                                    "Width":"","Thick":"","RT":"",
                                    "MT": item["total_mt"],
                                    "Customer":"","Remark":""})
                            else:
                                csv_rows.append({
                                    "Seq":      item["seq"],
                                    "Coil":     item["coil"],
                                    "Width":    item["width"],
                                    "Thick":    item["thick"],
                                    "RT":       item["rt"],
                                    "MT":       item["weight"],  # key is weight in rolling sheet
                                    "Customer": item["customer"],
                                    "Remark":   item["remark"]})
                        coil_rows = [r for r in csv_rows if r["Seq"] != ""]
                        st.dataframe(pd.DataFrame(coil_rows),
                                     use_container_width=True,
                                     hide_index=True, height=300)
                        st.download_button(
                            f"⬇️ {mill} sheet (CSV)",
                            data=pd.DataFrame(csv_rows).to_csv(index=False),
                            file_name=f"rolling_sheet_{mill.replace('-','')}.csv",
                            mime="text/csv",
                            use_container_width=True,
                            key=f"dl_{mill}")

                st.divider()

                # ══════════════════════════════════════════════════════
                # SECTION F — COMBINED SHIFT BRIEFING
                # ══════════════════════════════════════════════════════
                st.subheader("📱 F — Combined Shift Briefing")
                combined_briefing = (
                    pr["briefing"] + "\n\n" + rp["briefing"])
                st.code(combined_briefing, language="text")
                sc1, sc2, sc3 = st.columns(3)
                sc1.download_button(
                    "⬇️ Full briefing (.txt)",
                    data=combined_briefing,
                    file_name=f"shift_briefing_shift{shift_no}.txt",
                    mime="text/plain", key="dl_brief")

                shortlist = get_today_coil_shortlist(rp)
                all_rows = []
                for mill_s, its in shortlist.items():
                    for item in its:
                        if item["type"] == "coil":
                            all_rows.append({
                                "Mill":mill_s,"Campaign":item["section"],
                                "Seq":item["seq"],"Coil":item["coil"],
                                "Width":item["width"],"Thick":item["thick"],
                                "RT":item["rt"],"MT":item["mt"],
                                "Customer":item["customer"],"Age(d)":item["age"]})
                sc2.download_button(
                    "⬇️ Coil shortlist (CSV)",
                    data=pd.DataFrame(all_rows).to_csv(index=False),
                    file_name="todays_coil_shortlist.csv",
                    mime="text/csv", key="dl_shortlist")

                if "optimised_sequence" in crs:
                    crs_csv = pd.DataFrame([{
                        "Pos":i+1,"Coil":c["coil_number"],
                        "Width":c["width"],"Thick":c["thick"],
                        "MT":round(c["weight"],3),"Customer":c["customer"]}
                        for i,c in enumerate(crs["optimised_sequence"])])
                    sc3.download_button(
                        "⬇️ CRS sequence (CSV)",
                        data=crs_csv.to_csv(index=False),
                        file_name="crs_sequence.csv",
                        mime="text/csv", key="dl_crs")

                # Mode comparison
                with st.expander("🔄 Compare all planning modes"):
                    comp_rows = []
                    for m in MODES:
                        r = compute_priority(secs, wip_df=raw_df,
                                            mode=m, shift_no=shift_no,
                                            downstream_demand=cons_overrides)
                        t04 = r["crm04_sequence"][0] if r["crm04_sequence"] else None
                        t06 = r["crm06_sequence"][0] if r["crm06_sequence"] else None
                        comp_rows.append({
                            "Mode":           MODES[m],
                            "CRM04 #1":       t04.section_key.replace("_"," ").title() if t04 else "—",
                            "CRM04 Score":    t04.total_score if t04 else 0,
                            "CRM06 #1":       t06.section_key.replace("_"," ").title() if t06 else "—",
                            "CRM06 Score":    t06.total_score if t06 else 0,
                            "Direct MT":      r["kpis"]["direct_mt"],
                            "Anneal MT":      r["kpis"]["anneal_mt"],
                        })
                    st.dataframe(pd.DataFrame(comp_rows),
                                 use_container_width=True, hide_index=True)

            except Exception as e:
                st.error(f"Analysis error: {e}")
                import traceback; st.code(traceback.format_exc())


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — OUTCOME LOGGER
# ══════════════════════════════════════════════════════════════════════════════
elif page == "📓 Outcome Logger":
    import pandas as pd
    from outcome_logger import (save_outcome, load_outcomes,
                                 delete_outcome, calibrate_from_outcomes)
    from priority_advisor import CONFIG

    st.title("📓 Daily Outcome Logger")
    st.caption(
        "Log what actually happened each day. After 5+ days this auto-calibrates "
        "all assumed constants — consumption rates, roll life, change times — "
        "replacing guesses with your measured reality.")

    # Safe import — roll_campaign_planner may not exist on older deployments
    try:
        from roll_campaign_planner import ROLL_TYPES
    except ImportError:
        ROLL_TYPES = ["Light Matt", "Bright", "Super Bright",
                      "Chrome Plated", "Heavy Matt"]

    tab_log, tab_history, tab_calibration = st.tabs(
        ["📝 Log Today", "📅 History", "🔬 Calibration"])

    # ── TAB 1: LOG TODAY ──────────────────────────────────────────────
    with tab_log:
        st.subheader("Log Today's Outcomes")

        with st.form("outcome_form"):
            fc1, fc2, fc3 = st.columns(3)
            log_date = fc1.date_input("Date", value=pd.Timestamp.now().date())
            shift_no = fc2.selectbox("Shift",
                [0, 1, 2, 3],
                format_func=lambda x: {0:"Full day",1:"Shift 1",
                                       2:"Shift 2",3:"Shift 3"}[x])
            logged_by = fc3.text_input("Logged by", placeholder="Your name")

            st.markdown("---")
            st.markdown("#### 🏭 Actual Rolling Output")
            oc1, oc2 = st.columns(2)
            crm04_mt = oc1.number_input("CRM-04 MT rolled",
                min_value=0.0, max_value=500.0, value=0.0, step=1.0)
            crm06_mt = oc2.number_input("CRM-06 MT rolled",
                min_value=0.0, max_value=500.0, value=0.0, step=1.0)

            st.markdown("#### 🔩 Roll Changes")
            rc1, rc2, rc3, rc4 = st.columns(4)
            c04_changes = rc1.number_input("CRM-04 changes",
                min_value=0, max_value=10, value=0, step=1)
            c04_time    = rc2.number_input("CRM-04 change time (min)",
                min_value=0, max_value=300, value=0, step=5)
            c06_changes = rc3.number_input("CRM-06 changes",
                min_value=0, max_value=10, value=0, step=1)
            c06_time    = rc4.number_input("CRM-06 change time (min)",
                min_value=0, max_value=300, value=0, step=5)

            st.markdown("#### 📦 Actual Consumption per Consumer (MT/day)")
            st.caption("How much did each downstream consumer actually consume today?")
            con_cols = st.columns(5)
            crs_cons  = con_cols[0].number_input("CRS",       0.0, 500.0, 0.0, 5.0)
            ht_cons   = con_cols[1].number_input("H&T Line",  0.0, 200.0, 0.0, 5.0)
            spm_cons  = con_cols[2].number_input("Skin Pass", 0.0, 200.0, 0.0, 5.0)
            ann_cons  = con_cols[3].number_input("Annealing", 0.0, 500.0, 0.0, 5.0)
            tube_cons = con_cols[4].number_input("Tube Plant",0.0, 300.0, 0.0, 5.0)

            st.markdown("#### 🔴 Starvation Events")
            st.caption("Did any consumer actually run out of material or slow down?")
            st1, st2, st3, st4, st5 = st.columns(5)
            crs_s  = st1.checkbox("CRS starved")
            ht_s   = st2.checkbox("H&T starved")
            spm_s  = st3.checkbox("SPM starved")
            ann_s  = st4.checkbox("Annealing starved")
            tube_s = st5.checkbox("Tube starved")

            st.markdown("#### 🎯 Priority Recommendation Tracking")
            rpa, rpb, rpc = st.columns(3)
            mode_used   = rpa.selectbox(
                "Mode recommended today",
                ["BALANCED","TUBE_URGENT","HT_URGENT","CRS_URGENT",
                 "MAX_PROD","CLEAR_BACKLOG","FEED_ANNEAL",
                 "DISPATCH_RECOVERY","PIPELINE_PROTECTION","(not used)"])
            rec_followed = rpb.checkbox(
                "Shift followed the priority recommendation?")
            rec_accurate = rpc.checkbox(
                "Following it gave a good outcome?",
                disabled=not rec_followed)

            st.markdown("#### 🔩 Roll State at End of Shift")
            rs1, rs2 = st.columns(2)
            crm04_roll_type = rs1.selectbox("CRM-04 roll type used",
                ROLL_TYPES, key="olog_rt04")
            crm04_roll_mt   = rs1.number_input(
                "CRM-04 MT on current roll", 0.0, 500.0, 0.0, 5.0)
            crm06_roll_type = rs2.selectbox("CRM-06 roll type used",
                ROLL_TYPES, key="olog_rt06",
                index=ROLL_TYPES.index("Light Matt")
                if "Light Matt" in ROLL_TYPES else 0)
            crm06_roll_mt   = rs2.number_input(
                "CRM-06 MT on current roll", 0.0, 500.0, 0.0, 5.0)

            notes = st.text_area("Notes / observations",
                placeholder="Any issues, surprises, or context for today...")

            submitted = st.form_submit_button(
                "💾 Save Outcome", type="primary", use_container_width=True)

        if submitted:
            outcome = {
                "log_date":              str(log_date),
                "shift_no":              shift_no,
                "logged_by":             logged_by,
                "crm04_mt_rolled":       crm04_mt,
                "crm06_mt_rolled":       crm06_mt,
                "total_mt_rolled":       round(crm04_mt + crm06_mt, 1),
                "crm04_roll_changes":    c04_changes,
                "crm04_change_min":      c04_time,
                "crm06_roll_changes":    c06_changes,
                "crm06_change_min":      c06_time,
                "crs_consumed_mt":       crs_cons,
                "ht_consumed_mt":        ht_cons,
                "spm_consumed_mt":       spm_cons,
                "anneal_consumed_mt":    ann_cons,
                "tube_consumed_mt":      tube_cons,
                "crs_starved":           crs_s,
                "ht_starved":            ht_s,
                "spm_starved":           spm_s,
                "anneal_starved":        ann_s,
                "tube_starved":          tube_s,
                "mode_used":             mode_used,
                "recommendation_followed": rec_followed,
                "recommendation_accurate": rec_accurate,
                "crm04_roll_type":       crm04_roll_type,
                "crm04_roll_mt_used":    crm04_roll_mt,
                "crm06_roll_type":       crm06_roll_type,
                "crm06_roll_mt_used":    crm06_roll_mt,
                "notes":                 notes,
            }
            ok = save_outcome(outcome)
            if ok:
                st.success(f"✅ Outcome saved for {log_date} Shift {shift_no}")
            else:
                st.error("Save failed — check Supabase connection")

    # ── TAB 2: HISTORY ────────────────────────────────────────────────
    with tab_history:
        st.subheader("Outcome History")
        days_back = st.slider("Show last N days", 7, 90, 30)
        df_hist = load_outcomes(days_back)

        if df_hist.empty:
            st.info("No outcomes logged yet. Start logging daily after each shift.")
            st.markdown("""
**What to log each day (takes ~5 minutes):**
1. Actual MT rolled on CRM-04 and CRM-06
2. Number of roll changes and actual time lost
3. Approximate MT consumed by each downstream consumer
4. Whether any consumer actually starved (yes/no)
5. Whether you followed the priority recommendation

**After 5 days** — the Calibration tab starts replacing guessed constants.
**After 30 days** — the system is tuned to your actual operation.
            """)
        else:
            st.success(f"{len(df_hist)} records from the last {days_back} days")

            # Summary metrics
            sm1, sm2, sm3, sm4 = st.columns(4)
            sm1.metric("Avg daily MT",
                round(pd.to_numeric(df_hist.get("total_mt_rolled", 0),
                      errors="coerce").mean(), 1))
            sm2.metric("Avg roll changes/day",
                round((pd.to_numeric(df_hist.get("crm04_roll_changes", 0),
                       errors="coerce").fillna(0) +
                       pd.to_numeric(df_hist.get("crm06_roll_changes", 0),
                       errors="coerce").fillna(0)).mean(), 1))

            STARVE_COLS = ["crs_starved","ht_starved","spm_starved",
                           "anneal_starved","tube_starved"]
            LABELS      = ["CRS","H&T","SPM","Ann","Tube"]
            any_starved = sum(
                df_hist[c].map(lambda x: x if isinstance(x,bool)
                               else str(x).lower()=="true").sum()
                for c in STARVE_COLS if c in df_hist.columns)
            sm3.metric("Starvation events", int(any_starved),
                       f"in {len(df_hist)} days",
                       delta_color="inverse")

            if "recommendation_followed" in df_hist.columns:
                fol = df_hist["recommendation_followed"].map(
                    lambda x: x if isinstance(x,bool)
                    else str(x).lower()=="true")
                sm4.metric("Recommendation followed",
                           f"{fol.mean()*100:.0f}%")

            st.divider()

            # Consumption trend
            st.markdown("#### Actual Consumption Rates (MT/day)")
            cons_data = {}
            for label, col in [("CRS","crs_consumed_mt"),
                                ("H&T","ht_consumed_mt"),
                                ("SPM","spm_consumed_mt"),
                                ("Annealing","anneal_consumed_mt"),
                                ("Tube","tube_consumed_mt")]:
                if col in df_hist.columns:
                    vals = pd.to_numeric(df_hist[col], errors="coerce")
                    vals = vals[vals > 0]
                    if len(vals) > 0:
                        cons_data[label] = vals.values[::-1]

            if cons_data:
                dates = df_hist["log_date"].dt.strftime("%d/%m")[::-1]
                cons_df = pd.DataFrame(cons_data,
                    index=range(len(df_hist)))
                st.line_chart(cons_df)
                st.caption("Actual MT consumed per day by each downstream consumer")

            # Starvation heatmap
            st.markdown("#### Starvation Events")
            starve_df = pd.DataFrame()
            for label, col in zip(LABELS, STARVE_COLS):
                if col in df_hist.columns:
                    starve_df[label] = df_hist[col].map(
                        lambda x: 1 if (x is True or
                                        str(x).lower()=="true") else 0)
            if not starve_df.empty:
                starve_summary = starve_df.sum().reset_index()
                starve_summary.columns = ["Consumer","Starvation Events"]
                starve_summary["Frequency %"] = (
                    starve_summary["Starvation Events"] /
                    len(df_hist) * 100).round(1)
                st.dataframe(starve_summary, use_container_width=True,
                             hide_index=True)

            # Raw log
            with st.expander("Raw outcome log"):
                display_cols = ["log_date","shift_no","total_mt_rolled",
                    "crm04_mt_rolled","crm06_mt_rolled",
                    "crs_consumed_mt","ht_consumed_mt",
                    "crm04_roll_changes","crm06_roll_changes",
                    "mode_used","recommendation_followed","notes"]
                show_cols = [c for c in display_cols if c in df_hist.columns]
                st.dataframe(df_hist[show_cols], use_container_width=True,
                             hide_index=True)

                # Delete option
                del_date = st.text_input(
                    "Delete record by date (YYYY-MM-DD)", key="del_date")
                del_shift = st.number_input(
                    "Shift", 0, 3, 0, key="del_shift")
                if st.button("🗑️ Delete this record", key="del_btn"):
                    if del_date:
                        if delete_outcome(del_date, del_shift):
                            st.success("Deleted")
                            st.rerun()
                        else:
                            st.error("Delete failed")

    # ── TAB 3: CALIBRATION ────────────────────────────────────────────
    with tab_calibration:
        st.subheader("🔬 Auto-Calibration from Outcomes")
        st.caption(
            "Once you have ≥5 days logged, these measured values replace "
            "the assumed constants in the Priority Advisor and Roll Planner.")

        df_cal = load_outcomes(90)
        cal    = calibrate_from_outcomes(df_cal)

        if cal["status"] == "insufficient_data":
            st.warning(cal["message"])
            st.markdown("""
**Currently assumed values (not yet measured):**

| Constant | Assumed value | Source |
|---|---|---|
| CRS daily consumption | 110 MT/day | Estimate |
| H&T daily consumption | 45 MT/day | Estimate |
| Skin Pass daily consumption | 60 MT/day | Estimate |
| Annealing daily consumption | 130 MT/day | Estimate |
| Tube Plant daily consumption | 80 MT/day | Estimate |
| CRM-04 daily capacity | 150–180 MT | Known range |
| CRM-06 daily capacity | 170–200 MT | Known range |
| Roll change time | 45 min | Planner confirmed |
| Bright roll life | 180 MT | Estimate |
| Light Matt roll life | 300 MT | Estimate |

Start logging daily outcomes. These will be measured and replaced automatically.
            """)
        else:
            st.success(
                f"✅ Calibrated from **{cal['n_days']} days** of actual data")
            calibrated = cal.get("calibrated", {})

            # Consumption rates
            st.markdown("#### Consumption Rates (MT/day)")
            cons_rows = []
            for consumer in ["CRS","H&T Line","Skin Pass","Annealing","Tube Plant"]:
                key  = f"{consumer}_daily_mt"
                assumed = CONFIG["consumers"].get(consumer, {}).get("daily_mt", "—")
                if key in calibrated:
                    c = calibrated[key]
                    delta = round(c["recommended"] - assumed, 1) if isinstance(assumed, (int,float)) else None
                    cons_rows.append({
                        "Consumer":   consumer,
                        "Assumed":    assumed,
                        "Measured":   c["recommended"],
                        "Range":      f"{c['p25']}–{c['p75']}",
                        "n days":     c["n"],
                        "Δ vs assumed": delta,
                    })
                else:
                    cons_rows.append({
                        "Consumer": consumer,
                        "Assumed":  assumed,
                        "Measured": "not yet",
                        "Range":    "—",
                        "n days":   0,
                        "Δ vs assumed": "—",
                    })
            cons_cal_df = pd.DataFrame(cons_rows)
            st.dataframe(cons_cal_df, use_container_width=True, hide_index=True)

            # Apply button — updates CONFIG live
            if st.button("✅ Apply measured consumption rates to Priority Advisor",
                         use_container_width=True):
                from priority_advisor import CONFIG as PA_CONFIG
                applied = []
                for consumer in ["CRS","H&T Line","Skin Pass",
                                  "Annealing","Tube Plant"]:
                    key = f"{consumer}_daily_mt"
                    if key in calibrated:
                        PA_CONFIG["consumers"][consumer]["daily_mt"] = \
                            calibrated[key]["recommended"]
                        applied.append(
                            f"{consumer}: {calibrated[key]['recommended']} MT/day")
                if applied:
                    st.success("Applied: " + " | ".join(applied))

            st.divider()

            # Mill capacity
            st.markdown("#### Mill Daily Capacity")
            cap_rows = []
            for mill, low_def, high_def in [
                    ("CRM04", 150, 180), ("CRM06", 170, 200)]:
                key = f"{mill}_daily_capacity"
                if key in calibrated:
                    c = calibrated[key]
                    cap_rows.append({
                        "Mill":           mill,
                        "Assumed low":    low_def,
                        "Assumed high":   high_def,
                        "Measured avg":   c["mean"],
                        "Measured P25":   c["p25"],
                        "Measured P75":   c["p75"],
                        "n days":         c["n"],
                    })
            if cap_rows:
                st.dataframe(pd.DataFrame(cap_rows),
                             use_container_width=True, hide_index=True)

            st.divider()

            # Roll change time
            st.markdown("#### Roll Change Time")
            chg_rows = []
            for mill in ["CRM04", "CRM06"]:
                key = f"{mill}_change_time_min"
                if key in calibrated:
                    c = calibrated[key]
                    chg_rows.append({
                        "Mill":          mill,
                        "Assumed":       "45 min",
                        "Measured avg":  f"{c['mean']} min",
                        "Measured median": f"{c['median']} min",
                        "n events":      c["n_events"],
                        "Recommended":   f"{c['recommended']} min",
                    })
            if chg_rows:
                st.dataframe(pd.DataFrame(chg_rows),
                             use_container_width=True, hide_index=True)

            st.divider()

            # Starvation risk
            st.markdown("#### Starvation Risk by Consumer")
            starve_rows = []
            for consumer in ["CRS","H&T Line","Skin Pass",
                              "Annealing","Tube Plant"]:
                key = f"{consumer}_starvation_pct"
                if key in calibrated:
                    c = calibrated[key]
                    risk_color = {"HIGH":"🔴","MEDIUM":"🟠","LOW":"🟢"}
                    starve_rows.append({
                        "Consumer":    consumer,
                        "Risk":        f"{risk_color.get(c['risk'],'⚪')} {c['risk']}",
                        "Frequency":   f"{c['frequency_pct']}%",
                        "Events":      c["n_events"],
                        "Days logged": c["n_days"],
                    })
            if starve_rows:
                st.dataframe(pd.DataFrame(starve_rows),
                             use_container_width=True, hide_index=True)
                st.caption(
                    "HIGH risk (>30% of days) = priority advisor must always "
                    "prioritise this consumer. Use these numbers to confirm "
                    "whether the BALANCED mode is working.")

            # Recommendation accuracy
            rec_key = "recommendation_accuracy"
            if rec_key in calibrated:
                c = calibrated[rec_key]
                st.divider()
                st.markdown("#### Priority Recommendation Accuracy")
                ra1, ra2, ra3 = st.columns(3)
                ra1.metric("Days recommendation followed",
                           f"{c['followed_pct']}%")
                if c.get("accurate_when_followed_pct") is not None:
                    ra2.metric("Accurate when followed",
                               f"{c['accurate_when_followed_pct']}%")
                ra3.metric("Days in dataset", c["n_days"])
                if c.get("accurate_when_followed_pct", 0) and \
                   c["accurate_when_followed_pct"] < 60:
                    st.warning(
                        "⚠️ Recommendation accuracy below 60% — "
                        "the scoring weights need tuning. "
                        "Consider switching to BALANCED mode and building "
                        "more history before using specialised modes.")
