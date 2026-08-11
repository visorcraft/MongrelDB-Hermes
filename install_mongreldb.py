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


VERSION = "0.64.16"
KEY_FILENAME = "mongreldb_hermes.key"
RELEASE_BASE = f"https://github.com/visorcraft/MongrelDB/releases/download/v{VERSION}"

_ASSETS = {
    "linux-x64-gnu": {
        "native": ("mongreldb-native-linux-x64-gnu.tar.gz", "b48ff2f16f70e629e8b7ad15cd109d4fb2a2e0059fd6a999d49870832778ecaf"),
        "server": ("mongreldb-server-linux-x64", "2bca542a7128e6558958a4927b33fb3bff7f325180c9e44ce8b68ddd5149bfd8"),
        "library_member": "mongreldb-native/lib/libmongreldb.so",
        "library_name": "libmongreldb.so",
    },
    "linux-x64-musl": {
        "native": ("mongreldb-native-linux-x64-musl.tar.gz", "076d8e7655e7373858c639c665953a0c4832ab65dc8180370e44a387f82ebce9"),
        "server": ("mongreldb-server-linux-x64-musl", "34b61388beb533f89ed4e18a110e28bef732a34a94fdcce25d812d0f76f821cd"),
        "library_member": "mongreldb-native/lib/libmongreldb.so",
        "library_name": "libmongreldb.so",
    },
    "linux-arm64-gnu": {
        "native": ("mongreldb-native-linux-arm64-gnu.tar.gz", "a1e8a025b9e8bb4bb535676d3b88c1250115d72ed4fc97b6afb170b7d5d39365"),
        "server": ("mongreldb-server-linux-arm64", "9596eacc225269532340abb0583396786a655d99026e09bf5b329465efacf685"),
        "library_member": "mongreldb-native/lib/libmongreldb.so",
        "library_name": "libmongreldb.so",
    },
    "darwin-arm64": {
        "native": ("mongreldb-native-darwin-arm64.tar.gz", "a19bf7a3b4fa08ebdf4bc3e6d40ed8a69083983d5553d1c6ef9a7d602f6a0429"),
        "server": ("mongreldb-server-darwin-arm64", "ba689536475d591aa50d23c2fba73f60f64c96b0f9b49f4e9a2b51a7c2e75724"),
        "library_member": "mongreldb-native/lib/libmongreldb.dylib",
        "library_name": "libmongreldb.dylib",
    },
    "darwin-x64": {
        "native": ("mongreldb-native-darwin-x64.tar.gz", "749f5a7c20327285087b7063429a2d9288c364f5b7d374fad6a27d5feeaceda5"),
        "server": ("mongreldb-server-darwin-universal", "4c8cde437e1811ea78e9a97fdabf4bab416515ce6b7ccd4f77ddb7ab27c058df"),
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
