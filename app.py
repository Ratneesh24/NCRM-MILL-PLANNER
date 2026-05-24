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
         "🔩 Roll Life Tracker", "📐 Width Programme"],
        label_visibility="collapsed",
    )

    st.divider()

    # Storage status badge
    storage_mode = get_storage_mode()
    st.caption(f"Storage: {storage_mode}")

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
        "Upload the generated plan and the planner's corrected version. "
        "Every correction is extracted and stored as a routing rule."
    )

    col1, col2 = st.columns(2)
    with col1:
        gen_file = st.file_uploader("Generated plan (system output)", type=["xlsx"], key="gen")
    with col2:
        act_file = st.file_uploader("Corrected plan (planner version)", type=["xlsx"], key="act")

    learn_date = st.date_input("Plan date", value=date.today())

    if gen_file and act_file and st.button("🧠 Run Learning Session",
                                            type="primary", use_container_width=True):
        with st.spinner("Comparing plans and updating rules…"):
            try:
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tg:
                    tg.write(gen_file.read()); gen_path = tg.name
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as ta:
                    ta.write(act_file.read()); act_path = ta.name

                # ── Run the diff + pattern extraction ─────────────────
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

                # ── Save to Supabase (+ local backup) ─────────────────
                saved = save_db(current_db)

                st.success(
                    f"Learning complete! Accuracy: **{acc['overall_accuracy']*100:.1f}%**  "
                    + ("☁️ Saved to Supabase" if is_supabase_connected()
                       else "💾 Saved locally")
                )

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Section accuracy",   f"{acc['section_accuracy']*100:.1f}%")
                c2.metric("Ordering accuracy",  f"{acc['ordering_accuracy']*100:.1f}%")
                c3.metric("Inclusion accuracy", f"{acc['inclusion_accuracy']*100:.1f}%")
                c4.metric("Overall",            f"{acc['overall_accuracy']*100:.1f}%")

                import pandas as pd
                ct = session["corrections_by_type"]
                ct_df = pd.DataFrame([
                    {"Correction type": k.replace("_", " ").title(), "Count": v}
                    for k, v in ct.items() if v > 0
                ])
                if not ct_df.empty:
                    st.subheader("Corrections by Type")
                    st.dataframe(ct_df, use_container_width=True, hide_index=True)

                r1, r2, r3 = st.columns(3)
                r1.metric("New rules",        added)
                r2.metric("Reinforced",       reinforced)
                r3.metric("Conflicts",        conflicts)

                os.unlink(gen_path); os.unlink(act_path)

            except Exception as e:
                st.error(f"Learning error: {e}")
                import traceback; st.code(traceback.format_exc())

    elif not (gen_file and act_file):
        st.info("👆 Upload both files to start a learning session.")


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

    st.title("🔩 Roll Life Tracker")
    st.caption(
        "Enter the current roll status on each mill, upload today's WIP, "
        "and see exactly where each roll will exhaust — before it happens on the floor."
    )

    # ── Roll status input ─────────────────────────────────────────────
    st.subheader("Current Roll Status")
    st.info("Enter the roll type currently mounted and how many MT have already been rolled on it this campaign.")

    ROLL_TYPES_CRM04 = ['LIGHT_MATT', 'BRIGHT', 'SUPER_BRIGHT', 'CHROME_PLATED']
    ROLL_TYPES_CRM06 = ['LIGHT_MATT', 'BRIGHT', 'HEAVY_MATT']

    col04, col06 = st.columns(2)
    with col04:
        st.markdown("**CRM-04**")
        r04_type = st.selectbox("Roll type on CRM04", ROLL_TYPES_CRM04, key="r04t")
        r04_used = st.number_input("MT already rolled on this roll",
                                    min_value=0.0, max_value=500.0,
                                    value=0.0, step=5.0, key="r04u")
        r04_life = st.number_input("Roll life (MT) — leave 0 for default",
                                    min_value=0.0, max_value=1000.0,
                                    value=float(DEFAULT_ROLL_LIFE['CRM04'].get(r04_type, 180)),
                                    step=10.0, key="r04l")
        r04_num  = st.text_input("Roll number (optional)", key="r04n")

    with col06:
        st.markdown("**CRM-06**")
        r06_type = st.selectbox("Roll type on CRM06", ROLL_TYPES_CRM06, key="r06t")
        r06_used = st.number_input("MT already rolled on this roll",
                                    min_value=0.0, max_value=500.0,
                                    value=0.0, step=5.0, key="r06u")
        r06_life = st.number_input("Roll life (MT) — leave 0 for default",
                                    min_value=0.0, max_value=1000.0,
                                    value=float(DEFAULT_ROLL_LIFE['CRM06'].get(r06_type, 160)),
                                    step=10.0, key="r06l")
        r06_num  = st.text_input("Roll number (optional)", key="r06n")

    st.divider()
    wip_rl = st.file_uploader("Upload WIP file", type=["xlsx"], key="wip_rl")

    if wip_rl and st.button("🔍 Analyse Roll Life", type="primary",
                             use_container_width=True):
        with st.spinner("Simulating roll campaigns…"):
            try:
                import tempfile, os
                from generator import load_wip, filter_rolling_coils, \
                                       assign_all, build_sections

                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                    tmp.write(wip_rl.read()); wip_path = tmp.name

                df       = load_wip(wip_path)
                eligible = filter_rolling_coils(df)
                assigned = assign_all(eligible, load_db())
                sections = build_sections(assigned, load_db())
                os.unlink(wip_path)

                crm04_state = RollState(
                    mill='CRM04', roll_type=r04_type,
                    mt_used=r04_used,
                    mt_life=r04_life or DEFAULT_ROLL_LIFE['CRM04'].get(r04_type, 180),
                    roll_number=r04_num,
                )
                crm06_state = RollState(
                    mill='CRM06', roll_type=r06_type,
                    mt_used=r06_used,
                    mt_life=r06_life or DEFAULT_ROLL_LIFE['CRM06'].get(r06_type, 160),
                    roll_number=r06_num,
                )

                rl = analyse_roll_life(sections, crm04_state, crm06_state)
                s  = rl['summary']

                # ── Status banner ─────────────────────────────────────
                STATUS_FN = {
                    'CRITICAL': st.error,
                    'WARNING':  st.warning,
                    'OK':       st.success,
                }
                STATUS_FN.get(s['status'], st.info)(
                    f"Roll Status: **{s['status']}** — "
                    f"{s['critical_count']} critical, "
                    f"{s['warning_count']} warnings"
                )

                k1, k2, k3, k4 = st.columns(4)
                k1.metric("CRM04 roll changes", s['crm04_roll_changes'])
                k2.metric("CRM04 total MT",     f"{s['total_mt_crm04']:.1f}")
                k3.metric("CRM06 roll changes", s['crm06_roll_changes'])
                k4.metric("CRM06 total MT",     f"{s['total_mt_crm06']:.1f}")

                st.divider()

                # ── Campaign breakdown ────────────────────────────────
                for mill_label, campaigns in [
                    ("CRM-04", rl['crm04_campaigns']),
                    ("CRM-06", rl['crm06_campaigns']),
                ]:
                    st.subheader(f"{mill_label} — Roll Campaigns")
                    if not campaigns:
                        st.info("No campaigns found.")
                        continue

                    rows = []
                    for c in campaigns:
                        status_icon = {
                            'CRITICAL': '🔴', 'WARNING': '🟡',
                            'MONITOR': '🟠', 'OK': '🟢'
                        }.get(c['status'], '⚪')
                        rows.append({
                            "Status":       f"{status_icon} {c['status']}",
                            "Roll Type":    c['roll_type'],
                            "Sections":     ', '.join(
                                s.replace('_',' ').title() for s in c['sections']),
                            "MT Planned":   c['total_mt'],
                            "MT Used Start":c['start_mt_used'],
                            "MT Used End":  c['end_mt_used'],
                            "Roll Life":    c['mt_life'],
                            "% Consumed":   f"{c['pct_consumed']:.0f}%",
                            "Exhausts At":  c['exhausts_at'] or '—',
                        })
                    st.dataframe(pd.DataFrame(rows),
                                 use_container_width=True, hide_index=True)

                # ── Critical warnings with actions ────────────────────
                critical_warns = [w for w in rl['all_warnings']
                                  if w.get('severity') == 'CRITICAL']
                if critical_warns:
                    st.divider()
                    st.subheader("🔴 Critical Warnings & Actions")
                    for w in critical_warns:
                        with st.expander(f"⚠️ {w['message'][:80]}…", expanded=True):
                            st.error(w['message'])
                            if 'recommendation' in w:
                                st.info(f"**Recommended Action:** {w['recommendation']}")
                            if 'mt_overrun' in w and w['mt_overrun'] > 0:
                                st.warning(
                                    f"Roll overrun: **{w['mt_overrun']:.1f} MT** "
                                    f"beyond rated life — risk of surface damage "
                                    f"and strip quality defects."
                                )

                # ── Roll life reference table ─────────────────────────
                with st.expander("📖 Default Roll Life Reference"):
                    ref_rows = []
                    for mill, types in DEFAULT_ROLL_LIFE.items():
                        for rt, life in types.items():
                            ref_rows.append({
                                "Mill": mill, "Roll Type": rt,
                                "Default Life (MT)": life
                            })
                    st.dataframe(pd.DataFrame(ref_rows),
                                 use_container_width=True, hide_index=True)
                    st.caption("Update these values to match your actual roll specifications.")

            except Exception as e:
                st.error(f"Error: {e}")
                import traceback; st.code(traceback.format_exc())

    elif not wip_rl:
        st.info("👆 Enter roll status above, then upload the WIP file to analyse.")


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
