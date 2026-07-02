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
        ["📋 Generate Plan", "🧠 Learn",
         "🎯 Priority Advisor", "📊 Stats"],
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

    from outcome_logger import save_outcome, load_outcomes
    import pandas as _olpd

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


elif page == "🎯 Priority Advisor":
    import pandas as pd
    from priority_advisor import (
        compute_priority, MODES, CONFIG,
        forecast_depletion, build_rolling_sheet, optimise_crs_sequence,
        build_consumer_coverage,
    )

    # Roll types — inline fallback so no external import needed at module level
    ROLL_TYPES    = ["Light Matt", "Bright", "Super Bright", "Chrome Plated", "Heavy Matt"]
    ROLL_LIFE_MAX = {"Light Matt": 300, "Bright": 180, "Super Bright": 120,
                     "Chrome Plated": 80, "Heavy Matt": 200}
    ROLL_CHANGE_MIN = 45

    st.title("🎯 Priority Advisor")
    SICON = {"CRITICAL": "🔴", "WARNING": "🟠", "WATCH": "🟡", "OK": "🟢"}

    # ── Session state for rolled coils ────────────────────────────────────
    if "rolled_coils" not in st.session_state:
        st.session_state.rolled_coils = set()
    if "pa_sections" not in st.session_state:
        st.session_state.pa_sections = None
    if "pa_raw_df" not in st.session_state:
        st.session_state.pa_raw_df = None
    if "pa_result" not in st.session_state:
        st.session_state.pa_result = None

    # ══════════════════════════════════════════════════════════════════════
    # INPUTS PANEL
    # ══════════════════════════════════════════════════════════════════════
    with st.expander("⚙️ Setup", expanded=st.session_state.pa_sections is None):
        wip_pa = st.file_uploader("WIP file", type=["xlsx"], key="wip_pa_v3")

        ic1, ic2 = st.columns(2)
        mode     = ic1.selectbox("Planning mode",
            list(MODES.keys()), format_func=lambda k: MODES[k])
        shift_no = ic2.selectbox("Shift", [1,2,3],
            format_func=lambda x: f"Shift {x}")

        st.markdown("**Current rolls**")
        rca, rcb = st.columns(2)
        with rca:
            st.caption("CRM-04")
            rt04     = st.selectbox("Roll type", ROLL_TYPES, key="rt04v3")
            mtr04    = st.number_input("MT remaining", 0.0,
                float(ROLL_LIFE_MAX[rt04]), float(ROLL_LIFE_MAX[rt04])*0.5,
                10.0, key="mtr04v3")
            st.progress(int(mtr04/ROLL_LIFE_MAX[rt04]*100),
                        text=f"{mtr04/ROLL_LIFE_MAX[rt04]*100:.0f}% life")
        with rcb:
            st.caption("CRM-06")
            idx06    = ROLL_TYPES.index("Light Matt") if "Light Matt" in ROLL_TYPES else 0
            rt06     = st.selectbox("Roll type", ROLL_TYPES, key="rt06v3", index=idx06)
            mtr06    = st.number_input("MT remaining", 0.0,
                float(ROLL_LIFE_MAX[rt06]), float(ROLL_LIFE_MAX[rt06])*0.5,
                10.0, key="mtr06v3")
            st.progress(int(mtr06/ROLL_LIFE_MAX[rt06]*100),
                        text=f"{mtr06/ROLL_LIFE_MAX[rt06]*100:.0f}% life")

        cpc1, cpc2 = st.columns(2)
        cap04 = cpc1.number_input("CRM-04 capacity MT/day", 50.0, 300.0, 165.0, 5.0)
        cap06 = cpc2.number_input("CRM-06 capacity MT/day", 50.0, 300.0, 185.0, 5.0)

        st.markdown("**Consumption rates (MT/day)** — adjust to actuals")
        cons_cols = st.columns(4)
        cons_ovr  = {}
        for col, cname in zip(cons_cols, ["H&T Line","Tube Plant","OEM","Annealing"]):
            def_val = CONFIG["consumers"].get(cname, {}).get("daily_mt", 50.0)
            cons_ovr[cname] = col.number_input(cname, 0.0, 500.0,
                float(def_val), 5.0, key=f"cons_{cname}")

        run_btn = st.button("🚀 Generate Plan & Run Analysis",
                            type="primary", use_container_width=True,
                            disabled=wip_pa is None)

    if run_btn and wip_pa:
        with st.spinner("Running…"):
            try:
                import tempfile, os as _os
                from generator import load_wip, filter_rolling_coils, \
                                       assign_all, build_sections
                wip_pa.seek(0)
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as t:
                    t.write(wip_pa.read()); fp = t.name
                raw_df = pd.read_excel(fp)
                wip_df = load_wip(fp)
                secs   = build_sections(
                    assign_all(filter_rolling_coils(wip_df), load_db()), load_db())
                _os.unlink(fp)
                pr = compute_priority(secs, wip_df=raw_df,
                                      mode=mode, shift_no=shift_no,
                                      downstream_demand=cons_ovr)
                st.session_state.pa_sections  = secs
                st.session_state.pa_raw_df    = raw_df
                st.session_state.pa_result    = pr
                st.session_state.pa_mode      = mode
                st.session_state.pa_shift     = shift_no
                st.session_state.pa_cap04     = cap04
                st.session_state.pa_cap06     = cap06
                st.session_state.pa_rt04      = rt04
                st.session_state.pa_rt06      = rt06
                st.session_state.pa_mtr04     = mtr04
                st.session_state.pa_mtr06     = mtr06
                st.session_state.pa_cons_ovr  = cons_ovr
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")
                import traceback; st.code(traceback.format_exc())

    if st.session_state.pa_sections is None:
        st.info("👆 Upload WIP file and click **Generate Plan & Run Analysis**.")
        st.stop()

    # ── Restore state ─────────────────────────────────────────────────────
    secs      = st.session_state.pa_sections
    raw_df    = st.session_state.pa_raw_df
    pr        = st.session_state.pa_result
    mode      = st.session_state.get("pa_mode", "H&T_FIRST")
    shift_no  = st.session_state.get("pa_shift", 1)
    cap04     = st.session_state.get("pa_cap04", 165.0)
    cap06     = st.session_state.get("pa_cap06", 185.0)
    rt04      = st.session_state.get("pa_rt04", "Bright")
    rt06      = st.session_state.get("pa_rt06", "Light Matt")
    mtr04     = st.session_state.get("pa_mtr04", 90.0)
    mtr06     = st.session_state.get("pa_mtr06", 150.0)
    cons_ovr  = st.session_state.get("pa_cons_ovr", {})
    rolled    = st.session_state.rolled_coils

    # ── Re-score when mode changes ────────────────────────────────────────
    remode = st.selectbox("🔄 Change mode without re-uploading",
        list(MODES.keys()), format_func=lambda k: MODES[k],
        index=list(MODES.keys()).index(mode) if mode in MODES else 0,
        key="pa_remode")
    if remode != mode:
        pr = compute_priority(secs, wip_df=raw_df,
                              mode=remode, shift_no=shift_no,
                              downstream_demand=cons_ovr)
        st.session_state.pa_result = pr
        st.session_state.pa_mode   = remode
        mode = remode

    for w in pr["warnings"]:
        st.warning(w)

    # ══════════════════════════════════════════════════════════════════════
    # A — COVERAGE BOARD
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("📊 A — Consumer Coverage")
    cov_items = sorted(pr["coverage"].items(),
                       key=lambda x: x[1].priority, reverse=True)
    cov_cols  = st.columns(len(cov_items))
    for col, (cname, cov) in zip(cov_cols, cov_items):
        col.metric(f"{SICON[cov.status]} {cname}",
                   f"{cov.coverage_today:.1f}d",
                   f"Buffer {cov.buffer_mt:.0f}MT + Plan {cov.incoming_today_mt:.0f}MT",
                   delta_color="off")
        if cov.required_today_mt > 0:
            col.caption(f"⚡ Roll {cov.required_today_mt:.0f}MT for 3-day cover")

    # 7-day chart
    with st.expander("📉 7-Day Depletion Forecast"):
        fc = forecast_depletion(raw_df, secs, cons_ovr)
        proj_rows = []
        for cname, r in fc.items():
            for p in r["projection"]:
                proj_rows.append({"Day": p["date"], "Consumer": cname,
                                   "Buffer MT": p["buffer_mt"]})
        if proj_rows:
            pivot = (pd.DataFrame(proj_rows)
                     .pivot(index="Day", columns="Consumer", values="Buffer MT"))
            st.line_chart(pivot)
            st.caption("Lines hitting zero = starvation on that date")

    st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # B — LIVE ROLLING SHEET (coil confirmation + re-planning)
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("🏭 B — Rolling Plan & Coil Confirmation")

    rolled_count = len(rolled)
    total_coils  = sum(len(s["coils_df"]) for s in secs)
    rolled_mt    = sum(
        float(r.get("Input Coil Weight", 0) or 0)
        for s in secs
        for _, r in s["coils_df"].iterrows()
        if str(r.get("Coil Number","")) in rolled)

    pcol1, pcol2, pcol3 = st.columns(3)
    pcol1.metric("Rolled this shift", f"{rolled_count} coils",
                 f"{rolled_mt:.1f} MT")
    pcol2.metric("Remaining", f"{total_coils - rolled_count} coils")
    pcol3.metric("Progress", f"{rolled_count/max(total_coils,1)*100:.0f}%")
    st.progress(rolled_count / max(total_coils, 1))

    if rolled_count > 0:
        if st.button("🔄 Re-prioritise remaining coils", key="replan_btn"):
            pr = compute_priority(secs, wip_df=raw_df,
                                  mode=mode, shift_no=shift_no,
                                  downstream_demand=cons_ovr)
            st.session_state.pa_result = pr
            st.rerun()
        if st.button("🗑️ Clear rolled coils", key="clear_rolled"):
            st.session_state.rolled_coils = set()
            st.rerun()

    sheets = build_rolling_sheet(secs, pr, rolled_coils=rolled)

    mill_tab04, mill_tab06 = st.tabs(["🏭 CRM-04", "🏭 CRM-06"])
    for mill_tab, mill, roll_type, mt_rem in [
            (mill_tab04, "CRM04", rt04, mtr04),
            (mill_tab06, "CRM06", rt06, mtr06)]:
        with mill_tab:
            # Roll status bar
            life_pct = int(mt_rem / ROLL_LIFE_MAX.get(roll_type, 200) * 100)
            st.progress(life_pct,
                text=f"Current roll: {roll_type}  ·  {mt_rem:.0f}MT remaining  ·  {life_pct}% life")

            for item in sheets[mill]:
                if item["type"] == "header":
                    pending = item["pending_count"]
                    done    = item["done_count"]
                    st.markdown(
                        f"**⬛ P{item['priority']}: {item['section'].replace('_',' ').title()}** "
                        f"— {item['consumer']}  ·  {pending} pending / {done} rolled  ·  {item['pending_mt']}MT left")
                else:
                    if item["rolled"]:
                        st.markdown(
                            f"~~{item['coil']}~~ ✅ rolled  "
                            f"W={item['width']:.0f} T={item['thick']:.2f} "
                            f"{item['weight']}MT",
                            help="Already rolled this shift")
                    else:
                        col_coil, col_btn = st.columns([4, 1])
                        age_flag = "🔴" if item["age"] > 21 else "🟡" if item["age"] > 14 else ""
                        col_coil.markdown(
                            f"**{item['seq']}. {item['coil']}** {age_flag}  "
                            f"W={item['width']:.0f}mm  T={item['thick']:.2f}→{item['rt']:.2f}mm  "
                            f"{item['weight']}MT  *{item['customer']}*  Age:{item['age']:.0f}d")
                        if col_btn.button("✅", key=f"roll_{mill}_{item['coil']}",
                                          help="Mark as rolled"):
                            st.session_state.rolled_coils.add(item["coil"])
                            st.rerun()

    st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # C — ROLL CAMPAIGN & CHANGE OPTIMISATION
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("🔩 C — Roll Campaign Plan")

    # Section → roll type mapping
    SEC_TO_ROLL = {
        "ROLLING": "Light Matt", "FIRST_ROLLING": "Light Matt",
        "RE_ROLLING": "Light Matt", "HT_FINISH": "Bright",
        "CRCA_FINISH": "Bright", "CRCA_FINISH_CRM06": "Bright",
        "TUBE_FH": "Bright", "SKIN_PASS_SUPER_BRIGHT": "Super Bright",
        "SKIN_PASS_CHROME": "Chrome Plated", "ROLLING_BRIGHT": "Chrome Plated",
        "SKIN_PASS_HEAVY_MATT": "Heavy Matt",
    }

    for mill, current_rt, mt_remaining, capacity in [
            ("CRM04", rt04, mtr04, cap04),
            ("CRM06", rt06, mtr06, cap06)]:
        st.markdown(f"#### 🏭 {mill}")
        mill_secs   = [s for s in secs if s["mill"] == mill]
        seq_key     = "crm04_sequence" if mill == "CRM04" else "crm06_sequence"
        priority_seq = pr.get(seq_key, [])
        rank_map     = {s.section_key: (s.rank_crm04 if mill == "CRM04"
                                         else s.rank_crm06)
                        for s in priority_seq}
        mill_secs.sort(key=lambda s: rank_map.get(s["section_key"], 99))

        # Build campaigns
        remaining_cap = capacity
        current_roll  = current_rt
        mt_on_roll    = mt_remaining
        campaigns     = []

        for s in mill_secs:
            sk         = s["section_key"]
            needed_rt  = SEC_TO_ROLL.get(sk, "Light Matt")
            sec_mt     = float(s["coils_df"]["Input Coil Weight"].sum())

            if needed_rt != current_roll:
                # Roll change needed
                change_cap = remaining_cap * (ROLL_CHANGE_MIN / 480)
                remaining_cap -= change_cap
                mt_on_roll    = ROLL_LIFE_MAX.get(needed_rt, 200)
                current_roll  = needed_rt
                campaigns.append({
                    "type": "change",
                    "from_roll": current_roll, "to_roll": needed_rt,
                    "cost_min": ROLL_CHANGE_MIN,
                })

            if remaining_cap <= 0:
                campaigns.append({"type": "deferred", "section": sk, "mt": sec_mt})
                continue

            rollable = min(sec_mt, remaining_cap, mt_on_roll)
            deferred = sec_mt - rollable
            remaining_cap -= rollable
            mt_on_roll    -= rollable
            campaigns.append({
                "type": "section", "section": sk, "roll": needed_rt,
                "rollable_mt": round(rollable, 1),
                "deferred_mt": round(deferred, 1),
                "consumer": CONFIG["section_to_consumer"].get(sk, ""),
            })

        # Render campaigns
        for c in campaigns:
            if c["type"] == "change":
                st.markdown(f"🔄 **Roll change → {c['to_roll']}** ({c['cost_min']} min)")
            elif c["type"] == "deferred":
                st.caption(f"⏭️ {c['section']} deferred ({c['mt']:.0f}MT) — beyond capacity")
            else:
                bar = int(c["rollable_mt"] / max(c["rollable_mt"] + c["deferred_mt"], 0.01) * 20)
                st.markdown(
                    f"  {'█'*bar}{'░'*(20-bar)} "
                    f"**{c['section'].replace('_',' ').title()}** "
                    f"[{c['roll']}]  {c['rollable_mt']}MT → {c['consumer']}"
                    + (f"  _(+{c['deferred_mt']:.0f}MT deferred)_" if c['deferred_mt'] > 0 else ""))
        st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # C2 — MILL PLAN (PRIORITY ORDER + ROLL OPTIMISATION)   SPEC §07.7
    # ══════════════════════════════════════════════════════════════════════
    from priority_advisor import (build_plan_comparison, count_roll_changes,
                                   SEC_TO_ROLL as PA_SEC_TO_ROLL,
                                   ROLL_CHANGE_MIN as PA_CHANGE_MIN)

    with st.expander("📋 C2 — Mill Plan (Priority Order + Roll Optimisation)",
                     expanded=True):
        comp = build_plan_comparison(secs, pr, rt04, rt06)
        prio = comp["priority"]
        alt  = comp["alternate"]

        # ── Comparison table (only when alternate exists) ─────────────────
        if alt:
            st.markdown("#### Plan Comparison")
            comp_df = pd.DataFrame([
                {"": "CRM-04 roll changes",
                 "⚡ Priority Plan": prio["changes04"],
                 "🔩 Alternate Plan": alt["changes04"]},
                {"": "CRM-06 roll changes",
                 "⚡ Priority Plan": prio["changes06"],
                 "🔩 Alternate Plan": alt["changes06"]},
                {"": "Total changes",
                 "⚡ Priority Plan": prio["total_changes"],
                 "🔩 Alternate Plan": alt["total_changes"]},
                {"": "Downtime (min)",
                 "⚡ Priority Plan": prio["downtime_min"],
                 "🔩 Alternate Plan": alt["downtime_min"]},
                {"": "Savings (min)",
                 "⚡ Priority Plan": "—",
                 "🔩 Alternate Plan": alt["savings_min"]},
            ])
            st.dataframe(comp_df, use_container_width=True, hide_index=True)

            for w in comp["warnings"]:
                st.warning(w)

            plan_choice = st.radio(
                "Which plan to run?",
                ["⚡ Priority Plan — follow consumer urgency exactly",
                 f"🔩 Alternate Plan — save {alt['savings_min']} min, slight reorder"],
                index=0, key="c2_plan_choice")
            use_alt = plan_choice.startswith("🔩")
        else:
            st.success(
                f"✅ Priority Plan already roll-optimal — "
                f"{prio['total_changes']} change(s), "
                f"{prio['downtime_min']} min downtime. No alternate needed.")
            use_alt = False

        chosen = alt if (use_alt and alt) else prio
        plan_name = "Alternate Plan" if (use_alt and alt) else "Priority Plan"

        # ── Full coil table per mill with roll-change marker rows ─────────
        for mill, current_roll in (("CRM04", rt04), ("CRM06", rt06)):
            ordered  = chosen[mill]
            n_chg    = chosen["changes04"] if mill == "CRM04" else chosen["changes06"]
            n_coils  = sum(len(s["coils_df"]) for s in ordered)
            tot_mt   = sum(float(s["coils_df"]["Input Coil Weight"].sum())
                           for s in ordered)
            st.markdown(
                f"**🏭 {mill} · {plan_name}** — {len(ordered)} sections · "
                f"{n_coils} coils · {tot_mt:.1f} MT · {n_chg} roll change(s) · "
                f"{n_chg * PA_CHANGE_MIN} min downtime")

            rows, roll, seq = [], current_roll, 0
            for s in ordered:
                needed = PA_SEC_TO_ROLL.get(s["section_key"], "Light Matt")
                if needed != roll:
                    rows.append({"Seq": "🔄", "Coil Number":
                        f"ROLL CHANGE · {roll} → {needed} · {PA_CHANGE_MIN} min",
                        "Section": "", "Roll Type": "", "Width": "",
                        "Thick": "", "RT": "", "MT": "", "Customer": "",
                        "Age": "", "Consumer": ""})
                    roll = needed
                sec_mt = float(s["coils_df"]["Input Coil Weight"].sum())
                rows.append({"Seq": "", "Coil Number":
                    f"═══ {s['section_key'].replace('_',' ').title()} ═══",
                    "Section": "", "Roll Type": needed, "Width": "",
                    "Thick": "", "RT": "", "MT": round(sec_mt, 1),
                    "Customer": "",
                    "Age": "", "Consumer":
                        CONFIG["section_to_consumer"].get(s["section_key"], "")})
                for _, r in s["coils_df"].iterrows():
                    seq += 1
                    rows.append({
                        "Seq": seq,
                        "Coil Number": str(r.get("Coil Number", "")),
                        "Section": s["section_key"],
                        "Roll Type": needed,
                        "Width": float(r.get("Actual Width", 0) or 0),
                        "Thick": float(r.get("Actual Thick", 0) or 0),
                        "RT": float(r.get("Plan Rolling Thick 1", 0) or 0),
                        "MT": round(float(r.get("Input Coil Weight", 0) or 0), 3),
                        "Customer": str(r.get("Customer Desc", ""))[:18],
                        "Age": float(r.get("Coil Age(# Days)", 0) or 0),
                        "Consumer": CONFIG["section_to_consumer"].get(
                            s["section_key"], ""),
                    })
            st.dataframe(pd.DataFrame(rows), use_container_width=True,
                         hide_index=True, height=320)

        # ── Downloads — Excel matches standard plan format (write_sheet) ──
        def _plan_xlsx(order04, order06):
            from openpyxl import Workbook
            from generator import write_sheet
            wb = Workbook(); wb.remove(wb.active)
            write_sheet(wb, date.today(), order04 + order06, load_db())
            buf = io.BytesIO(); wb.save(buf); buf.seek(0)
            return buf.getvalue()

        dcol1, dcol2, dcol3 = st.columns(3)
        dcol1.download_button(
            "⬇️ Priority Plan — Excel",
            data=_plan_xlsx(prio["CRM04"], prio["CRM06"]),
            file_name=f"mill_plan_priority_{date.today().strftime('%d-%m-%Y')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True, key="dl_c2_prio")
        if alt:
            dcol2.download_button(
                "⬇️ Alternate Plan — Excel",
                data=_plan_xlsx(alt["CRM04"], alt["CRM06"]),
                file_name=f"mill_plan_alternate_{date.today().strftime('%d-%m-%Y')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, key="dl_c2_alt")
        csv_rows = []
        for mill, current_roll in (("CRM04", rt04), ("CRM06", rt06)):
            roll = current_roll
            for s in chosen[mill]:
                needed = PA_SEC_TO_ROLL.get(s["section_key"], "Light Matt")
                if needed != roll:
                    csv_rows.append({"Mill": mill,
                        "Coil": f"ROLL CHANGE {roll}->{needed} {PA_CHANGE_MIN}min"})
                    roll = needed
                for _, r in s["coils_df"].iterrows():
                    csv_rows.append({
                        "Mill": mill, "Coil": str(r.get("Coil Number","")),
                        "Section": s["section_key"], "Roll": needed,
                        "Width": r.get("Actual Width",""),
                        "Thick": r.get("Actual Thick",""),
                        "RT": r.get("Plan Rolling Thick 1",""),
                        "MT": r.get("Input Coil Weight",""),
                        "Customer": str(r.get("Customer Desc",""))[:18]})
        dcol3.download_button(
            f"⬇️ {plan_name} — CSV",
            data=pd.DataFrame(csv_rows).to_csv(index=False),
            file_name="mill_plan_priority_synced.csv", mime="text/csv",
            use_container_width=True, key="dl_c2_csv")

    st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # D — CRS SEQUENCE OPTIMISER
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("🔧 D — CRS Setting Change Optimiser")
    urgency_aware = st.checkbox("Urgency-aware (run aged coils first even if slight extra cost)",
                                value=True, key="crs_urgency")
    crs = optimise_crs_sequence(secs, urgency_aware=urgency_aware,
                                rolled_coils=rolled)
    if "error" in crs:
        st.info(crs["error"])
    else:
        xc1, xc2, xc3 = st.columns(3)
        xc1.metric("CRS coils remaining", crs["total_coils"])
        xc2.metric("Changes (original)", crs["original_changes"])
        xc3.metric("Changes (optimised)", crs["optimised_changes"],
                   delta=f"-{crs['changes_saved']}",
                   delta_color="inverse" if crs["changes_saved"] > 0 else "off")
        for r in crs["recommendations"]:
            (st.success if r.startswith("✅") else st.warning)(r)

        with st.expander("CRS sequence detail"):
            rows = []
            for i, c in enumerate(crs["optimised_sequence"], 1):
                ev  = next((e for e in crs["change_events"] if e["position"] == i-1), None)
                rows.append({
                    "Pos": i, "Coil": c["coil_number"],
                    "Width": c["width"], "Thick": c["thick"],
                    "MT": round(c["weight"], 3), "Customer": c["customer"][:15],
                    "Age(d)": c["age"],
                    "⚠️ Change": " | ".join(ev["changes"]) if ev else "",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            st.download_button("⬇️ CRS sequence CSV",
                data=pd.DataFrame(rows).to_csv(index=False),
                file_name="crs_sequence.csv", mime="text/csv",
                use_container_width=True, key="dl_crs_v3")

    st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # E — DOWNLOAD ROLLING SHEET
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("📄 E — Download Rolling Sheets")
    dl1, dl2 = st.columns(2)
    for col, mill in [(dl1,"CRM04"),(dl2,"CRM06")]:
        rows = []
        for item in sheets[mill]:
            if item["type"] == "header":
                rows.append({"Seq":"","Coil":f"=== {item['section']} ===",
                             "Width":"","Thick":"","RT":"","MT":item["total_mt"],
                             "Customer":"","Remark":"","Rolled":""})
            else:
                rows.append({"Seq":item["seq"],"Coil":item["coil"],
                             "Width":item["width"],"Thick":item["thick"],
                             "RT":item["rt"],"MT":item["weight"],
                             "Customer":item["customer"],"Remark":item["remark"],
                             "Rolled":"YES" if item["rolled"] else ""})
        col.download_button(f"⬇️ {mill} Rolling Sheet",
            data=pd.DataFrame(rows).to_csv(index=False),
            file_name=f"rolling_sheet_{mill}.csv", mime="text/csv",
            use_container_width=True, key=f"dl_{mill}_v3")

