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
from pathlib import Path

import streamlit as st

# ── path fix so sibling modules resolve correctly ──────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from generator import generate_daily_plan
from learner   import load_db, save_db, backup_db, learn
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
# Persistent DB path  (Streamlit Cloud: /tmp is writable)
# ──────────────────────────────────────────────────────────────────────────
DB_PATH = "/tmp/learning_db.json"


def _load_db_cached():
    return load_db(DB_PATH)


# ──────────────────────────────────────────────────────────────────────────
# Sidebar — branding + navigation
# ──────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🏭 Mill Planner")
    st.markdown("**Tata Steel CRM Sahibabad**  \nNarrow Complex — Rolling")
    st.divider()

    page = st.radio(
        "Navigate",
        ["📋 Generate Plan", "🧠 Learn from Corrections", "📊 Stats & Rules"],
        label_visibility="collapsed",
    )

    st.divider()
    db = _load_db_cached()
    n_sessions = db.get("total_sessions", 0)
    cum_acc    = db.get("cumulative_accuracy", 0.0)
    n_rules    = len(db.get("grade_routing", {}))

    st.metric("Sessions learned", n_sessions)
    st.metric("Cumulative accuracy", f"{cum_acc*100:.1f}%" if n_sessions else "—")
    st.metric("Learned rules", n_rules)


# ══════════════════════════════════════════════════════════════════════════
# PAGE 1 — GENERATE
# ══════════════════════════════════════════════════════════════════════════
if page == "📋 Generate Plan":
    st.title("📋 Generate Mill Plan")
    st.caption("Upload the daily WIP coil staging export → get a formatted rolling plan.")

    col1, col2 = st.columns([2, 1])

    with col1:
        wip_file = st.file_uploader(
            "Upload WIP file  (`Narrow_Data_Coil_Stage.xlsx`)",
            type=["xlsx"],
            key="wip_upload",
        )

    with col2:
        plan_date = st.date_input(
            "Planning date",
            value=date.today(),
            min_value=date(2024, 1, 1),
            max_value=date(2030, 12, 31),
        )
        days = st.number_input(
            "Days to generate",
            min_value=1, max_value=7, value=1, step=1,
        )
        use_db = st.checkbox("Apply learned rules", value=True,
                             help="Use corrections from previous sessions to improve routing")

    if wip_file and st.button("🚀 Generate Plan", type="primary", use_container_width=True):
        with st.spinner("Analysing WIP data and building plan…"):
            try:
                # Write uploaded file to a temp location
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp_wip:
                    tmp_wip.write(wip_file.read())
                    wip_path = tmp_wip.name

                out_path = f"/tmp/mill_plan_{plan_date.strftime('%d-%m-%Y')}.xlsx"
                learning_db = _load_db_cached() if use_db else None

                # Capture console output
                import io as _io, contextlib
                buf = _io.StringIO()
                with contextlib.redirect_stdout(buf):
                    result = generate_daily_plan(
                        wip_file    = wip_path,
                        plan_date   = plan_date.strftime("%Y-%m-%d"),
                        output_file = out_path,
                        days        = int(days),
                        learning_db = learning_db,
                        verbose     = True,
                    )
                console_out = buf.getvalue()

                # ── Metrics ──────────────────────────────────────────
                sections = result["sections"]
                total_coils  = sum(len(s["coils_df"]) for s in sections)
                total_weight = sum(s["coils_df"]["Input Coil Weight"].sum()
                                   for s in sections)

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Total coils",   total_coils)
                m2.metric("Total weight",  f"{total_weight:.1f} MT")
                m3.metric("Sections",      len(sections))
                m4.metric("Excluded",      result["excluded_count"])

                st.success("Plan generated successfully!")

                # ── Section breakdown table ───────────────────────────
                st.subheader("Section Breakdown")
                rows = []
                for s in sections:
                    rows.append({
                        "Section":      SECTION_SHORT_NAME.get(s["section_key"], s["section_key"]),
                        "Mill":         s["mill"],
                        "Coils":        len(s["coils_df"]),
                        "Weight (MT)":  round(s["coils_df"]["Input Coil Weight"].sum(), 2),
                    })
                import pandas as pd
                st.dataframe(
                    pd.DataFrame(rows),
                    use_container_width=True,
                    hide_index=True,
                )

                # ── Download button ───────────────────────────────────
                with open(out_path, "rb") as f:
                    xlsx_bytes = f.read()

                st.download_button(
                    label    = f"⬇️  Download  mill_plan_{plan_date.strftime('%d-%m-%Y')}.xlsx",
                    data     = xlsx_bytes,
                    file_name= f"mill_plan_{plan_date.strftime('%d-%m-%Y')}.xlsx",
                    mime     = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )

                # ── Console log (collapsible) ─────────────────────────
                with st.expander("Console log"):
                    st.code(console_out, language="text")

                os.unlink(wip_path)

            except Exception as e:
                st.error(f"Error during generation: {e}")
                import traceback
                st.code(traceback.format_exc())

    elif not wip_file:
        st.info("👆 Upload a WIP file to get started.")


