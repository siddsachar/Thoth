"""Chart tool — create interactive Plotly charts from data files.

The agent calls ``create_chart`` with a structured specification (chart type,
column names, optional parameters).  The tool loads the data from a
workspace file or a cached attachment, builds a Plotly figure, and returns
its JSON representation wrapped in a ``__CHART__`` marker so the UI layer
can render it inline.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Optional

# Module-level attachment cache — set by the UI layer before agent invocation.
_attachment_cache: dict[str, bytes] = {}

import plotly.express as px
import plotly.graph_objects as go
import pandas as pd
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from row_bot.tools.base import BaseTool
from row_bot.tools import registry

# ── Chart marker prefix — the UI layer detects this in tool output ───────
_CHART_MARKER = "__CHART__:"

# ── Supported chart types ────────────────────────────────────────────────
_CHART_TYPES = {
    "bar", "horizontal_bar", "line", "scatter", "pie", "donut",
    "histogram", "box", "area", "heatmap",
}

# Common aliases LLMs try — silently resolve instead of erroring
_CHART_ALIASES: dict[str, str] = {
    "grouped_bar": "bar",
    "stacked_bar": "bar",
    "stacked_area": "area",
    "barh": "horizontal_bar",
    "hbar": "horizontal_bar",
    "doughnut": "donut",
    "ring": "donut",
    "boxplot": "box",
    "heat_map": "heatmap",
    "correlation": "heatmap",
}

# Diagram types that should be rendered as Mermaid text, not Plotly charts.
_MERMAID_DIAGRAM_TYPES = {
    "flow",
    "flowchart",
    "mermaid",
    "sequence",
    "state",
    "er",
    "entity_relationship",
    "mindmap",
    "architecture",
    "graphviz",
    "sankey",
}

# ── Maximum rows to plot (guard against huge datasets) ───────────────────
_MAX_PLOT_ROWS = 10_000


# ── Pydantic input schema ───────────────────────────────────────────────
class _CreateChartInput(BaseModel):
    chart_type: str = Field(
        description=(
            "Type of chart to create.  One of: bar, horizontal_bar, line, "
            "scatter, pie, donut, histogram, box, area, heatmap.  "
            "For grouped/stacked bars use 'bar' with comma-separated "
            "y_column values (e.g. 'Q1,Q2,Q3,Q4')."
        )
    )
    data_source: str = Field(
        description=(
            "The data to chart.  Accepts THREE formats:\n"
            "1. **Inline CSV/TSV** — paste the data directly, e.g. "
            "'City,Population\\nLondon,9000000\\nParis,2100000'\n"
            "2. **File path** — path to a CSV, Excel, JSON, or TSV file "
            "in the workspace\n"
            "3. **Attached filename** — the name of a file the user "
            "uploaded in this conversation"
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
    save_to_file: Optional[str] = Field(
        default=None,
        description=(
            "Optional filename to save the chart as a PNG image in the "
            "workspace (e.g. 'sales_chart.png'). When set the chart is "
            "saved to disk AND displayed inline. The returned message "
            "includes the absolute file path — useful for sending the "
            "image via Telegram or email."
        ),
    )


# ── Data loading helper ─────────────────────────────────────────────────
def _normalise_newlines(s: str) -> str:
    """Replace escaped literal '\\n' sequences with real newlines."""
    # Agents often emit the two-char sequence \n instead of a real newline
    if "\\n" in s and "\n" not in s:
        return s.replace("\\n", "\n")
    return s


def _looks_like_inline_data(s: str) -> bool:
    """Return True if *s* appears to be inline CSV/TSV rather than a path."""
    normed = _normalise_newlines(s)
    return "\n" in normed.strip() and ("," in normed or "\t" in normed)


def _load_data(data_source: str, sheet: str | None) -> pd.DataFrame:
    """Load a DataFrame from inline data, a cached attachment, or a file."""

    # 0) Inline CSV / TSV data — the agent passed raw data directly
    if _looks_like_inline_data(data_source):
        data_source = _normalise_newlines(data_source)
        sep = "\t" if "\t" in data_source else ","
        df = pd.read_csv(io.StringIO(data_source), sep=sep, on_bad_lines="skip")
        if len(df.columns) >= 1 and len(df) >= 1:
            return df

    # 1) Try the attachment cache (populated by the UI when user attaches files)
    name_lower = Path(data_source).name.lower()
    from row_bot.application.attachment_context import tool_cache
    for cached_name, cached_bytes in tool_cache("data", _attachment_cache).items():
        if cached_name.lower() == name_lower:
            suffix = Path(cached_name).suffix.lower()
            buf = io.BytesIO(cached_bytes)
            return _read_df(buf, suffix, sheet)

    # 2) Try as a workspace file path
    path = Path(data_source)
    if not path.is_absolute():
        # Resolve relative to the filesystem tool's configured workspace root
        bases: list[Path] = []
        try:
            from row_bot.tools import registry as _reg
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
        # Pivot for heatmap — need a numeric column for values
        if color and color in df.columns and x in df.columns and y in df.columns:
            # Use color as values if numeric, otherwise as an axis
            if df[color].dtype.kind in ("i", "f"):
                pivot = df.pivot_table(index=y, columns=x, values=color, aggfunc="mean")
            elif df[y].dtype.kind in ("i", "f"):
                pivot = df.pivot_table(index=color, columns=x, values=y, aggfunc="mean")
            else:
                pivot = df.pivot_table(index=y, columns=x, values=num_cols[0], aggfunc="mean")
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


# ── Wide-format auto-melt ────────────────────────────────────────────────
def _auto_melt_if_needed(
    df: pd.DataFrame,
    x_column: str | None,
    y_column: str | None,
) -> pd.DataFrame:
    """Auto-unpivot wide-format data when the agent requests columns that
    don't exist but could be derived by melting numeric columns.

    Real-world example
    ------------------
    Data:   Region, Category, Q1, Q2, Q3, Q4
    Agent:  x_column='Quarter', y_column='Sales'

    Neither column exists, but there are 4 numeric columns that can be
    melted.  Result:  Region, Category, Quarter, Sales  (long format).

    This handles the generic shape mismatch between how LLMs conceptualise
    data (long/tidy) and how users supply it (wide from spreadsheets).
    """
    cols = set(df.columns)
    num_cols = df.select_dtypes(include="number").columns.tolist()

    if len(num_cols) < 2:
        return df  # not enough numeric columns to melt

    # Gather which requested column parts are missing
    x_parts = [p.strip() for p in x_column.split(",")] if x_column else []
    y_parts = [p.strip() for p in y_column.split(",")] if y_column else []

    # Only melt when an entire side (x or y) is fully absent.
    # Partial mismatches (e.g. "Q1,Sales" where Q1 exists) are left for
    # normal validation — melting would destroy the existing column.
    x_fully_missing = bool(x_parts) and all(c not in cols for c in x_parts)
    y_fully_missing = bool(y_parts) and all(c not in cols for c in y_parts)

    if not x_fully_missing and not y_fully_missing:
        return df  # nothing to melt — requested columns already exist

    # Don't melt if the *other* side references numeric columns that would
    # be consumed.  e.g. x='Quarter'(missing), y='Q1,Q2,Q3,Q4'(present)
    # means the agent wants multi-series — melting would destroy y.
    num_set = set(num_cols)
    if x_fully_missing and not y_fully_missing:
        if any(p.strip() in num_set for p in y_parts):
            return df
    if y_fully_missing and not x_fully_missing:
        if any(p.strip() in num_set for p in x_parts):
            return df

    id_cols = [c for c in df.columns if c not in num_cols]

    # Name the two new columns after what the agent asked for
    var_name = x_parts[0] if x_fully_missing else "variable"
    value_name = y_parts[0] if y_fully_missing else "value"

    # Avoid name collision
    if var_name == value_name:
        value_name = value_name + "_value"

    melted = pd.melt(
        df,
        id_vars=id_cols or None,
        value_vars=num_cols,
        var_name=var_name,
        value_name=value_name,
    )

    return melted


# ── Main tool function ───────────────────────────────────────────────────
def _create_chart(
    chart_type: str,
    data_source: str,
    x_column: str | None = None,
    y_column: str | None = None,
    color_column: str | None = None,
    title: str | None = None,
    sheet: str | None = None,
    save_to_file: str | None = None,
) -> str:
    """Create a chart and return a JSON marker for the UI to render."""

    chart_type = chart_type.strip().lower()
    if chart_type in _MERMAID_DIAGRAM_TYPES:
        return (
            "This tool creates Plotly data charts, not Mermaid diagrams. "
            "For flowcharts/process/relationship diagrams, respond directly "
            "with a fenced Mermaid block (```mermaid ... ```). "
            "For memory relationship graphs, use explore_connections first "
            "(it already returns Mermaid)."
        )
    chart_type = _CHART_ALIASES.get(chart_type, chart_type)
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

    # Auto-unpivot wide-format data when the agent asks for conceptual
    # columns (e.g. "Quarter", "Sales") that don't exist in the raw data
    df = _auto_melt_if_needed(df, x_column, y_column)

    # Post-melt: if the melt created generic "variable"/"value" columns
    # and the agent references a conceptual name elsewhere (e.g.
    # color_column='Sales' but melt named it 'value'), rename to match.
    for req_col in (x_column, y_column, color_column):
        if not req_col:
            continue
        for part in req_col.split(","):
            part = part.strip()
            if part and part not in df.columns:
                if "variable" in df.columns:
                    df = df.rename(columns={"variable": part})
                elif "value" in df.columns:
                    df = df.rename(columns={"value": part})

    # Validate columns exist
    for col_name, col_label in [(x_column, "x_column"), (y_column, "y_column"), (color_column, "color_column")]:
        if col_name:
            for part in col_name.split(","):
                part = part.strip()
                if part and part not in df.columns:
                    close = [c for c in df.columns if part.lower() in c.lower()]
                    hint = f" Did you mean: {', '.join(close[:3])}?" if close else ""
                    num = df.select_dtypes(include="number").columns.tolist()
                    multi_hint = ""
                    if len(num) >= 2 and col_label == "y_column":
                        multi_hint = (
                            f" TIP: for multi-series, use the actual column "
                            f"names with commas: y_column='{','.join(num[:4])}'."
                        )
                    return (
                        f"Column '{part}' not found in data. "
                        f"Available columns: {', '.join(df.columns.tolist())}."
                        f"{hint}{multi_hint}"
                    )

    try:
        fig = _build_figure(df, chart_type, x_column, y_column, color_column, title)
    except Exception as e:
        return f"Error building chart: {e}"

    # Serialize the figure as JSON and wrap in marker
    fig_json = fig.to_json()
    chart_info = f"{chart_type} chart of {data_source}"
    if title:
        chart_info = title

    result = f"{_CHART_MARKER}{fig_json}\n\nChart created: {chart_info} ({len(df):,} data points)"

    # Optionally save as PNG image
    if save_to_file:
        try:
            save_name = save_to_file.strip()
            if not save_name.lower().endswith(".png"):
                save_name += ".png"
            # Resolve to workspace root
            save_path = Path(save_name)
            if not save_path.is_absolute():
                try:
                    from row_bot.tools import registry as _reg
                    fs_tool = _reg.get_tool("filesystem")
                    if fs_tool:
                        ws_root = fs_tool.get_config("workspace_root", "")
                        if ws_root:
                            save_path = Path(ws_root) / save_name
                except Exception:
                    pass
                if not save_path.is_absolute():
                    save_path = Path.cwd() / save_name
            save_path.parent.mkdir(parents=True, exist_ok=True)
            fig.write_image(str(save_path), width=1200, height=700, scale=2)
            result += f"\n\n📁 Chart saved to: {save_path}"
        except Exception as exc:
            result += f"\n\n⚠️ Could not save chart image: {exc}"

    return result


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
            "and heatmap charts from CSV, Excel, JSON, and TSV files. "
            "Not for Mermaid flowcharts or relationship diagrams."
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
                    "Do NOT use this for Mermaid flowcharts/process diagrams/"
                    "relationship graphs — output Mermaid code directly instead. "
                    "Use save_to_file to save the chart as a PNG image "
                    "(e.g. for sending via Telegram or email). "
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
