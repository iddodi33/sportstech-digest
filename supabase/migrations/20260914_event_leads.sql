-- 20260914_event_leads.sql
-- event_leads: landing table for LinkedIn keyword/seed-author event discovery.
-- Applied to project xwqmnofkvdwpagfweqmj on 2026-09-14 via the Supabase MCP
-- apply_migration tool. Kept here so the schema is reviewable in-repo.
--
-- Deliberately NOT public.events. Keyword-matched LinkedIn posts carry a far
-- higher false-positive rate than the 5 structured-source adapters, so they get
-- their own review queue until the pattern is proven out. Nothing in this table
-- reaches events.* except by a human promoting a lead (promoted_event_id).
--
-- Column names mirror the two tables this sits between:
--   from public.social_posts: linkedin_post_id, post_url, author_name,
--                             author_linkedin_url, content, posted_at,
--                             reactions, comments, raw, scraped_at
--   from public.events:       source, status, rejected_reason, reviewed_at,
--                             first_seen_at, last_seen_at

CREATE TABLE IF NOT EXISTS public.event_leads (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Identity. linkedin_post_id is the dedup key across keywords AND seed
  -- authors: the same post routinely matches several queries in one run.
  linkedin_post_id    text NOT NULL UNIQUE,
  post_url            text,

  -- Author / organiser.
  author_name         text,
  author_linkedin_url text,
  author_type         text,              -- 'company' | 'profile' (actor's author.type)

  -- Provenance: which query surfaced this post.
  matched_by          text NOT NULL,     -- 'keyword' | 'seed_author'
  matched_value       text NOT NULL,     -- the keyword string, or the seed author URL
  -- Every matcher that hit this post, accumulated across queries and across
  -- runs. A post hit by three keywords is a stronger triage signal than one
  -- hit by a single keyword, and that signal is lost if only the first is kept.
  matched_all         text[] NOT NULL DEFAULT '{}',

  -- Payload.
  content             text,
  posted_at           timestamptz,
  reactions           integer DEFAULT 0,
  comments            integer DEFAULT 0,
  raw                 jsonb,

  -- Triage.
  source              text NOT NULL DEFAULT 'linkedin_keywords',
  status              text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'verified', 'rejected', 'promoted')),
  rejected_reason     text,
  reviewed_at         timestamptz,
  reviewed_by         uuid,
  promoted_event_id   uuid REFERENCES public.events(id) ON DELETE SET NULL,

  -- Timestamps (events.* naming).
  scraped_at          timestamptz DEFAULT now(),
  first_seen_at       timestamptz DEFAULT now(),
  last_seen_at        timestamptz
);

-- Triage queue: "show me pending leads, newest post first".
CREATE INDEX IF NOT EXISTS idx_event_leads_status_posted_at
  ON public.event_leads (status, posted_at DESC);

-- "Which keywords are actually earning their cost?" — answered without a scan.
CREATE INDEX IF NOT EXISTS idx_event_leads_matched_value
  ON public.event_leads (matched_by, matched_value);

-- RLS on, matching public.events. Writes come from the pipeline's service-role
-- key, which bypasses RLS; the admin panel reads as an authenticated user.
ALTER TABLE public.event_leads ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Authenticated read event_leads" ON public.event_leads;
CREATE POLICY "Authenticated read event_leads"
  ON public.event_leads FOR SELECT
  TO authenticated
  USING (true);


-- ── 2026-09-14 (second session): Claude classification columns ──────────────
-- Applied as migration `event_leads_classification`.
--
-- The first real run put 384 leads in this table in one week. That is too many
-- to hand-triage, and the crude event-language regex used to sanity-check the
-- batch matched only 24% — a precision ceiling, not a filter. These columns hold
-- a Haiku classification per lead so the same table can be sorted by confidence
-- instead of read blind.
--
-- Nothing is deleted on a low score. A classifier that silently dropped rows
-- would make its own false negatives invisible, which is exactly what this
-- pipeline cannot afford: the founding example (Meath's Women in Sport
-- Conference) was missed by five adapters and a manual search already.

ALTER TABLE public.event_leads
  ADD COLUMN IF NOT EXISTS is_event_relevant    boolean,
  ADD COLUMN IF NOT EXISTS relevance_confidence smallint
    CHECK (relevance_confidence IS NULL OR (relevance_confidence BETWEEN 0 AND 100)),
  ADD COLUMN IF NOT EXISTS post_kind            text
    CHECK (post_kind IS NULL OR post_kind IN (
      'event_announcement', 'event_recap', 'attendee_post',
      'generic_content', 'job_post', 'other')),
  ADD COLUMN IF NOT EXISTS rating_notes         text,   -- social_posts naming
  ADD COLUMN IF NOT EXISTS classification       jsonb,  -- raw model output, events.extraction naming
  ADD COLUMN IF NOT EXISTS classified_at        timestamptz,
  ADD COLUMN IF NOT EXISTS classifier_model     text;

-- The triage queue this exists for: pending leads, most confident events first.
CREATE INDEX IF NOT EXISTS idx_event_leads_triage
  ON public.event_leads (is_event_relevant, relevance_confidence DESC)
  WHERE status = 'pending';

-- Work queue for the classifier step: pending leads not yet classified.
CREATE INDEX IF NOT EXISTS idx_event_leads_unclassified
  ON public.event_leads (first_seen_at)
  WHERE classified_at IS NULL;
