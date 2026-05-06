"""Opt-in real-world MCP end-to-end tests.

Set THOTH_MCP_REAL_WORLD_E2E=1 to run these checks. They touch live public MCP
servers and are intentionally skipped during normal CI and local smoke tests.
"""

from __future__ import annotations

import argparse
import os
import unittest

from scripts.mcp_real_world_e2e import run


@unittest.skipUnless(os.environ.get("THOTH_MCP_REAL_WORLD_E2E") == "1", "set THOTH_MCP_REAL_WORLD_E2E=1 to run live MCP E2E checks")
class McpRealWorldE2ETests(unittest.TestCase):
    def test_public_no_auth_targets(self) -> None:
        args = argparse.Namespace(
            targets=[],
            include_stdio=False,
            connect_timeout=30.0,
            tool_timeout=45.0,
            output_limit=50000,
            json=False,
        )
        results = run(args)
        failures = [result for result in results if result.status == "fail"]
        self.assertFalse(failures, [failure.__dict__ for failure in failures])
        self.assertGreaterEqual(sum(1 for result in results if result.status == "pass"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)