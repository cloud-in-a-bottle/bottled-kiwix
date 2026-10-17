import os
from pathlib import Path

import attr

DEFAULT_CATALOG_URL = "https://opds.library.kiwix.org"
DEFAULT_META4_URL_PREFIXES = (
    "https://lb.download.kiwix.org/",
    "https://download.kiwix.org/",
    "https://mirror.download.kiwix.org/",
)
# Keep this much space free on the filesystem holding the ZIM files. The Cloud in a Bottle storage guard stops apps
# below 500 MB free, so a download must never push the disk close to that.
DEFAULT_RESERVE_BYTES = 2 * 1024**3


@attr.s(auto_attribs=True, frozen=True)
class Config:
    data_dir: Path
    zim_dir: Path
    partial_dir: Path
    library_path: Path
    kiwix_serve_bin: str
    kiwix_manage_bin: str
    kiwix_port: int
    catalog_url: str
    meta4_url_prefixes: tuple[str, ...]
    allow_insecure_mirrors: bool
    reserve_bytes: int


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


def load_config() -> Config:
    data_dir = Path(_required_env("BOTTLE_APP_DATA_DIR"))
    archive_dir = Path(_required_env("BOTTLE_APP_ARCHIVE_DIR"))
    zim_dir = archive_dir / "zims"
    prefixes = os.environ.get("KIWIX_META4_URL_PREFIXES")
    return Config(
        data_dir=data_dir,
        zim_dir=zim_dir,
        # Partial downloads live on the same filesystem as the finished files so completion is an atomic rename.
        partial_dir=archive_dir / "partial",
        library_path=data_dir / "library.xml",
        kiwix_serve_bin=os.environ.get("KIWIX_SERVE_BIN", "kiwix-serve"),
        kiwix_manage_bin=os.environ.get("KIWIX_MANAGE_BIN", "kiwix-manage"),
        kiwix_port=int(os.environ.get("KIWIX_SERVE_PORT", "8081")),
        catalog_url=os.environ.get("KIWIX_CATALOG_URL", DEFAULT_CATALOG_URL).rstrip("/"),
        meta4_url_prefixes=tuple(prefixes.split(",")) if prefixes else DEFAULT_META4_URL_PREFIXES,
        allow_insecure_mirrors=os.environ.get("KIWIX_ALLOW_INSECURE_MIRRORS") == "1",
        reserve_bytes=int(os.environ.get("KIWIX_RESERVE_BYTES", str(DEFAULT_RESERVE_BYTES))),
    )
