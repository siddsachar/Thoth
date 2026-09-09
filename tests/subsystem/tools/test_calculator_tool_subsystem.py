"""Deterministic subsystem tests for the Calculator tool.

Tests cover:
- Tool identity and registration contracts
- Basic arithmetic operations
- Mathematical functions (sqrt, sin, cos, log, factorial, etc.)
- Constants (pi, e, tau)
- Integer result formatting (no trailing .0)
- Division by zero handling
- Invalid expression handling
- Math domain errors (e.g. sqrt of negative)
- Overflow protection
- Safety — no access to builtins like import, exec, eval, os
"""

from __future__ import annotations

import math

import pytest


pytestmark = pytest.mark.subsystem


# ── Identity & registration ─────────────────────────────────────────────────


def test_calculator_tool_name_and_display_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import CalculatorTool

    tool = CalculatorTool()
    assert tool.name == "calculator"
    assert tool.display_name == "🧮 Calculator"


def test_calculator_tool_enabled_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import CalculatorTool

    assert CalculatorTool().enabled_by_default is True


def test_calculator_tool_requires_no_api_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import CalculatorTool

    assert CalculatorTool().required_api_keys == {}


def test_calculator_tool_is_registered(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools import registry

    import row_bot.tools.calculator_tool  # noqa: F401

    tool = registry.get_tool("calculator")
    assert tool is not None
    assert tool.name == "calculator"


# ── Basic arithmetic ────────────────────────────────────────────────────────


def test_addition(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("2 + 3")
    assert "= 5" in result


def test_subtraction(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("10 - 4")
    assert "= 6" in result


def test_multiplication(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("7 * 8")
    assert "= 56" in result


def test_division(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("15 / 3")
    assert "= 5" in result


def test_floor_division(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("17 // 3")
    assert "= 5" in result


def test_modulo(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("17 % 5")
    assert "= 2" in result


def test_exponentiation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("2 ** 10")
    assert "= 1024" in result


# ── Mathematical functions ───────────────────────────────────────────────────


def test_sqrt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("sqrt(144)")
    assert "= 12" in result


def test_abs_function(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("abs(-42)")
    assert "= 42" in result


def test_round_function(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("round(3.7)")
    assert "= 4" in result


def test_factorial(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("factorial(10)")
    assert "= 3628800" in result


def test_log_natural(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("log(e)")
    assert "= 1" in result


def test_log2(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("log2(1024)")
    assert "= 10" in result


def test_log10(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("log10(1000)")
    assert "= 3" in result


def test_sin_of_zero(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("sin(0)")
    assert "= 0" in result


def test_cos_of_zero(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("cos(0)")
    assert "= 1" in result


def test_ceil_function(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("ceil(4.2)")
    assert "= 5" in result


def test_floor_function(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("floor(4.8)")
    assert "= 4" in result


def test_gcd(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("gcd(48, 18)")
    assert "= 6" in result


def test_pow_function(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("pow(3, 4)")
    assert "= 81" in result


def test_min_max(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    assert "= 1" in _calculate("min(1, 5, 3)")
    assert "= 5" in _calculate("max(1, 5, 3)")


# ── Constants ────────────────────────────────────────────────────────────────


def test_pi_constant(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("pi")
    assert str(math.pi) in result


def test_e_constant(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("e")
    assert str(math.e) in result


def test_tau_constant(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("tau")
    assert str(math.tau) in result


# ── Integer formatting ───────────────────────────────────────────────────────


def test_integer_result_has_no_trailing_dot_zero(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("10.0 + 5.0")
    # Should show = 15, not = 15.0
    assert "= 15" in result
    assert ".0" not in result.split("=")[1]


def test_float_result_preserved_when_not_integer(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("10 / 3")
    assert "3.333" in result


# ── Error handling ───────────────────────────────────────────────────────────


def test_division_by_zero(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("1 / 0")
    assert "division by zero" in result.lower()


def test_invalid_expression(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("2 + * 3")
    # Should return an error, not crash
    assert "error" in result.lower() or "invalid" in result.lower()


def test_sqrt_of_negative_returns_math_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("sqrt(-1)")
    assert "error" in result.lower() or "math" in result.lower()


def test_empty_expression(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("")
    # Should handle gracefully, not crash
    assert isinstance(result, str)


# ── Safety: blocked dangerous operations ─────────────────────────────────────


def test_import_blocked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("__import__('os').system('echo hacked')")
    # simpleeval should reject this
    assert "error" in result.lower() or "invalid" in result.lower()


def test_exec_blocked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("exec('print(1)')")
    assert "error" in result.lower() or "invalid" in result.lower()


def test_eval_blocked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("eval('2+2')")
    assert "error" in result.lower() or "invalid" in result.lower()


def test_open_blocked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("open('/etc/passwd')")
    assert "error" in result.lower() or "invalid" in result.lower()


def test_attribute_access_blocked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("''.__class__.__mro__")
    assert "error" in result.lower() or "invalid" in result.lower()


# ── execute() method ─────────────────────────────────────────────────────────


def test_execute_delegates_to_calculate(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import CalculatorTool

    tool = CalculatorTool()
    result = tool.execute("2 ** 8")
    assert "= 256" in result


# ── LangChain tool wrapper ──────────────────────────────────────────────────


def test_as_langchain_tools_returns_calculate_tool(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import CalculatorTool

    tool = CalculatorTool()
    lc_tools = tool.as_langchain_tools()

    assert len(lc_tools) == 1
    assert lc_tools[0].name == "calculate"
    assert "sqrt" in lc_tools[0].description
    assert "factorial" in lc_tools[0].description


def test_langchain_tool_accepts_expression_argument(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import CalculatorTool

    tool = CalculatorTool()
    lc_tool = tool.as_langchain_tools()[0]

    # The StructuredTool should accept an `expression` argument
    result = lc_tool.invoke({"expression": "sqrt(25)"})
    assert "= 5" in result


# ── Combined expression tests ────────────────────────────────────────────────


def test_nested_function_calls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    result = _calculate("round(sqrt(2), 4)")
    assert "1.4142" in result


def test_expression_with_constants_and_functions(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.calculator_tool import _calculate

    # 2 * pi should be approximately tau
    result = _calculate("round(2 * pi, 10)")
    assert str(round(2 * math.pi, 10)) in result
