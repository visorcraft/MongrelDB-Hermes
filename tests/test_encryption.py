import importlib.util
import os
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
        for key in ("MONGRELDB_ENCRYPTION", "MONGRELDB_PASSPHRASE")
        if key in os.environ
    }
    try:
        with tempfile.TemporaryDirectory() as temp_name:
            module = _load_plugin({})
            provider = module.MongrelDBHermesMemoryProvider()
            provider._resolve_config(temp_name)
            assert provider._encryption == "enabled"
            assert provider._passphrase
            assert (Path(temp_name) / "mongreldb_hermes.key").is_file()

            module = _load_plugin(
                {"memory": {"mongreldb_hermes": {"encryption": "disabled"}}}
            )
            provider = module.MongrelDBHermesMemoryProvider()
            provider._resolve_config(temp_name)
            assert provider._encryption == "disabled"
            assert provider._passphrase is None
    finally:
        os.environ.update(environment)


if __name__ == "__main__":
    test_encryption_defaults_on_and_can_be_disabled()
