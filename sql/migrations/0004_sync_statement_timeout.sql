-- The Data API role keeps statement_timeout at 8s. A full historical
-- sync_commit or sync_snapshot exceeds that and is cancelled before commit.
-- Only these two functions may run up to 60s. Other RPCs stay on the role default.
ALTER FUNCTION public.marketreview_sync_commit(jsonb) SET statement_timeout = '60s';
ALTER FUNCTION marketreview.sync_commit(jsonb) SET statement_timeout = '60s';
ALTER FUNCTION public.marketreview_sync_snapshot(jsonb) SET statement_timeout = '60s';
ALTER FUNCTION marketreview.sync_snapshot(jsonb) SET statement_timeout = '60s';
