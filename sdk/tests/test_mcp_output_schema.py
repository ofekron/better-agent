import asyncio

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
