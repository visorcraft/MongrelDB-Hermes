"""Install the MongrelDB binaries bundled at runtime by this plugin."""

import hashlib
import os
import platform
import secrets
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path


VERSION = "0.64.6"
KEY_FILENAME = "mongreldb_hermes.key"
RELEASE_BASE = f"https://github.com/visorcraft/MongrelDB/releases/download/v{VERSION}"

_ASSETS = {
    "linux-x64-gnu": {
        "native": ("mongreldb-native-linux-x64-gnu.tar.gz", "e13c8baf4385d019eeed6eb0f97a649042c553560e42d4ec34cc651bc5bd0d64"),
        "server": ("mongreldb-server-linux-x64", "de00b777613f9c1afb2101fd12fd069f87c52ac9b2964edbe30b4a9488818915"),
        "library_member": "mongreldb-native/lib/libmongreldb.so",
        "library_name": "libmongreldb.so",
    },
    "linux-x64-musl": {
        "native": ("mongreldb-native-linux-x64-musl.tar.gz", "7b5d5bf593c428254b86a42ad715863cfa2a05c87686c1dcd5c4fa9d0215af37"),
        "server": ("mongreldb-server-linux-x64-musl", "4a825c087f9b0789c64806f3d9799beba6aa4b62de69620ec6c614e43af576d0"),
        "library_member": "mongreldb-native/lib/libmongreldb.so",
        "library_name": "libmongreldb.so",
    },
    "linux-arm64-gnu": {
        "native": ("mongreldb-native-linux-arm64-gnu.tar.gz", "5d8a97451bf2669246b946db21420dcaee84b22528eb52b6d1d1ee0336a47f90"),
        "server": ("mongreldb-server-linux-arm64", "70e58920f7afe9be5e640ccd043a96c26a12f6f79e2d1ac89508db48fda79f78"),
        "library_member": "mongreldb-native/lib/libmongreldb.so",
        "library_name": "libmongreldb.so",
    },
    "darwin-arm64": {
        "native": ("mongreldb-native-darwin-arm64.tar.gz", "d1c1a342143048ba90f6a31b4e3b1cc8a3435b45555a18c97ea54500d4bab768"),
        "server": ("mongreldb-server-darwin-arm64", "a349c42ad209d2d2f706f210d495d03027d05094c92f28d40bee0dcc222dd0f5"),
        "library_member": "mongreldb-native/lib/libmongreldb.dylib",
        "library_name": "libmongreldb.dylib",
    },
    "darwin-x64": {
        "native": ("mongreldb-native-darwin-x64.tar.gz", "fd82fda8c768355c5c654bf4c8b900ecc8816b105fda853a44350ef790b869aa"),
        "server": ("mongreldb-server-darwin-universal", "8ce042fd6a88e475383d659e62745ed9eae5fab1e832bdd746ab1bcfa92ee1e5"),
        "library_member": "mongreldb-native/lib/libmongreldb.dylib",
        "library_name": "libmongreldb.dylib",
    },
}


def _platform_key() -> str:
    machine = platform.machine().lower()
    arch = "x64" if machine in {"x86_64", "amd64"} else "arm64" if machine in {"aarch64", "arm64"} else machine
    if sys.platform == "darwin":
        key = f"darwin-{arch}"
    elif sys.platform.startswith("linux"):
        libc = "musl" if platform.libc_ver()[0].lower() == "musl" or any(Path("/lib").glob("ld-musl-*.so.1")) else "gnu"
        key = f"linux-{arch}-{libc}"
    else:
        key = f"{sys.platform}-{arch}"
    if key not in _ASSETS:
        raise RuntimeError(f"MongrelDB {VERSION} binaries are unavailable for {key}")
    return key


def load_or_create_passphrase(hermes_home=None) -> str:
    """Return the persistent default passphrase, creating it mode 0600."""
    root = Path(hermes_home or os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    path = root / KEY_FILENAME
    root.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlinked MongrelDB key file: {path}")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        passphrase = secrets.token_urlsafe(48)
        with os.fdopen(descriptor, "w", encoding="utf-8") as key_file:
            key_file.write(passphrase + "\n")
            key_file.flush()
            os.fsync(key_file.fileno())
        return passphrase
    passphrase = path.read_text(encoding="utf-8").strip()
    if not passphrase:
        raise RuntimeError(f"MongrelDB key file is empty: {path}")
    os.chmod(path, 0o600)
    return passphrase


def _download(name: str, expected_sha256: str, destination: Path) -> None:
    print(f"Downloading {name}...")
    request = urllib.request.Request(
        f"{RELEASE_BASE}/{name}", headers={"User-Agent": "mongreldb-hermes-installer"}
    )
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    actual = digest.hexdigest()
    if actual != expected_sha256:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"SHA-256 mismatch for {name}: expected {expected_sha256}, got {actual}")


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def install(plugin_dir=None):
    """Install both native and daemon binaries, returning their paths."""
    root = Path(plugin_dir or Path(__file__).resolve().parent)
    asset = _ASSETS[_platform_key()]
    vendor = root / "vendor"
    destination = vendor / VERSION
    library = destination / asset["library_name"]
    server = destination / "mongreldb-server"

    if library.is_file() and server.is_file():
        for child in vendor.iterdir():
            if child != destination:
                _remove(child)
        return str(library), str(server)

    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mongreldb-install-", dir=root) as temp_name:
        temp = Path(temp_name)
        native_name, native_sha = asset["native"]
        server_name, server_sha = asset["server"]
        native_download = temp / native_name
        server_download = temp / server_name
        _download(native_name, native_sha, native_download)
        _download(server_name, server_sha, server_download)

        staged = temp / VERSION
        staged.mkdir()
        with tarfile.open(native_download, "r:gz") as archive:
            member = archive.getmember(asset["library_member"])
            if not member.isfile():
                raise RuntimeError(f"Missing shared library in {native_name}")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"Cannot extract shared library from {native_name}")
            with source, (staged / asset["library_name"]).open("wb") as output:
                shutil.copyfileobj(source, output)
        shutil.copyfile(server_download, staged / "mongreldb-server")
        os.chmod(staged / asset["library_name"], 0o755)
        os.chmod(staged / "mongreldb-server", 0o755)

        vendor.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            _remove(destination)
        shutil.move(str(staged), str(destination))
        for child in vendor.iterdir():
            if child != destination:
                _remove(child)

    print(f"Installed MongrelDB {VERSION} in {destination}")
    return str(library), str(server)


if __name__ == "__main__":
    install()
