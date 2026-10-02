"""Local schema retrieval regression: no provider calls or ambient memory."""
import copy
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agents.local_tool_budget import retrieve_local_tools, MAX_SCHEMA_BYTES, MAX_TOOLS


def tool(name, description="Opaque capability"):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}}


def catalogue():
    return [tool("self_introspection__list_capabilities"), tool("self_introspection__describe_skill")] + [tool(f"irrelevant_{i}__operation", "Long unrelated schema description " * 100) for i in range(250)] + [tool("files__read", "Read files safely"), tool("files__write", "Write files safely")]


def test_small_arithmetic_keeps_latest_user_policy_and_full_registry():
    messages = [{"role": "system", "content": "POLICY: require actual authorization; never invent tool completion."}, {"role": "user", "content": "17 + 25?"}]
    tools = catalogue()
    before = copy.deepcopy((messages, tools))
    selected_messages, selected = retrieve_local_tools(messages, tools)
    assert len(selected) == 2
    assert len(json.dumps(selected, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_SCHEMA_BYTES
    assert selected_messages[0]["content"].startswith(messages[0]["content"])
    assert selected_messages[-1] == messages[-1]
    assert (messages, tools) == before
    assert "not a measured token count" in selected_messages[0]["content"]


def test_retrieval_from_described_skill_and_forced_tail_tool_preserves_schema():
    tools = catalogue()
    messages = [{"role": "user", "content": "Please write this file"}, {"role": "assistant", "tool_calls": [{"function": {"name": "self_introspection__describe_skill", "arguments": '{"skill_id":"files"}'}}]}, {"role": "tool", "content": "description"}]
    _, selected = retrieve_local_tools(messages, tools, force_tool="files__write")
    assert tools[-1] in selected and tools[-2] in selected
    assert selected[0] == tools[-1]
    assert len(selected) <= MAX_TOOLS
    assert selected[0]["function"]["parameters"]["additionalProperties"] is False


def test_oversized_required_schema_fails_without_dropping_forced_tool():
    with pytest.raises(ValueError, match="Explicit local tool schemas"):
        retrieve_local_tools([{"role": "user", "content": "Do it"}], [tool("required", "x" * 20_000)], force_tool="required")
    with pytest.raises(ValueError, match="not available"):
        retrieve_local_tools([], catalogue(), force_tool="missing")


def test_multibyte_budget_counts_utf8_not_characters_and_preserves_opaque_tools():
    tools = [tool("files__read", "Read 文件 🦦 " * 1000)]
    messages = [{"role": "system", "content": "policy"}, {"role": "user", "content": "Read files"}]
    _, selected = retrieve_local_tools(messages, tools, max_schema_bytes=1000)
    assert selected == []
    assert tools[0]["function"]["description"].endswith("🦦 ")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "stream", "routed", "fallback"])
async def test_real_provider_body_uses_retrieval_on_every_local_http_path(path):
    from agents.llm_provider import LLMProvider
    sent = []
    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        if body.get("stream"):
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"42"}}]}\n\ndata: [DONE]\n\n', headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "42"}}]})
    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = "ollama"
    llm.model = "fixture-local-model"
    llm.base_url = "http://127.0.0.1:11435/v1"
    llm.api_key = "ollama"
    llm._config = {"fallback_providers": []}
    llm._local_engine = None
    llm._budget_check = AsyncMock(return_value=None)
    llm._budget_record = AsyncMock()
    llm.client = httpx.AsyncClient(base_url=llm.base_url, transport=httpx.MockTransport(respond))
    messages = [{"role": "system", "content": "POLICY exact"}, {"role": "user", "content": "17 + 25?"}]
    try:
        if path == "chat":
            result = await llm.chat(messages, catalogue())
            assert not result.get("error")
        elif path == "stream":
            events = [event async for event in llm.chat_stream(messages, catalogue())]
            assert not any(event["type"] == "error" for event in events)
        elif path == "fallback":
            original_client=httpx.AsyncClient
            def isolated_client(*args, **kwargs):
                kwargs["transport"]=httpx.MockTransport(respond)
                return original_client(*args, **kwargs)
            with patch("agents.llm_provider.httpx.AsyncClient",isolated_client):
                result=await llm._call_provider("lmstudio",{"model":"fixture-local-model","api_key":"lm-studio","base_url":"http://127.0.0.1:1234/v1"},messages,catalogue(),temperature=0.7,max_tokens=1024)
            assert not result.get("error")
        else:
            result = await llm._call_provider("ollama", {"model": llm.model}, messages, catalogue(), temperature=0.7, max_tokens=1024)
            assert not result.get("error")
        assert len(sent) == 1 and len(sent[0]["tools"]) == 2
        assert sent[0]["messages"][-1] == messages[-1]
        assert sent[0]["messages"][0]["content"].startswith("POLICY exact")
    finally:
        await llm.client.aclose()


