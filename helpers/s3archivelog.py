#!/usr/bin/env python3
"""Standalone archive-log staging helper.

Supports mounted S3/local filesystem paths by default. If source_base starts with s3://,
it uses awscli when available.
"""

import fnmatch
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path


class ArchiveLogStager:
    def __init__(self, source_base: str, local_base: str, archive_pattern="*_archivelog_1*.tar.gz", tar_bin="/usr/bin/tar", logger=None):
        self.source_base = source_base.rstrip("/") + "/"
        self.local_base = local_base.rstrip("/") + "/"
        self.archive_pattern = archive_pattern
        self.tar_bin = tar_bin
        self.logger = logger
        self.staged_days = set()
        self.processed_days = []
        self.requested_days = []
        self.bridge_days = []
        self.current_run_candidate_files = []
        self.source_archivelog_tar_files = []
        self.local_archivelog_tar_files = []

    def _log(self, level, message):
        if self.logger:
            getattr(self.logger, level)(message)

    @staticmethod
    def date_range(start_date_obj: datetime, days_to_scan: int):
        for i in range(int(days_to_scan) + 1):
            yield (start_date_obj + timedelta(days=i)).strftime("%Y_%m_%d")

    def _is_s3_uri(self, path: str) -> bool:
        return path.startswith("s3://")

    def _is_target_archivelog_tar(self, file_name: str) -> bool:
        return fnmatch.fnmatch(file_name.lower(), self.archive_pattern.lower())

    def _safe_move_flattened_file(self, src: str, dst_dir: str) -> str:
        base = os.path.basename(src)
        dst = os.path.join(dst_dir, base)
        if os.path.abspath(src) == os.path.abspath(dst):
            return dst
        if not os.path.exists(dst):
            shutil.move(src, dst)
            return dst
        name, ext = os.path.splitext(base)
        counter = 1
        while True:
            candidate = os.path.join(dst_dir, f"{name}_{counter}{ext}")
            if not os.path.exists(candidate):
                shutil.move(src, candidate)
                return candidate
            counter += 1

    def _copy_from_s3(self, source_date_uri: str, local_dir: str) -> list[str]:
        cmd = ["aws", "s3", "cp", source_date_uri, local_dir, "--recursive", "--exclude", "*", "--include", self.archive_pattern]
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise RuntimeError(f"aws s3 cp failed: {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}")
        return [str(p) for p in Path(local_dir).glob(self.archive_pattern)]

    def _copy_from_filesystem(self, source_date_dir: str, local_dir: str) -> list[str]:
        if not os.path.isdir(source_date_dir):
            self._log("warning", f"Source date directory not found: {source_date_dir}")
            return []
        copied = []
        for file_name in sorted(os.listdir(source_date_dir)):
            src = os.path.join(source_date_dir, file_name)
            if not os.path.isfile(src):
                continue
            if not self._is_target_archivelog_tar(file_name):
                self._log("info", f"Skipping file because it does not match {self.archive_pattern}: {file_name}")
                continue
            dst = os.path.join(local_dir, file_name)
            shutil.copy2(src, dst)
            copied.append(dst)
            self.source_archivelog_tar_files.append(src)
        return copied

    def stage_archivelog_date(self, date_str: str, role="REQUESTED") -> bool:
        if date_str in self.staged_days:
            self._log("info", f"Date {date_str} is already staged. Role={role}.")
            return True
        local_dir = os.path.join(self.local_base, date_str)
        os.makedirs(local_dir, exist_ok=True)
        try:
            if self._is_s3_uri(self.source_base):
                source_date = self.source_base + date_str + "/"
                local_tars = self._copy_from_s3(source_date, local_dir)
                self.source_archivelog_tar_files.extend([source_date + os.path.basename(p) for p in local_tars])
            else:
                source_date = os.path.join(self.source_base, date_str)
                local_tars = self._copy_from_filesystem(source_date, local_dir)
            if not local_tars:
                self._log("warning", f"No matching archive-log tar files found for {date_str}. Pattern: {self.archive_pattern}")
                return False
            for tar_file in local_tars:
                self.local_archivelog_tar_files.append(tar_file)
                extract_dir = os.path.join(local_dir, f".extract_{date_str}_{os.path.basename(tar_file).replace('.tar.gz', '')}")
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.makedirs(extract_dir, exist_ok=True)
                proc = subprocess.run([self.tar_bin, "-xzf", tar_file, "-C", extract_dir], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if proc.returncode != 0:
                    raise RuntimeError(f"tar extraction failed for {tar_file}\n{proc.stdout}\n{proc.stderr}")
                for root, _, files in os.walk(extract_dir, topdown=False):
                    for extracted_file in files:
                        src_extracted = os.path.join(root, extracted_file)
                        if os.path.isfile(src_extracted):
                            final_path = self._safe_move_flattened_file(src_extracted, local_dir)
                            self.current_run_candidate_files.append(final_path)
                shutil.rmtree(extract_dir, ignore_errors=True)
            for root, dirs, files in os.walk(local_dir):
                for d in dirs:
                    os.chmod(os.path.join(root, d), 0o775)
                for f in files:
                    os.chmod(os.path.join(root, f), 0o664)
            self.staged_days.add(date_str)
            self.processed_days.append(date_str)
            if role == "REQUESTED":
                self.requested_days.append(date_str)
            elif role == "BRIDGE":
                self.bridge_days.append(date_str)
            return True
        except Exception as exc:
            self._log("error", f"Stage/extract error for {date_str}: {exc}")
            return False

    def stage_requested_dates(self, start_date_obj: datetime, days_to_scan: int) -> bool:
        return any(self.stage_archivelog_date(date_str, "REQUESTED") for date_str in self.date_range(start_date_obj, days_to_scan))

    def stage_bridge_date_by_index(self, start_date_obj: datetime, lookback_index: int) -> bool:
        return self.stage_archivelog_date((start_date_obj - timedelta(days=lookback_index)).strftime("%Y_%m_%d"), "BRIDGE")

    def collect_candidate_files(self) -> list[str]:
        candidates = []
        for full_path in self.current_run_candidate_files:
            if os.path.isfile(full_path) and not full_path.endswith(".tar.gz") and os.path.getsize(full_path) > 0:
                candidates.append(full_path)
        self._log("info", f"Current-run candidate files selected for RMAN catalog: {len(candidates)}")
        return candidates
