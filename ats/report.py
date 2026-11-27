"""Turns ScoreResults into a ranked table and CSV/Excel exports."""
from __future__ import annotations

import io

import pandas as pd

from .rubric import Criterion
from .scorer import ScoreResult

STATUS_ORDER = {"scored": 0, "error": 1, "unreadable": 2, "duplicate": 3}


def to_dataframe(results: list[ScoreResult], rubric: list[Criterion]) -> pd.DataFrame:
    rows = []
    for r in results:
        row = {
            "Score": r.score,
            "Candidate": r.candidate_name or r.filename,
            "File": r.filename,
            "Email": r.email,
            "Phone": r.phone,
            "Latest role": r.role,
            "Exp (yrs)": r.experience_years,
        }
        for c in rubric:
            row[c.name] = (r.criteria.get(c.key) or {}).get("score")
        row.update({
            "Summary": r.summary,
            "Strengths": "; ".join(r.strengths),
            "Gaps": "; ".join(r.gaps),
            "Red flags": "; ".join(r.red_flags),
            "Status": r.status,
            "Error": r.error,
        })
        rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["_s"] = df["Status"].map(STATUS_ORDER).fillna(9)
    df = df.sort_values(["_s", "Score", "Candidate"], ascending=[True, False, True], na_position="last")
    df = df.drop(columns="_s").reset_index(drop=True)
    # Dense rank among scored resumes; ties share a rank.
    scored = df["Status"] == "scored"
    df.insert(0, "Rank", pd.NA)
    df.loc[scored, "Rank"] = df.loc[scored, "Score"].rank(method="min", ascending=False).astype(int)
    return df


def to_excel(df: pd.DataFrame, rubric: list[Criterion], jd: str) -> bytes:
    buf = io.BytesIO()
    out = df.copy()
    for c in rubric:  # "points/weight" as text, e.g. 12/20 (Excel would otherwise read it as a date)
        out[c.name] = out[c.name].map(lambda v, w=int(c.weight): "" if pd.isna(v) else f"{int(v)}/{w}")
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        out.to_excel(xw, sheet_name="Ranked candidates", index=False)
        pd.DataFrame([{"Criterion": c.name, "Weight (%)": round(c.weight, 1), "What scores high": c.description}
                      for c in rubric]).to_excel(xw, sheet_name="Rubric", index=False)
        pd.DataFrame({"Job description": jd.splitlines()}).to_excel(xw, sheet_name="Job description", index=False)

        ws = xw.sheets["Ranked candidates"]
        ws.freeze_panes = "D2"
        widths = {"Rank": 6, "Score": 7, "Candidate": 26, "File": 28, "Summary": 60, "Strengths": 45,
                  "Gaps": 45, "Red flags": 30, "Email": 28}
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
        for idx, col in enumerate(df.columns, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = widths.get(col, 14)
            cell = ws.cell(row=1, column=idx)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E79")
        for c in rubric:  # text format so Excel keeps "12/20" as typed, even if the cell is edited
            col = list(df.columns).index(c.name) + 1
            for row in range(2, ws.max_row + 1):
                ws.cell(row=row, column=col).number_format = "@"
        ws.auto_filter.ref = ws.dimensions
        for name in ("Rubric", "Job description"):
            xw.sheets[name].column_dimensions["A"].width = 30 if name == "Rubric" else 110
            if name == "Rubric":
                xw.sheets[name].column_dimensions["C"].width = 100
    return buf.getvalue()
