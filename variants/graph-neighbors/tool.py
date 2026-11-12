"""Single-anchor retrieval over a loaded, hash-checked path session."""
from collections import deque
from copy import deepcopy
import json

DIRECTIONS = {"downstream": "forward", "upstream": "reverse"}

def validate_filters(direction, relation_types):
    """Validate public arguments and normalize a raw row.type allowlist."""
    if direction not in ("forward", "reverse", "undirected"):
        raise ValueError("InvalidDirection: expected forward, reverse or undirected")
    if relation_types is None:
        return None
    if not isinstance(relation_types, list) or any(
        not isinstance(value, str) or not value for value in relation_types
    ):
        raise ValueError("InvalidRelationTypes: expected a list of nonempty type strings")
    return frozenset(relation_types)


def iter_steps(row, direction, relation_types):
    """Yield eligible traversal endpoints; raw evidence remains in stored orientation."""
    if relation_types is not None:
        label = row.get("type")
        if not isinstance(label, str) or label not in relation_types:
            return
    source, target = row["source"], row["target"]
    if direction in ("forward", "undirected"):
        yield source, target
    if direction == "reverse" or (direction == "undirected" and source != target):
        yield target, source


VERSION = 'typed-neighbors-dictionary-v1'


def dictionary_id(value, key, ids, table):
    if key not in ids:
        ids[key] = len(table)
        table.append(value)
    return ids[key]


def broadcast(values):
    if values and all(v == values[0] for v in values):
        return values[0]
    return values


def encode_prefix(candidates, count, anchor):
    namespaces, metadata, location_types = [], [], []
    prefixes, tails, parents, row_indices, attributes, locations, lines = [], [], [], [], [], [], []
    namespace_ids, metadata_ids, location_ids = {}, {}, {}
    node_indices = {anchor: 0}
    other_symbols = []
    for i in range(count):
        candidate = candidates[i]
        node = candidate['node_id']
        prefix, dot, tail = node.rpartition('.')
        namespace = prefix + dot
        prefixes.append(dictionary_id(namespace, namespace, namespace_ids, namespaces))
        tails.append(tail)
        node_indices[node] = i + 1
        if candidate['symbol'] != node:
            other_symbols.append([i + 1, candidate['symbol']])
        via = candidate['via']
        parents.append(node_indices[via['parent_node_id']])
        row_indices.append(via['row_index'])
        attrs = {k: v for k, v in via['row'].items() if k not in ('source', 'target')}
        # Serialized keys distinguish e.g. false, 0 and 0.0 inside arbitrary JSON metadata.
        key = json.dumps(attrs, sort_keys=True, separators=(',', ':'))
        attributes.append(dictionary_id(attrs, key, metadata_ids, metadata))
        loc = candidate['source_location']
        if loc is None:
            locations.append(-1)
            lines.append(0)
        else:
            typ = (loc['path'], loc['kind'])
            locations.append(dictionary_id(list(typ), typ, location_ids, location_types))
            lines.append(loc['line'])
    return dict(namespaces=namespaces, namespace=broadcast(prefixes), name=tails,
                symbol_overrides=other_symbols, parent=broadcast(parents), row_index=row_indices,
                row_metadata=metadata, metadata=broadcast(attributes), location_types=location_types,
                location=broadcast(locations), line=broadcast(lines))


def cell(column, i):
    return column if isinstance(column, int) else column[i]


def decode_packet(packet):
    """Expand a JSON-roundtripped compact P1 packet, including exact witness attributes."""
    if packet.get('version') != VERSION:
        raise ValueError('Unknown compact P1 format')
    result = {key: deepcopy(val) for key, val in packet.items() if key != 'encoding'}
    result['version'] = 'typed-neighbors-v1'
    candidates = result['candidates'] = []
    e = packet['encoding']
    symbols = dict(e['symbol_overrides'])
    upstream = result['direction'] == 'upstream'
    for i, tail in enumerate(e['name']):
        node = e['namespaces'][cell(e['namespace'], i)] + tail
        parent_index = cell(e['parent'], i)
        if not 0 <= parent_index <= i:
            raise ValueError('Invalid witness parent index')
        parent = candidates[parent_index-1] if parent_index else None
        parent_id = parent['node_id'] if parent else result['anchor_resolution']['input']
        source, target = (node, parent_id) if upstream else (parent_id, node)
        row = deepcopy(e['row_metadata'][cell(e['metadata'], i)])
        row.update(source=source, target=target)
        loc_index = cell(e['location'], i)
        location = None
        if loc_index != -1:
            path, kind = e['location_types'][loc_index]
            location = dict(path=path, kind=kind, line=cell(e['line'], i))
        candidates.append(dict(node_id=node, symbol=deepcopy(symbols.get(i+1, node)),
                               distance=parent['distance']+1 if parent else 1,
                               source_location=location,
                               via=dict(parent_node_id=parent_id, row_index=e['row_index'][i], row=row)))
    if len(candidates) != result['returned_count']:
        raise ValueError('Invalid returned candidate count')
    return result


