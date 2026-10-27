from __future__ import annotations

import os
import stat
from collections import Counter
from collections.abc import Mapping
from typing import TYPE_CHECKING

from goodnotes_cli.errors import GoodnotesError

from .records import (
    _FILE_NAME,
    _KEY,
    _STAGING_MARKER,
    _EntryRecord,
    _PreparedFile,
)

if TYPE_CHECKING:
    from .blobs import ArtifactBlobs
    from .filesystem import ArtifactFilesystem
    from .io import ArtifactIO
    from .metadata import ArtifactMetadata


class ArtifactRetention:
    def __init__(
        self,
        fs: ArtifactFilesystem,
        io: ArtifactIO,
        blobs: ArtifactBlobs,
        metadata: ArtifactMetadata,
        records: dict[str, _EntryRecord],
        active_stages: dict[str, tuple[int, int]],
    ) -> None:
        self._fs, self._io, self._blobs, self._metadata = fs, io, blobs, metadata
        self._records, self._active_stages = records, active_stages

    def _scan_entries(self) -> dict[str, _EntryRecord]:
        self._fs._verify_managed_directory("entries")
        try:
            items = list(os.scandir(self._fs.entries))
        except OSError as exc:
            raise GoodnotesError(f"Could not inspect artifact entries: {self._fs.entries}") from exc
        records: dict[str, _EntryRecord] = {}
        png_references: Counter[str] = Counter()
        blob_map = self._blobs._blob_inventory()
        for item in items:
            if _KEY.fullmatch(item.name) is None:
                raise GoodnotesError(f"Unexpected item in artifact entries: {item.name}")
            path = self._fs.entries / item.name
            if stat.S_ISLNK(item.stat(follow_symlinks=False).st_mode):
                raise GoodnotesError(f"Artifact entry must not be a symlink: {path}")
            record = self._metadata._read_entry(
                path,
                expected_key=item.name,
                blob_map=blob_map,
                verify_blobs=False,
                verify_files=False,
            )
            records[item.name] = record
            for file in record.files:
                if file.kind == "png":
                    assert file.digest is not None
                    png_references[file.digest] += 1

        stage_references = self._active_stage_png_references(blob_map)
        for digest, info in blob_map.items():
            expected_links = 1 + png_references[digest] + stage_references[digest]
            if info.st_nlink != expected_links:
                raise GoodnotesError(
                    f"Artifact blob has unexpected hardlinks: {self._fs.blobs / (digest + '.png')}"
                )
        missing = set(png_references) - set(blob_map)
        if missing:
            raise GoodnotesError("An artifact entry refers to a missing generated PNG blob.")
        self._blobs._blob_digests_by_stat = {
            self._fs._content_signature(info): digest for digest, info in blob_map.items()
        }
        return records

    def _active_stage_png_references(self, blob_map: Mapping[str, os.stat_result]) -> Counter[str]:
        references: Counter[str] = Counter()
        for name, identity in self._active_stages.items():
            stage = self._fs.staging_root / name
            info = self._fs._directory(stage, purpose="artifact staging directory")
            if self._fs._identity(info) != identity:
                raise GoodnotesError(f"Artifact staging directory changed: {stage}")
            self._io._verify_staging_marker(
                stage, kind="staging", token=name.removeprefix(".staging-")
            )
            for child in os.scandir(stage):
                if child.name == _STAGING_MARKER:
                    continue
                if _FILE_NAME.fullmatch(child.name) is None:
                    raise GoodnotesError(f"Invalid staged artifact filename: {child.name}")
                path = stage / child.name
                file_info = self._fs._regular_file(path, purpose="staged artifact file")
                self._fs._require_owner(file_info, purpose="staged artifact file")
                if child.name.endswith(".png") and file_info.st_nlink > 1:
                    signature = self._fs._content_signature(file_info)
                    digest = self._blobs._blob_digests_by_stat.get(signature)
                    if digest is None:
                        raise GoodnotesError(
                            "A staged PNG hardlink must refer to a verified artifact cache blob."
                        )
                    blob_info = blob_map.get(digest)
                    if blob_info is None or self._fs._identity(file_info) != self._fs._identity(
                        blob_info
                    ):
                        raise GoodnotesError(
                            "A staged PNG hardlink must refer to a verified artifact cache blob."
                        )
                    self._blobs._verified_blob(digest)
                    references[digest] += 1
                elif file_info.st_nlink != 1:
                    raise GoodnotesError(f"Staged non-image files must not be hardlinked: {path}")
        return references

    def _entry_is_staged(self, record: _EntryRecord) -> bool:
        protected_digests = self._active_stage_png_references(self._blobs._blob_inventory())
        return any(item.kind == "png" and item.digest in protected_digests for item in record.files)

    def _projected_size(
        self,
        prepared: tuple[_PreparedFile, ...],
        marker_size: int,
        *,
        records: Mapping[str, _EntryRecord] | None = None,
        new_marker: bool = True,
    ) -> int:
        metadata_size = marker_size if new_marker else 0
        payload_size = 0
        png_digests: set[str] = set()
        blob_sizes: dict[str, int] = {}
        for record in (self._records if records is None else records).values():
            metadata_size += record.marker_size
            for item in record.files:
                if item.kind == "png":
                    assert item.digest is not None
                    png_digests.add(item.digest)
                    blob_sizes[item.digest] = item.size
                else:
                    payload_size += item.size
        new_png_sizes: dict[str, int] = {}
        for item in prepared:
            if item.kind == "png":
                assert item.digest is not None
                png_digests.add(item.digest)
                new_png_sizes[item.digest] = item.size
            else:
                payload_size += item.size
        for digest in png_digests:
            if digest in blob_sizes:
                continue
            if digest not in new_png_sizes:
                # A PNG used by a remaining entry must already have a canonical blob.
                raise GoodnotesError("An artifact entry refers to an unavailable PNG blob.")
            blob_sizes[digest] = new_png_sizes[digest]
        return metadata_size + payload_size + sum(blob_sizes.values())

    def _storage_size(self) -> int:
        return self._projected_size((), 0, new_marker=False)
