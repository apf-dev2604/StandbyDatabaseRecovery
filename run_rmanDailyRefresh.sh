#!/bin/bash
# Load user profile to ensure Oracle paths are set
source ~/.bash_profile

# Navigate to the script directory
cd /home/oracle/scripts

# Run the script using the absolute path to the venv python
# We use $(date +\%Y_\%m_\%d) to pass today's date automatically
#~/scripts/recovery/bin/python3 rmanDailyRefresh.py --bridge-lookback-days 1


set -u


SCRIPT_DIR=/home/oracle/scripts/modular-scripts/StandbyDatabaseRecovery-public
LOG_DIR=/home/oracle/scripts/modular-scripts/StandbyDatabaseRecovery-public/logs
RUN_DATE=$(date +%Y_%m_%d)

mkdir -p "$LOG_DIR"

cd "$SCRIPT_DIR" || exit 1

~/scripts/recovery/bin/python3  main.py "$RUN_DATE" 0 \
  >> "$LOG_DIR/rmanDailyRefresh_${RUN_DATE}.log" 2>&1

