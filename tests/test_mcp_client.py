"""Focused tests for Thoth's MCP client foundation.

These tests avoid network dependence. They validate the core invariants that
keep MCP from breaking Thoth when config, dependencies, directories, or local
stdio servers are unavailable.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


class McpClientFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_data_dir = os.environ.get("THOTH_DATA_DIR")
        os.environ["THOTH_DATA_DIR"] = self._tmp.name

    def tearDown(self) -> None:
        try:
            import mcp_client.runtime as runtime
            runtime.shutdown()
        except Exception:
            pass
        try:
            import threads
            threads.conn.close()
        except Exception:
            pass
        if self._old_data_dir is None:
            os.environ.pop("THOTH_DATA_DIR", None)
        else:
            os.environ["THOTH_DATA_DIR"] = self._old_data_dir
        self._tmp.cleanup()

    def _reload_config(self):
        import mcp_client.config as cfg
        return importlib.reload(cfg)

    def test_bad_config_degrades_to_empty_disabled_config(self) -> None:
        cfg = self._reload_config()
        Path(self._tmp.name, "mcp_servers.json").write_text("{bad json", encoding="utf-8")
        loaded = cfg.load_config()
        self.assertFalse(loaded["enabled"])
        self.assertEqual(loaded["servers"], {})

    def test_config_round_trip_masks_secrets_and_normalizes_tools(self) -> None:
        cfg = self._reload_config()
        cfg.upsert_server("demo", {
            "enabled": True,
            "transport": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "Bearer abcdefghijklmnop"},
            "tools": {"resources_enabled": True},
        })
        loaded = cfg.get_servers()["demo"]
        self.assertEqual(loaded["transport"], "streamable_http")
        self.assertTrue(loaded["tools"]["resources_enabled"])
        self.assertFalse(loaded["tools"]["prompts_enabled"])
        masked = cfg.masked_config()
        self.assertNotIn("abcdefghijklmnop", str(masked))

    def test_destructive_detection_uses_annotations_and_names(self) -> None:
        from mcp_client.safety import is_destructive_tool, prefixed_tool_name

        self.assertTrue(is_destructive_tool("delete_file"))
        self.assertFalse(is_destructive_tool("search_messages"))
        readonly_tool = SimpleNamespace(annotations=SimpleNamespace(readOnlyHint=True, destructiveHint=False))
        self.assertFalse(is_destructive_tool("update_index", tool_obj=readonly_tool))
        destructive_tool = SimpleNamespace(annotations={"destructiveHint": True})
        self.assertTrue(is_destructive_tool("lookup", tool_obj=destructive_tool))
        self.assertFalse(is_destructive_tool("browser_click", "Perform click on a web page", destructive_tool))
        self.assertFalse(is_destructive_tool("browser_navigate", "Navigate to a URL", destructive_tool))
        self.assertFalse(is_destructive_tool("browser_fill_form", "Fill multiple form fields", destructive_tool))
        self.assertTrue(is_destructive_tool("browser_evaluate", "Evaluate JavaScript expression on page or element", destructive_tool))
        self.assertTrue(is_destructive_tool("browser_run_code", "Run Playwright code snippet", destructive_tool))
        self.assertTrue(is_destructive_tool("browser_file_upload", "Upload one or multiple files", destructive_tool))
        self.assertEqual(prefixed_tool_name("My Server", "Delete File"), "mcp_my_server_delete_file")

    def test_marketplace_unknown_source_falls_back_to_curated_catalog(self) -> None:
        import mcp_client.marketplace as marketplace
        importlib.reload(marketplace)
        with patch.object(marketplace, "_load_cache", return_value=[]):
            results = marketplace.search_marketplace("filesystem", sources=["unknown-source"], limit=5)
            status = marketplace.search_marketplace_with_status("filesystem", sources=["unknown-source"], limit=5)
        self.assertGreaterEqual(len(results), 1)
        self.assertTrue(any(entry.source == "curated" for entry in results))
        self.assertEqual(status.mode, "curated")
        self.assertEqual(status.query, "filesystem")
        self.assertGreaterEqual(status.source_counts.get("curated", 0), 1)
        self.assertEqual([entry.id for entry in status.entries], [entry.id for entry in results])

    def test_recommended_catalog_excludes_memory_and_marks_overlaps(self) -> None:
        import mcp_client.marketplace as marketplace
        importlib.reload(marketplace)
        from mcp_client.conflicts import conflicts_for_entry

        recommended = [entry for entry in marketplace.CURATED_STARTER_CATALOG if entry.recommended]
        self.assertGreaterEqual(len(recommended), 10)
        self.assertFalse(any("memory" in entry.name.lower() for entry in recommended))

        playwright = next(entry for entry in recommended if entry.id == "microsoft-playwright")
        self.assertIn("browser", playwright.overlaps_native)
        conflicts = conflicts_for_entry(playwright)
        self.assertEqual([conflict.capability for conflict in conflicts], ["browser"])

    def test_marketplace_import_preserves_trust_risk_and_overlap_metadata(self) -> None:
        import mcp_client.marketplace as marketplace
        importlib.reload(marketplace)

        playwright = next(entry for entry in marketplace.CURATED_STARTER_CATALOG if entry.id == "microsoft-playwright")
        cfg = marketplace.entry_to_server_config(playwright)
        source = cfg["source"]
        self.assertFalse(cfg["enabled"])
        self.assertTrue(source["not_verified_by_thoth"])
        self.assertEqual(source["trust_tier"], "official_vendor")
        self.assertEqual(source["risk_level"], "medium")
        self.assertEqual(source["overlaps_native"], ["browser"])
        self.assertEqual(source["conflicts"][0]["capability"], "browser")

    def test_conflict_policy_uses_manual_selection_for_overlap_and_high_risk(self) -> None:
        from mcp_client.conflicts import conflicts_for_server, requires_manual_tool_selection, unique_server_name
        from ui.mcp_settings import _apply_probe_defaults

        overlap_cfg = {
            "name": "playwright",
            "source": {"overlaps_native": ["browser"], "risk_level": "medium"},
        }
        self.assertTrue(requires_manual_tool_selection("playwright", overlap_cfg))
        self.assertEqual(conflicts_for_server("playwright", overlap_cfg)[0].capability, "browser")
        probe = {"tools": [
            {
                "name": "navigate",
                "description": "Navigate to a page.",
                "destructive": False,
                "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}},
            },
            {"name": "delete_cookie", "description": "Delete a cookie.", "destructive": True},
        ]}
        updated = _apply_probe_defaults(dict(overlap_cfg), probe)
        self.assertEqual(updated["tools"]["enabled"], {"navigate": False, "delete_cookie": False})
        self.assertEqual(updated["tools"]["require_approval"], ["delete_cookie"])
        self.assertEqual(updated["tools"]["catalog"]["navigate"]["description"], "Navigate to a page.")
        self.assertEqual(updated["tools"]["catalog"]["navigate"]["input_schema"]["properties"]["url"]["type"], "string")
        self.assertTrue(updated["tools"]["catalog"]["delete_cookie"]["destructive"])

        high_risk_cfg = {"name": "stripe", "source": {"risk_level": "high"}}
        self.assertTrue(requires_manual_tool_selection("stripe", high_risk_cfg))
        web_search_overlap_cfg = {"name": "context7", "source": {"overlaps_native": ["web_search"], "risk_level": "low"}}
        self.assertTrue(requires_manual_tool_selection("context7", web_search_overlap_cfg))
        self.assertEqual(unique_server_name("Playwright MCP", {"playwright-mcp"}), "playwright-mcp-2")

    def test_marketplace_search_filters_unrelated_live_results(self) -> None:
        import mcp_client.marketplace as marketplace
        importlib.reload(marketplace)

        live_results = [
            marketplace.MarketplaceEntry(
                id="unrelated",
                name="Static Site Builder",
                description="Build websites with agents.",
                source="glama",
            ),
            marketplace.MarketplaceEntry(
                id="github-tools",
                name="GitHub MCP",
                description="Manage repositories, issues, and pull requests.",
                source="glama",
            ),
        ]
        with patch.object(marketplace, "_glama_search", return_value=live_results):
            result = marketplace.search_marketplace_with_status("github", sources=["glama"], limit=10)

        self.assertEqual(result.mode, "live")
        self.assertEqual([entry.id for entry in result.entries], ["github-github-mcp-server", "github-tools"])
        self.assertEqual(result.source_counts, {"curated": 1, "glama": 1})

    def test_marketplace_search_uses_curated_when_live_source_ignores_query(self) -> None:
        import mcp_client.marketplace as marketplace
        importlib.reload(marketplace)

        ignored_query_results = [
            marketplace.MarketplaceEntry(
                id="statalog",
                name="Stata MCP",
                description="Controls Stata through automation.",
                source="glama",
            )
        ]
        with patch.object(marketplace, "_glama_search", return_value=ignored_query_results), \
             patch.object(marketplace, "_load_cache", return_value=[]):
            result = marketplace.search_marketplace_with_status("playwright", sources=["glama"], limit=10)

        self.assertEqual(result.mode, "curated")
        self.assertEqual([entry.name for entry in result.entries], ["Playwright MCP"])

    def test_marketplace_search_uses_directory_page_fallback(self) -> None:
        import mcp_client.marketplace as marketplace
        importlib.reload(marketplace)

        html = """
        <html><body>
          <a href="/servers/example-filesystem">
            <article>
              <h2>Example Filesystem MCP</h2>
              <p>Read and write local filesystem data through MCP.</p>
            </article>
          </a>
                    <a href="/servers/example-filesystem-icon">
                        <article>
                            <h2>Example Filesystem MCP</h2>
                            <p>Read and write local filesystem data through MCP.</p>
                        </article>
                    </a>
        </body></html>
        """
        with patch.object(marketplace, "_fetch_json", side_effect=RuntimeError("gone")), \
             patch.object(marketplace, "_fetch_text", return_value=html):
            result = marketplace.search_marketplace_with_status("filesystem", sources=["pulsemcp"], limit=10)

        self.assertEqual(result.mode, "live")
        self.assertEqual(result.source_counts, {"curated": 1, "pulsemcp": 1})
        self.assertEqual(len(result.entries), 2)
        self.assertEqual(result.entries[0].id, "modelcontextprotocol-filesystem")
        self.assertEqual(result.entries[1].id, "example-filesystem")
        self.assertEqual(result.entries[1].name, "Example Filesystem MCP")
        self.assertEqual(result.entries[1].source, "pulsemcp")
        self.assertTrue(result.entries[1].metadata["page_fallback"])

    def test_marketplace_search_does_not_match_repository_host_only(self) -> None:
        import mcp_client.marketplace as marketplace
        importlib.reload(marketplace)

        with patch.object(marketplace, "_load_cache", return_value=[]):
            result = marketplace.search_marketplace_with_status("github", sources=["unknown-source"], limit=10)

        self.assertEqual(result.mode, "curated")
        self.assertEqual([entry.id for entry in result.entries], ["github-github-mcp-server"])

    def test_result_normalization_truncates_and_marks_errors(self) -> None:
        from mcp_client.results import normalize_call_result

        result = SimpleNamespace(
            content=[SimpleNamespace(text="abcdef")],
            structuredContent={"ok": True},
            isError=True,
        )
        text = normalize_call_result(result, output_limit=20)
        self.assertTrue(text.startswith("MCP tool error:"))
        self.assertIn("Truncated MCP output", text)

    def test_mcp_array_schema_preserves_items_for_gemini_tools(self) -> None:
        import mcp_client.runtime as runtime

        tool_info = runtime.McpToolInfo(
            server_name="playwright",
            name="browser_fill_form",
            prefixed_name="mcp_playwright_browser_fill_form",
            description="Fill multiple form fields.",
            input_schema={
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "array",
                        "description": "Fields to fill.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "value": {"type": "string"},
                            },
                        },
                    },
                    "modifiers": {"type": "array", "items": {"type": "string"}},
                    "values": {"type": "array"},
                },
            },
            destructive=False,
            requires_approval=False,
            enabled=True,
        )
        schema = runtime._schema_to_model(tool_info).model_json_schema()
        properties = schema["properties"]
        self.assertEqual(properties["fields"]["type"], "array")
        self.assertIn("items", properties["fields"])
        self.assertIn("items", properties["modifiers"])
        self.assertIn("items", properties["values"])

    def test_stdio_command_resolution_handles_missing_launchers(self) -> None:
        self._reload_config()
        import mcp_client.requirements as requirements
        requirements = importlib.reload(requirements)
        import mcp_client.runtime as runtime
        runtime = importlib.reload(runtime)

        command_name = "fake-mcp"
        executable_name = "fake-mcp.cmd" if os.name == "nt" else "fake-mcp"
        executable = Path(self._tmp.name, executable_name)
        executable.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)

        resolved = runtime._resolve_stdio_command(command_name, {"PATH": self._tmp.name})
        self.assertEqual(Path(resolved).name.lower(), executable_name.lower())

        with self.assertRaises(runtime.McpStdioCommandNotFound) as caught:
            runtime._resolve_stdio_command("npx", {"PATH": ""})
        self.assertIn("MCP stdio command 'npx' was not found on PATH", str(caught.exception))
        self.assertIn("Node.js LTS is required", str(caught.exception))

        managed_bin = Path(self._tmp.name, "runtimes", "node", "bin")
        managed_bin.mkdir(parents=True)
        managed_npx = managed_bin / ("npx.cmd" if os.name == "nt" else "npx")
        managed_npx.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
        managed_npx.chmod(0o755)
        requirements._write_manifest("node", {"version": "test", "bin_dir": str(managed_bin), "root": str(managed_bin)})

        env = {"PATH": ""}
        resolved = runtime._resolve_stdio_command("npx", env)
        self.assertEqual(Path(resolved).name.lower(), managed_npx.name.lower())
        self.assertIn(str(managed_bin), env["PATH"])

    def test_runtime_requirements_infer_non_curated_and_install_known_runtimes(self) -> None:
        self._reload_config()
        import mcp_client.requirements as requirements
        requirements = importlib.reload(requirements)

        server_cfg = {"transport": "stdio", "command": "npx", "args": ["-y", "unknown-server"]}
        checks = requirements.check_server_requirements(server_cfg, {"PATH": ""})
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].requirement.id, "node")
        self.assertTrue(checks[0].installable)
        self.assertEqual(checks[0].missing_commands, ("npx",))

        docker_cfg = {"transport": "stdio", "command": "docker", "args": ["run", "example"]}
        docker_check = requirements.check_server_requirements(docker_cfg, {"PATH": ""})[0]
        self.assertEqual(docker_check.requirement.id, "docker")
        self.assertFalse(docker_check.installable)
        self.assertIn("Docker Desktop", docker_check.message)

        with patch.object(requirements, "_install_node", return_value=requirements.RuntimeInstallResult(True, "node", "installed", "bin", "v-test")) as installer:
            result = requirements.install_managed_runtime("node")
        self.assertTrue(result.ok)
        self.assertEqual(result.runtime_id, "node")
        installer.assert_called_once()

        manual = requirements.install_managed_runtime("docker")
        self.assertFalse(manual.ok)
        self.assertIn("manually", manual.message)

        playwright_cfg = {"transport": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp"]}
        playwright_checks = requirements.check_server_requirements(playwright_cfg, {"PATH": ""})
        self.assertEqual([check.requirement.id for check in playwright_checks], ["node", "playwright-chrome"])
        self.assertTrue(playwright_checks[1].installable)

        browsers_dir = Path(requirements.playwright_browsers_path())
        browsers_dir.mkdir(parents=True)
        browser_exe = Path(self._tmp.name, "managed-chromium.exe" if os.name == "nt" else "managed-chromium")
        browser_exe.write_text("ok", encoding="utf-8")
        requirements._write_manifest("playwright-chrome", {"installed": True, "browsers_dir": str(browsers_dir), "executable_path": str(browser_exe)})
        original_process_env = {
            "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
            "PLAYWRIGHT_MCP_EXECUTABLE_PATH": os.environ.get("PLAYWRIGHT_MCP_EXECUTABLE_PATH"),
        }
        base_env = {"PATH": ""}
        env = requirements.apply_managed_runtime_env(playwright_cfg, base_env)
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], str(browsers_dir))
        self.assertEqual(env["PLAYWRIGHT_MCP_EXECUTABLE_PATH"], str(browser_exe))
        self.assertNotIn("PLAYWRIGHT_BROWSERS_PATH", base_env)
        self.assertNotIn("PLAYWRIGHT_MCP_EXECUTABLE_PATH", base_env)
        self.assertEqual(original_process_env["PLAYWRIGHT_BROWSERS_PATH"], os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
        self.assertEqual(original_process_env["PLAYWRIGHT_MCP_EXECUTABLE_PATH"], os.environ.get("PLAYWRIGHT_MCP_EXECUTABLE_PATH"))

        unrelated_cfg = {"transport": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]}
        unrelated_env = requirements.apply_managed_runtime_env(unrelated_cfg, {"PATH": ""})
        self.assertNotIn("PLAYWRIGHT_BROWSERS_PATH", unrelated_env)
        self.assertNotIn("PLAYWRIGHT_MCP_EXECUTABLE_PATH", unrelated_env)

        with patch.object(requirements, "_install_playwright_chrome", return_value=requirements.RuntimeInstallResult(True, "playwright-chrome", "installed", str(browsers_dir), "chromium")) as browser_installer:
            browser_result = requirements.install_managed_runtime("playwright-chrome")
        self.assertTrue(browser_result.ok)
        browser_installer.assert_called_once()

    def test_playwright_chrome_install_handles_empty_process_output(self) -> None:
        self._reload_config()
        import mcp_client.requirements as requirements
        requirements = importlib.reload(requirements)

        npx_path = str(Path(self._tmp.name, "npx.cmd" if os.name == "nt" else "npx"))
        completed = subprocess.CompletedProcess([npx_path], 1, stdout=None, stderr=None)
        with patch.object(requirements, "resolve_command", return_value=(npx_path, {}, None)), patch.object(requirements.subprocess, "run", return_value=completed):
            result = requirements.install_managed_runtime("playwright-chrome")

        self.assertFalse(result.ok)
        self.assertIn("Playwright install exited with code 1", result.message)

    def test_playwright_browser_install_uses_user_space_chromium(self) -> None:
        self._reload_config()
        import mcp_client.requirements as requirements
        requirements = importlib.reload(requirements)

        npx_path = str(Path(self._tmp.name, "npx.cmd" if os.name == "nt" else "npx"))

        def _fake_run(command, **kwargs):
            browsers_dir = Path(kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"])
            system = requirements.platform.system().lower()
            if system == "windows":
                browser_exe = browsers_dir / "chromium-1234" / "chrome-win" / "chrome.exe"
            elif system == "darwin":
                browser_exe = browsers_dir / "chromium-1234" / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
            else:
                browser_exe = browsers_dir / "chromium-1234" / "chrome-linux" / "chrome"
            browser_exe.parent.mkdir(parents=True, exist_ok=True)
            browser_exe.write_text("ok", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

        with patch.object(requirements, "resolve_command", return_value=(npx_path, {}, None)), patch.object(requirements.subprocess, "run", side_effect=_fake_run) as run_mock:
            result = requirements.install_managed_runtime("playwright-chrome")

        self.assertTrue(result.ok)
        self.assertEqual(result.version, "chromium")
        run_command = run_mock.call_args.args[0]
        self.assertEqual(run_command[-2:], ["install", "chromium"])
        self.assertEqual(requirements._read_manifest("playwright-chrome")["browser"], "chromium")
        self.assertTrue(Path(requirements.playwright_browser_executable_path()).exists())

    def test_settings_rows_include_configured_tools_without_live_catalog(self) -> None:
        from ui.mcp_settings import _OPEN_SERVER_EXPANSIONS, _display_tool_rows, _has_pending_enabled_servers, _requirement_label, _schema_summary, _server_expansion_default_open, _set_server_expansion_open
        from mcp_client.requirements import requirements_for_server

        server_cfg = {
            "tools": {
                "enabled": {"echo": True, "delete_note": False},
                "require_approval": ["delete_note"],
                "catalog": {
                    "echo": {
                        "description": "Echo a message back to the caller.",
                        "destructive": False,
                        "requires_approval": False,
                        "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}},
                    },
                    "delete_note": {
                        "description": "Delete a saved note.",
                        "destructive": True,
                        "requires_approval": True,
                    },
                },
            }
        }
        rows = _display_tool_rows(server_cfg, [])
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(set(by_name), {"echo", "delete_note"})
        self.assertTrue(by_name["echo"]["enabled"])
        self.assertEqual(by_name["echo"]["description"], "Echo a message back to the caller.")
        self.assertEqual(by_name["echo"]["input_schema"]["properties"]["message"]["type"], "string")
        self.assertFalse(by_name["delete_note"]["enabled"])
        self.assertEqual(by_name["delete_note"]["description"], "Delete a saved note.")
        self.assertTrue(by_name["delete_note"]["destructive"])
        self.assertTrue(by_name["delete_note"]["requires_approval"])
        self.assertTrue(all(row["configured_only"] for row in rows))
        self.assertEqual(_schema_summary(by_name["echo"]["input_schema"]), "Inputs: message: string")
        self.assertTrue(_has_pending_enabled_servers({"manual": {"enabled": True}}, {"manual": {"status": "connecting"}}))
        self.assertTrue(_has_pending_enabled_servers({"manual": {"enabled": True}}, {"manual": {"status": "not_started"}}))
        self.assertFalse(_has_pending_enabled_servers({"manual": {"enabled": True}}, {"manual": {"status": "connected"}}))
        self.assertFalse(_has_pending_enabled_servers({"manual": {"enabled": False}}, {"manual": {"status": "connecting"}}))

        _OPEN_SERVER_EXPANSIONS.clear()
        self.assertFalse(_server_expansion_default_open("manual"))
        _set_server_expansion_open("manual", True)
        self.assertTrue(_server_expansion_default_open("manual"))
        self.assertIn("manual", _OPEN_SERVER_EXPANSIONS)
        _set_server_expansion_open("manual", False)
        self.assertFalse(_server_expansion_default_open("manual"))
        self.assertNotIn("manual", _OPEN_SERVER_EXPANSIONS)

        req = requirements_for_server({"transport": "stdio", "command": "npx"})[0]
        self.assertIn("Requires Node.js LTS", _requirement_label(req))

    def test_stdio_server_discovers_and_calls_dynamic_tool(self) -> None:
        cfg = self._reload_config()
        import mcp_client.runtime as runtime

        if not runtime.sdk_available():
            self.skipTest("mcp SDK is not installed")

        server_script = Path(self._tmp.name, "stdio_echo_server.py")
        server_script.write_text(textwrap.dedent("""
            from mcp.server.fastmcp import FastMCP

            mcp = FastMCP("Thoth Test MCP")


            @mcp.tool()
            def echo(message: str) -> str:
                return f"echo:{message}"


            if __name__ == "__main__":
                mcp.run("stdio")
        """).strip() + "\n", encoding="utf-8")

        next_config = cfg.load_config()
        next_config["enabled"] = True
        next_config["servers"]["local"] = cfg.normalize_server_config("local", {
            "enabled": True,
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(server_script)],
            "connect_timeout": 10,
            "tool_timeout": 10,
        })
        cfg.save_config(next_config)

        runtime.shutdown()
        runtime = importlib.reload(runtime)
        runtime.discover_enabled_servers()

        deadline = time.monotonic() + 10
        summary = runtime.get_status_summary()
        while time.monotonic() < deadline:
            summary = runtime.get_status_summary()
            if summary["connected_server_count"] == 1 and summary["tool_count"] == 1:
                break
            server_status = summary["servers"].get("local", {})
            if server_status.get("status") in {"failed", "dependency_missing"}:
                self.fail(f"MCP server failed to start: {server_status}")
            time.sleep(0.1)
        else:
            self.fail(f"Timed out waiting for MCP discovery: {summary}")

        tools = {tool.name: tool for tool in runtime.get_langchain_tools()}
        self.assertIn("mcp_local_echo", tools)
        output = tools["mcp_local_echo"].invoke({"message": "hello"})
        self.assertIn("echo:hello", output)
        self.assertIn("STRUCTURED_CONTENT", output)
        self.assertEqual(runtime.get_destructive_tool_names(), set())

        cfg.set_tool_enabled("local", "echo", False)
        self.assertNotIn("mcp_local_echo", {tool.name for tool in runtime.get_langchain_tools()})
        self.assertEqual(runtime.get_status_summary()["enabled_tool_count"], 0)

        cfg.set_tool_enabled("local", "echo", True)
        self.assertIn("mcp_local_echo", {tool.name for tool in runtime.get_langchain_tools()})
        self.assertEqual(runtime.get_status_summary()["enabled_tool_count"], 1)

        cfg.set_tool_requires_approval("local", "echo", True)
        self.assertIn("mcp_local_echo", runtime.get_destructive_tool_names())

        cfg.set_tool_requires_approval("local", "echo", False)
        self.assertEqual(runtime.get_destructive_tool_names(), set())

    def test_background_allow_all_runs_mcp_destructive_tool_without_interrupt_gate(self) -> None:
        import agent
        from langchain_core.tools import StructuredTool

        interrupt_calls: list[dict] = []
        captured_tools: dict[str, object] = {}

        def _dangerous() -> str:
            return "ran"

        def _make_mcp_parent(tool):
            return SimpleNamespace(
                as_langchain_tools=lambda: [tool],
                destructive_tool_names={"mcp_manual_delete_note"},
            )

        def _build_graph_and_call(mode: str, tool) -> str:
            captured_tools.clear()

            def _capture_agent(*, tools, **kwargs):
                for item in tools:
                    captured_tools[item.name] = item
                return SimpleNamespace(tools=tools)

            agent.clear_agent_cache()
            bg_token = agent._background_workflow_var.set(True)
            mode_token = agent._safety_mode_var.set(mode)
            try:
                with patch.object(agent.tool_registry, "get_tool", return_value=_make_mcp_parent(tool)), \
                     patch.object(agent, "get_llm", return_value=object()), \
                     patch.object(agent, "get_current_model", return_value="test-model"), \
                     patch.object(agent, "get_context_size", return_value=8192), \
                     patch.object(agent, "get_agent_system_prompt", return_value="test prompt"), \
                     patch.object(agent, "create_react_agent", side_effect=_capture_agent), \
                     patch.object(agent, "interrupt", side_effect=lambda payload: interrupt_calls.append(payload) or True):
                    agent.get_agent_graph(["mcp"])
                    return captured_tools["mcp_manual_delete_note"].func()
            finally:
                agent._safety_mode_var.reset(mode_token)
                agent._background_workflow_var.reset(bg_token)
                agent.clear_agent_cache()

        allow_all_tool = StructuredTool.from_function(
            func=_dangerous,
            name="mcp_manual_delete_note",
            description="Delete a note through MCP.",
        )
        self.assertEqual(_build_graph_and_call("allow_all", allow_all_tool), "ran")
        self.assertEqual(interrupt_calls, [])

        approve_tool = StructuredTool.from_function(
            func=_dangerous,
            name="mcp_manual_delete_note",
            description="Delete a note through MCP.",
        )
        self.assertEqual(_build_graph_and_call("approve", approve_tool), "ran")
        self.assertEqual(len(interrupt_calls), 1)
        self.assertEqual(interrupt_calls[0]["tool"], "mcp_manual_delete_note")

    def test_mcp_dynamic_tool_display_name_uses_actual_tool(self) -> None:
        import agent

        with patch("mcp_client.runtime.get_catalog_snapshot", return_value={
            "microsoft-learn-mcp": [
                {
                    "prefixed_name": "mcp_microsoft_learn_mcp_microsoft_docs_search",
                    "name": "microsoft_docs_search",
                }
            ]
        }):
            label = agent._resolve_tool_display_name("mcp_microsoft_learn_mcp_microsoft_docs_search")

        self.assertEqual(label, "MCP: microsoft_docs_search (microsoft-learn-mcp)")

    def test_mcp_browser_outputs_use_browser_loop_controls(self) -> None:
        import agent

        messages = [HumanMessage(content="use playwright mcp to browse a shopping site")]
        for index in range(max(agent._keep_browser_snapshots() + 2, 10)):
            messages.append(AIMessage(content="", tool_calls=[{
                "id": f"call-{index}",
                "name": "mcp_playwright_mcp_browser_snapshot",
                "args": {},
            }]))
            messages.append(ToolMessage(
                content=f"URL: https://example.test/{index}\nTitle: Page {index}\n" + ("item\n" * 200),
                name="mcp_playwright_mcp_browser_snapshot",
                tool_call_id=f"call-{index}",
            ))

        with patch.object(agent, "get_context_size", return_value=120000), \
             patch.object(agent, "is_background_workflow", return_value=False):
            result = agent._pre_model_trim({"messages": messages})["llm_input_messages"]

        tool_texts = [msg.content for msg in result if getattr(msg, "type", "") == "tool"]
        self.assertTrue(any("[Prior browser snapshot" in text for text in tool_texts))
        system_text = "\n".join(str(msg.content) for msg in result if getattr(msg, "type", "") == "system")
        self.assertIn("Stop browsing now", system_text)

        self.assertTrue(agent._is_browser_tool_name("mcp_playwright_mcp_browser_take_screenshot"))
        self.assertEqual(agent._browser_action_name("mcp_playwright_mcp_browser_take_screenshot"), "take_screenshot")

    def test_thoth_status_mcp_tool_toggle_controls_global_client(self) -> None:
        cfg = self._reload_config()
        import mcp_client.runtime as runtime
        runtime = importlib.reload(runtime)
        import tools.mcp_tool  # noqa: F401 - registers the MCP parent tool
        from tools import registry as tool_registry
        from tools.thoth_status_tool import _update_setting

        with patch.object(runtime, "discover_enabled_servers") as discover_mock:
            cfg.set_global_enabled(True)

        self.assertTrue(cfg.is_globally_enabled())
        self.assertTrue(tool_registry.is_enabled("mcp"))
        discover_mock.assert_called_once()

        with patch("langgraph.types.interrupt", return_value=True), \
             patch.object(runtime, "shutdown") as shutdown_mock:
            result = _update_setting("tool_toggle", "External MCP Tools:off")

        self.assertIn("MCP client and tool 'External MCP Tools' disabled", result)
        self.assertFalse(cfg.is_globally_enabled())
        self.assertFalse(tool_registry.is_enabled("mcp"))
        shutdown_mock.assert_called_once()

        with patch("langgraph.types.interrupt", return_value=True), \
             patch.object(runtime, "discover_enabled_servers") as discover_mock:
            result = _update_setting("tool_toggle", "mcp:on")

        self.assertIn("MCP client and tool 'External MCP Tools' enabled", result)
        self.assertTrue(cfg.is_globally_enabled())
        self.assertTrue(tool_registry.is_enabled("mcp"))
        discover_mock.assert_called_once()

    def test_bad_stdio_server_reports_failure_without_tools(self) -> None:
        cfg = self._reload_config()
        import mcp_client.runtime as runtime

        if not runtime.sdk_available():
            self.skipTest("mcp SDK is not installed")

        next_config = cfg.load_config()
        next_config["enabled"] = True
        next_config["servers"]["broken"] = cfg.normalize_server_config("broken", {
            "enabled": True,
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-c", "import sys; sys.exit(3)"],
            "connect_timeout": 5,
            "tool_timeout": 5,
        })
        cfg.save_config(next_config)

        runtime.shutdown()
        runtime = importlib.reload(runtime)
        runtime.discover_enabled_servers()

        deadline = time.monotonic() + 10
        summary = runtime.get_status_summary()
        while time.monotonic() < deadline:
            summary = runtime.get_status_summary()
            server_status = summary["servers"].get("broken", {})
            if server_status.get("status") == "failed":
                break
            time.sleep(0.1)
        else:
            self.fail(f"Timed out waiting for MCP failure status: {summary}")

        server_status = summary["servers"]["broken"]
        self.assertEqual(server_status["status"], "failed")
        self.assertTrue(server_status["last_error"])
        self.assertEqual(runtime.get_langchain_tools(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)