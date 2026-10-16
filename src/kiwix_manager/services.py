import attr
import httpx

from kiwix_manager.catalog import CatalogClient
from kiwix_manager.config import Config
from kiwix_manager.downloads import USER_AGENT
from kiwix_manager.downloads import DownloadManager
from kiwix_manager.kiwix_serve import KiwixServe
from kiwix_manager.library import Library
from kiwix_manager.settings import SettingsStore


@attr.s(auto_attribs=True, frozen=True)
class Services:
    config: Config
    settings: SettingsStore
    library: Library
    downloads: DownloadManager
    kiwix: KiwixServe
    catalog: CatalogClient
    catalog_http: httpx.AsyncClient
    upstream_http: httpx.AsyncClient


def build_services(config: Config) -> Services:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    kiwix = KiwixServe(config.kiwix_serve_bin, config.library_path, config.kiwix_port)
    library = Library(config.library_path, config.zim_dir, config.kiwix_manage_bin, kiwix.wait_for_books)
    catalog_http = httpx.AsyncClient(timeout=httpx.Timeout(30.0), headers={"User-Agent": USER_AGENT})
    upstream_http = httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=600.0, write=60.0, pool=30.0))
    return Services(
        config=config,
        settings=SettingsStore(config.data_dir / "settings.json"),
        library=library,
        downloads=DownloadManager(
            state_path=config.data_dir / "downloads.json",
            partial_dir=config.partial_dir,
            zim_dir=config.zim_dir,
            reserve_bytes=config.reserve_bytes,
            local_dir=config.data_dir,
            on_completed=library.reconcile,
        ),
        kiwix=kiwix,
        catalog=CatalogClient(config.catalog_url, catalog_http),
        catalog_http=catalog_http,
        upstream_http=upstream_http,
    )
