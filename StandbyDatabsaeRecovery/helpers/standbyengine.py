#!/usr/bin/env python3
"""Portable standby refresh engine. Business logic lives here; DB execution lives in DbManager."""

import logging
import os
import sys
from collections import defaultdict
from datetime import datetime

from helpers.dbManager import DbManager
from helpers.s3archivelog import ArchiveLogStager


class StandbyEngine:
    def __init__(self, config, start_date, days_input=0, db_manager=None, mailer=None, messager=None, logger=None):
        self.config = config
        self.sid = config.get("oracle", "sid")
        self.db_home = config.get("oracle", "db_home")
        self.local_base = config.get("paths", "local_base")
        self.start_date_obj = datetime.strptime(start_date, "%Y_%m_%d")
        self.days_to_scan = int(days_input)
        self.logger = logger or logging.getLogger("StandbyEngine")
        self.mailer = mailer
        self.messager = messager
        self.db = db_manager or DbManager(self.sid, self.db_home, config.get("paths", "query_file"), logger=self.logger)
        self.stager = ArchiveLogStager(
            source_base=config.get("paths", "s3_base"),
            local_base=self.local_base,
            archive_pattern=config.get("recovery", "archive_pattern", fallback="*_archivelog_1*.tar.gz"),
            tar_bin=config.get("paths", "tar_bin", fallback="/usr/bin/tar"),
            logger=self.logger,
        )
        self.bridge_lookback_days = config.getint("recovery", "bridge_lookback_days", fallback=1)
        self.open_read_only_after_recovery = config.getboolean("recovery", "open_read_only", fallback=True)
        self.cleanup_local_after_success = config.getboolean("cleanup", "cleanup_local_after_success", fallback=True)
        self.uncatalog_local_after_success = config.getboolean("cleanup", "uncatalog_local_after_success", fallback=True)
        self.cleanup_local_on_rejected_run = config.getboolean("cleanup", "cleanup_local_on_rejected_run", fallback=True)
        self.uncatalog_local_on_rejected_run = config.getboolean("cleanup", "uncatalog_local_on_rejected_run", fallback=True)
        self.strict_old_logs_stop = config.getboolean("recovery", "strict_old_logs_stop", fallback=False)
        self.fail_if_no_forward = config.getboolean("recovery", "fail_if_no_forward", fallback=False)
        self.cataloged_files = []
        self.cataloged_path_set = set()
        self.archive_log_details = []
        self.usable_forward_archive_logs = []
        self.old_or_already_applied_archive_logs = []
        self.before_recovery_scn = 0
        self.target_recovery_scn = 0
        self.after_recovery_scn = 0
        self.current_branch = "UNKNOWN"
        self.archivelog_branches = set()
        self.recovery_applied = False
        self.validation_status = "NOT_STARTED"
        self.recovery_status = "NOT_STARTED"
        self.cleanup_deleted_files = []
        self.cleanup_skipped_files = []
        self.uncataloged_files = []
        self.uncatalog_failed_files = []
        self.summary_file = None

    @staticmethod
    def _sql_literal_list(values):
        if not values:
            return None
        return ",".join("'" + value.replace("'", "''") + "'" for value in values)

    @staticmethod
    def _parse_int_or_zero(value):
        try:
            lines = [x.strip() for x in (value or "").splitlines() if x.strip() and not x.strip().upper().startswith(("SP2-", "ORA-"))]
            return int(lines[-1]) if lines else 0
        except Exception:
            return 0

    @staticmethod
    def _split_pipe_rows(sql_output, expected_columns):
        rows = []
        for line in (sql_output or "").splitlines():
            parts = line.strip().split("|")
            if len(parts) == expected_columns:
                rows.append(parts)
        return rows

    @staticmethod
    def _is_under_directory(file_path, base_dir):
        try:
            return os.path.commonpath([os.path.realpath(file_path), os.path.realpath(base_dir)]) == os.path.realpath(base_dir)
        except Exception:
            return False

    def notify_error(self, subject, body):
        if self.mailer:
            try:
                self.mailer.send(subject, body, attachments=[self.summary_file] if self.summary_file else None)
            except Exception as exc:
                self.logger.warning(f"Mailer failed: {exc}")
        if self.messager:
            try:
                self.messager.send(f"{subject}\n{body}")
            except Exception as exc:
                self.logger.warning(f"Messager failed: {exc}")

    def get_open_mode(self):
        result = self.db.run_query_no_raise("get_open_mode")
        if not result:
            return "IDLE_OR_UNKNOWN"
        if "ORA-01034" in result or "not available" in result.lower():
            return "IDLE_OR_UNKNOWN"
        return result.strip()

    def prepare_db_state(self):
        self.logger.info("--- Phase 0: Database State Verification ---")
        status = self.get_open_mode()
        self.logger.info(f"Current DB status: {status}")
        if status == "IDLE_OR_UNKNOWN":
            self.logger.warning("Database appears down or idle. Attempting STARTUP MOUNT.")
            self.db.run_sql("STARTUP MOUNT;", raise_on_error=False)
            status = self.get_open_mode()
        elif "MOUNTED" not in status:
            self.logger.info(f"Database is currently {status}. Restarting to MOUNT.")
            self.db.run_sql("SHUTDOWN ABORT;", raise_on_error=False)
            self.db.run_sql("STARTUP MOUNT;", raise_on_error=False)
            status = self.get_open_mode()
        if "MOUNTED" not in status:
            raise RuntimeError(f"Database is not mounted after preparation. Current status: {status}")
        self.logger.info("Database is verified in MOUNTED state.")

    def catalog_new_files(self):
        self.logger.info("--- RMAN Catalog Registration ---")
        candidates = self.stager.collect_candidate_files()
        if not candidates:
            self.logger.error("No candidate archive-log files found after extraction.")
            return False
        for file_path in candidates:
            if file_path in self.cataloged_path_set:
                continue
            ok, file_type = self.db.catalog_file(file_path)
            if ok:
                self.logger.info(f"[CATALOGED {file_type}] {file_path}")
                self.cataloged_files.append((file_path, file_type))
                self.cataloged_path_set.add(file_path)
            else:
                self.logger.warning(f"[NOT CATALOGED] {file_path}")
        self.logger.info(f"Total current-run cataloged files: {len(self.cataloged_files)}")
        return len(self.cataloged_files) > 0

    def _parse_branch_rows(self, sql_output):
        return {line.strip() for line in (sql_output or "").splitlines() if "|" in line.strip()}

    def get_current_resetlogs_branch(self):
        return self.db.run_query("get_current_branch").strip()

    def get_cataloged_backup_redolog_branches(self):
        handles = self._sql_literal_list([p for p, t in self.cataloged_files if t == "BACKUPPIECE"])
        return set() if not handles else self._parse_branch_rows(self.db.run_query_no_raise("backup_redolog_branches", handles=handles))

    def get_cataloged_raw_archivelog_branches(self):
        names = self._sql_literal_list([p for p, t in self.cataloged_files if t == "ARCHIVELOG"])
        return set() if not names else self._parse_branch_rows(self.db.run_query_no_raise("raw_archivelog_branches", names=names))

    def check_branch_compatibility(self):
        self.logger.info("--- Branch Compatibility Check ---")
        self.current_branch = self.get_current_resetlogs_branch()
        self.archivelog_branches = self.get_cataloged_backup_redolog_branches().union(self.get_cataloged_raw_archivelog_branches())
        self.logger.info(f"Current DB branch: {self.current_branch}")
        self.logger.info(f"Archive-log branch: {', '.join(sorted(self.archivelog_branches)) if self.archivelog_branches else 'NONE'}")
        if not self.archivelog_branches:
            self.validation_status = "FAILED_NO_ARCHIVELOG_BRANCH_METADATA"
            return False
        if self.current_branch not in self.archivelog_branches:
            self.validation_status = "FAILED_BRANCH_MISMATCH"
            self.logger.warning("Safety stop: cataloged archive logs do not match the mounted database branch.")
            return False
        return True

    def get_current_datafile_scn(self):
        return self._parse_int_or_zero(self.db.run_query("get_current_datafile_scn"))

    def collect_archive_log_details(self, current_db_scn):
        self.archive_log_details = []
        self.usable_forward_archive_logs = []
        self.old_or_already_applied_archive_logs = []
        handles = self._sql_literal_list([p for p, t in self.cataloged_files if t == "BACKUPPIECE"])
        names = self._sql_literal_list([p for p, t in self.cataloged_files if t == "ARCHIVELOG"])
        if handles:
            for row in self._split_pipe_rows(self.db.run_query_no_raise("backup_redolog_details", handles=handles), 7):
                self._add_archive_detail("BACKUP_REDOLOG", row, current_db_scn)
        if names:
            for row in self._split_pipe_rows(self.db.run_query_no_raise("raw_archivelog_details", names=names), 7):
                self._add_archive_detail("ARCHIVED_LOG", row, current_db_scn)
        self.logger.info(f"Archive logs inspected: {len(self.archive_log_details)}")
        self.logger.info(f"Usable forward archive logs: {len(self.usable_forward_archive_logs)}")
        self.logger.info(f"Old/already-applied archive logs: {len(self.old_or_already_applied_archive_logs)}")
        return len(self.archive_log_details) > 0

    def _add_archive_detail(self, source_type, row, current_db_scn):
        path, thread_no, sequence_no, first_scn, next_scn, first_time, next_time = row
        detail = {"source_type": source_type, "path": path, "thread": int(thread_no), "sequence": int(sequence_no), "first_scn": int(first_scn), "next_scn": int(next_scn), "first_time": first_time, "next_time": next_time, "status": "UNCLASSIFIED"}
        if detail["next_scn"] > current_db_scn:
            detail["status"] = "USABLE_FORWARD"
            self.usable_forward_archive_logs.append(detail)
        else:
            detail["status"] = "OLD_OR_ALREADY_APPLIED"
            self.old_or_already_applied_archive_logs.append(detail)
        self.archive_log_details.append(detail)

    def validate_forward_recovery_plan(self):
        if not self.archive_log_details:
            self.validation_status = "FAILED_NO_ARCHIVELOG_METADATA"
            return False
        if self.strict_old_logs_stop and self.old_or_already_applied_archive_logs:
            self.validation_status = "FAILED_OLD_ARCHIVELOGS_PRESENT_STRICT_MODE"
            return False
        if not self.usable_forward_archive_logs:
            self.validation_status = "NO_FORWARD_ARCHIVELOGS"
            return False
        logs_by_thread = defaultdict(list)
        for detail in self.usable_forward_archive_logs:
            logs_by_thread[detail["thread"]].append(detail)
        for thread_no, logs in logs_by_thread.items():
            logs.sort(key=lambda item: item["sequence"])
            if logs[0]["first_scn"] > self.before_recovery_scn:
                self.validation_status = "FAILED_START_SCN_GAP"
                self.logger.error(f"Archive-log SCN gap detected. THREAD={thread_no} FIRST_SEQ={logs[0]['sequence']} CURRENT_DB_SCN={self.before_recovery_scn}")
                return False
            previous_sequence = None
            for detail in logs:
                if previous_sequence is not None and detail["sequence"] != previous_sequence + 1:
                    self.validation_status = "FAILED_SEQUENCE_GAP"
                    return False
                previous_sequence = detail["sequence"]
        self.target_recovery_scn = max(detail["next_scn"] for detail in self.usable_forward_archive_logs)
        if self.target_recovery_scn <= self.before_recovery_scn:
            self.validation_status = "FAILED_TARGET_NOT_FORWARD"
            return False
        self.validation_status = "PASSED_FORWARD_ONLY"
        self.logger.info(f"Forward-only validation passed. Target SCN will be {self.target_recovery_scn}.")
        return True

    def log_archive_gap(self):
        gap = self.db.run_query_no_raise("archive_gap")
        self.logger.warning(f"V$ARCHIVE_GAP reported: {gap}") if gap and "-" in gap else self.logger.info("V$ARCHIVE_GAP: no gap reported by control file.")

    def log_archive_coverage_from_details(self):
        if not self.archive_log_details:
            self.logger.warning("No archive-log details found for coverage summary.")
            return
        by_thread = defaultdict(list)
        for detail in self.archive_log_details:
            by_thread[detail["thread"]].append(detail)
        for thread_no, logs in sorted(by_thread.items()):
            self.logger.info(f"THREAD={thread_no} SEQ_RANGE={min(x['sequence'] for x in logs)}-{max(x['sequence'] for x in logs)} MAX_NEXT_SCN={max(x['next_scn'] for x in logs)}")

    def refresh_metadata_and_validate(self):
        if not self.catalog_new_files():
            self.validation_status = "FAILED_CATALOG"
            return False
        if not self.check_branch_compatibility():
            return False
        self.collect_archive_log_details(self.before_recovery_scn)
        self.log_archive_coverage_from_details()
        return self.validate_forward_recovery_plan()

    def uncatalog_current_run_files(self, allow_without_recovery=False):
        if not self.recovery_applied and not allow_without_recovery:
            self.logger.warning("Recovery was not applied. Uncatalog will not run.")
            return
        for file_path, file_type in self.cataloged_files:
            if not self._is_under_directory(file_path, self.local_base):
                self.uncatalog_failed_files.append(file_path)
                continue
            ok, output = self.db.uncatalog_file(file_path, file_type)
            if ok:
                self.uncataloged_files.append(file_path)
                self.logger.info(f"Uncataloged local staging file: {file_path}")
            else:
                self.uncatalog_failed_files.append(file_path)
                self.logger.warning(f"Could not uncatalog {file_path}. RMAN output:\n{output}")

    def cleanup_local_archivelog_files(self, allow_without_recovery=False):
        if not self.recovery_applied and not allow_without_recovery:
            self.logger.warning("Recovery was not applied. Cleanup will not run.")
            return
        unique_files = sorted(set(self.stager.current_run_candidate_files + self.stager.local_archivelog_tar_files))
        for file_path in unique_files:
            if not self._is_under_directory(file_path, self.local_base):
                self.cleanup_skipped_files.append(file_path)
                continue
            if not os.path.isfile(file_path):
                self.cleanup_skipped_files.append(file_path)
                continue
            try:
                os.remove(file_path)
                self.cleanup_deleted_files.append(file_path)
            except Exception:
                self.cleanup_skipped_files.append(file_path)
        for date_str in self.stager.processed_days:
            date_dir = os.path.join(self.local_base, date_str)
            try:
                if self._is_under_directory(date_dir, self.local_base) and os.path.isdir(date_dir) and not os.listdir(date_dir):
                    os.rmdir(date_dir)
            except Exception as exc:
                self.logger.warning(f"Could not remove local date directory {date_dir}: {exc}")

    def cleanup_rejected_or_no_forward_run(self, reason):
        self.logger.warning(f"Recovery did not proceed. Reason={reason}. Cleaning local staging files to preserve space.")
        if self.uncatalog_local_on_rejected_run:
            self.uncatalog_current_run_files(allow_without_recovery=True)
        if self.cleanup_local_on_rejected_run:
            self.cleanup_local_archivelog_files(allow_without_recovery=True)

    def perform_catalog_and_recovery(self):
        if not self.catalog_new_files():
            self.recovery_status = "FAILED_CATALOG"
            self.write_recovery_summary(self.recovery_status)
            return False
        if not self.check_branch_compatibility():
            self.recovery_status = self.validation_status
            self.cleanup_rejected_or_no_forward_run(self.recovery_status)
            self.write_recovery_summary(self.recovery_status)
            return False
        self.log_archive_gap()
        self.before_recovery_scn = self.get_current_datafile_scn()
        self.after_recovery_scn = self.before_recovery_scn
        self.collect_archive_log_details(self.before_recovery_scn)
        self.log_archive_coverage_from_details()
        valid = self.validate_forward_recovery_plan()
        if not valid and self.validation_status == "FAILED_START_SCN_GAP":
            for lookback_index in range(1, self.bridge_lookback_days + 1):
                if self.stager.stage_bridge_date_by_index(self.start_date_obj, lookback_index):
                    valid = self.refresh_metadata_and_validate()
                    if valid or self.validation_status != "FAILED_START_SCN_GAP":
                        break
        if not valid:
            self.recovery_status = self.validation_status
            self.cleanup_rejected_or_no_forward_run(self.recovery_status)
            self.write_recovery_summary(self.recovery_status)
            return True if self.recovery_status == "NO_FORWARD_ARCHIVELOGS" and not self.fail_if_no_forward else False
        self.db.recover_database_until_scn(self.target_recovery_scn)
        self.recovery_applied = True
        self.after_recovery_scn = self.get_current_datafile_scn()
        if self.after_recovery_scn <= self.before_recovery_scn:
            self.recovery_status = "FAILED_NO_SCN_ADVANCE_AFTER_RECOVERY"
            self.write_recovery_summary(self.recovery_status)
            return False
        self.recovery_status = "RECOVERY_APPLIED_SUCCESSFULLY"
        if self.uncatalog_local_after_success:
            self.uncatalog_current_run_files()
        if self.cleanup_local_after_success:
            self.cleanup_local_archivelog_files()
        if self.open_read_only_after_recovery:
            self.open_read_only()
        self.write_recovery_summary(self.recovery_status)
        return True

    def open_read_only(self):
        status = self.get_open_mode()
        if "READ ONLY" in status:
            return
        if "MOUNTED" not in status:
            self.db.run_sql("SHUTDOWN ABORT;", raise_on_error=False)
            self.db.run_sql("STARTUP MOUNT;", raise_on_error=False)
        self.db.run_sql("ALTER DATABASE OPEN READ ONLY;")
        self.logger.info(f"Database open mode after read-only open: {self.get_open_mode()}")

    def write_recovery_summary(self, recovery_status):
        summary_dir = os.path.join(self.local_base, "_refresh_summaries")
        os.makedirs(summary_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.summary_file = os.path.join(summary_dir, f"rman_refresh_summary_{self.sid}_{self.start_date_obj.strftime('%Y_%m_%d')}_{timestamp}.log")
        lines = ["RMAN DAILY ARCHIVE-LOG REFRESH SUMMARY", "=" * 80,
                 f"SID: {self.sid}", f"Days scanned inclusive: {self.days_to_scan}",
                 f"Source base: {self.stager.source_base}", f"Local base: {self.local_base}", f"Oracle home: {self.db_home}",
                 f"Requested days staged: {', '.join(self.stager.requested_days) if self.stager.requested_days else 'NONE'}",
                 f"Bridge days staged: {', '.join(self.stager.bridge_days) if self.stager.bridge_days else 'NONE'}",
                 f"Current DB branch: {self.current_branch}", f"Archive-log branches: {', '.join(sorted(self.archivelog_branches)) if self.archivelog_branches else 'NONE'}",
                 f"Validation status: {self.validation_status}", f"Recovery status: {recovery_status}", "", "SCN SUMMARY", "-" * 80,
                 f"Before recovery datafile SCN: {self.before_recovery_scn}", f"Target recovery SCN: {self.target_recovery_scn}",
                 f"After recovery datafile SCN: {self.after_recovery_scn}", f"Recovery applied: {self.recovery_applied}"]
        def add_section(title, items):
            lines.extend(["", title, "-" * 80])
            lines.extend(items if items else ["NONE"])
        add_section("SOURCE ARCHIVE TAR FILES", self.stager.source_archivelog_tar_files)
        add_section("LOCAL ARCHIVE TAR FILES", self.stager.local_archivelog_tar_files)
        add_section("CATALOGED CURRENT-RUN FILES", [f"{t}|{p}" for p, t in self.cataloged_files])
        add_section("FORWARD ARCHIVE LOGS USED TO CHOOSE TARGET SCN", [str(x) for x in self.usable_forward_archive_logs])
        add_section("OLD OR ALREADY APPLIED ARCHIVE LOGS EXCLUDED FROM TARGET SCN", [str(x) for x in self.old_or_already_applied_archive_logs])
        add_section("UNCATALOGED LOCAL STAGING FILES", self.uncataloged_files)
        add_section("UNCATALOG FAILED OR SKIPPED FILES", self.uncatalog_failed_files)
        add_section("CLEANUP DELETED LOCAL FILES", self.cleanup_deleted_files)
        add_section("CLEANUP SKIPPED OR FAILED LOCAL FILES", self.cleanup_skipped_files)
        with open(self.summary_file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        self.logger.info(f"Recovery summary written to: {self.summary_file}")

    def verify_data(self):
        self.logger.info("--- Phase 3: Data Verification ---")
        if "MOUNTED" in self.get_open_mode() and self.open_read_only_after_recovery:
            self.open_read_only()
        results = self.db.run_query_no_raise("verify_business_data")
        for line in results.splitlines():
            if "|" in line:
                tbl, ts = line.split("|", 1)
                self.logger.info(f"{tbl.ljust(35)} Latest: {ts}")

    def run(self):
        try:
            self.prepare_db_state()
            if not self.stager.stage_requested_dates(self.start_date_obj, self.days_to_scan):
                self.recovery_status = "FAILED_NO_REQUESTED_DATE_STAGED"
                self.write_recovery_summary(self.recovery_status)
                return False
            ok = self.perform_catalog_and_recovery()
            if ok:
                self.verify_data()
            self.logger.info(f"Final DB Status: {self.get_open_mode()}")
            return ok
        except Exception as exc:
            self.recovery_status = f"FATAL_ERROR: {exc}"
            self.logger.error(self.recovery_status)
            try:
                self.write_recovery_summary(self.recovery_status)
            except Exception:
                pass
            self.notify_error(f"Standby refresh failed for {self.sid}", self.recovery_status)
            raise
