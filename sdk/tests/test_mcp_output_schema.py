import asyncio

from better_agent_sdk import surfaces
from better_agent_sdk.surfaces import OperationSpec, build_mcp_server


def _server():
    def echo(value: str) -> dict:
        return {"value": value}

    return build_mcp_server(
        "test-output-schema",
        (OperationSpec(name="echo", handler=echo, description="Echo a value."),),
        local=True,
    )


def test_tools_list_advertises_no_invalid_output_schema():
    tools = asyncio.run(_server().list_tools())
    assert tools
    for tool in tools:
        assert tool.outputSchema is None, tool.outputSchema


def test_tool_call_still_returns_the_raw_result():
    server = _server()
    result = asyncio.run(server.call_tool("echo", {"value": "x"}))
    content = result[0] if isinstance(result, tuple) else result
    assert '"value"' in content[0].text
    assert '"x"' in content[0].text


def test_broker_preserves_unset_tool_arguments(monkeypatch):
    def switch_model(
        model: str = "",
        provider_id: str = "",
        reasoning_effort: str = "",
    ) -> dict:
        return {}

    specs = (
        OperationSpec(
            name="switch_model",
            handler=switch_model,
            operation="runtime_session_control_switch_model",
        ),
    )
    registry = surfaces.build_registry(specs)
    executor = surfaces._BrokerExecutor(specs, registry)
    executor._generation = "test"
    request_model = surfaces.request_model_for_callable(
        "switch_model",
        switch_model,
    )
    request = request_model(reasoning_effort="none")
    captured = {}

    class Transport:
        def request(self, payload):
            captured.update(payload)
            return {"result": {"success": True}}

    monkeypatch.setattr(surfaces, "RuntimeTransport", Transport)

    asyncio.run(executor.run("switch_model", request))

    assert captured["payload"] == {"reasoning_effort": "none"}
