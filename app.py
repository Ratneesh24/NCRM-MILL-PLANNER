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
    import io as _io, pandas as pd
    from datetime import date as _date
    from priority_advisor import (
        MODES, CONFIG, ROLL_TYPES, get_roll_life,
        build_coverage, score_sections, build_roll_campaigns,
        build_alternate_order, optimise_crs, forecast_depletion,
    )

    st.title("🎯 Priority Advisor")
    st.caption("Plan your shift: select sections to run, get roll campaign "
               "sequence, check consumer coverage, download rolling sheet.")

    SICON = {"CRITICAL":"🔴","WARNING":"🟠","WATCH":"🟡","OK":"🟢"}

    # ── Session state ─────────────────────────────────────────────────────
    for k, v in [("pa_sections", None), ("pa_raw", None),
                 ("pa_result", None), ("pa_selected", {}),
                 ("pa_rolled", set())]:
        if k not in st.session_state:
            st.session_state[k] = v

    # ══════════════════════════════════════════════════════════════════════
    # STEP 1 — SETUP
    # ══════════════════════════════════════════════════════════════════════
    with st.expander("⚙️ Step 1 — Setup",
                     expanded=st.session_state.pa_sections is None):
        wip_file = st.file_uploader("Today's WIP file", type=["xlsx"],
                                    key="pa_wip")

        sc1, sc2, sc3 = st.columns(3)
        mode     = sc1.selectbox("Planning mode", list(MODES.keys()),
                                 format_func=lambda k: MODES[k])
        shift_no = sc2.selectbox("Shift", [1,2,3],
                                 format_func=lambda x: f"Shift {x}")
        st.markdown("**Current rolls on mills**")
        rc1, rc2 = st.columns(2)
        with rc1:
            st.caption("CRM-04")
            rt04  = st.selectbox("Roll type", ROLL_TYPES, key="rt04")
            mu04  = st.number_input("MT already rolled on this roll",
                                    0.0, 500.0, 0.0, 5.0, key="mu04")
        with rc2:
            st.caption("CRM-06")
            idx06 = ROLL_TYPES.index("Light Matt")
            rt06  = st.selectbox("Roll type", ROLL_TYPES, key="rt06",
                                 index=idx06)
            mu06  = st.number_input("MT already rolled on this roll",
                                    0.0, 500.0, 0.0, 5.0, key="mu06")

        # Show roll life remaining
        _cr1, _cr2 = st.columns(2)
        _life04 = get_roll_life(rt04, "HT_FINISH" if rt04=="Bright"
                                else "RE_ROLLING")
        _life06 = get_roll_life(rt06, "FIRST_ROLLING")
        _rem04  = max(0, _life04 - mu04)
        _rem06  = max(0, _life06 - mu06)
        _cr1.progress(int(_rem04/_life04*100) if _life04 else 0,
                      text=f"CRM-04 {rt04}: {_rem04:.0f}MT remaining ({_life04}MT life)")
        _cr2.progress(int(_rem06/_life06*100) if _life06 else 0,
                      text=f"CRM-06 {rt06}: {_rem06:.0f}MT remaining ({_life06}MT life)")

        st.markdown("**Consumption overrides (MT/day)** — leave as-is if no change")
        _cc1, _cc2, _cc3 = st.columns(3)
        cons_ovr = {
            "H&T Line":   _cc1.number_input("H&T Line", 0.0, 200.0,
                float(CONFIG["consumers"]["H&T Line"]["daily_mt"]), 5.0),
            "Tube Plant": _cc2.number_input("Tube Plant", 0.0, 500.0,
                float(CONFIG["consumers"]["Tube Plant"]["daily_mt"]), 5.0),
            "OEM":        _cc3.number_input("OEM", 0.0, 300.0,
                float(CONFIG["consumers"]["OEM"]["daily_mt"]), 5.0),
        }

        run_btn = st.button("🚀 Load WIP & Score Sections",
                            type="primary", use_container_width=True,
                            disabled=wip_file is None)

    if run_btn and wip_file:
        with st.spinner("Loading WIP and scoring sections…"):
            try:
                import tempfile, os as _os
                from generator import load_wip, filter_rolling_coils,                                        assign_all, build_sections
                wip_file.seek(0)
                with tempfile.NamedTemporaryFile(suffix=".xlsx",
                                                  delete=False) as t:
                    t.write(wip_file.read()); fp = t.name
                raw_df = pd.read_excel(fp)
                wip_df = load_wip(fp)
                secs   = build_sections(
                    assign_all(filter_rolling_coils(wip_df), load_db()),
                    load_db())
                _os.unlink(fp)
                coverage = build_coverage(raw_df, cons_ovr)
                scored   = score_sections(secs, coverage, mode,
                                          {"CRM04": rt04, "CRM06": rt06})
                st.session_state.pa_sections  = secs
                st.session_state.pa_raw       = raw_df
                st.session_state.pa_result    = scored
                st.session_state.pa_coverage  = coverage
                st.session_state.pa_mode      = mode
                st.session_state.pa_shift     = shift_no
                st.session_state.pa_rt04      = rt04
                st.session_state.pa_rt06      = rt06
                st.session_state.pa_mu04      = mu04
                st.session_state.pa_mu06      = mu06
                st.session_state.pa_cons_ovr  = cons_ovr
                st.session_state.pa_selected  = {
                    s.section_key: True for s in scored}
                st.session_state.pa_rolled    = set()
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")
                import traceback; st.code(traceback.format_exc())

    if st.session_state.pa_sections is None:
        st.info("👆 Upload WIP file and click **Load WIP & Score Sections**.")
        st.stop()

    # Restore state
    scored    = st.session_state.pa_result
    coverage  = st.session_state.pa_coverage
    secs      = st.session_state.pa_sections
    raw_df    = st.session_state.pa_raw
    mode      = st.session_state.get("pa_mode", "H&T_PRIORITY")
    rt04      = st.session_state.get("pa_rt04", "Bright")
    rt06      = st.session_state.get("pa_rt06", "Light Matt")
    mu04      = st.session_state.get("pa_mu04", 0.0)
    mu06      = st.session_state.get("pa_mu06", 0.0)
    cons_ovr  = st.session_state.get("pa_cons_ovr", {})
    rolled    = st.session_state.pa_rolled

    # Live mode change without re-upload
    new_mode = st.selectbox("🔄 Change mode (live re-score)",
        list(MODES.keys()), format_func=lambda k: MODES[k],
        index=list(MODES.keys()).index(mode),
        key="pa_live_mode")
    if new_mode != mode:
        cov2   = build_coverage(raw_df, cons_ovr)
        scored = score_sections(secs, cov2, new_mode,
                                {"CRM04": rt04, "CRM06": rt06})
        st.session_state.pa_result   = scored
        st.session_state.pa_mode     = new_mode
        st.session_state.pa_coverage = cov2
        coverage = cov2
        mode     = new_mode

    # ══════════════════════════════════════════════════════════════════════
    # A — CONSUMER COVERAGE
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("📊 A — Consumer Coverage")
    cov_cols = st.columns(3)
    for col, (cname, cov) in zip(
            cov_cols,
            sorted(coverage.items(), key=lambda x: -x[1].daily_mt)):
        col.metric(f"{SICON[cov.status]} {cname}",
                   f"{cov.coverage_days:.1f}d cover",
                   f"Buffer {cov.buffer_mt:.0f}MT | Ask {cov.daily_mt:.0f}MT/day",
                   delta_color="off")
        if cov.shortfall_mt > 0:
            col.caption(f"⚡ Need {cov.shortfall_mt:.0f}MT to reach target")
    for cname, cov in coverage.items():
        if cov.status == "CRITICAL":
            st.error(f"🔴 {cname} CRITICAL — only {cov.coverage_days:.1f}d cover")
        elif cov.status == "WARNING":
            st.warning(f"🟠 {cname} WARNING — {cov.coverage_days:.1f}d cover")

    st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # B — SELECT SECTIONS FOR TODAY
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("☑️ B — Select Sections to Run Today")
    st.caption("Tick sections you plan to run. System will sequence them "
               "per mill with minimum roll changes.")

    selected_keys = set()
    for mill in ("CRM04", "CRM06"):
        mill_scored = sorted([s for s in scored if s.mill == mill],
                             key=lambda x: x.priority_rank)
        if not mill_scored:
            continue
        st.markdown(f"**🏭 {mill}**")
        cap_type_map = {"first_rolling": "first_rolling",
                        "re_rolling": "re_rolling", "finishing": "finishing",
                        "rolling": "re_rolling"}
        for s in mill_scored:
            cap_type = ("first_rolling" if s.section_key == "FIRST_ROLLING"
                        else "re_rolling" if s.section_key in
                             ("RE_ROLLING","ROLLING")
                        else "finishing")
            shift_cap = CONFIG["shift_capacity"][mill].get(cap_type, 80)
            age_flag  = (f" 🔴 oldest={s.coils_df['Coil Age(# Days)'].max():.0f}d"
                         if hasattr(s.coils_df,'iterrows') and
                            float(s.coils_df["Coil Age(# Days)"].max()) > 14
                         else "")
            checked = st.session_state.pa_selected.get(s.section_key, True)
            col_cb, col_info = st.columns([1, 8])
            new_val = col_cb.checkbox("", value=checked,
                                      key=f"sel_{mill}_{s.section_key}")
            st.session_state.pa_selected[s.section_key] = new_val
            col_info.markdown(
                f"**P{s.priority_rank}. {s.section_key.replace('_',' ').title()}**"
                f" [{s.roll_type}] · {s.n_coils}c · {s.total_mt:.1f}MT"
                f" · {SICON[coverage.get(s.consumer, type('x',(),{'status':'OK'})()).status] if hasattr(coverage.get(s.consumer, None),'status') else '⚪'} {s.consumer}"
                f"{age_flag}  `shift cap {shift_cap}MT`")
            if new_val:
                selected_keys.add(s.section_key)

    selected_sections = [s for s in scored
                         if s.section_key in selected_keys]
    if not selected_sections:
        st.warning("No sections selected. Tick at least one above.")
        st.stop()

    st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # C — ROLL CAMPAIGN PLAN
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("🔩 C — Roll Campaign Plan")

    def _shift_cap(mill: str, sections: List) -> float:
        """Estimate shift capacity based on mix of selected sections."""
        types = {"first_rolling":0,"re_rolling":0,"finishing":0}
        for s in sections:
            if s.mill != mill: continue
            if s.section_key == "FIRST_ROLLING":
                types["first_rolling"] += s.total_mt
            elif s.section_key in ("RE_ROLLING","ROLLING"):
                types["re_rolling"] += s.total_mt
            else:
                types["finishing"] += s.total_mt
        tot = sum(types.values()) or 1
        cap = CONFIG["shift_capacity"][mill]
        return (types["first_rolling"]/tot * cap["first_rolling"] +
                types["re_rolling"]  /tot * cap["re_rolling"] +
                types["finishing"]   /tot * cap["finishing"])

    plan_compare_rows = []
    camp_data = {}

    for mill, cur_roll, mt_used in [
            ("CRM04", rt04, mu04), ("CRM06", rt06, mu06)]:
        mill_secs_prio = sorted(
            [s for s in selected_sections if s.mill == mill],
            key=lambda x: x.priority_rank)
        mill_secs_alt  = build_alternate_order(
            selected_sections, cur_roll, mill)
        sh_cap = _shift_cap(mill, selected_sections)

        camps_prio, defer_prio, nc_prio, dt_prio = build_roll_campaigns(
            mill_secs_prio, cur_roll, mt_used, mill, sh_cap)
        camps_alt, defer_alt, nc_alt, dt_alt = build_roll_campaigns(
            mill_secs_alt, cur_roll, mt_used, mill, sh_cap)

        camp_data[mill] = {
            "prio": camps_prio, "alt": camps_alt,
            "defer_prio": defer_prio, "defer_alt": defer_alt,
            "nc_prio": nc_prio, "nc_alt": nc_alt,
            "dt_prio": dt_prio, "dt_alt": dt_alt,
            "sh_cap": sh_cap, "cur_roll": cur_roll, "mt_used": mt_used,
        }
        plan_compare_rows.append({
            "Mill": mill,
            "Plan": "Priority",
            "Roll changes": nc_prio,
            "Downtime (min)": dt_prio,
            "MT planned": round(sum(c.total_mt for c in camps_prio), 1),
        })
        plan_compare_rows.append({
            "Mill": mill,
            "Plan": "Alternate (min changes)",
            "Roll changes": nc_alt,
            "Downtime (min)": dt_alt,
            "MT planned": round(sum(c.total_mt for c in camps_alt), 1),
        })

    # Comparison table
    st.markdown("#### Plan Comparison — Priority vs Alternate")
    st.dataframe(pd.DataFrame(plan_compare_rows),
                 use_container_width=True, hide_index=True)

    # Per-mill plan choice
    plan_choice = {}
    for mill in ("CRM04", "CRM06"):
        d = camp_data[mill]
        savings = d["dt_prio"] - d["dt_alt"]
        if savings > 0:
            choices = [
                f"⚡ Priority Plan — {d['nc_prio']} change(s), {d['dt_prio']} min downtime",
                f"🔩 Alternate Plan — {d['nc_alt']} change(s), {d['dt_alt']} min downtime, saves {savings} min",
            ]
            choice = st.radio(f"**{mill} — which sequence?**", choices,
                              key=f"choice_{mill}", index=0)
            plan_choice[mill] = "alt" if "Alternate" in choice else "prio"
        else:
            st.success(f"✅ {mill}: Priority order already minimises roll changes "
                       f"({d['nc_prio']} change(s))")
            plan_choice[mill] = "prio"

    # Show chosen campaigns per mill
    for mill in ("CRM04", "CRM06"):
        d       = camp_data[mill]
        use_key = plan_choice.get(mill, "prio")
        camps   = d[use_key]
        defer   = d[f"defer_{use_key}"]
        st.markdown(f"**🏭 {mill} — {'Priority' if use_key=='prio' else 'Alternate'} Plan**")
        for i, camp in enumerate(camps, 1):
            if camp.preceded_by_change:
                st.markdown(
                    f"🔄 **ROLL CHANGE: {camp.change_from} → {camp.roll_type}** "
                    f"({CONFIG['roll_change_min']} min)")
            life_used_pct = int(camp.mt_used_end / camp.roll_life * 100
                                ) if camp.roll_life else 0
            warn = " ⚠️ Exceeds roll life!" if camp.exceeds_life else ""
            with st.expander(
                    f"Campaign {i}: {camp.roll_type} — "
                    f"{camp.n_coils} coils · {camp.total_mt:.1f}MT{warn}",
                    expanded=(i == 1)):
                st.progress(min(life_used_pct, 100),
                            text=f"Roll life: {camp.mt_used_start:.0f}MT start → "
                                 f"{camp.mt_used_end:.0f}MT end / {camp.roll_life}MT max")
                camp_rows = [{"Seq": j+1, "Coil": c["coil"],
                              "Width": c["width"], "Thick": c["thick"],
                              "RT": c["rt"], "MT": c["mt"],
                              "Customer": c["customer"], "Age(d)": c["age"]}
                             for j, c in enumerate(camp.coils)]
                st.dataframe(pd.DataFrame(camp_rows),
                             use_container_width=True, hide_index=True)
        if defer:
            st.info("⏭️ Deferred (beyond shift capacity): " + " · ".join(defer))

    st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # D — CRS SEQUENCE OPTIMISER
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("🔧 D — CRS Sequence Optimiser")
    crs = optimise_crs(selected_sections, rolled_coils=rolled)
    if "error" in crs:
        st.info(crs["error"])
    else:
        xc1, xc2, xc3 = st.columns(3)
        xc1.metric("CRS coils",         crs["total_coils"])
        xc2.metric("Changes (original)", crs["original_changes"])
        xc3.metric("Changes (optimised)",crs["optimised_changes"],
                   delta=f"-{crs['saved']}" if crs["saved"] else "0",
                   delta_color="inverse" if crs["saved"] > 0 else "off")
        for r in crs["recommendations"]:
            (st.success if r.startswith("✅") else st.warning)(r)
        with st.expander("CRS sequence detail"):
            crs_rows = []
            for i, c in enumerate(crs["optimised"], 1):
                ev = next((e for e in crs["change_events"]
                           if e["position"] == i-1), None)
                crs_rows.append({"Pos": i, "Coil": c["coil_number"],
                    "Width": c["width"], "Thick": c["thick"],
                    "MT": round(c["weight"],3), "Customer": c["customer"][:15],
                    "Age(d)": c["age"],
                    "⚠️ Change": " | ".join(ev["changes"]) if ev else ""})
            st.dataframe(pd.DataFrame(crs_rows),
                         use_container_width=True, hide_index=True)
            st.download_button("⬇️ CRS sequence CSV",
                data=pd.DataFrame(crs_rows).to_csv(index=False),
                file_name="crs_sequence.csv", mime="text/csv",
                use_container_width=True, key="dl_crs")

    st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # E — COIL TICK-OFF
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("✅ E — Coil Confirmation (mark as rolled)")
    tot_coils  = sum(s.n_coils for s in selected_sections)
    done_coils = len(rolled)
    st.progress(done_coils / max(tot_coils, 1),
                text=f"Rolled: {done_coils}/{tot_coils} coils")
    if rolled:
        if st.button("🗑️ Clear rolled coils", key="clr"):
            st.session_state.pa_rolled = set()
            st.rerun()

    for mill in ("CRM04","CRM06"):
        st.markdown(f"**🏭 {mill}**")
        for s in sorted([s for s in selected_sections if s.mill==mill],
                        key=lambda x: x.priority_rank):
            st.caption(f"{s.section_key.replace('_',' ').title()} "
                       f"[{s.roll_type}] → {s.consumer}")
            for _, row in s.coils_df.iterrows():
                cn = str(row.get("Coil Number",""))
                if cn in rolled:
                    st.markdown(f"~~{cn}~~ ✅  "
                                f"W={row.get('Actual Width',0):.0f} "
                                f"T={row.get('Actual Thick',0):.2f} "
                                f"{row.get('Input Coil Weight',0):.3f}MT")
                else:
                    age = float(row.get("Coil Age(# Days)",0) or 0)
                    af  = "🔴" if age>21 else "🟡" if age>14 else ""
                    c1, c2 = st.columns([5,1])
                    c1.markdown(
                        f"{af} **{cn}** "
                        f"W={row.get('Actual Width',0):.0f}mm "
                        f"T={row.get('Actual Thick',0):.2f}→"
                        f"{row.get('Plan Rolling Thick 1',0):.2f}mm "
                        f"{row.get('Input Coil Weight',0):.3f}MT "
                        f"*{str(row.get('Customer Desc',''))[:15]}* "
                        f"Age:{age:.0f}d")
                    if c2.button("✅", key=f"roll_{mill}_{cn}"):
                        st.session_state.pa_rolled.add(cn)
                        st.rerun()

    st.divider()

    # ══════════════════════════════════════════════════════════════════════
    # F — ROLLING SHEET DOWNLOAD (standard Tata Steel format)
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("📄 F — Download Rolling Sheet")
    st.caption("Excel with CRM-04 and CRM-06 on separate sheets, "
               "matching standard plan format.")

    def _build_excel(plan_choice_map):
        from openpyxl import Workbook
        from generator import write_sheet
        # Build ordered section list per the chosen plan
        ordered_all = []
        for mill in ("CRM04","CRM06"):
            d       = camp_data[mill]
            use_key = plan_choice_map.get(mill, "prio")
            camps   = d[use_key]
            # Flatten campaign coils back to section order
            sk_seen = []
            for camp in camps:
                for sk in camp.sections:
                    if sk not in sk_seen:
                        sk_seen.append(sk)
            mill_sec_map = {s.section_key: s for s in selected_sections
                            if s.mill == mill}
            for sk in sk_seen:
                if sk in mill_sec_map:
                    # Reconstruct section dict for write_sheet
                    s = mill_sec_map[sk]
                    ordered_all.append({
                        "section_key": sk,
                        "mill": mill,
                        "label": s.label,
                        "coils_df": s.coils_df,
                    })
        wb = Workbook(); wb.remove(wb.active)
        n, ws = write_sheet(wb, _date.today(), ordered_all, load_db())
        buf = _io.BytesIO(); wb.save(buf); buf.seek(0)
        return buf.getvalue()

    dc1, dc2 = st.columns(2)
    dc1.download_button(
        "⬇️ Download Rolling Sheet (chosen plan)",
        data=_build_excel(plan_choice),
        file_name=f"rolling_sheet_{_date.today().strftime('%d-%m-%Y')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True, key="dl_sheet")

    # Forecast
    with st.expander("📉 7-Day Consumer Buffer Forecast"):
        fc = forecast_depletion(raw_df, selected_sections, cons_ovr)
        proj = []
        for cname, r in fc.items():
            for p in r["projection"]:
                proj.append({"Date": p["date"], "Consumer": cname,
                             "Buffer MT": p["buffer_mt"]})
        if proj:
            pivot = pd.DataFrame(proj).pivot(index="Date",
                columns="Consumer", values="Buffer MT")
            st.line_chart(pivot)
            st.caption("Lines hitting zero = starvation on that date")
