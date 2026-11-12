"""Installable access to the verified graph search implementations."""
from copy import deepcopy
from functools import lru_cache
import hashlib
import importlib.util
import json
from pathlib import Path
import threading

from ._sources import SOURCE_HASHES


def bundle_root():
    installed = Path(__file__).resolve().parent / '_bundle'
    root = installed if installed.is_dir() else Path(__file__).resolve().parents[1]
    if not (root / 'variants/graph-find-paths/tool.py').is_file():
        raise FileNotFoundError('all-paths source bundle is missing')
    return root


@lru_cache(maxsize=1)
def _native():
    root = bundle_root()
    for name, expected in SOURCE_HASHES.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError('SourceSHA256Mismatch: ' + name)
    path = root / 'variants/graph-find-paths/tool.py'
    spec = importlib.util.spec_from_file_location('_all_paths_native', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AllPathsSession = _native().AllPathsSession
make_graph_find_paths = _native().make_graph_find_paths


def get_tool_schema():
    return json.loads((bundle_root() / 'tool_schema.json').read_text())


@lru_cache(maxsize=1)
def _neighbor_native():
    _native()  # Verify the shared source bundle before loading its second tool.
    path = bundle_root() / 'variants/graph-neighbors/tool.py'
    spec = importlib.util.spec_from_file_location('_graph_neighbors_native', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_neighbors_schema():
    path = bundle_root() / 'variants/graph-neighbors/tool_schema.json'
    return json.loads(path.read_text())


def decode_neighbors(packet):
    """Expand the compact candidate table and its original witness edges."""
    return _neighbor_native().decode_packet(packet)


def decode_paths(packet):
    """Decode all pair path lists, including minimal-resolution packets."""
    return [[[packet['nodes'][index] for index in path['nodes']]
             for path in pair.get('paths', [])] for pair in packet['pairs']]


class GraphRuntime:
    """Context-managed local host; each query retains native budgets and errors."""
    def __init__(self, graph_path, expected_graph_sha256=None, **limits):
        path = Path(graph_path).expanduser().resolve()
        if expected_graph_sha256 is None:
            with path.open('rb') as stream:
                raw = stream.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError('FileCapExceeded')
            expected_graph_sha256 = hashlib.sha256(raw).hexdigest()
        self._session = AllPathsSession(path, expected_graph_sha256, **limits)
        self._lock = threading.RLock()
        self.closed = False

    @property
    def schema(self):
        return deepcopy(get_tool_schema())

    def graph_find_paths(self, source_nodes: list[str], target_nodes: list[str], max_hops: int = 4) -> dict:
        with self._lock:
            if self.closed:
                raise RuntimeError('SessionClosed')
            return self._session.graph_find_paths(source_nodes, target_nodes, max_hops)

    query = graph_find_paths

    def graph_neighbors(self, anchor_node: str, *, direction: str = 'downstream',
                        max_hops: int = 4, relation_types=None, output_limit: int = 12_000) -> dict:
        """Find distinct symbols reachable from an exact anchor along stored edges."""
        with self._lock:
            if self.closed:
                raise RuntimeError('SessionClosed')
            search = _neighbor_native().NeighborQuery(self._session, output_limit)
            return search.graph_neighbors(anchor_node, direction, max_hops, relation_types)

    def close(self):
        with self._lock:
            self.closed = True
            self._session = None

    def __enter__(self):
        if self.closed:
            raise RuntimeError('SessionClosed')
        return self

    def __exit__(self, *_):
        self.close()
