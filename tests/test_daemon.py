import importlib.util
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def test_daemon_round_trip():
    module = _load_plugin()

    class Handler(BaseHTTPRequestHandler):
        tables = []
        row = None

        def _assert_auth(self):
            assert self.headers["Authorization"] == "Bearer test-token"

        def _send(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._assert_auth()
            if self.path == "/tables":
                self._send(self.tables)
            else:
                self._send({"status": "ok"})

        def do_POST(self):
            self._assert_auth()
            size = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(size))
            if self.path == "/kit/create_table":
                assert any(index["kind"] == "sparse" for index in payload["indexes"])
                self.tables.append(payload["name"])
                self._send({"table_id": 1})
            elif self.path == "/kit/txn":
                operation = payload["ops"][0]
                if "put" in operation:
                    type(self).row = operation["put"]["cells"]
                    self._send({"status": "committed", "results": [{"kind": "put"}]})
                else:
                    type(self).row = None
                    self._send({"status": "committed", "results": [{"kind": "deleted"}]})
            elif self.path == "/kit/search":
                self._send({"hits": [] if self.row is None else [{"cells": self.row}]})
            else:
                raise AssertionError(self.path)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = module.MongrelDBHermesMemoryProvider()
        provider._mode = "daemon"
        provider._daemon_url = f"http://127.0.0.1:{server.server_port}"
        provider._daemon_auth_token = "test-token"
        provider._embedder = module._Embedder("")
        provider._open_db()
        remembered = provider.handle_tool_call(
            "mongreldb_remember", {"content": "daemon memory", "tags": ["test"]}
        )
        assert remembered["success"]
        assert provider.handle_tool_call(
            "mongreldb_search", {"query": "daemon", "top_k": 5}
        )["count"] == 1
        assert provider.handle_tool_call(
            "mongreldb_forget", {"memory_id": remembered["memory_id"]}
        )["success"]
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    test_daemon_round_trip()