# ══════════════════════════════════════════════════════════════════════════
# PAGE 2 — LEARN
# ══════════════════════════════════════════════════════════════════════════
elif page == "🧠 Learn from Corrections":
    st.title("🧠 Learn from Planner Corrections")
    st.caption(
        "Upload the plan the system generated alongside the planner's corrected version. "
        "The diff engine will extract every correction and update the routing rules."
    )

    col1, col2 = st.columns(2)
    with col1:
        gen_file = st.file_uploader(
            "Generated plan  (what the system produced)",
            type=["xlsx"], key="gen_upload",
        )
    with col2:
        act_file = st.file_uploader(
            "Corrected plan  (what the planner actually used)",
            type=["xlsx"], key="act_upload",
        )

    learn_date = st.date_input(
        "Plan date (used to select the correct sheet tab)",
        value=date.today(),
    )

    if gen_file and act_file and st.button("🧠 Run Learning Session",
                                            type="primary",
                                            use_container_width=True):
        with st.spinner("Comparing plans and updating rules…"):
            try:
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tg:
                    tg.write(gen_file.read()); gen_path = tg.name
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as ta:
                    ta.write(act_file.read()); act_path = ta.name

                import io as _io, contextlib
                buf = _io.StringIO()
                with contextlib.redirect_stdout(buf):
                    session, corrections = learn(
                        generated_path = gen_path,
                        actual_path    = act_path,
                        db_path        = DB_PATH,
                        plan_date      = learn_date,
                        verbose        = True,
                    )
                console_out = buf.getvalue()

                acc = session["overall_accuracy"]
                st.success(f"Learning session complete! Overall accuracy: **{acc*100:.1f}%**")

                # ── Accuracy metrics ──────────────────────────────────
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Section accuracy",   f"{session['section_accuracy']*100:.1f}%")
                c2.metric("Ordering accuracy",  f"{session['sort_accuracy']*100:.1f}%")
                c3.metric("Inclusion accuracy", f"{session['inclusion_accuracy']*100:.1f}%")
                c4.metric("Overall accuracy",   f"{acc*100:.1f}%")

                # ── Corrections breakdown ─────────────────────────────
                st.subheader("Corrections by Type")
                ct = session["corrections_by_type"]
                import pandas as pd
                ct_df = pd.DataFrame(
                    [{"Correction type": k.replace("_"," ").title(),
                      "Count": v}
                     for k, v in ct.items() if v > 0]
                )
                if not ct_df.empty:
                    st.dataframe(ct_df, use_container_width=True, hide_index=True)
                else:
                    st.info("No corrections found — plans were identical.")

                # ── Rule updates ──────────────────────────────────────
                r1, r2, r3 = st.columns(3)
                r1.metric("New rules added",   session["new_rules_added"])
                r2.metric("Rules reinforced",  session["rules_reinforced"])
                r3.metric("Conflicts flagged", session["conflicts_flagged"])

                with st.expander("Session log"):
                    st.code(console_out, language="text")

                os.unlink(gen_path); os.unlink(act_path)

            except Exception as e:
                st.error(f"Learning error: {e}")
                import traceback
                st.code(traceback.format_exc())

    elif not (gen_file and act_file):
        st.info("👆 Upload both the generated plan and the corrected plan to start learning.")


