"""From the checkout root: python3 scripts/check_neighbors50.py /path/to/graphs"""
from hashlib import sha256
import json
from pathlib import Path
import sys

from code_graph_search_once import GraphRuntime, decode_neighbors

def main(graph_dir):
    root = Path(__file__).resolve().parents[1]
    data = root / "benchmark50/data"
    expected = json.loads((root / "tests/data/neighbors50-sha256.json").read_bytes())
    assets = {a["query_id"]: a for a in json.loads((data/"graph-assets.json").read_bytes())}
    questions = [json.loads(line) for line in (data/"questions.jsonl").read_text().splitlines() if line]
    ids = {q["query_id"] for q in questions}
    assert len(questions) == len(ids) == 50 and ids == set(expected) == set(assets)
    hits = 0
    for question in questions:
        key = question["query_id"]
        asset = assets[key]
        with GraphRuntime(graph_dir / asset["graph_filename"], asset["sha256"]) as runtime:
            packet = runtime.graph_neighbors(question["start"])
        wire = json.dumps(packet)
        assert len(wire) <= 12_000 and sha256(wire.encode()).hexdigest() == expected[key], key
        candidates = decode_neighbors(packet)["candidates"]
        hits += question["target"] in {row["node_id"] for row in candidates}
    assert hits == 41
    print(f"Frozen packets matched: {len(ids)}/50; hidden targets returned: {hits}/50")

if __name__ == "__main__":
    main(Path(sys.argv[1]))
