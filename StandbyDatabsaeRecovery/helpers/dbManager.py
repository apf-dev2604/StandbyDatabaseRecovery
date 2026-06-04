#!/usr/bin/env python3
"""Portable Oracle SQL*Plus and RMAN execution manager."""

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple


class DbManager:
    def __init__(self, sid: str, db_home: str, query_file: Optional[str] = None, logger=None):
        self.sid = sid
        self.db_home = db_home
        self.sql_bin = os.path.join(db_home, "bin", "sqlplus")
        self.rman_bin = os.path.join(db_home, "bin", "rman")
        self.logger = logger
        self.query_file = query_file
        self.queries = self._load_queries(query_file) if query_file else {}
        self.env = os.environ.copy()
        self.env.update({
            "ORACLE_SID": sid,
            "ORACLE_HOME": db_home,
            "LD_LIBRARY_PATH": f"{db_home}/lib:{db_home}/network/lib",
            "PATH": f"{db_home}/bin:/usr/bin:/bin:{self.env.get('PATH', '')}",
        })

    def _load_queries(self, query_file: str) -> Dict[str, str]:
        queries: Dict[str, list[str]] = {}
        current = None
        path = Path(query_file)
        if not path.exists():
            raise FileNotFoundError(f"SQL query file not found: {query_file}")
        for line in path.read_text(encoding="utf-8").splitlines():
            marker = re.match(r"^\s*--\s*name:\s*([A-Za-z0-9_]+)\s*$", line)
            if marker:
                current = marker.group(1)
                queries[current] = []
                continue
            if current:
                queries[current].append(line)
        return {name: "\n".join(lines).strip() for name, lines in queries.items()}

    def get_query(self, name: str, **kwargs) -> str:
        if name not in self.queries:
            raise KeyError(f"Query '{name}' not found in {self.query_file}")
        return self.queries[name].format(**kwargs)

    def _safe_exec(self, cmd: list[str], input_str: Optional[str] = None) -> Tuple[str, str, int]:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, text=True)
            stdout, stderr = proc.communicate(input=input_str)
            return stdout or "", stderr or "", proc.returncode
        except Exception as exc:
            return "", str(exc), 1

    @staticmethod
    def has_oracle_error(text: str) -> bool:
        return bool(re.search(r"\b(ORA|SP2|RMAN|LRM)-\d{4,5}\b", text or "", re.IGNORECASE))

    @staticmethod
    def rman_has_fatal_error(text: str) -> bool:
        fatal_patterns = [
            r"\bRMAN-00569\b", r"\bRMAN-03002\b", r"\bRMAN-03009\b",
            r"\bRMAN-06025\b", r"\bRMAN-06053\b", r"\bRMAN-06054\b", r"\bORA-\d{5}\b",
        ]
        return any(re.search(pattern, text or "", re.IGNORECASE) for pattern in fatal_patterns)

    @staticmethod
    def clean_sql_output(output: str) -> str:
        cleaned = []
        noise_prefixes = ("SQL>", "Connected to:", "Oracle Database", "Copyright", "Last Successful login")
        for line in (output or "").splitlines():
            s = line.strip()
            if not s or any(s.startswith(prefix) for prefix in noise_prefixes):
                continue
            cleaned.append(s)
        return "\n".join(cleaned).strip()

    def run_sql(self, sql: str, raise_on_error: bool = True) -> str:
        sql_script = f"""
SET SQLBLANKLINES ON
SET FEEDBACK OFF
SET HEADING OFF
SET PAGESIZE 0
SET LINESIZE 1000
SET TRIMSPOOL ON
SET TAB OFF
WHENEVER SQLERROR EXIT SQL.SQLCODE
WHENEVER OSERROR EXIT FAILURE
{sql}
EXIT;
"""
        stdout, stderr, rc = self._safe_exec([self.sql_bin, "-L", "-S", "/", "as", "sysdba"], sql_script)
        combined = f"{stdout}\n{stderr}".strip()
        if raise_on_error and (rc != 0 or self.has_oracle_error(combined)):
            raise RuntimeError(f"SQL failed. RC={rc}\n{combined}")
        return self.clean_sql_output(stdout)

    def run_query(self, name: str, raise_on_error: bool = True, **kwargs) -> str:
        return self.run_sql(self.get_query(name, **kwargs), raise_on_error=raise_on_error)

    def run_sql_no_raise(self, sql: str) -> str:
        try:
            return self.run_sql(sql, raise_on_error=False)
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"SQL warning ignored: {exc}")
            return ""

    def run_query_no_raise(self, name: str, **kwargs) -> str:
        try:
            return self.run_query(name, raise_on_error=False, **kwargs)
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"SQL query warning ignored: {name}: {exc}")
            return ""

    def run_rman(self, rman_script: str, raise_on_error: bool = True) -> tuple[str, int]:
        stdout, stderr, rc = self._safe_exec([self.rman_bin, "target", "/"], rman_script)
        combined = f"{stdout}\n{stderr}".strip()
        if raise_on_error and (rc != 0 or self.rman_has_fatal_error(combined)):
            raise RuntimeError(f"RMAN failed. RC={rc}\n{combined}")
        return combined, rc

    @staticmethod
    def rman_catalog_success(text: str) -> bool:
        lower = (text or "").lower()
        return any(phrase in lower for phrase in ["cataloged backup piece", "cataloged archived log", "cataloged archive log", "already cataloged"])

    def catalog_file(self, file_path: str) -> tuple[bool, str]:
        escaped = file_path.replace("'", "''")
        output, _ = self.run_rman(f"CATALOG BACKUPPIECE '{escaped}';\nEXIT;\n", raise_on_error=False)
        if self.rman_catalog_success(output):
            return True, "BACKUPPIECE"
        output, _ = self.run_rman(f"CATALOG ARCHIVELOG '{escaped}';\nEXIT;\n", raise_on_error=False)
        if self.rman_catalog_success(output):
            return True, "ARCHIVELOG"
        return False, "UNKNOWN"

    def uncatalog_file(self, file_path: str, file_type: str) -> tuple[bool, str]:
        escaped = file_path.replace("'", "''")
        if file_type == "BACKUPPIECE":
            script = f"CHANGE BACKUPPIECE '{escaped}' UNCATALOG;\nEXIT;\n"
        elif file_type == "ARCHIVELOG":
            script = f"CHANGE ARCHIVELOG LIKE '{escaped}' UNCATALOG;\nEXIT;\n"
        else:
            return False, "Unknown catalog type"
        output, rc = self.run_rman(script, raise_on_error=False)
        return rc == 0 and not self.rman_has_fatal_error(output), output

    def recover_database_until_scn(self, target_scn: int) -> tuple[str, int]:
        script = f"""
RUN {{
    SET UNTIL SCN {target_scn};
    RECOVER DATABASE;
}}
EXIT;
"""
        return self.run_rman(script, raise_on_error=True)