# ══════════════════════════════════════════════════════════════════════════
# PAGE 3 — STATS
# ══════════════════════════════════════════════════════════════════════════
elif page == "📊 Stats & Rules":
    st.title("📊 Learning Database — Stats & Rules")

    db = _load_db_cached()

    # ── Top-level metrics ─────────────────────────────────────────────
    n_sessions = db.get("total_sessions", 0)
    cum_acc    = db.get("cumulative_accuracy", 0.0)
    grade_rules = db.get("grade_routing", {})
    conflicts   = [c for c in db.get("conflict_log", []) if not c.get("resolved")]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sessions",        n_sessions)
    c2.metric("Cumulative acc.", f"{cum_acc*100:.1f}%" if n_sessions else "—")
    c3.metric("Grade rules",     len(grade_rules))
    c4.metric("Conflicts",       len(conflicts), delta_color="inverse")

    # ── Accuracy trend chart ──────────────────────────────────────────
    sessions = db.get("session_log", [])
    if sessions:
        import pandas as pd
        trend_df = pd.DataFrame([
            {
                "Date":     s.get("session_date", ""),
                "Overall":  round(s.get("overall_accuracy", 0) * 100, 1),
                "Section":  round(s.get("section_accuracy",  0) * 100, 1),
                "Ordering": round(s.get("sort_accuracy",     0) * 100, 1),
            }
            for s in sessions
        ])
        st.subheader("Accuracy Trend")
        st.line_chart(trend_df.set_index("Date")[["Overall", "Section", "Ordering"]])

    st.divider()

    # ── Grade routing rules table ─────────────────────────────────────
    st.subheader("Grade Routing Rules")
    if grade_rules:
        import pandas as pd
        rules_df = pd.DataFrame([
            {
                "Key (Quality|TDC|ProdCode|NextStage)": k,
                "Section":    v.get("section", ""),
                "Mill":       v.get("mill", ""),
                "Confidence": v.get("confidence", 0),
                "Source":     v.get("source", ""),
                "Last seen":  v.get("last_seen", ""),
            }
            for k, v in grade_rules.items()
        ]).sort_values("Confidence", ascending=False)

        # Colour code by confidence
        conf_filter = st.select_slider(
            "Min confidence to show",
            options=[1, 2, 3, 5, 10],
            value=1,
        )
        filtered = rules_df[rules_df["Confidence"] >= conf_filter]
        st.dataframe(filtered, use_container_width=True, hide_index=True,
                     height=min(400, 40 + len(filtered) * 36))
    else:
        st.info("No learned rules yet. Run a learning session first.")

    st.divider()

    # ── Conflicts ─────────────────────────────────────────────────────
    if conflicts:
        st.subheader(f"⚠️ Unresolved Conflicts  ({len(conflicts)})")
        st.caption("Resolve via `mill_planner.py rule-add` or the form below.")
        import pandas as pd
        conf_df = pd.DataFrame([
            {
                "Key":             c.get("key", ""),
                "Existing rule":   c.get("existing_rule", ""),
                "New evidence":    c.get("new_evidence", ""),
                "Confidence":      c.get("existing_confidence", 0),
                "Date":            c.get("date", ""),
            }
            for c in conflicts
        ])
        st.dataframe(conf_df, use_container_width=True, hide_index=True)

        st.subheader("Resolve a Conflict")
        with st.form("resolve_form"):
            key_in  = st.text_input("Routing key (Quality|TDC|ProdCode|NextStage)")
            sec_in  = st.selectbox("Correct section", options=list(SECTION_SHORT_NAME.keys()))
            mill_in = st.selectbox("Correct mill",    options=["CRM04", "CRM06", "CRM04/06"])
            conf_in = st.slider("Set confidence", 1, 10, 5)
            submitted = st.form_submit_button("✅ Save Rule", type="primary")
            if submitted and key_in:
                db["grade_routing"][key_in] = {
                    "section":      sec_in,
                    "mill":         mill_in,
                    "confidence":   conf_in,
                    "observations": conf_in,
                    "overrides":    0,
                    "last_seen":    date.today().isoformat(),
                    "source":       "manual_ui",
                }
                for c in db.get("conflict_log", []):
                    if c.get("key") == key_in and not c.get("resolved"):
                        c["resolved"]   = True
                        c["resolution"] = f"Resolved via UI → {sec_in}|{mill_in}"
                save_db(db, DB_PATH)
                st.success(f"Rule saved: {key_in} → {sec_in} @ {mill_in}")
                st.rerun()

    # ── Export DB ─────────────────────────────────────────────────────
    st.divider()
    st.subheader("Export / Import Learning DB")
    col_exp, col_imp = st.columns(2)
    with col_exp:
        db_json = json.dumps(db, indent=2, default=str)
        st.download_button(
            "⬇️ Download learning_db.json",
            data      = db_json,
            file_name = "learning_db.json",
            mime      = "application/json",
            use_container_width=True,
        )
    with col_imp:
        up_db = st.file_uploader("Upload learning_db.json to restore",
                                  type=["json"], key="db_upload")
        if up_db and st.button("↩️ Restore DB", use_container_width=True):
            new_db = json.loads(up_db.read())
            save_db(new_db, DB_PATH)
            st.success("Learning DB restored.")
            st.rerun()
