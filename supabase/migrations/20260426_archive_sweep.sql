-- 20260426_archive_sweep.sql
-- Archive sweep: tracking columns for the jobs pipeline weekly cadence.
-- Run manually in the Supabase SQL editor for project xwqmnofkvdwpagfweqmj.
-- Do NOT execute from the Python scripts.

-- 1. Per-job visibility tracker: when was this job last seen in a scrape run?
ALTER TABLE jobs
  ADD COLUMN IF NOT EXISTS last_seen_in_scrape_run timestamptz;

-- 2. Per-source success tracker: when did this source last return at least one job successfully?
ALTER TABLE company_careers_sources
  ADD COLUMN IF NOT EXISTS last_successful_scrape_at timestamptz;

-- 3. Per-source attempt tracker: when was this source last attempted, pass or fail?
ALTER TABLE company_careers_sources
  ADD COLUMN IF NOT EXISTS last_scrape_run_at timestamptz;

-- 4. Index to speed archive-sweep candidate queries.
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen_in_scrape_run
  ON jobs (last_seen_in_scrape_run)
  WHERE status IN ('approved', 'pending');