@pytest.mark.asyncio
async def test_cloud_body_not_locally_pruned():
    from agents.llm_provider import LLMProvider
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    llm = LLMProvider.__new__(LLMProvider)
    llm.provider = "deepseek"
    llm.model = "fixture"
    llm.base_url = "https://api.example.test/v1"
    llm.api_key = "fixture"
    llm._config = {"fallback_providers": []}
    llm._local_engine = None
    llm._budget_check = AsyncMock(return_value=None)
    llm._budget_record = AsyncMock()
    llm.client = httpx.AsyncClient(base_url=llm.base_url, transport=httpx.MockTransport(respond))
    try:
        await llm.chat([{"role": "user", "content": "hello"}], catalogue())
        assert len(sent[0]["tools"]) == len(catalogue())
    finally:
        await llm.client.aclose()


def test_complete_local_wire_byte_budget_never_truncates_latest_input_or_policy(caplog):
    from agents.local_tool_budget import validate_local_request
    body = {'messages': [{'role':'system','content':'POLICY exact'}, {'role':'user','content':'秘密' * 6000}], 'tools':[]}
    saved = copy.deepcopy(body)
    with caplog.at_level('INFO', logger='feral.llm'):
        with pytest.raises(ValueError, match='no request was sent'):
            validate_local_request(body, 'ollama')
    assert body == saved
    assert 'history=' in caplog.text and 'tokenizer_verified=false' in caplog.text
    assert '秘密' not in caplog.text and 'POLICY exact' not in caplog.text
    validate_local_request(body, 'openai')  # No cloud behavior change.


@pytest.mark.asyncio
async def test_actual_orchestrator_builder_keeps_identity_policy_but_omits_local_registry_prose():
    from pathlib import Path
    from types import SimpleNamespace as NS
    from agents.identity_loader import IdentityLoader
    from agents.orchestrator import Orchestrator
    from models.skill_manifest import SkillManifest
    from agents.local_tool_budget import validate_local_request
    # Actual checked-in production registry, not synthetic repeated filler.
    manifests=[]
    for path in (Path(__file__).parents[1] / 'skills/manifests').glob('*.json'):
        try: manifests.append(SkillManifest.model_validate_json(path.read_text()))
        except ValueError: pass
    assert len(manifests) >= 40 and sum(len(s.endpoints) for s in manifests) >= 250
    class Frame:
        connected_nodes=[]
        def to_system_context(self): return 'No sensor data available.'
    orch=Orchestrator.__new__(Orchestrator)
    orch.identity_loader=IdentityLoader()
    orch.identity_loader._messaging_channels_section=lambda:''
    orch.identity_loader._build_connected_hardware_section=lambda:''
    orch.skills=NS(skills={s.skill_id:s for s in manifests})
    orch.tool_runner=NS(plan_mode=NS(is_active=lambda sid:False))
    orch._load_identity=lambda:'You are FERAL. USER POLICY: ask before purchases.'
    orch.llm=NS(provider='openai')
    full=await orch._build_system_prompt(Frame(),manifests,'sid',query='17+25')
    orch.llm.provider='ollama'
    lean=await orch._build_system_prompt(Frame(),manifests,'sid',query='17+25')
    assert 'USER POLICY: ask before purchases.' in lean
    assert 'permission_needed' in lean and 'Honest' in lean
    assert 'Available (full catalog)' in full and 'Available (full catalog)' not in lean
    assert 'self_introspection__describe_skill' in lean
    assert len(full.encode()) - len(lean.encode()) > 70_000
    messages,tools=retrieve_local_tools([{'role':'system','content':lean},{'role':'user','content':'17+25'}],catalogue())
    validate_local_request({'messages':messages,'tools':tools},'ollama')
    assert messages[-1] == {'role':'user','content':'17+25'}


@pytest.mark.asyncio
@pytest.mark.parametrize('path',['chat','stream','routed','fallback'])
async def test_oversized_policy_is_refused_before_each_actual_local_http_transport(path):
    from agents.llm_provider import LLMProvider
    sent=[]
    def respond(request):
        sent.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'bad'}}]})
    llm=LLMProvider.__new__(LLMProvider)
    llm.provider='ollama'; llm.model='fixture'; llm.api_key='ollama'
    llm.base_url='http://127.0.0.1:11435/v1'; llm._config={'fallback_providers':[]}
    llm._local_engine=None
    llm._budget_check=AsyncMock(return_value=None); llm._budget_record=AsyncMock()
    llm.client=httpx.AsyncClient(base_url=llm.base_url,transport=httpx.MockTransport(respond))
    messages=[{'role':'system','content':'AUTHORIZATION POLICY '*2000},{'role':'user','content':'17+25'}]
    original=copy.deepcopy(messages)
    try:
        with pytest.raises(ValueError,match='no request was sent'):
            if path=='chat': await llm.chat(messages,tools=None)
            elif path=='stream':
                async for _ in llm.chat_stream(messages,tools=None): pass
            elif path=='routed':
                await llm._call_provider('ollama',{'model':llm.model},messages,None,temperature=.7,max_tokens=1024)
            else:
                original_client=httpx.AsyncClient
                def isolated_client(*args,**kwargs):
                    kwargs['transport']=httpx.MockTransport(respond)
                    return original_client(*args,**kwargs)
                with patch('agents.llm_provider.httpx.AsyncClient',isolated_client):
                    await llm._call_provider('lmstudio',{'model':'fixture','api_key':'lm-studio','base_url':'http://127.0.0.1:1234/v1'},messages,None,temperature=.7,max_tokens=1024)
        assert sent==[] and messages==original
    finally: await llm.client.aclose()
