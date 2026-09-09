from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.subsystem
ROOT = Path(__file__).resolve().parents[3]


def _generator():
    spec = importlib.util.spec_from_file_location("client_contract_generator", ROOT / "scripts/generate_client_platform_contracts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_outputs_are_deterministic_and_cover_actual_routes():
    from row_bot.api.v1.routes import create_router
    from row_bot.api.v1.security import ClientSecurity
    generator = _generator()
    outputs = generator.outputs()
    assert outputs == generator.outputs()
    for path, expected in outputs.items():
        assert path.read_text(encoding="utf-8") == expected, str(path)
    actual = {(method.lower(), route.path) for route in create_router(object(), ClientSecurity("fixture")).routes
              for method in route.methods}
    promised = {(method, "/api/v1" + path) for method, path, _, _ in generator.OPERATIONS}
    assert actual == promised
    openapi = json.loads(outputs[ROOT / "contracts/client-platform/v1/schema/openapi.json"])
    for path, operations in openapi["paths"].items():
        for operation in operations.values():
            assert operation["responses"]["200"]["content"], path


def test_generated_event_page_keeps_closed_nested_variant_payloads():
    bundle = _generator().schema_bundle()
    schema = bundle["EventPage"]
    payloads = [definition for definition in schema["$defs"].values()
                if "payload" in definition.get("properties", {})]
    assert len(payloads) >= 8
    for definition in payloads:
        assert definition["additionalProperties"] is False
        payload = definition["properties"]["payload"]
        assert "$ref" in payload
        assert schema["$defs"][payload["$ref"].split("/")[-1]]["additionalProperties"] is False


def test_generated_typescript_runs_without_dependencies_and_rejects_unknown_records():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable; Python/schema checks remain required")
    major = int(subprocess.check_output([node, "--version"], text=True).strip().lstrip("v").split(".")[0])
    if major < 24:
        pytest.skip("Node 24 type stripping is required for the optional TypeScript execution probe")
    source = r'''
import assert from 'node:assert/strict';
const sdk = await import(process.argv[1]);
const id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const command = {command_id:id,client_session_id:id,type:'conversation.stop',expected_revision:'0',payload:{}};
assert(sdk.isCommand(command));
assert(!sdk.isCommand({...command,payload:{hidden_secret:'sentinel'}}));
assert.throws(() => sdk.validateWire('ConversationPage',{items:[],has_more:false,private_path:'sentinel'}));
const proof={client_session_id:id,csrf_token:'x'.repeat(43)};
globalThis.fetch = async (url,options) => {
  assert.equal(options.credentials,'same-origin'); assert.equal(options.cache,'no-store');
  assert.equal(options.headers['X-CSRF-Token'],proof.csrf_token);
  assert(url.endsWith('/api/v1/conversations?limit=50'));
  return {ok:true,json:async()=>({items:[],has_more:false,next_cursor:null})};
};
assert.deepEqual(await sdk.listConversations('http://fixture.invalid',proof),{items:[],has_more:false,next_cursor:null});
globalThis.fetch = async () => ({ok:true,json:async()=>({items:[],has_more:false,secret:'sentinel'})});
await assert.rejects(sdk.listConversations('http://fixture.invalid',proof),/protocol_incompatible/);
'''
    completed = subprocess.run([node, "--input-type=module", "-e", source,
                                (ROOT / "contracts/client-platform/v1/typescript/client.ts").as_uri()],
                               text=True, capture_output=True, timeout=30, check=False)
    assert completed.returncode == 0, completed.stderr
