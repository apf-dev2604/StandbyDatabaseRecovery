#!/bin/bash
set -e

DATE_ARG="$1"
DAYS_ARG="${2:-0}"

SID="stdbyinp"
ORACLE_HOME="/opt/oracle/product/19c/dbhome_1"
LOCAL_BASE="/u03/backup/rman/stdbyinp"
S3_BASE="/u03/backup/rman/stdbyinp"

export ORACLE_SID="$SID"
export ORACLE_HOME="$ORACLE_HOME"
export PATH="$ORACLE_HOME/bin:/usr/bin:/bin:$PATH"
export LD_LIBRARY_PATH="$ORACLE_HOME/lib"

echo "[INFO] Running strict pre-check..."

sqlplus -S / as sysdba @strict_archivelog_scn_check.sql > /tmp/strict_precheck_${SID}_${DATE_ARG}.log

if grep -E "SCN_GAP_BEFORE_THIS_LOG|DIFFERENT_RESETLOGS|FUZZY|ERROR" /tmp/strict_precheck_${SID}_${DATE_ARG}.log; then
    echo "[ERROR] Strict pre-check failed."
    echo "[ERROR] See /tmp/strict_precheck_${SID}_${DATE_ARG}.log"
    exit 1
fi

echo "[INFO] Strict pre-check passed."
echo "[INFO] Running rmanDailyRefresh.py..."

python3 rmanDailyRefresh.py "$DATE_ARG" "$DAYS_ARG" \
  --sid "$SID" \
  --s3-base "$S3_BASE" \
  --local-base "$LOCAL_BASE" \
  --db-home "$ORACLE_HOME" \
  --archive-pattern "*_archivelog_1*.tar.gz" \
  --bridge-lookback-days 1
