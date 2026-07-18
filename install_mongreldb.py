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


VERSION = "0.60.2"
KEY_FILENAME = "mongreldb_hermes.key"
RELEASE_BASE = f"https://github.com/visorcraft/MongrelDB/releases/download/v{VERSION}"

_ASSETS = {
    "linux-x64-gnu": {
        "native": ("mongreldb-native-linux-x64-gnu.tar.gz", "98cf2d8cd811e84833bce77e4794a3661180733ca5ca3db6b8aa561d544decdf"),
        "server": ("mongreldb-server-linux-x64", "2c07439b83644515c8db85d914336f26812510ab5bacd211e69c22a8e487b68d"),
        "library_member": "mongreldb-native/lib/libmongreldb.so",
        "library_name": "libmongreldb.so",
    },
    "linux-x64-musl": {
        "native": ("mongreldb-native-linux-x64-musl.tar.gz", "6435079f6325c85b4c1c2dbb7459451fc2f3cecc1f051508b65b981fcd6db777"),
        "server": ("mongreldb-server-linux-x64-musl", "3b86819c554d3fcf07a7207c6842a086a95007ff6d4590941f96de19c1aea014"),
        "library_member": "mongreldb-native/lib/libmongreldb.so",
        "library_name": "libmongreldb.so",
    },
    "linux-arm64-gnu": {
        "native": ("mongreldb-native-linux-arm64-gnu.tar.gz", "91e84ac49b8381fb5f28c8f48186193a7576a391a0c4b52873de5171ddf816d9"),
        "server": ("mongreldb-server-linux-arm64", "837992bc818c44e87d6f4764dc055023dab229651991ddcb4c5a2314631114bb"),
        "library_member": "mongreldb-native/lib/libmongreldb.so",
        "library_name": "libmongreldb.so",
    },
    "darwin-arm64": {
        "native": ("mongreldb-native-darwin-arm64.tar.gz", "6b923421de3df558a06886721febea038b1ef58b4c97c184cac358ff8bf5255e"),
        "server": ("mongreldb-server-darwin-arm64", "46c417d8d9c483c7ad5b6735867bddc0e2116a5931260b6af9ac72ffe2e0aaef"),
        "library_member": "mongreldb-native/lib/libmongreldb.dylib",
        "library_name": "libmongreldb.dylib",
    },
    "darwin-x64": {
        "native": ("mongreldb-native-darwin-x64.tar.gz", "5555097a4a326a9a0b3f0944fd50dfdfcfdb7cdb64af2b515e67099c235504b3"),
        "server": ("mongreldb-server-darwin-universal", "f3922d3771f270b3b5404886accf37f81f689a366c378020b548f65369c2b5e3"),
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
