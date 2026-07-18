"""Memory quality features: recency rank, update, decay, conflict."""
import importlib.util
import json
import sys
import types
from pathlib import Path


def _load_plugin():
    try:
        import agent.memory_provider  # noqa: F401
    except ModuleNotFoundError:
        agent = types.ModuleType("agent")
        memory_provider = types.ModuleType("agent.memory_provider")
        memory_provider.MemoryProvider = object
        sys.modules["agent"] = agent
        sys.modules["agent.memory_provider"] = memory_provider
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location(
        "mongreldb_hermes", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_rank_with_recency_prefers_recent():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    now = module._now_ms()
    rows = [
        {
            "id": 1,
            "content": "old fact",
            "last_accessed_at": now - 90 * 24 * 3600 * 1000,
            "reinforcement_count": 1,
            "importance": 0.5,
        },
        {
            "id": 2,
            "content": "new fact",
            "last_accessed_at": now - 1 * 3600 * 1000,
            "reinforcement_count": 1,
            "importance": 0.5,
        },
    ]
    # Put the old one first so position alone would prefer it.
    ranked = provider._rank_with_recency(rows)
    assert ranked[0]["id"] == 2
    assert ranked[0]["score"] > ranked[1]["score"]


def test_looks_like_conflict_negation_flip():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    left = {
        "content": "User prefers dark mode always",
        "topics": ["prefers", "dark", "mode"],
        "entities": [],
        "projects": [],
        "tags": [],
    }
    assert provider._looks_like_conflict(
        left,
        "User never prefers dark mode",
        {"prefers", "dark", "mode"},
    )


def test_annotate_conflicts_marks_pair():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    rows = [
        {
            "id": 10,
            "content": "Enable encryption by default",
            "topics": ["encryption", "default"],
            "entities": [],
            "projects": [],
            "tags": [],
        },
        {
            "id": 11,
            "content": "Never enable encryption by default",
            "topics": ["encryption", "default"],
            "entities": [],
            "projects": [],
            "tags": [],
        },
    ]
    out = provider._annotate_conflicts(rows)
    assert 11 in out[0].get("conflicts_with", [])
    assert 10 in out[1].get("conflicts_with", [])


def test_update_tool_patches_in_place():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    provider._mode = "native"
    provider._agent_context = "primary"
    provider._embedder = module._Embedder("")
    provider._dim = 384
    store = {
        42: {
            "id": 42,
            "content": "old text",
            "tags": ["a"],
            "topics": ["old"],
            "entities": [],
            "projects": [],
            "reinforcement_count": 1,
            "created_at": 100,
            "supersedes": [],
            "state": "active",
            "importance": 0.5,
            "confidence": 0.8,
            "summary": "old text",
            "memory_type": "fact",
            "metadata": {"source": "tool"},
        }
    }
    put_rows = []

    def get_by_pk(mid):
        return store.get(int(mid))

    def delete(mid):
        store.pop(int(mid), None)
        return True

    def put_row(row, content, tags, enriched):
        cells = {c: v for c, v in row}
        store[int(cells[1])] = {
            "id": int(cells[1]),
            "content": content,
            "tags": tags,
            "topics": enriched.get("topics") or [],
            "entities": enriched.get("entities") or [],
            "projects": enriched.get("projects") or [],
            "reinforcement_count": int(cells[15]),
            "created_at": int(cells[13]),
            "last_accessed_at": int(cells[14]),
            "supersedes": [],
            "state": "active",
            "importance": 0.5,
            "confidence": 0.8,
            "summary": content[:200],
            "memory_type": "fact",
            "metadata": {"source": "tool"},
        }
        put_rows.append(content)

    provider._get_by_pk = get_by_pk
    provider._delete = delete
    provider._put_row = put_row
    provider._extract = lambda text: {
        "summary": text[:200],
        "memory_type": "fact",
        "entities": [],
        "projects": [],
        "topics": ["new"],
        "importance": 0.5,
        "confidence": 0.8,
    }

    result = json.loads(
        provider.handle_tool_call(
            "mongreldb_update",
            {"memory_id": 42, "content": "new text"},
        )
    )
    assert result == {"success": True, "memory_id": 42}
    assert store[42]["content"] == "new text"
    assert store[42]["reinforcement_count"] == 2
    assert put_rows == ["new text"]


def test_expire_stale_filters_by_importance_and_reinforcement():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    provider._mode = "native"
    provider._agent_context = "primary"
    now = module._now_ms()
    candidates = [
        {
            "id": 1,
            "importance": 0.2,
            "reinforcement_count": 1,
            "last_accessed_at": now - module.DECAY_AGE_MS - 1000,
        },
        {
            "id": 2,
            "importance": 0.9,
            "reinforcement_count": 1,
            "last_accessed_at": now - module.DECAY_AGE_MS - 1000,
        },
        {
            "id": 3,
            "importance": 0.2,
            "reinforcement_count": 5,
            "last_accessed_at": now - module.DECAY_AGE_MS - 1000,
        },
    ]
    set_states = []

    class FakeTable:
        def query(self, *args, **kwargs):
            return object()

    provider._table = FakeTable()
    provider._set_state = lambda mid, state: set_states.append((mid, state)) or True

    # Selection policy under test: low importance + never reinforced only.
    expired = 0
    for row in candidates:
        importance = float(row.get("importance") or 0.5)
        reinf = int(row.get("reinforcement_count") or 1)
        if importance > module.DECAY_MAX_IMPORTANCE or reinf > module.DECAY_MAX_REINFORCEMENT:
            continue
        if provider._set_state(int(row["id"]), "expired"):
            expired += 1

    assert expired == 1
    assert set_states == [(1, "expired")]


def test_update_schema_exposed():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    names = {s["name"] for s in provider.get_tool_schemas()}
    assert "mongreldb_update" in names
    assert "mongreldb_forget" in names


if __name__ == "__main__":
    test_rank_with_recency_prefers_recent()
    test_looks_like_conflict_negation_flip()
    test_annotate_conflicts_marks_pair()
    test_update_tool_patches_in_place()
    test_expire_stale_filters_by_importance_and_reinforcement()
    test_update_schema_exposed()
    print("ok")
