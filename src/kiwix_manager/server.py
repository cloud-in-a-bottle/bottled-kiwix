import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from litestar import Litestar
from litestar.datastructures import State
from litestar.logging.config import LoggingConfig
from loguru import logger

from kiwix_manager.asgi import Receive
from kiwix_manager.asgi import Scope
from kiwix_manager.asgi import Send
from kiwix_manager.proxy import ReaderProxy
from kiwix_manager.routes.api import api_router
from kiwix_manager.routes.pages import healthz
from kiwix_manager.routes.pages import manage_page
from kiwix_manager.routes.pages import static_router
from kiwix_manager.services import Services


def create_app(services: Services) -> "Dispatcher":
    # httpx logs every request at INFO, which would log each proxied reader request and catalog thumbnail.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(_: Litestar) -> AsyncIterator[None]:
        try:
            services.library.initialize()
            await asyncio.to_thread(services.library.reconcile)
            services.downloads.start()
            await services.kiwix.start()
            logger.info("Kiwix manager started with {} books", len(services.library.books()))
            yield
        finally:
            await services.kiwix.stop()
            await asyncio.to_thread(services.downloads.stop)
            await services.catalog_http.aclose()
            await services.upstream_http.aclose()

    manager = Litestar(
        route_handlers=[manage_page, healthz, static_router, api_router],
        lifespan=[lifespan],
        state=State({"services": services}),
        logging_config=LoggingConfig(log_exceptions="always", disable_stack_trace={400, 401, 403, 404, 405}),
    )
    reader = ReaderProxy(
        client=services.upstream_http,
        upstream=services.kiwix.base_url,
        is_public=lambda: services.settings.get().public,
        is_running=lambda: services.kiwix.running,
        library_is_empty=lambda: not services.library.books(),
    )
    return Dispatcher(manager, reader)


class Dispatcher:
    """Routes /_manage to the manager and everything else to kiwix-serve."""

    def __init__(self, manager: Litestar, reader: ReaderProxy) -> None:
        self.manager = manager
        self.reader = reader

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.manager(scope, receive, send)  # type: ignore[arg-type]
            return
        path: str = scope["path"]
        if path == "/_manage" or path.startswith("/_manage/"):
            await self.manager(scope, receive, send)  # type: ignore[arg-type]
            return
        await self.reader(scope, receive, send)
