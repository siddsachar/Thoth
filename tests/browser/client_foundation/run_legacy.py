"""Run unchanged NiceGUI assertions with new Phase 2 evidence destinations."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import time
import uuid

from run_browser import EVIDENCE, ROOT, fingerprint


def main() -> int:
    source = ROOT / "tests/browser/client_platform/run_browser.py"
    spec = importlib.util.spec_from_file_location("unchanged_legacy_browser", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suffix = uuid.uuid4().hex[:8]
    module.RUN = EVIDENCE / (time.strftime("nicegui-%Y%m%d-%H%M%S-") + suffix)
    module.SHORT = ROOT / ".tmp" / ("p2ng-" + suffix)
    before = fingerprint()
    code = module.main()
    after = fingerprint()
    (Path(module.RUN) / "phase-2-source.json").write_text(json.dumps({
        "source_before": before, "source_after": after, "source_stable": before == after,
        "exit_code": code, "legacy_source": source.relative_to(ROOT).as_posix(),
        "behavior_changes": 0,
    }, indent=2) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
