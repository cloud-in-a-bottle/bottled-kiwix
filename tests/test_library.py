import shutil
from pathlib import Path

import pytest

from kiwix_manager.library import Library
from kiwix_manager.library import LibraryError
from tests.conftest import TEST_ZIM_ID


@pytest.fixture
def library(tmp_path: Path, kiwix_manage_bin: str) -> Library:
    data = tmp_path / "data"
    data.mkdir()
    instance = Library(data / "library.xml", tmp_path / "archive" / "zims", kiwix_manage_bin)
    instance.initialize()
    return instance


def test_reconcile_adds_and_removes(library: Library, tmp_path: Path, test_zim: Path) -> None:
    zim_dir = tmp_path / "archive" / "zims"
    assert library.books() == ()
    shutil.copy(test_zim, zim_dir / test_zim.name)
    result = library.reconcile()
    assert result.added == (test_zim.name,)
    (book,) = library.books()
    assert book.id == TEST_ZIM_ID
    assert book.path == (zim_dir / test_zim.name).resolve()
    assert book.content_name == test_zim.stem
    assert book.flavour == "mini"
    assert book.article_count > 0

    (zim_dir / test_zim.name).unlink()
    result = library.reconcile()
    assert result.removed == (test_zim.name,)
    assert library.books() == ()


def test_reconcile_flags_invalid_files(library: Library, tmp_path: Path) -> None:
    zim_dir = tmp_path / "archive" / "zims"
    (zim_dir / "garbage.zim").write_bytes(b"not a zim")
    result = library.reconcile()
    assert result.invalid == ("garbage.zim",)
    assert library.books() == ()
    (zim_dir / "garbage.zim").unlink()
    assert library.reconcile().invalid == ()


def test_reconcile_ignores_other_files(library: Library, tmp_path: Path, test_zim: Path) -> None:
    zim_dir = tmp_path / "archive" / "zims"
    shutil.copy(test_zim, zim_dir / "notes.txt")
    assert library.reconcile().added == ()


def test_delete_removes_file_and_entry(library: Library, tmp_path: Path, test_zim: Path) -> None:
    zim_dir = tmp_path / "archive" / "zims"
    shutil.copy(test_zim, zim_dir / test_zim.name)
    library.reconcile()
    library.delete(TEST_ZIM_ID)
    assert not (zim_dir / test_zim.name).exists()
    assert library.books() == ()
    with pytest.raises(LibraryError):
        library.delete(TEST_ZIM_ID)


def test_library_survives_reinitialize(
    library: Library, tmp_path: Path, test_zim: Path, kiwix_manage_bin: str
) -> None:
    zim_dir = tmp_path / "archive" / "zims"
    shutil.copy(test_zim, zim_dir / test_zim.name)
    library.reconcile()
    reopened = Library(tmp_path / "data" / "library.xml", zim_dir, kiwix_manage_bin)
    reopened.initialize()
    assert [book.id for book in reopened.books()] == [TEST_ZIM_ID]


def test_delete_refuses_paths_outside_zim_dir(library: Library, tmp_path: Path, test_zim: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    shutil.copy(test_zim, outside / test_zim.name)
    library_file = tmp_path / "data" / "library.xml"
    # Simulate a library entry pointing outside the ZIM directory (e.g. a hand-edited library file).
    library_file.write_text(
        f'<library version="20110515"><book id="{TEST_ZIM_ID}" path="{outside / test_zim.name}" title="x"/></library>'
    )
    with pytest.raises(LibraryError, match="outside the ZIM directory"):
        library.delete(TEST_ZIM_ID)
    assert (outside / test_zim.name).exists()


def test_reconcile_waits_for_the_server(tmp_path: Path, test_zim: Path, kiwix_manage_bin: str) -> None:
    seen: list[frozenset[str]] = []
    data = tmp_path / "data"
    data.mkdir()
    zim_dir = tmp_path / "zims"
    instance = Library(data / "library.xml", zim_dir, kiwix_manage_bin, wait_for_server=seen.append)
    instance.initialize()
    shutil.copy(test_zim, zim_dir / test_zim.name)
    instance.reconcile()
    (zim_dir / test_zim.name).unlink()
    instance.reconcile()
    assert seen == [frozenset({TEST_ZIM_ID}), frozenset()]