class NeighborQuery:
    """Local state for one query. The validated graph and raw rows are read-only."""
    def __init__(self, session, output_limit=12_000, *, max_candidates=20_000):
        if type(output_limit) is not int or output_limit < 4096:
            raise ValueError("output_limit must be an integer >= 4096")
        if type(max_candidates) is not int or max_candidates < 0:
            raise ValueError("max_candidates must be a nonnegative integer")
        self._graph = session._graph
        self._rows = session._rows
        self._limits = session._limits
        self._output_limit = output_limit
        self._candidate_limit = max_candidates

    def _search(self, anchor, orientation, allowed, max_hops):
        nodes = self._graph._nodes
        seen = {anchor}
        frontier = deque([(anchor, 0)])
        candidates = []
        examined = 0
        while frontier:
            parent, depth = frontier.popleft()
            for neighbor, relation in self._graph._adjacency[parent]:
                if neighbor in seen:
                    continue
                for indexed in self._graph._relations[relation]:
                    if examined == self._limits['edge_rows_examined']:
                        return candidates, examined, 'edge_row_limit'
                    examined += 1
                    index = indexed['index']
                    row = self._rows[index]
                    if (parent, neighbor) not in iter_steps(row, orientation, allowed):
                        continue
                    if len(candidates) == self._candidate_limit:
                        return candidates, examined, 'candidate_limit'
                    seen.add(neighbor)
                    distance = depth + 1
                    candidates.append(dict(node_id=neighbor, symbol=nodes[neighbor].get('name') or neighbor,
                                           distance=distance, source_location=None,
                                           via=dict(parent_node_id=parent, row_index=index, row=row)))
                    if distance < max_hops:
                        frontier.append((neighbor, distance))
                    break
        return candidates, examined, None

    def graph_neighbors(self, anchor_node, direction, max_hops=4, relation_types=None):
        if not isinstance(anchor_node, str) or not anchor_node:
            raise ValueError('anchor_node must be a nonempty exact node ID')
        if not isinstance(direction, str) or direction not in DIRECTIONS:
            raise ValueError('direction must be upstream or downstream')
        if type(max_hops) is not int or not 1 <= max_hops <= 16:
            raise ValueError('max_hops must be an integer in 1..16')
        orientation = DIRECTIONS[direction]
        allowed = validate_filters(orientation, relation_types)
        resolved = anchor_node in self._graph._nodes
        candidates, examined, reason = [], 0, None
        if resolved:
            candidates, examined, reason = self._search(anchor_node, orientation, allowed, max_hops)
        retained = len(candidates)
        searched = resolved and reason is None
        lower = retained + int(reason == 'candidate_limit') if resolved else None
        header = dict(version=VERSION, graph_sha256=self._graph.graph_sha256,
                      semantics='bounded_stored_edge_reachability',
                      anchor_resolution=dict(input=anchor_node, status='exact' if resolved else 'NodeNotInGraph'),
                      direction=direction, max_hops=max_hops,
                      relation_types=sorted(allowed) if allowed is not None else None,
                      query_details_omitted=False, candidate_count_lower_bound=lower,
                      candidate_count_exact=retained if searched else None,
                      work=dict(edge_rows_examined=examined, candidates_retained=retained),
                      search_complete=searched)

        def make_packet(count):
            output_complete = count == retained and not header['query_details_omitted']
            reasons = [reason] if reason else []
            if not output_complete:
                reasons.append('output_limit')
            return dict(header, returned_count=count, omitted_candidates=retained-count if searched else None,
                        truncation=reasons, output_complete=output_complete, complete=searched and output_complete,
                        encoding=encode_prefix(candidates, count, anchor_node))

        def fits(packet):
            # json.dumps defaults to ASCII, so its character count equals the UTF-8 wire bytes.
            return len(json.dumps(packet)) <= self._output_limit

        best = make_packet(0)
        if not fits(best):
            header['anchor_resolution'] = dict(status=header['anchor_resolution']['status'])
            header.update(relation_types=None, query_details_omitted=True)
            return make_packet(0)
        # The full header can shrink when the last candidate clears output_limit.
        # Try the full packet before searching the monotonically growing partial prefixes.
        full = make_packet(retained)
        if fits(full):
            return deepcopy(full)
        low, high = 1, retained - 1
        while low <= high:
            size = (low + high) // 2
            trial = make_packet(size)
            if fits(trial):
                best, low = trial, size + 1
            else:
                high = size - 1
        return deepcopy(best)
