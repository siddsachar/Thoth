"""Helpers for assembling channel-facing agent output."""

from __future__ import annotations

from collections.abc import Sequence


def assemble_agent_answer(
    answer: str,
    tool_reports: Sequence[str],
    notices: Sequence[str] = (),
) -> str:
    """Return final channel text, favoring the model's answer over tool logs."""
    notice_lines = list(dict.fromkeys(str(notice) for notice in notices if str(notice).strip()))
    if str(answer or "").strip():
        return answer + ("\n\n" + "\n".join(notice_lines) if notice_lines else "")
    reports = [str(report) for report in tool_reports if str(report).strip()]
    if reports:
        base = "\n".join(reports)
        return base + ("\n\n" + "\n".join(notice_lines) if notice_lines else "")
    return "\n".join(notice_lines)
