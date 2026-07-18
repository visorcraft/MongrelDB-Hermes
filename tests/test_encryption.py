import builtins
import importlib.util
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path


def _load_plugin(config):
    try:
        import agent.memory_provider  # noqa: F401
    except ModuleNotFoundError:
        agent = types.ModuleType("agent")
        memory_provider = types.ModuleType("agent.memory_provider")
        memory_provider.MemoryProvider = object
        sys.modules["agent"] = agent
        sys.modules["agent.memory_provider"] = memory_provider

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_config = types.ModuleType("hermes_cli.config")
    hermes_config.load_config = lambda: config

    def cfg_get(value, *keys, default=None):
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value

    hermes_config.cfg_get = cfg_get
    sys.modules["hermes_cli"] = hermes_cli
    sys.modules["hermes_cli.config"] = hermes_config
    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: Path("/tmp/.hermes")
    sys.modules["hermes_constants"] = hermes_constants

    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location(
        "mongreldb_hermes", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_encryption_defaults_on_and_can_be_disabled():
    environment = {
        key: os.environ.pop(key)
        for key in (
            "MONGRELDB_ENCRYPTION",
            "MONGRELDB_PASSPHRASE",
            "MONGRELDB_LLM_API_KEY",
            "MONGRELDB_LLM_BASE_URL",
            "MONGRELDB_LLM_MODEL",
            "OPENAI_API_KEY",
        )
        if key in os.environ
    }
    try:
        with tempfile.TemporaryDirectory() as temp_name:
            module = _load_plugin({})
            provider = module.MongrelDBHermesMemoryProvider()
            provider._resolve_config(temp_name)
            assert provider._encryption == "enabled"
            assert provider._embedding_model_name == "all-MiniLM-L6-v2"
            assert provider._dim == 384
            assert provider._passphrase
            assert (Path(temp_name) / "mongreldb_hermes.key").is_file()
            schema = provider.get_config_schema()
            assert schema
            assert not any(field.get("secret") for field in schema)
            retrieval = next(field for field in schema if field["key"] == "retrieval_mode")
            assert retrieval["default"] == "dense"
            assert retrieval["choices"] == ["dense", "sparse"]
            enrichment = next(field for field in schema if field["key"] == "enrichment_mode")
            assert enrichment["default"] == "heuristic"
            assert enrichment["choices"] == ["heuristic", "llm"]
            llm_base_url = next(field for field in schema if field["key"] == "llm_base_url")
            llm_model = next(field for field in schema if field["key"] == "llm_model")
            assert llm_base_url["when"] == {"enrichment_mode": "llm"}
            assert llm_model["when"] == {"enrichment_mode": "llm"}
            assert not any(field["key"] in {"embedding_model", "dim"} for field in schema)

            module._install_binaries = lambda: ("libmongreldb.so", "mongreldb-server")
            downloaded = []
            module._Embedder._load = lambda embedder: downloaded.append(embedder.model_name)
            saved = []
            sys.modules["hermes_cli.config"].save_config = saved.append
            provider.save_config({"retrieval_mode": "dense", "encryption": "enabled"}, temp_name)
            dense = saved[-1]["memory"]["mongreldb_hermes"]
            assert dense["embedding_model"] == "all-MiniLM-L6-v2"
            assert dense["dim"] == 384
            assert downloaded == ["all-MiniLM-L6-v2"]

            provider.save_config({"retrieval_mode": "sparse", "encryption": "enabled"}, temp_name)
            sparse = saved[-1]["memory"]["mongreldb_hermes"]
            assert sparse["embedding_model"] == ""
            assert downloaded == ["all-MiniLM-L6-v2"]

            module = _load_plugin(
                {"memory": {"mongreldb_hermes": {"encryption": "disabled"}}}
            )
            provider = module.MongrelDBHermesMemoryProvider()
            provider._resolve_config(temp_name)
            assert provider._encryption == "disabled"
            assert provider._passphrase is None
    finally:
        os.environ.update(environment)


def test_dense_installs_missing_dependency():
    module = _load_plugin({})
    original_import = builtins.__import__
    original_run = module.subprocess.run
    original_which = shutil.which
    fake_package = types.ModuleType("sentence_transformers")
    fake_package.SentenceTransformer = lambda name: ("model", name)
    installed = False
    commands = []

    def fake_import(name, *args, **kwargs):
        if name == "sentence_transformers":
            if not installed:
                raise ImportError(name)
            return fake_package
        return original_import(name, *args, **kwargs)

    def fake_run(command, **kwargs):
        nonlocal installed
        installed = True
        commands.append((command, kwargs))

    builtins.__import__ = fake_import
    module.subprocess.run = fake_run
    shutil.which = lambda name: "/usr/bin/uv" if name == "uv" else None
    try:
        model = module._Embedder("all-MiniLM-L6-v2")._load()
    finally:
        builtins.__import__ = original_import
        module.subprocess.run = original_run
        shutil.which = original_which

    assert model == ("model", "all-MiniLM-L6-v2")
    assert commands == [
        (["/usr/bin/uv", "pip", "install", "--python", sys.executable, "sentence-transformers"], {"check": True, "timeout": 600})
    ]


def test_openai_compatible_llm_config():
    module = _load_plugin({})
    calls = []
    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = lambda **kwargs: calls.append(kwargs) or "client"
    original_openai = sys.modules.get("openai")
    sys.modules["openai"] = fake_openai
    try:
        provider = module.MongrelDBHermesMemoryProvider()
        provider._llm_api_key = "provider-key"
        provider._llm_base_url = "http://127.0.0.1:11434/v1"
        provider._llm_model = "local-model"
        assert provider._make_llm_client() == "client"
    finally:
        if original_openai is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = original_openai
    assert calls == [{"api_key": "provider-key", "base_url": "http://127.0.0.1:11434/v1"}]

    model_calls = []
    response = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='{"summary":"ok","memory_type":"fact","entities":[],"projects":[],"topics":[],"importance":0.5,"confidence":0.8}'))]
    )
    provider._llm_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(
                create=lambda **kwargs: model_calls.append(kwargs) or response
            )
        )
    )
    assert provider._extract_llm("test memory")["summary"] == "ok"
    assert model_calls[0]["model"] == "local-model"


if __name__ == "__main__":
    test_encryption_defaults_on_and_can_be_disabled()
    test_dense_installs_missing_dependency()
    test_openai_compatible_llm_config()
