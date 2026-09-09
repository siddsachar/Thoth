"""The generated TypeScript decoder consumes the same recorded wire events."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.contract


def test_generated_typescript_decodes_recorded_events_and_rejects_critical_unknown():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable; generated TypeScript execution requires the existing runtime")
    events, commands = [], []
    for path in sorted((ROOT / "contracts/client-platform/v1/fixtures").glob("F-P*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        events.extend(record["value"] for record in document["records"] if record["schema"] == "Event")
        commands.extend(record["value"] for record in document["records"] if record["schema"] == "Command")
    assert events and commands, "Versioned protocol recordings must exist before decoder verification"
    schema = json.loads((ROOT / "contracts/client-platform/v1/schema/Event.schema.json").read_text(encoding="utf-8"))
    assert {event["type"] for event in events} == set(schema["discriminator"]["mapping"])
    module = (ROOT / "contracts/client-platform/v1/typescript/client.ts").as_uri()
    script = """
import { isEvent, isCommand } from MODULE;
let text = '';
for await (const chunk of process.stdin) text += chunk;
const {events, commands} = JSON.parse(text);
for (const event of events) {
  if (!isEvent(event)) throw new Error('valid recorded event rejected: ' + event.type);
  if (isEvent({...event, type: 'critical.unknown'})) throw new Error('unknown critical variant accepted');
  if (isEvent({...event, payload: {...event.payload, unnegotiated: true}})) throw new Error('open payload accepted');
  if (isEvent({...event, source_sequence_start: '99', source_sequence_end: '1'})) throw new Error('reversed sequence accepted');
}
for (const command of commands) {
  if (!isCommand(command)) throw new Error('valid recorded command rejected: ' + command.type);
  if (isCommand({...command, type: 'conversation.unknown'})) throw new Error('unknown command accepted');
}
process.stdout.write(JSON.stringify({events: events.length, commands: commands.length}));
""".replace("MODULE", json.dumps(module))
    result = subprocess.run([node, "--input-type=module", "-e", script],
                            input=json.dumps({"events": events, "commands": commands}),
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"events": len(events), "commands": len(commands)}
