"""Verify both tool declarations and native packets over a real MCP stdio connection."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from code_graph_search_once import GraphRuntime, get_neighbors_schema, get_tool_schema

ROOT = Path(__file__).resolve().parents[1]


async def check_packet(client, name, arguments, expected):
    result = await client.call_tool(name, arguments)
    assert not result.isError and len(result.content) == 1
    assert result.content[0].type == 'text'
    assert result.content[0].text == json.dumps(expected)


async def main():
    with tempfile.TemporaryDirectory() as temp:
        graph = Path(temp) / 'chain.json'
        graph.write_text(json.dumps({
            'nodes': [{'id': str(i)} for i in range(17)],
            'edges': [{'source': str(i), 'target': str(i + 1), 'type': 'calls',
                       'note': 'x' * 300} for i in range(16)],
        }))
        # Set this to the installed entry point to exercise a wheel outside its checkout.
        executable = os.environ.get('ALL_PATHS_MCP_EXECUTABLE')
        args = ['--graph', str(graph), 'serve']
        params = StdioServerParameters(command=executable or sys.executable,
            args=args if executable else ['-m', 'code_graph_search_once.cli'] + args,
            cwd=temp if executable else str(ROOT))
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                discovered = (await client.list_tools()).tools
                tools = {tool.name: tool for tool in discovered}
                expected_schemas = [get_tool_schema(), get_neighbors_schema()]
                assert len(discovered) == 2 and set(tools) == {'graph_find_paths', 'graph_neighbors'}
                for schema in expected_schemas:
                    tool = tools[schema['name']]
                    assert tool.description == schema['description']
                    assert tool.inputSchema == schema['parameters']

                paths = {'source_nodes': ['0'], 'target_nodes': ['16'], 'max_hops': 16}
                with GraphRuntime(graph) as runtime:
                    path_packet = runtime.query(**paths)
                    assert path_packet['pairs'][0]['returned_count'] == 1 and path_packet['complete']
                    assert len(json.dumps(path_packet)) > 4096
                    await check_packet(client, 'graph_find_paths', paths, path_packet)
                    for query in [
                        {'anchor_node': '0'},
                        {'anchor_node': '0', 'output_limit': 12_000},
                        {'anchor_node': '16', 'direction': 'upstream', 'max_hops': 16,
                         'relation_types': ['calls'], 'output_limit': 4096},
                        {'anchor_node': '0', 'relation_types': []},
                        {'anchor_node': 'absent'},
                    ]:
                        expected = runtime.graph_neighbors(**query)
                        await check_packet(client, 'graph_neighbors', query, expected)
                        if query['anchor_node'] == 'absent':
                            assert expected['anchor_resolution']['status'] == 'NodeNotInGraph'
                    # A small neighborhood budget cannot change the still-open path session.
                    await check_packet(client, 'graph_find_paths', paths, path_packet)

                for name, bad in [
                    ('graph_find_paths', dict(paths, max_hops=True)),
                    ('graph_find_paths', dict(paths, extra=1)),
                    ('graph_neighbors', {}),
                    ('graph_neighbors', {'anchor_node': '0', 'relation_types': None}),
                    ('graph_neighbors', {'anchor_node': '0', 'output_limit': 4095}),
                    ('graph_neighbors', {'anchor_node': '0', 'extra': 1}),
                    ('unknown', {}),
                ]:
                    error = await client.call_tool(name, bad)
                    assert error.isError
                    packet = json.loads(error.content[0].text)
                    assert set(packet) == {'scope', 'error_type', 'message'}
                    assert packet['scope'] == 'transport'
    print(json.dumps({'mcp_verified': True, 'exact_native_schema': True,
                      'native_packet_unchanged': True, 'sixteen_hop_path': True,
                      'neighbors_packet_unchanged': True, 'path_budget_independent': True,
                      'validation_errors_are_transport_errors': True}))


if __name__ == '__main__':
    asyncio.run(main())
