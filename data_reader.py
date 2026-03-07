"""Shared structured-data reader for CSV, Excel, and JSON files.

Used by both the filesystem tool (agent reads from workspace) and the
attachment handler (user drags a file into the chat).

Output format: schema + stats + preview rows — designed to give the
agent maximum analytical context without blowing up the token budget.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd

# ── Tunables ─────────────────────────────────────────────────────────────────
_PREVIEW_ROWS = 50          # rows to include in the preview table
_MAX_OUTPUT_CHARS = 80_000  # hard cap (matches filesystem_tool._MAX_READ_CHARS)
_DATA_EXTENSIONS = {".csv", ".xlsx", ".xls", ".json", ".jsonl", ".tsv"}


def is_data_file(name_or_path: str) -> bool:
    """Return True if the file extension is a supported structured data format."""
    return Path(name_or_path).suffix.lower() in _DATA_EXTENSIONS


def read_data_file(
    source: str | Path | io.BytesIO,
    *,
    name: str = "",
    sheet: str = "",
    max_chars: int = _MAX_OUTPUT_CHARS,
) -> str:
    """Read a structured data file and return a human-readable summary.

    Parameters
    ----------
    source : path-like or BytesIO
        File path on disk or an in-memory BytesIO buffer (for attachments).
    name : str
        Display name / filename (used for the header line). If empty and
        *source* is a path, the filename is derived from the path.
    sheet : str
        (Excel only) sheet name to read.  Empty → first / active sheet.
    max_chars : int
        Hard cap on the output length.

    Returns
    -------
    str
        Formatted summary: header, column schema, stats, preview rows.
    """
    # Resolve name
    if not name and isinstance(source, (str, Path)):
        name = Path(source).name
    suffix = Path(name).suffix.lower() if name else ""

    try:
        df, extra_info = _load_dataframe(source, suffix, sheet)
    except Exception as exc:
        return f"Error reading '{name}': {exc}"

    return _format_dataframe(df, name, suffix, extra_info, max_chars)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _load_dataframe(
    source: str | Path | io.BytesIO,
    suffix: str,
    sheet: str,
) -> tuple[pd.DataFrame, str]:
    """Load data into a DataFrame. Returns (df, extra_info_string)."""

    extra = ""

    if suffix in (".csv", ".tsv"):
        sep = "\t" if suffix == ".tsv" else ","
        if isinstance(source, io.BytesIO):
            df = pd.read_csv(source, sep=sep, on_bad_lines="skip")
        else:
            df = pd.read_csv(str(source), sep=sep, on_bad_lines="skip")

    elif suffix in (".xlsx", ".xls"):
        kwargs: dict = {}
        if isinstance(source, io.BytesIO):
            kwargs["io"] = source
        else:
            kwargs["io"] = str(source)

        # Discover sheet names
        xls = pd.ExcelFile(kwargs["io"])
        sheet_names = xls.sheet_names
        extra = f"Sheets: {', '.join(sheet_names)}"

        target_sheet = sheet if sheet and sheet in sheet_names else sheet_names[0]
        if sheet and sheet not in sheet_names:
            extra += f"\n(Requested sheet '{sheet}' not found — reading '{target_sheet}' instead)"

        df = pd.read_excel(xls, sheet_name=target_sheet)
        extra += f"\nReading sheet: '{target_sheet}'"

    elif suffix in (".json", ".jsonl"):
        if isinstance(source, io.BytesIO):
            raw = source.read()
            source.seek(0)
            text = raw.decode("utf-8", errors="replace")
        else:
            text = Path(source).read_text(encoding="utf-8", errors="replace")

        if suffix == ".jsonl" or text.lstrip().startswith("["):
            # Array of objects or JSON Lines
            if suffix == ".jsonl":
                df = pd.read_json(io.StringIO(text), lines=True)
            else:
                df = pd.read_json(io.StringIO(text))
        else:
            # Single object → try to normalise into a table
            obj = json.loads(text)
            if isinstance(obj, dict):
                # Find the first key whose value is a list of dicts
                for key, val in obj.items():
                    if isinstance(val, list) and val and isinstance(val[0], dict):
                        df = pd.json_normalize(val)
                        extra = f"Extracted from key: '{key}'"
                        break
                else:
                    # Flat object → single-row DataFrame
                    df = pd.json_normalize(obj)
                    extra = "Single object (flat)"
            else:
                df = pd.DataFrame({"value": [obj]})
                extra = "Single scalar value"
    else:
        raise ValueError(f"Unsupported file format: '{suffix}'")

    return df, extra


def _format_dataframe(
    df: pd.DataFrame,
    name: str,
    suffix: str,
    extra_info: str,
    max_chars: int,
) -> str:
    """Format a DataFrame as a readable summary string."""
    rows, cols = df.shape
    parts: list[str] = []

    # ── Header ───────────────────────────────────────────────────────────
    ext_label = suffix.lstrip(".").upper()
    parts.append(f"[{ext_label}: {name} | {rows:,} rows × {cols} columns]")
    if extra_info:
        parts.append(extra_info)

    # ── Column schema ────────────────────────────────────────────────────
    col_info = []
    for c in df.columns:
        dtype = str(df[c].dtype)
        nulls = int(df[c].isna().sum())
        null_note = f", {nulls} null" if nulls else ""
        col_info.append(f"  {c} ({dtype}{null_note})")
    parts.append("Columns:\n" + "\n".join(col_info))

    # ── Statistics for numeric columns ───────────────────────────────────
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    if numeric_cols:
        stat_lines = []
        for c in numeric_cols:
            s = df[c].describe()
            stat_lines.append(
                f"  {c}: min={_fmt(s['min'])}, max={_fmt(s['max'])}, "
                f"mean={_fmt(s['mean'])}, median={_fmt(s['50%'])}, "
                f"std={_fmt(s['std'])}"
            )
        parts.append("Statistics (numeric columns):\n" + "\n".join(stat_lines))

    # ── Preview rows ─────────────────────────────────────────────────────
    preview = df.head(_PREVIEW_ROWS)
    table_str = preview.to_string(index=False, max_colwidth=80)
    if rows > _PREVIEW_ROWS:
        table_str += f"\n... ({rows - _PREVIEW_ROWS:,} more rows)"

    parts.append(f"Preview (first {min(rows, _PREVIEW_ROWS)} rows):\n{table_str}")

    result = "\n\n".join(parts)

    # ── Cap output ───────────────────────────────────────────────────────
    if len(result) > max_chars:
        result = result[:max_chars] + (
            f"\n\n[Truncated — showing first {max_chars:,} characters. "
            f"Full data is {rows:,} rows × {cols} columns.]"
        )

    return result


def _fmt(val) -> str:
    """Format a numeric value for display."""
    if pd.isna(val):
        return "N/A"
    if isinstance(val, float):
        if val == int(val) and abs(val) < 1e15:
            return str(int(val))
        return f"{val:,.4f}".rstrip("0").rstrip(".")
    return str(val)
