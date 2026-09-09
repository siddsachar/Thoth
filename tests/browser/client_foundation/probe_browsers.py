"""Probe already-installed browser engines in a fresh private environment."""
from __future__ import annotations

import json
import argparse
import secrets
import subprocess
import sys
import time
import uuid

from run_browser import EVIDENCE, ROOT, private_environment, stop_owned


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("chromium", "firefox", "webkit", "msedge"))
    options = parser.parse_args()
    suffix = uuid.uuid4().hex[:8]
    short = ROOT / ".tmp" / ("p2pb-" + suffix)
    env = private_environment(short, 0, secrets.token_urlsafe(32))
    module = (ROOT / "frontend/node_modules/playwright/index.mjs").as_uri()
    engines = [("chromium", None), ("firefox", None), ("webkit", None), ("chromium", "msedge")]
    if options.engine:
        engines = [(engine, channel) for engine, channel in engines
                   if (channel or engine) == options.engine]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    results = []
    for engine, channel in engines:
        script = "import { " + engine + " as launcher } from " + json.dumps(module) + ";\n"
        script += "const engine = " + json.dumps(engine) + "; const channel = " + json.dumps(channel) + ";\n"
        script += """
  let browser;
  try {
    browser = await launcher.launch({headless: true, channel: channel ?? undefined, timeout: 15000,
      ...(engine === 'chromium' ? {args: ['--disable-background-networking', '--disable-component-update']} : {})});
    console.log(JSON.stringify({engine, channel: channel ?? 'bundled', status: 'available', version: browser.version()}));
  } catch (error) {
    console.log(JSON.stringify({engine, channel: channel ?? 'bundled', status: 'unavailable',
      reason: String(error.message).split('\\n').slice(0, 2).join(' ')}));
  } finally { if (browser) await browser.close(); }
"""
        child = subprocess.Popen(["node", "--input-type=module", "-e", script], cwd=ROOT / "frontend",
                             env=env, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             creationflags=flags)
        try:
            stdout, stderr = child.communicate(timeout=30)
            result = {**json.loads(stdout), "exit_code": child.returncode, "stderr": stderr}
        except subprocess.TimeoutExpired as error:
            result = {"engine": engine, "channel": channel or "bundled", "status": "timed_out",
                      "error": "Owned engine probe exceeded 30 seconds", "partial_stdout":
                      (error.stdout or b"").decode("utf-8", errors="replace")}
        except Exception as error:
            result = {"engine": engine, "channel": channel or "bundled", "status": "probe_error", "error": str(error)}
        finally:
            stop_owned(child)
        results.append(result)
        print(json.dumps(result), flush=True)
    result = {"engines": results}
    output = EVIDENCE / (time.strftime("browser-engines-%Y%m%d-%H%M%S-") + suffix + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(output.relative_to(ROOT).as_posix())
    return 0 if all(item["status"] not in {"timed_out", "probe_error"} for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
