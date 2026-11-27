"""SDK1 boundary that rejects undeclared fields before model conversion."""
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp_argument_contract import ArgumentContractError, check_arguments, closed_schema


class StrictFastMCP(FastMCP):
    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            tool.inputSchema = closed_schema(tool.inputSchema)
        return tools

    async def call_tool(self, name, arguments):
        tools = {tool.name: tool for tool in await self.list_tools()}
        try:
            if name in tools:
                check_arguments(tools[name].inputSchema, arguments or {})
        except ArgumentContractError as error:
            raise ToolError(str(error)) from error
        return await super().call_tool(name, arguments)
