import threading
from pathlib import Path

import attr

from kiwix_manager.jsonio import atomic_write_json
from kiwix_manager.jsonio import read_json


@attr.s(auto_attribs=True, frozen=True)
class Settings:
    # When false, only the instance owner can read the library.
    public: bool = False


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._settings = self._load()

    def _load(self) -> Settings:
        if not self._path.exists():
            return Settings()
        raw = read_json(self._path)
        public = raw["public"]
        if not isinstance(public, bool):
            raise ValueError(f"{self._path}: 'public' must be a boolean")
        return Settings(public=public)

    def get(self) -> Settings:
        return self._settings

    def update(self, settings: Settings) -> None:
        with self._lock:
            atomic_write_json(self._path, attr.asdict(settings))
            self._settings = settings
