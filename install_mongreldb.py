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


VERSION = "0.64.3"
KEY_FILENAME = "mongreldb_hermes.key"
RELEASE_BASE = f"https://github.com/visorcraft/MongrelDB/releases/download/v{VERSION}"

_ASSETS = {
    "linux-x64-gnu": {
        "native": ("mongreldb-native-linux-x64-gnu.tar.gz", "6c2fdca8643f325415f641a952fa5b1f945be0292a4ada45bca7ab7cb79692ab"),
        "server": ("mongreldb-server-linux-x64", "c521bf3ddabbfbf0474cad3eb786e008af120aee5f6a9ce02306bf890de9be0b"),
        "library_member": "mongreldb-native/lib/libmongreldb.so",
        "library_name": "libmongreldb.so",
    },
    "linux-x64-musl": {
        "native": ("mongreldb-native-linux-x64-musl.tar.gz", "61edc8fc71c4bbde58f6f57aea7f181a66d15e76501a9511602f06e873cf013c"),
        "server": ("mongreldb-server-linux-x64-musl", "eccbe93232160cd473dfacc23a5b4ece5fc0fc04eb194da2213e4186f718609c"),
        "library_member": "mongreldb-native/lib/libmongreldb.so",
        "library_name": "libmongreldb.so",
    },
    "linux-arm64-gnu": {
        "native": ("mongreldb-native-linux-arm64-gnu.tar.gz", "043aea4ec7c8c45751059c16dbce5f5c5c22700c12b26a0358d9eb1afcdf0032"),
        "server": ("mongreldb-server-linux-arm64", "0ffaa88d792c51ead4860e481463d6f43ceb2665ef57b338d76fdb14b3bd38e3"),
        "library_member": "mongreldb-native/lib/libmongreldb.so",
        "library_name": "libmongreldb.so",
    },
    "darwin-arm64": {
        "native": ("mongreldb-native-darwin-arm64.tar.gz", "22e0257f8d941280d8eb04bebd60a7cbcacacd60e11a69b00af80096c912b681"),
        "server": ("mongreldb-server-darwin-arm64", "e3cdea697c5fa58fd4dce721a9bcb9498ee886b454dbe37c41ac233186ce250e"),
        "library_member": "mongreldb-native/lib/libmongreldb.dylib",
        "library_name": "libmongreldb.dylib",
    },
    "darwin-x64": {
        "native": ("mongreldb-native-darwin-x64.tar.gz", "a3066670947a5c4ebd974ef4b48d460d822fc18baf22caecea7c44d86701b6d4"),
        "server": ("mongreldb-server-darwin-universal", "fd28900b8d3f2832d9afad557512d37f164fa4598b3132652d68b14ed24f3fa8"),
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
