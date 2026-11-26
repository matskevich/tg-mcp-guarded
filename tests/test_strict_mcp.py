import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp_strict import StrictFastMCP


@pytest.mark.asyncio
async def test_sdk1_never_drops_unknown_reply_before_handler():
    calls=[]
    server=StrictFastMCP('legacy-synthetic')
    @server.tool()
    async def send(text:str,dry_run:bool=True)->dict:
        calls.append(text)
        return {'success':True}
    assert (await server.list_tools())[0].inputSchema['additionalProperties'] is False
    with pytest.raises(ToolError,match='unknown_tool_arguments:reply_to_msg_id'):
        await server.call_tool('send',{'text':'synthetic','dry_run':True,'reply_to_msg_id':98})
    assert calls==[]
