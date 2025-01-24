#! /bin/bash
# Runs the a program via cron, with logging
set -e

EXEC_LOG=/opt/cron-log.txt

echo '============= Cron App ============'> $EXEC_LOG
date >> $EXEC_LOG

# Both output to stdout and log to a file
# leaguesync -c /etc/leaguesync.env -vv sync  | tee -a $EXEC_LOG

echo 'Running cron apps' >> $EXEC_LOG
echo 'Running cron apps'

date >> $EXEC_LOG
date > /opt/data/html/last-cron-time.txt

