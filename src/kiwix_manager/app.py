from kiwix_manager.config import load_config
from kiwix_manager.server import create_app
from kiwix_manager.services import build_services

app = create_app(build_services(load_config()))
