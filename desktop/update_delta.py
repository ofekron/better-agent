from __future__ import annotations

import gzip
import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Mapping

import pyxdelta
from tufup.client import SUFFIX_FAILED, Client
from tufup.common import TargetMeta

DELTA_FORMAT = "xdelta3-v1"
_BUFFER_SIZE = 1024 * 1024

logger = logging.getLogger(__name__)


def _copy_and_fingerprint(source, target=None) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(_BUFFER_SIZE):
        if target is not None:
            target.write(chunk)
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def _fingerprint(tar_path: Path) -> tuple[int, str]:
    with tar_path.open("rb") as source:
        return _copy_and_fingerprint(source)


def _extract_tar(archive_path: Path, tar_path: Path) -> dict:
    with gzip.open(archive_path, "rb") as source, tar_path.open("wb") as target:
        size, digest = _copy_and_fingerprint(source, target)
    return {
        "tar_size": size,
        "tar_hash": digest,
        "tar_hash_algorithm": "sha256",
        "delta_format": DELTA_FORMAT,
    }


def _verify_tar(tar_path: Path, metadata: dict) -> None:
    if metadata.get("delta_format") != DELTA_FORMAT:
        raise ValueError("unsupported desktop update delta format")
    if metadata.get("tar_hash_algorithm") != "sha256":
        raise ValueError("unsupported desktop update hash algorithm")

    size, digest = _fingerprint(tar_path)
    if size != metadata.get("tar_size"):
        raise ValueError("desktop update tar size mismatch")
    if digest != metadata.get("tar_hash"):
        raise ValueError("desktop update tar hash mismatch")


def create_patch(
    source_archive: Path | str,
    target_archive: Path | str,
    patch_path: Path | str,
) -> dict:
    source_archive = Path(source_archive)
    target_archive = Path(target_archive)
    patch_path = Path(patch_path)
    patch_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ba-xdelta-") as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)
        source_tar = temp_dir / "source.tar"
        target_tar = temp_dir / "target.tar"
        _extract_tar(source_archive, source_tar)
        metadata = _extract_tar(target_archive, target_tar)

        descriptor, raw_patch_path = tempfile.mkstemp(
            prefix=f".{patch_path.name}.",
            dir=patch_path.parent,
        )
        os.close(descriptor)
        temporary_patch = Path(raw_patch_path)
        temporary_patch.unlink()
        try:
            if not pyxdelta.run(
                str(source_tar),
                str(target_tar),
                str(temporary_patch),
            ):
                raise RuntimeError("xdelta patch generation failed")
            os.replace(temporary_patch, patch_path)
        finally:
            temporary_patch.unlink(missing_ok=True)
    return metadata


def apply_patches(
    source_archive: Path | str,
    target_archive: Path | str,
    patch_targets: Mapping[TargetMeta, Path],
) -> None:
    if not patch_targets:
        raise ValueError("no desktop update patches")

    source_archive = Path(source_archive)
    target_archive = Path(target_archive)
    target_archive.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ba-xdelta-") as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)
        current_tar = temp_dir / "source.tar"
        _extract_tar(source_archive, current_tar)

        for index, (patch_meta, patch_path) in enumerate(sorted(patch_targets.items())):
            metadata = patch_meta.custom_internal
            if metadata.get("delta_format") != DELTA_FORMAT:
                raise ValueError("unsupported desktop update delta format")
            next_tar = temp_dir / f"target-{index}.tar"
            if not pyxdelta.decode(
                str(current_tar),
                str(patch_path),
                str(next_tar),
            ):
                raise RuntimeError("xdelta patch application failed")
            _verify_tar(next_tar, metadata)
            current_tar = next_tar

        descriptor, raw_archive_path = tempfile.mkstemp(
            prefix=f".{target_archive.name}.",
            dir=target_archive.parent,
        )
        os.close(descriptor)
        temporary_archive = Path(raw_archive_path)
        try:
            with current_tar.open("rb") as source, gzip.open(
                temporary_archive,
                "wb",
            ) as target:
                while chunk := source.read(_BUFFER_SIZE):
                    target.write(chunk)
            os.replace(temporary_archive, target_archive)
        finally:
            temporary_archive.unlink(missing_ok=True)


class TufupDeltaClient(Client):
    def _apply_updates(self, install, skip_confirmation, **kwargs):
        downloaded = self.downloaded_target_files
        if not downloaded or next(iter(downloaded)).is_archive:
            return super()._apply_updates(
                install=install,
                skip_confirmation=skip_confirmation,
                **kwargs,
            )

        try:
            if not all(target.is_patch for target in downloaded):
                raise ValueError("desktop update targets mix archives and patches")
            apply_patches(
                self.current_archive_local_path,
                self.new_archive_local_path,
                downloaded,
            )
        except Exception as error:
            for file_path in downloaded.values():
                failed_path = file_path.with_suffix(file_path.suffix + SUFFIX_FAILED)
                file_path.replace(failed_path)
            logger.error("desktop update patching aborted: %s", error)
            return None

        archive_target = TargetMeta(target_path=self.new_archive_local_path)
        self.downloaded_target_files = {
            archive_target: self.new_archive_local_path,
        }
        return super()._apply_updates(
            install=install,
            skip_confirmation=skip_confirmation,
            **kwargs,
        )
