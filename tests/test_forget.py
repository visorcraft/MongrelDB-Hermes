"""Native and daemon forget (delete by primary key) coverage."""
import importlib.util
import json
import os
import sys
import tempfile
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


def test_native_forget_uses_table_delete_by_pk():
    module = _load_plugin()
    provider = module.MongrelDBHermesMemoryProvider()
    provider._mode = "native"

    class FakeTable:
        def __init__(self):
            self.deleted = []

        def delete_by_pk(self, pk):
            self.deleted.append(int(pk))
            return int(pk) == 42

    table = FakeTable()
    provider._table = table

    assert json.loads(provider.handle_tool_call("mongreldb_forget", {"memory_id": 42})) == {
        "success": True,
        "memory_id": 42,
    }
    assert json.loads(provider.handle_tool_call("mongreldb_forget", {"memory_id": 99})) == {
        "success": False,
        "memory_id": 99,
    }
    assert json.loads(provider.handle_tool_call("mongreldb_forget", {})) == {
        "error": "memory_id is required"
    }
    assert json.loads(provider.handle_tool_call("mongreldb_forget", {"memory_id": "nope"})) == {
        "error": "memory_id must be an integer"
    }
    assert table.deleted == [42, 99]


def test_native_forget_round_trip_with_real_lib():
    """Insert + forget against a real libmongreldb if one is on the machine."""
    root = Path(__file__).parents[1]
    version = "0.64.6"
    try:
        spec = importlib.util.spec_from_file_location(
            "install_mongreldb", root / "install_mongreldb.py"
        )
        install_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(install_mod)
        version = install_mod.VERSION
    except Exception:
        pass
    candidates = [
        os.environ.get("MONGRELDB_LIB"),
        str(Path.home() / f".hermes/plugins/mongreldb_hermes/vendor/{version}/libmongreldb.so"),
        str(root / f"vendor/{version}/libmongreldb.so"),
    ]
    lib = next((path for path in candidates if path and os.path.isfile(path)), None)
    if not lib:
        print("skip: no libmongreldb.so available for native forget integration test")
        return

    os.environ["MONGRELDB_LIB"] = lib
    # Force re-import of _ffi with the resolved library path.
    sys.modules.pop("mongreldb_hermes", None)
    for name in list(sys.modules):
        if name.startswith("mongreldb_hermes") or name == "_ffi":
            sys.modules.pop(name, None)

    module = _load_plugin()
    with tempfile.TemporaryDirectory() as temp_name:
        hermes_home = Path(temp_name)
        data_dir = hermes_home / "mongreldb_hermes_data"
        key_file = hermes_home / "mongreldb_hermes.key"
        key_file.write_text("test-passphrase-for-forget-roundtrip\n", encoding="utf-8")
        os.chmod(key_file, 0o600)

        provider = module.MongrelDBHermesMemoryProvider()
        provider._mode = "native"
        provider._db_dir = str(data_dir)
        provider._encryption = "enabled"
        provider._passphrase = "test-passphrase-for-forget-roundtrip"
        provider._embedding_model_name = ""
        provider._embedder = module._Embedder("")
        provider._dim = 384
        provider._open_db()

        remembered = json.loads(
            provider.handle_tool_call(
                "mongreldb_remember",
                {"content": "secret API key sk-test-forget-me", "tags": ["private"]},
            )
        )
        assert remembered["success"], remembered
        memory_id = remembered["memory_id"]

        found = json.loads(
            provider.handle_tool_call(
                "mongreldb_search", {"query": "API key forget", "top_k": 5}
            )
        )
        assert found["count"] >= 1
        assert any(r.get("id") == memory_id for r in found.get("results", [])), found

        forgotten = json.loads(
            provider.handle_tool_call("mongreldb_forget", {"memory_id": memory_id})
        )
        assert forgotten == {"success": True, "memory_id": memory_id}

        missing = json.loads(
            provider.handle_tool_call("mongreldb_forget", {"memory_id": memory_id})
        )
        assert missing == {"success": False, "memory_id": memory_id}

        after = json.loads(
            provider.handle_tool_call(
                "mongreldb_search", {"query": "API key forget", "top_k": 5}
            )
        )
        assert all(r.get("id") != memory_id for r in after.get("results", [])), after

        # Close before TemporaryDirectory tears down the data path.
        if getattr(provider, "_table", None) is not None:
            provider._table = None
        if getattr(provider, "_db", None) is not None:
            provider._db.close()
            provider._db = None


if __name__ == "__main__":
    test_native_forget_uses_table_delete_by_pk()
    test_native_forget_round_trip_with_real_lib()
    print("ok")
