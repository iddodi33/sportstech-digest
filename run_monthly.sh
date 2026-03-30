#!/bin/bash
set -e
mkdir -p logs research
MONTH=$(date +%Y-%m)
LOG=logs/$MONTH-run.log
echo "=== Sports D3c0d3d Monthly Run: $MONTH ===" | tee $LOG
echo "Started: $(date)" | tee -a $LOG
python enhanced_sportstech_job_scraper_v3.py 2>&1 | tee -a $LOG
python news_pipeline.py 2>&1 | tee -a $LOG
python digest.py 2>&1 | tee -a $LOG
echo "Done: $(date)" | tee -a $LOG
