"""Download, verify and install the pinned static kiwix-tools release for the build architecture."""

import hashlib
import io
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "3.8.2"
SHA256_BY_ARCH = {
    "x86_64": "b0ae98dd344aa0469a15ab42feff6d5aafb79541a82fb4e2647c74b073123815",
    "aarch64": "a9898fc5363437327350db669bbc03dcc7ecfd498dae727d7184bebe77019514",
}
BINARIES = ("kiwix-serve", "kiwix-manage")


def main() -> None:
    arch = platform.machine()
    if arch not in SHA256_BY_ARCH:
        sys.exit(f"unsupported architecture: {arch}")
    url = f"https://download.kiwix.org/release/kiwix-tools/kiwix-tools_linux-{arch}-{VERSION}.tar.gz"
    with urllib.request.urlopen(url, timeout=300) as response:
        payload = response.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != SHA256_BY_ARCH[arch]:
        sys.exit(f"checksum mismatch for {url}: got {digest}")
    destination = Path("/usr/local/bin")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for name in BINARIES:
            member = archive.getmember(f"kiwix-tools_linux-{arch}-{VERSION}/{name}")
            source = archive.extractfile(member)
            assert source is not None
            target = destination / name
            target.write_bytes(source.read())
            target.chmod(0o755)


if __name__ == "__main__":
    main()
