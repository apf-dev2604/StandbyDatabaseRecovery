-- name: get_open_mode
SELECT open_mode FROM v$database;

-- name: get_current_branch
SELECT TO_CHAR(resetlogs_change#) || '|' || TO_CHAR(resetlogs_time, 'YYYY-MM-DD HH24:MI:SS')
FROM v$database;

-- name: get_current_datafile_scn
SELECT NVL(MIN(checkpoint_change#), 0)
FROM v$datafile;

-- name: archive_gap
SELECT LOW_SEQUENCE# || '-' || HIGH_SEQUENCE#
FROM v$archive_gap;

-- name: backup_redolog_branches
SELECT DISTINCT TO_CHAR(r.resetlogs_change#) || '|' || TO_CHAR(r.resetlogs_time, 'YYYY-MM-DD HH24:MI:SS')
FROM v$backup_redolog r, v$backup_piece p
WHERE r.set_stamp = p.set_stamp
  AND r.set_count = p.set_count
  AND p.handle IN ({handles})
  AND r.resetlogs_change# IS NOT NULL
ORDER BY 1;

-- name: raw_archivelog_branches
SELECT DISTINCT TO_CHAR(resetlogs_change#) || '|' || TO_CHAR(resetlogs_time, 'YYYY-MM-DD HH24:MI:SS')
FROM v$archived_log
WHERE name IN ({names})
  AND resetlogs_change# IS NOT NULL
ORDER BY 1;

-- name: backup_redolog_details
SELECT p.handle || '|' || r.thread# || '|' || r.sequence# || '|' || r.first_change# || '|' || r.next_change# || '|' ||
       TO_CHAR(r.first_time, 'YYYY-MM-DD HH24:MI:SS') || '|' || TO_CHAR(r.next_time, 'YYYY-MM-DD HH24:MI:SS')
FROM v$backup_redolog r, v$backup_piece p
WHERE r.set_stamp = p.set_stamp
  AND r.set_count = p.set_count
  AND p.handle IN ({handles})
  AND r.resetlogs_change# = (SELECT resetlogs_change# FROM v$database)
ORDER BY r.thread#, r.sequence#;

-- name: raw_archivelog_details
SELECT name || '|' || thread# || '|' || sequence# || '|' || first_change# || '|' || next_change# || '|' ||
       TO_CHAR(first_time, 'YYYY-MM-DD HH24:MI:SS') || '|' || TO_CHAR(next_time, 'YYYY-MM-DD HH24:MI:SS')
FROM v$archived_log
WHERE name IN ({names})
  AND resetlogs_change# = (SELECT resetlogs_change# FROM v$database)
ORDER BY thread#, sequence#;

-- name: verify_business_data
SELECT 'SAMPLE.SOME_TABLES|' || TO_CHAR(MAX(BIZ_DATE), 'YYYY-MM-DD HH24:MI:SS')
FROM SAMPLE.SOME_TABLES;

SELECT 'SAMPLE.SOME_TABLES|' || TO_CHAR(MAX(TRANSFERDATE), 'YYYY-MM-DD HH24:MI:SS')
FROM SAMPE.SOME_TABLES;
