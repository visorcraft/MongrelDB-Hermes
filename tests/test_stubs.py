"""Coverage for previously stubbed provider behavior."""
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


def test_jaccard_and_member_strings():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    members = provider._member_strings(
        {"topics": ["alpha", "beta"], "entities": ["MongrelDB"], "projects": []},
        tags=["mem", "alpha"],
    )
    assert members == ["alpha", "beta", "MongrelDB", "mem"]
    assert provider._jaccard({"a", "b"}, {"b", "c"}) == 1 / 3
    assert provider._jaccard(set(), set()) == 1.0


def test_filter_project_entity():
    module = _load_plugin()
    rows = [
        {"id": 1, "projects": ["hermes"], "entities": ["Ada"]},
        {"id": 2, "projects": ["other"], "entities": ["Ada"]},
        {"id": 3, "projects": ["hermes"], "entities": ["Bob"]},
    ]
    assert [r["id"] for r in module.MongrelDBHermesMemoryProvider._filter_project_entity(
        rows, "hermes", None
    )] == [1, 3]
    assert [r["id"] for r in module.MongrelDBHermesMemoryProvider._filter_project_entity(
        rows, "hermes", "Ada"
    )] == [1]


def test_insert_consolidates_near_duplicates():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    provider._mode = "native"
    provider._agent_context = "primary"
    provider._embedder = module._Embedder("")
    provider._dim = 384

    existing = {
        "id": 111,
        "content": "User prefers dark mode in the editor",
        "tags": ["pref"],
        "topics": ["prefers", "dark", "mode", "editor"],
        "entities": [],
        "projects": [],
        "reinforcement_count": 2,
        "created_at": 1000,
        "supersedes": [],
    }
    provider._find_duplicates = lambda enriched, tags=None, exclude_id=0: [existing]
    deleted = []
    put_rows = []

    class FakeTable:
        def put(self, row):
            put_rows.append(row)
            return 1

        def delete_by_pk(self, pk):
            deleted.append(int(pk))
            return True

    provider._table = FakeTable()
    memory_id = provider._insert(
        "User prefers dark mode in the editor window",
        tags=["ui"],
        source="tool",
    )
    assert memory_id == 111
    assert deleted == [111]
    assert put_rows
    # reinforcement_count cell is column 15
    cells = {col: val for col, val in put_rows[0]}
    assert cells[15] == 3
    assert cells[1] == 111


def test_writes_skipped_for_subagent_context():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    provider._agent_context = "subagent"
    provider._mode = "native"
    provider._table = object()
    assert provider._insert("should not write", tags=[], source="turn") == 0


def test_on_memory_write_remove_and_replace():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    provider._agent_context = "primary"
    actions = []

    provider._insert = lambda content, tags=None, source="": actions.append(
        ("insert", content, tags, source)
    ) or 1
    provider._delete_matching_content = lambda content: actions.append(
        ("delete", content)
    ) or 1

    provider.on_memory_write("add", "user", "likes tea")
    provider.on_memory_write(
        "replace", "memory", "likes coffee", metadata={"old_text": "likes tea"}
    )
    provider.on_memory_write("remove", "memory", "likes coffee")
    assert actions == [
        ("insert", "likes tea", ["user"], "memory_tool"),
        ("delete", "likes tea"),
        ("insert", "likes coffee", ["memory"], "memory_tool"),
        ("delete", "likes coffee"),
    ]


def test_queue_prefetch_populates_cache():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    provider._search = lambda query, top_k=5, **kwargs: [
        {"summary": f"hit for {query}", "content": query}
    ]
    provider.queue_prefetch("dark mode")
    if provider._prefetch_thread:
        provider._prefetch_thread.join(timeout=2.0)
    text = provider.prefetch("ignored-because-cache")
    assert "hit for dark mode" in text


def test_tool_call_returns_json_string():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    provider._mode = "native"
    provider._search = lambda query, top_k=8, **kwargs: [{"id": 1, "content": "x"}]
    result = provider.handle_tool_call("mongreldb_search", {"query": "x"})
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed["count"] == 1


def test_on_session_end_extracts_preferences():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    provider._agent_context = "primary"
    provider._session_id = "s1"
    inserted = []
    provider._insert = lambda content, tags=None, source="": inserted.append(
        (content, tags, source)
    ) or 1
    provider.on_session_end(
        [
            {"role": "user", "content": "I prefer concise answers with code blocks."},
            {"role": "assistant", "content": "Got it."},
            {"role": "user", "content": "hello"},
        ]
    )
    assert inserted
    assert inserted[0][2] == "session_end"
    assert "preference" in inserted[0][1]


if __name__ == "__main__":
    test_jaccard_and_member_strings()
    test_filter_project_entity()
    test_insert_consolidates_near_duplicates()
    test_writes_skipped_for_subagent_context()
    test_on_memory_write_remove_and_replace()
    test_queue_prefetch_populates_cache()
    test_tool_call_returns_json_string()
    test_on_session_end_extracts_preferences()
    print("ok")
