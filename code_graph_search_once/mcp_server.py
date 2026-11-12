"""MCP stdio transport for path and neighborhood queries."""
import json
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .api import get_neighbors_schema


async def serve(runtime):
    server = Server('code-graph-search-once', version='2.0.0')
    tools = {
        'graph_find_paths': (runtime.schema, runtime.query),
        'graph_neighbors': (get_neighbors_schema(), runtime.graph_neighbors),
    }

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name=schema['name'], description=schema['description'],
                           inputSchema=schema['parameters']) for schema, _ in tools.values()]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            if name not in tools:
                raise ValueError('UnknownTool: ' + name)
            if type(arguments) is not dict:
                raise ValueError('Arguments must be an object')
            # At the wire boundary omission selects the default; null is not an array.
            if name == 'graph_neighbors' and arguments.get('relation_types', []) is None:
                raise ValueError('relation_types must be an array when provided')
            _, query = tools[name]
            packet = query(**arguments)
            return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(packet))])
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            # Native Python calls raise; MCP carries those failures as transport errors.
            return types.CallToolResult(isError=True, content=[types.TextContent(
                type='text', text=json.dumps({'error_type': type(exc).__name__, 'message': str(exc), 'scope': 'transport'}))])

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
