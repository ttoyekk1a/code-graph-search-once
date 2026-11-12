"""Contracts for the one-anchor packet, including its original witness rows."""
import hashlib
import importlib.util
import json
from collections import defaultdict, deque
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from code_graph_search_once import AllPathsSession, GraphRuntime, decode_neighbors, get_neighbors_schema


ROOT = Path(__file__).resolve().parents[1]


def expected_witnesses(rows, anchor, direction, types, max_hops):
    """Traverse raw oriented rows, recording the first eligible row for each pair."""
    adjacency = defaultdict(dict)
    for index, row in enumerate(rows):
        if types is not None and row.get('type') not in types:
            continue
        parent, child = row['source'], row['target']
        if direction == 'upstream':
            parent, child = child, parent
        adjacency[parent].setdefault(child, index)
    pending = deque([(anchor, 0)])
    seen = {anchor}
    result = []
    while pending:
        parent, distance = pending.popleft()
        if distance == max_hops:
            continue
        for node, index in sorted(adjacency[parent].items()):
            if node not in seen:
                seen.add(node)
                result.append((node, distance + 1, parent, index))
                pending.append((node, distance + 1))
    return result


class NeighborTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'graph.json'

    def write_graph(self, rows, *, nodes=None, field='edges'):
        if nodes is None:
            ids = {row[key] for row in rows for key in ('source', 'target')}
            nodes = [{'id': node} for node in sorted(ids)]
        self.path.write_text(json.dumps({'nodes': nodes, field: rows}), encoding='utf-8')

    def make_fanout(self):
        rows = []
        for i in range(350):
            node = 'pkg.node_%03d_' % i + 'x' * 100
            rows.extend([{'source': 'root', 'target': node, 'type': 'calls', 'line': i},
                         {'source': node, 'target': 'sink', 'type': 'returns'}])
        self.write_graph(rows)

    def test_rows_and_shortest_distances_against_independent_traversal(self):
        rows = [
            {'source': 'lib.Ω', 'target': 'lib.a', 'type': 'calls', 'attrs': {'v': [False]}},
            {'source': 'lib.Ω', 'target': 'lib.b', 'type': 'tags', 'attrs': {'v': [0]}},
            {'source': 'lib.Ω', 'target': 'lib.a', 'type': 'tags', 'attrs': {'v': [0.0]}},
            {'source': 'lib.b', 'target': 'lib.c', 'type': 'calls'},
            {'source': 'lib.a', 'target': 'lib.c', 'type': 'calls', 'extra': {'nested': ['δ', None]}},
            {'source': 'lib.c', 'target': 'lib.Ω', 'type': 'calls'},
            {'source': 'lib.Ω', 'target': 'lib.Ω', 'type': 'calls'},
            {'source': 'lib.d', 'target': 'lib.Ω', 'type': 'calls'},
            {'source': 'lib.a', 'target': 'lib.b', 'attrs': {'v': []}},
        ]
        ids = ['lib.a', 'lib.b', 'lib.c', 'lib.d', 'lib.Ω']
        nodes = [{'id': n, 'name': n + 'Δ'} for n in ids]
        for field in ['edges', 'links']:
            self.write_graph(rows, nodes=nodes, field=field)
            with GraphRuntime(self.path) as graph:
                for direction in ['upstream', 'downstream']:
                    for types in [None, [], ['CALLS'], ['tags'], ['calls'], ['calls', 'tags']]:
                        with self.subTest(field=field, direction=direction, types=types):
                            args = dict(direction=direction, relation_types=types, max_hops=2)
                            packet = graph.graph_neighbors('lib.Ω', **args)
                            self.assertTrue(packet['complete'])
                            candidates = decode_neighbors(json.loads(json.dumps(packet)))['candidates']
                            observed = [(c['node_id'], c['distance'], c['via']['parent_node_id'], c['via']['row_index'])
                                        for c in candidates]
                            self.assertEqual(observed, expected_witnesses(rows, 'lib.Ω', direction, types, 2))
                            for candidate in candidates:
                                via = candidate['via']
                                # JSON text comparison distinguishes boolean, integer and float metadata.
                                self.assertEqual(json.dumps(via['row'], sort_keys=True),
                                                 json.dumps(rows[via['row_index']], sort_keys=True))
                                self.assertIsNone(candidate['source_location'])
                                self.assertEqual(candidate['symbol'], candidate['node_id'] + 'Δ')

    def test_chain_bounds_and_exact_anchors(self):
        self.write_graph([{'source': 'pkg.n%d' % i, 'target': 'pkg.n%d' % (i+1)}
                          for i in range(17)])
        with GraphRuntime(self.path) as graph:
            self.path.unlink()  # All subsequent queries use the loaded snapshot.
            for bound in [None, 8, 16]:
                args = {} if bound is None else {'max_hops': bound}
                packet = graph.graph_neighbors('pkg.n0', **args)
                expected = bound or 4
                self.assertTrue(packet['complete'])
                self.assertEqual(packet['returned_count'], expected)
                self.assertEqual([c['distance'] for c in decode_neighbors(packet)['candidates']],
                                 list(range(1, expected + 1)))
            for anchor in ['n0', 'missing']:
                packet = graph.graph_neighbors(anchor)
                self.assertEqual(packet['anchor_resolution']['status'], 'NodeNotInGraph')
                self.assertFalse(packet['complete'])
                self.assertEqual(decode_neighbors(packet)['candidates'], [])

    def test_argument_contract(self):
        schema = get_neighbors_schema()['parameters']
        self.assertEqual(schema['required'], ['anchor_node'])
        self.assertEqual(schema['properties']['max_hops']['maximum'], 16)
        self.assertEqual(schema['properties']['output_limit']['default'], 12_000)
        with GraphRuntime(ROOT/'example_graph.json') as graph:
            for name, values in [('max_hops', [0, 17, True, 4.0]),
                                 ('direction', ['reverse', 'undirected', None]),
                                 ('output_limit', [4095, True, 12000.0]),
                                 ('relation_types', ['calls', ('calls',), [''], [0]])]:
                for value in values:
                    with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                        graph.graph_neighbors('a', **{name: value})
            for anchor in [None, '']:
                with self.assertRaises(ValueError):
                    graph.graph_neighbors(anchor)

    def test_independent_output_budgets_and_delivered_prefix(self):
        self.make_fanout()
        with GraphRuntime(self.path, output_limit=32_000) as graph:
            path_before = graph.query(['root'], ['sink'], 2)
            self.assertGreater(len(json.dumps(path_before)), 12_000)
            packets = []
            for budget in [4096, 12_000, 32_000]:
                packet = graph.graph_neighbors('root', output_limit=budget)
                self.assertLessEqual(len(json.dumps(packet)), budget)
                self.assertTrue(packet['search_complete'])
                self.assertFalse(packet['output_complete'])
                self.assertFalse(packet['complete'])
                self.assertEqual(packet['truncation'], ['output_limit'])
                self.assertEqual(packet['candidate_count_exact'], 351)
                self.assertEqual(packet['omitted_candidates'], 351 - packet['returned_count'])
                packets.append(packet)
            for small, large in zip(packets, packets[1:]):
                count = small['returned_count']
                self.assertLess(count, large['returned_count'])
                self.assertEqual(decode_neighbors(small)['candidates'], decode_neighbors(large)['candidates'][:count])
            self.assertEqual(graph.graph_neighbors('root'), packets[1])
            self.assertEqual(graph.query(['root'], ['sink'], 2), path_before)

    def test_oversize_query_header_is_decodable(self):
        with GraphRuntime(ROOT/'example_graph.json') as graph:
            for anchor, types, status in [('Ω'*20_000, None, 'NodeNotInGraph'),
                                          ('a', ['stored', 'x'*30_000], 'exact')]:
                with self.subTest(status=status):
                    packet = graph.graph_neighbors(anchor, relation_types=types, output_limit=4096)
                    self.assertLessEqual(len(json.dumps(packet)), 4096)
                    self.assertTrue(packet['query_details_omitted'])
                    self.assertEqual(packet['anchor_resolution'], {'status': status})
                    self.assertFalse(packet['complete'])
                    self.assertIn('output_limit', packet['truncation'])
                    self.assertEqual(decode_neighbors(packet)['candidates'], [])

    def test_edge_and_candidate_limits_interrupt_search(self):
        self.write_graph([{'source': 'a', 'target': n} for n in 'bcd'])
        with GraphRuntime(self.path, max_edge_rows=1) as graph:
            packet = graph.graph_neighbors('a')
            self.assertEqual(packet['work']['edge_rows_examined'], 1)
            self.assertEqual(packet['truncation'], ['edge_row_limit'])
            self.assertFalse(packet['search_complete'])
            self.assertTrue(packet['output_complete'])
            self.assertIsNone(packet['candidate_count_exact'])
            self.assertFalse(packet['complete'])
        # Candidate count is an internal search guard. Exercise it on the real session.
        source = ROOT/'variants/graph-neighbors/tool.py'
        spec = importlib.util.spec_from_file_location('_neighbor_limit_test', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        session = AllPathsSession(self.path, hashlib.sha256(self.path.read_bytes()).hexdigest())
        packet = module.NeighborQuery(session, max_candidates=1).graph_neighbors('a', 'downstream')
        self.assertEqual(packet['truncation'], ['candidate_limit'])
        self.assertFalse(packet['search_complete'])
        self.assertTrue(packet['output_complete'])
        self.assertFalse(packet['complete'])
        self.assertEqual(packet['returned_count'], 1)
        self.assertEqual(packet['candidate_count_lower_bound'], 2)

    def test_cli_raw_decoding_filters_and_independent_budget(self):
        self.make_fanout()
        command = [sys.executable, '-m', 'code_graph_search_once.cli', '--graph', str(self.path),
                   '--output-limit', '4096', 'neighbors', '--anchor', 'root']
        with GraphRuntime(self.path) as graph:
            default = graph.graph_neighbors('root')
            filtered = graph.graph_neighbors('root', max_hops=1, relation_types=['calls', 'returns'],
                                             output_limit=32_000)
        args = ['--max-hops', '1', '--relation-type', 'calls', '--relation-type', 'returns',
                '--output-limit', '32000']
        for extra, expected in [([], default), (args, filtered), (args + ['--decode'], decode_neighbors(filtered))]:
            result = subprocess.run(command + extra, cwd=ROOT, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, json.dumps(expected) + '\n')
        invalid = subprocess.run(command + ['--max-hops', '17'], cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertEqual(invalid.stdout, '')
        self.assertTrue(invalid.stderr.strip())


if __name__ == '__main__':
    unittest.main()
