"""Chart tool — create interactive Plotly charts from data files.

The agent calls ``create_chart`` with a structured specification (chart type,
column names, optional parameters).  The tool loads the data from a
workspace file or a cached attachment, builds a Plotly figure, and returns
its JSON representation wrapped in a ``__CHART__`` marker so the UI layer
can render it inline with ``st.plotly_chart()``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Optional

import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry

# ── Chart marker prefix — the UI layer detects this in tool output ───────
_CHART_MARKER = "__CHART__:"

# ── Supported chart types ────────────────────────────────────────────────
_CHART_TYPES = {
    "bar", "horizontal_bar", "line", "scatter", "pie", "donut",
    "histogram", "box", "area", "heatmap",
}

# ── Maximum rows to plot (guard against huge datasets) ───────────────────
_MAX_PLOT_ROWS = 10_000


# ── Pydantic input schema ───────────────────────────────────────────────
class _CreateChartInput(BaseModel):
    chart_type: str = Field(
        description=(
            "Type of chart to create.  One of: bar, horizontal_bar, line, "
            "scatter, pie, donut, histogram, box, area, heatmap."
        )
    )
    data_source: str = Field(
        description=(
            "Path to the data file (CSV, Excel, JSON, TSV) in the "
            "workspace, OR the filename of an attached file the user "
            "uploaded in this conversation."
        )
    )
    x_column: Optional[str] = Field(
        default=None,
        description="Column name for the X axis (categories or values).",
    )
    y_column: Optional[str] = Field(
        default=None,
        description=(
            "Column name for the Y axis.  For multiple series, separate "
            "column names with a comma: 'revenue,cost'."
        ),
    )
    color_column: Optional[str] = Field(
        default=None,
        description="Optional column to use for colour grouping / legend.",
    )
    title: Optional[str] = Field(
        default=None,
        description="Chart title.  If omitted a sensible default is generated.",
    )
    sheet: Optional[str] = Field(
        default=None,
        description="(Excel only) Sheet name to read.  Defaults to the first sheet.",
    )


# ── Data loading helper ─────────────────────────────────────────────────
def _load_data(data_source: str, sheet: str | None) -> pd.DataFrame:
    """Load a DataFrame from a workspace file or a cached attachment."""

    # 1) Try the attachment cache (populated by app.py when user attaches files)
    try:
        import streamlit as st
        cache: dict[str, bytes] = st.session_state.get("_attached_data_cache", {})
        name_lower = Path(data_source).name.lower()
        for cached_name, cached_bytes in cache.items():
            if cached_name.lower() == name_lower:
                suffix = Path(cached_name).suffix.lower()
                buf = io.BytesIO(cached_bytes)
                return _read_df(buf, suffix, sheet)
    except Exception:
        pass

    # 2) Try as a workspace file path
    path = Path(data_source)
    if not path.is_absolute():
        # Resolve relative to the filesystem tool's configured workspace root
        bases: list[Path] = []
        try:
            from tools import registry as _reg
            fs_tool = _reg.get_tool("filesystem")
            if fs_tool:
                ws_root = fs_tool.get_config("workspace_root", "")
                if ws_root:
                    bases.append(Path(ws_root))
        except Exception:
            pass
        bases.append(Path.cwd())
        for base in bases:
            candidate = base / path
            if candidate.exists():
                path = candidate
                break
    if path.exists():
        suffix = path.suffix.lower()
        return _read_df(str(path), suffix, sheet)

    raise FileNotFoundError(
        f"Data source '{data_source}' not found — check the file path or "
        "make sure the user attached this file in the current conversation."
    )


def _read_df(
    source: str | io.BytesIO,
    suffix: str,
    sheet: str | None,
) -> pd.DataFrame:
    """Read a DataFrame from a file or buffer."""
    if suffix in (".csv", ".tsv"):
        sep = "\t" if suffix == ".tsv" else ","
        return pd.read_csv(source, sep=sep, on_bad_lines="skip")
    if suffix in (".xlsx", ".xls"):
        xls = pd.ExcelFile(source)
        target = sheet if sheet and sheet in xls.sheet_names else xls.sheet_names[0]
        return pd.read_excel(xls, sheet_name=target)
    if suffix in (".json", ".jsonl"):
        if isinstance(source, io.BytesIO):
            raw = source.read()
            source.seek(0)
            text = raw.decode("utf-8", errors="replace")
        else:
            text = Path(source).read_text(encoding="utf-8", errors="replace")
        if suffix == ".jsonl" or text.lstrip().startswith("["):
            return pd.read_json(io.StringIO(text), lines=(suffix == ".jsonl"))
        obj = json.loads(text)
        if isinstance(obj, dict):
            for val in obj.values():
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    return pd.json_normalize(val)
            return pd.json_normalize(obj)
        return pd.DataFrame({"value": [obj]})
    raise ValueError(f"Unsupported file type: {suffix}")


# ── Chart builders ───────────────────────────────────────────────────────
def _build_figure(
    df: pd.DataFrame,
    chart_type: str,
    x: str | None,
    y: str | None,
    color: str | None,
    title: str | None,
) -> go.Figure:
    """Build a Plotly figure from the spec."""

    # Auto-pick columns if not provided
    num_cols = df.select_dtypes(include="number").columns.tolist()
    all_cols = df.columns.tolist()
    cat_cols = [c for c in all_cols if c not in num_cols]

    if not x and cat_cols:
        x = cat_cols[0]
    elif not x and all_cols:
        x = all_cols[0]

    if not y and num_cols:
        y = num_cols[0] if num_cols[0] != x else (num_cols[1] if len(num_cols) > 1 else num_cols[0])
    elif not y:
        y = all_cols[1] if len(all_cols) > 1 else all_cols[0]

    # Handle multi-series y (comma-separated)
    y_cols = [c.strip() for c in y.split(",")] if y and "," in y else None

    # Auto-generate title
    if not title:
        if chart_type in ("pie", "donut"):
            title = f"Distribution of {y}" if y else "Distribution"
        elif y_cols:
            title = f"{', '.join(y_cols)} by {x}"
        else:
            title = f"{y} by {x}" if x != y else f"Distribution of {x}"

    # ── Build the figure ─────────────────────────────────────────────
    if chart_type == "bar":
        if y_cols:
            fig = go.Figure()
            for yc in y_cols:
                if yc in df.columns:
                    fig.add_trace(go.Bar(name=yc, x=df[x], y=df[yc]))
            fig.update_layout(barmode="group")
        else:
            fig = px.bar(df, x=x, y=y, color=color, title=title)

    elif chart_type == "horizontal_bar":
        if y_cols:
            fig = go.Figure()
            for yc in y_cols:
                if yc in df.columns:
                    fig.add_trace(go.Bar(name=yc, y=df[x], x=df[yc], orientation="h"))
            fig.update_layout(barmode="group")
        else:
            fig = px.bar(df, y=x, x=y, color=color, title=title, orientation="h")

    elif chart_type == "line":
        if y_cols:
            fig = go.Figure()
            for yc in y_cols:
                if yc in df.columns:
                    fig.add_trace(go.Scatter(name=yc, x=df[x], y=df[yc], mode="lines+markers"))
        else:
            fig = px.line(df, x=x, y=y, color=color, title=title, markers=True)

    elif chart_type == "scatter":
        fig = px.scatter(df, x=x, y=y, color=color, title=title)

    elif chart_type in ("pie", "donut"):
        fig = px.pie(df, names=x, values=y, title=title,
                     hole=0.4 if chart_type == "donut" else 0)

    elif chart_type == "histogram":
        target = x if x else (y if y else all_cols[0])
        fig = px.histogram(df, x=target, color=color, title=title)

    elif chart_type == "box":
        fig = px.box(df, x=color or x, y=y, title=title)

    elif chart_type == "area":
        if y_cols:
            fig = go.Figure()
            for yc in y_cols:
                if yc in df.columns:
                    fig.add_trace(go.Scatter(name=yc, x=df[x], y=df[yc],
                                            fill="tonexty", mode="lines"))
        else:
            fig = px.area(df, x=x, y=y, color=color, title=title)

    elif chart_type == "heatmap":
        # Pivot for heatmap if colour column exists
        if color and color in df.columns and x in df.columns and y in df.columns:
            pivot = df.pivot_table(index=y, columns=x, values=color, aggfunc="mean")
            fig = px.imshow(pivot, title=title, aspect="auto")
        elif len(num_cols) >= 2:
            corr = df[num_cols].corr()
            fig = px.imshow(corr, title=title or "Correlation Matrix",
                            aspect="auto", text_auto=".2f")
        else:
            raise ValueError("Heatmap requires at least 2 numeric columns or explicit x/y/color.")

    else:
        raise ValueError(f"Unknown chart type: '{chart_type}'")

    # Common layout tweaks
    fig.update_layout(
        title=title,
        template="plotly_dark",
        margin=dict(l=40, r=40, t=60, b=40),
    )

    return fig


# ── Main tool function ───────────────────────────────────────────────────
def _create_chart(
    chart_type: str,
    data_source: str,
    x_column: str | None = None,
    y_column: str | None = None,
    color_column: str | None = None,
    title: str | None = None,
    sheet: str | None = None,
) -> str:
    """Create a chart and return a JSON marker for the UI to render."""

    chart_type = chart_type.strip().lower()
    if chart_type not in _CHART_TYPES:
        return (
            f"Unsupported chart type '{chart_type}'. "
            f"Supported types: {', '.join(sorted(_CHART_TYPES))}"
        )

    try:
        df = _load_data(data_source, sheet)
    except Exception as e:
        return f"Error loading data: {e}"

    # Guard against huge datasets
    if len(df) > _MAX_PLOT_ROWS:
        df = df.head(_MAX_PLOT_ROWS)

    # Validate columns exist
    for col_name, col_label in [(x_column, "x_column"), (y_column, "y_column"), (color_column, "color_column")]:
        if col_name:
            for part in col_name.split(","):
                part = part.strip()
                if part and part not in df.columns:
                    close = [c for c in df.columns if part.lower() in c.lower()]
                    hint = f" Did you mean: {', '.join(close[:3])}?" if close else ""
                    return f"Column '{part}' not found in data. Available columns: {', '.join(df.columns.tolist())}.{hint}"

    try:
        fig = _build_figure(df, chart_type, x_column, y_column, color_column, title)
    except Exception as e:
        return f"Error building chart: {e}"

    # Serialize the figure as JSON and wrap in marker
    fig_json = fig.to_json()
    chart_info = f"{chart_type} chart of {data_source}"
    if title:
        chart_info = title

    return f"{_CHART_MARKER}{fig_json}\n\nChart created: {chart_info} ({len(df):,} data points)"


# ── Tool class ───────────────────────────────────────────────────────────
class ChartTool(BaseTool):

    @property
    def name(self) -> str:
        return "chart"

    @property
    def display_name(self) -> str:
        return "📊 Chart"

    @property
    def description(self) -> str:
        return (
            "Create interactive charts and visualisations from data files. "
            "Supports bar, line, scatter, pie, donut, histogram, box, area, "
            "and heatmap charts from CSV, Excel, JSON, and TSV files."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    def as_langchain_tools(self) -> list:
        return [
            StructuredTool.from_function(
                func=_create_chart,
                name="create_chart",
                description=(
                    "Create an interactive chart from a data file. Supports "
                    "chart types: bar, horizontal_bar, line, scatter, pie, "
                    "donut, histogram, box, area, heatmap. Reads data from "
                    "CSV, Excel (XLSX/XLS), JSON, JSONL, or TSV files. "
                    "The tool auto-picks columns if x/y are not specified. "
                    "Use this when the user asks to visualise, plot, chart, "
                    "or graph data, or when a chart would help explain "
                    "tabular data you have analysed."
                ),
                args_schema=_CreateChartInput,
            )
        ]

    def execute(self, query: str) -> str:
        return "Use the create_chart sub-tool with structured parameters."


registry.register(ChartTool())
