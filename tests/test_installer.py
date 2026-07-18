import hashlib
import importlib.util
import io
import stat
import tarfile
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def test_installer_keeps_only_runtime_files():
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("install_mongreldb", root / "install_mongreldb.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with tempfile.TemporaryDirectory() as temp_name:
        temp = Path(temp_name)
        assets = temp / "assets"
        plugin = temp / "plugin"
        assets.mkdir()
        library_bytes = b"fake shared library"
        server_bytes = b"fake server"
        native_name = "native.tar.gz"
        server_name = "server"
        with tarfile.open(assets / native_name, "w:gz") as archive:
            info = tarfile.TarInfo("mongreldb-native/lib/libmongreldb.so")
            info.size = len(library_bytes)
            archive.addfile(info, io.BytesIO(library_bytes))
        (assets / server_name).write_bytes(server_bytes)

        native_sha = hashlib.sha256((assets / native_name).read_bytes()).hexdigest()
        server_sha = hashlib.sha256(server_bytes).hexdigest()
        module._ASSETS = {
            "test": {
                "native": (native_name, native_sha),
                "server": (server_name, server_sha),
                "library_member": "mongreldb-native/lib/libmongreldb.so",
                "library_name": "libmongreldb.so",
            }
        }
        module._platform_key = lambda: "test"

        passphrase = module.load_or_create_passphrase(temp / "hermes")
        assert len(passphrase) >= 48
        assert module.load_or_create_passphrase(temp / "hermes") == passphrase
        key_file = temp / "hermes" / module.KEY_FILENAME
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=assets, **kwargs)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        module.RELEASE_BASE = f"http://127.0.0.1:{server.server_port}"
        old = plugin / "vendor" / "old"
        old.mkdir(parents=True)
        (old / "junk.zip").write_bytes(b"junk")
        try:
            bad_download = temp / "bad"
            try:
                module._download(server_name, "0" * 64, bad_download)
            except RuntimeError:
                assert not bad_download.exists()
            else:
                raise AssertionError("bad SHA-256 accepted")
            library, binary = map(Path, module.install(plugin))
        finally:
            server.shutdown()
            server.server_close()

        assert library.read_bytes() == library_bytes
        assert binary.read_bytes() == server_bytes
        assert {path.name for path in (plugin / "vendor").iterdir()} == {module.VERSION}
        assert {path.name for path in library.parent.iterdir()} == {"libmongreldb.so", "mongreldb-server"}
        assert module.install(plugin) == (str(library), str(binary))


if __name__ == "__main__":
    test_installer_keeps_only_runtime_files()
