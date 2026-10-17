from pathlib import Path

from litestar import Response
from litestar import get
from litestar.response import Redirect
from litestar.static_files import create_static_files_router

from kiwix_manager.auth import is_owner
from kiwix_manager.auth import login_url
from kiwix_manager.routes.api import services_of
from kiwix_manager.routes.types import ManagerRequest
from kiwix_manager.services import Services

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text()


@get(["/_manage", "/_manage/"], sync_to_thread=False)
def manage_page(request: ManagerRequest) -> Response[str] | Redirect:
    if not is_owner(request.headers):
        return Redirect(path=login_url(request.headers, "/_manage/", ""))
    return Response(
        content=INDEX_HTML,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@get("/_manage/healthz", sync_to_thread=False)
def healthz(request: ManagerRequest) -> Response[dict[str, str]]:
    services: Services = services_of(request)
    if not services.kiwix.running:
        return Response(content={"status": "starting"}, status_code=503)
    return Response(content={"status": "ok"})


static_router = create_static_files_router(path="/_manage/static", directories=[STATIC_DIR], html_mode=False)
