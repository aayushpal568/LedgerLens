"""FileSource adapters.

- LocalPathFileSource: files already materialized on the local disk (used by
  the web build after downloading uploads to a temp dir).
- LocalDirectoryFileSource: walks a real folder tree (the Windows/Tauri build
  points this at a client folder — nested subfolders, safe skipping of
  unreadable entries).
"""
import os
from typing import List, Optional

from ..interfaces import FileSource
from ..models import FileRef
from ..checklist import SUPPORTED_EXTENSIONS


class LocalPathFileSource(FileSource):
    def __init__(self, records):
        """`records` is an iterable of dicts or FileRefs with a local `path`."""
        self._refs: List[FileRef] = []
        for r in records:
            if isinstance(r, FileRef):
                self._refs.append(r)
            else:
                self._refs.append(FileRef(
                    id=r["id"], name=r["name"], ext=(r.get("ext") or "").lower(),
                    size=r.get("size", 0), path=r.get("path"),
                ))

    def list_files(self) -> List[FileRef]:
        return list(self._refs)

    def open(self, ref: FileRef) -> str:
        if not ref.path:
            raise FileNotFoundError(f"No local path for {ref.name}")
        return ref.path


class LocalDirectoryFileSource(FileSource):
    """Scans an on-disk directory tree. Ready for the local Windows build.

    Note: not yet exercised in a real Windows environment — validated here only
    on POSIX sample folders via the engine tests.
    """

    def __init__(self, root: str, supported_only: bool = False,
                 supported_exts: Optional[set] = None, follow_symlinks: bool = False):
        self.root = root
        self.supported_only = supported_only
        self.supported_exts = set(supported_exts or SUPPORTED_EXTENSIONS)
        self.follow_symlinks = follow_symlinks
        self.errors: List[dict] = []  # dirs/files we could not access

    def list_files(self) -> List[FileRef]:
        refs: List[FileRef] = []
        for dirpath, _dirs, files in os.walk(self.root, followlinks=self.follow_symlinks, onerror=self._on_walk_error):
            for fname in files:
                full = os.path.join(dirpath, fname)
                ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                if self.supported_only and ext not in self.supported_exts:
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                    self.errors.append({"name": fname, "reason": "Could not stat file"})
                refs.append(FileRef(id=full, name=fname, ext=ext, size=size, path=full))
        return refs

    def open(self, ref: FileRef) -> str:
        if not ref.path or not os.path.isfile(ref.path):
            raise FileNotFoundError(ref.name)
        if not os.access(ref.path, os.R_OK):
            raise PermissionError(ref.name)
        return ref.path

    def _on_walk_error(self, err):
        self.errors.append({"name": getattr(err, "filename", "?"), "reason": "Inaccessible directory"})
