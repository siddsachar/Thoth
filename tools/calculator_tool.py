"""Calculator tool — safe math expression evaluation via simpleeval."""

from __future__ import annotations

import math

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry

# Functions exposed to the expression evaluator
_MATH_FUNCTIONS = {
    # Basic
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "int": int,
    "float": float,
    # Powers & roots
    "pow": pow,
    "sqrt": math.sqrt,
    "cbrt": math.cbrt,
    # Logarithms
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    # Trigonometry
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "radians": math.radians,
    "degrees": math.degrees,
    # Rounding
    "ceil": math.ceil,
    "floor": math.floor,
    "trunc": math.trunc,
    # Combinatorics
    "factorial": math.factorial,
    "comb": math.comb,
    "perm": math.perm,
    # Other
    "gcd": math.gcd,
    "lcm": math.lcm,
    "hypot": math.hypot,
}

# Constants exposed to the expression evaluator
_MATH_NAMES = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
}


class _CalculateInput(BaseModel):
    expression: str = Field(
        description=(
            "A mathematical expression to evaluate. Supports standard "
            "operators (+, -, *, /, //, %, **) and functions like "
            "sqrt(), sin(), cos(), log(), round(), abs(), factorial(), etc. "
            "Constants: pi, e, tau. Examples: '2 ** 10', 'sqrt(144)', "
            "'sin(radians(45))', 'log2(1024)', 'factorial(10)'."
        )
    )


def _calculate(expression: str) -> str:
    """Evaluate a math expression safely."""
    from simpleeval import simple_eval, InvalidExpression

    try:
        result = simple_eval(
            expression,
            functions=_MATH_FUNCTIONS,
            names=_MATH_NAMES,
        )
        # Format nicely — avoid trailing .0 for integers
        if isinstance(result, float) and result.is_integer() and abs(result) < 1e15:
            return f"{expression} = {int(result)}"
        return f"{expression} = {result}"
    except InvalidExpression as e:
        return f"Invalid expression: {e}"
    except ZeroDivisionError:
        return "Error: division by zero"
    except (ValueError, OverflowError, TypeError) as e:
        return f"Math error: {e}"
    except Exception as e:
        return f"Calculation error: {e}"


class CalculatorTool(BaseTool):

    @property
    def name(self) -> str:
        return "calculator"

    @property
    def display_name(self) -> str:
        return "🧮 Calculator"

    @property
    def description(self) -> str:
        return (
            "Evaluate mathematical expressions safely. Supports arithmetic, "
            "powers, roots, trigonometry, logarithms, factorials, and more. "
            "Use this for any calculation the user asks about."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    def as_langchain_tools(self) -> list:
        return [
            StructuredTool.from_function(
                func=_calculate,
                name="calculate",
                description=(
                    "Evaluate a mathematical expression. Supports +, -, *, /, "
                    "//, %, ** operators and functions: sqrt, sin, cos, tan, "
                    "log, log2, log10, abs, round, ceil, floor, factorial, "
                    "comb, perm, gcd, lcm, pow, min, max, sum, radians, "
                    "degrees, hypot. Constants: pi, e, tau. "
                    "Examples: 'sqrt(144)', '2**10', 'sin(radians(45))'."
                ),
                args_schema=_CalculateInput,
            )
        ]

    def execute(self, query: str) -> str:
        return _calculate(query)


registry.register(CalculatorTool())
