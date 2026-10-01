-- Schema version 1.
-- 日常 CLI 不执行本文件。管理连接显式迁移。
-- 旧库升级只建立空历史，不为升级前的缺行补写删除记录。
-- 否则首次本地独有组会被误当成已知删除而挡住上传。

BEGIN;

DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    CREATE ROLE anon NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    CREATE ROLE authenticated NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    CREATE ROLE service_role NOLOGIN;
  END IF;
END;
$roles$;

CREATE SCHEMA marketreview;

ALTER DEFAULT PRIVILEGES IN SCHEMA marketreview REVOKE ALL ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA marketreview GRANT EXECUTE ON FUNCTIONS TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA marketreview REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA marketreview
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO service_role;

CREATE TABLE marketreview.schema_meta (
  id integer PRIMARY KEY CHECK (id = 1),
  schema_version integer NOT NULL
);

CREATE TABLE marketreview.ledger (
  ledger_key text PRIMARY KEY CHECK (ledger_key = 'main'),
  schema_version integer NOT NULL,
  revision bigint NOT NULL CHECK (revision >= 0)
);

CREATE TABLE marketreview.group_change_history (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  group_kind text NOT NULL CHECK (group_kind IN ('review', 'event')),
  group_key jsonb NOT NULL,
  revision bigint NOT NULL,
  change_kind text NOT NULL CHECK (
    change_kind IN ('update', 'delete', 'direction_replace', 'sync')
  ),
  before_exists boolean NOT NULL,
  after_exists boolean NOT NULL,
  operation_id text,
  UNIQUE (group_kind, group_key, revision)
);

CREATE TABLE marketreview.sync_commit_result (
  operation_id text PRIMARY KEY,
  project_id text NOT NULL,
  ledger_id text NOT NULL,
  request_digest text NOT NULL,
  expected_revision bigint NOT NULL,
  committed_revision bigint NOT NULL,
  result_version integer NOT NULL,
  result_payload jsonb NOT NULL
);

CREATE TABLE marketreview.daily_market_review (
  trade_date text PRIMARY KEY,
  pullback_count integer,
  median_change_pct double precision,
  advancing_count integer,
  declining_count integer,
  margin_balance_sh double precision,
  margin_balance_sz double precision,
  margin_balance_bj double precision,
  sh_index_close double precision,
  sh_index_prev_close double precision,
  sz_index_close double precision,
  sz_index_prev_close double precision,
  cy_index_close double precision,
  cy_index_prev_close double precision,
  turnover_amount_sh double precision,
  turnover_amount_sz double precision,
  turnover_amount_cy double precision,
  turnover_amount_bj double precision,
  total_market_cap double precision,
  float_market_cap double precision,
  pe_sh double precision,
  pe_sz double precision,
  pe_cy double precision,
  pe_all double precision,
  avg_stock_price double precision,
  created_at text NOT NULL,
  updated_at text NOT NULL
);

CREATE TABLE marketreview.daily_price_limit_event (
  trade_date text NOT NULL,
  market text NOT NULL CHECK (market IN ('sh', 'sz', 'bj')),
  code text NOT NULL CHECK (code ~ '^[0-9]{6}$'),
  name text NOT NULL,
  direction text NOT NULL CHECK (direction IN ('up', 'down')),
  closed_at_limit integer NOT NULL CHECK (closed_at_limit IN (0, 1)),
  limit_rate_bp integer NOT NULL CHECK (limit_rate_bp IN (1000, 2000, 3000)),
  streak_height integer NOT NULL CHECK (streak_height >= 0),
  created_at text NOT NULL,
  updated_at text NOT NULL,
  PRIMARY KEY (trade_date, market, code, direction)
);

CREATE TABLE marketreview.daily_price_limit_event_detail (
  trade_date text NOT NULL,
  market text NOT NULL,
  code text NOT NULL,
  direction text NOT NULL,
  previous_turnover_amount double precision,
  auction_amount double precision,
  previous_close double precision,
  open_price double precision,
  turnover_amount double precision,
  turnover_rate double precision,
  is_leader integer CHECK (is_leader IS NULL OR is_leader IN (0, 1)),
  note text,
  created_at text NOT NULL,
  updated_at text NOT NULL,
  PRIMARY KEY (trade_date, market, code, direction),
  FOREIGN KEY (trade_date, market, code, direction)
    REFERENCES marketreview.daily_price_limit_event (trade_date, market, code, direction)
    ON DELETE CASCADE
);

CREATE TABLE marketreview.daily_price_limit_event_sector (
  trade_date text NOT NULL,
  market text NOT NULL,
  code text NOT NULL,
  direction text NOT NULL,
  position integer NOT NULL CHECK (position >= 0),
  value text NOT NULL,
  PRIMARY KEY (trade_date, market, code, direction, position),
  UNIQUE (trade_date, market, code, direction, value),
  FOREIGN KEY (trade_date, market, code, direction)
    REFERENCES marketreview.daily_price_limit_event (trade_date, market, code, direction)
    ON DELETE CASCADE
);

CREATE TABLE marketreview.daily_price_limit_event_reason (
  trade_date text NOT NULL,
  market text NOT NULL,
  code text NOT NULL,
  direction text NOT NULL,
  position integer NOT NULL CHECK (position >= 0),
  value text NOT NULL,
  PRIMARY KEY (trade_date, market, code, direction, position),
  UNIQUE (trade_date, market, code, direction, value),
  FOREIGN KEY (trade_date, market, code, direction)
    REFERENCES marketreview.daily_price_limit_event (trade_date, market, code, direction)
    ON DELETE CASCADE
);

CREATE INDEX daily_price_limit_event_trade_date_idx
  ON marketreview.daily_price_limit_event (trade_date);

CREATE INDEX group_change_history_group_idx
  ON marketreview.group_change_history (group_kind, group_key);

COMMENT ON TABLE marketreview.group_change_history IS
  '只记录升级之后的变更。本迁移不补写升级前缺行的删除记录。';

INSERT INTO marketreview.schema_meta (id, schema_version) VALUES (1, 1);
INSERT INTO marketreview.ledger (ledger_key, schema_version, revision) VALUES ('main', 1, 0);

CREATE FUNCTION marketreview.raise_app(p_code text, p_message text)
RETURNS void
LANGUAGE plpgsql
AS $fn$
#variable_conflict use_variable
BEGIN
  RAISE EXCEPTION '%', p_message
    USING ERRCODE = 'P0001', HINT = p_code;
END;
$fn$;

CREATE FUNCTION marketreview.json_type_is(p_value jsonb, p_expected text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $fn$
  SELECT jsonb_typeof(p_value) IS NOT DISTINCT FROM p_expected;
$fn$;

CREATE FUNCTION marketreview.assert_schema(p_request jsonb)
RETURNS void
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  actual integer;
BEGIN
  IF p_request IS NULL OR jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 请求必须为对象');
  END IF;
  -- 缺键时 -> 得到 SQL NULL，<> 比较不会进入拒绝分支。
  IF NOT marketreview.json_type_is(p_request -> 'schema_version', 'number')
     OR COALESCE((p_request ->> 'schema_version') !~ '^[0-9]+$', true) THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] schema_version 必须为整数');
  END IF;
  SELECT schema_version INTO actual FROM marketreview.schema_meta WHERE id = 1;
  IF actual IS NULL OR actual <> (p_request ->> 'schema_version')::integer THEN
    PERFORM marketreview.raise_app('SCHEMA_VERSION_MISMATCH', '[SCHEMA_VERSION_MISMATCH] schema 版本不一致');
  END IF;
END;
$fn$;

CREATE FUNCTION marketreview.parse_trade_date(p_value text, p_field text)
RETURNS text
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
BEGIN
  IF p_value IS NULL OR p_value !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', format('[INVALID_REQUEST] %s 必须为 YYYY-MM-DD', p_field));
  END IF;
  BEGIN
    PERFORM p_value::date;
  EXCEPTION
    WHEN datetime_field_overflow OR invalid_datetime_format THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', format('[INVALID_REQUEST] %s 不是有效日期', p_field));
  END;
  RETURN p_value;
END;
$fn$;

CREATE FUNCTION marketreview.require_timestamp(p_value text, p_field text)
RETURNS text
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
BEGIN
  IF p_value IS NULL
     OR p_value !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+00:00$' THEN
    PERFORM marketreview.raise_app(
      'INVALID_REQUEST',
      format('[INVALID_REQUEST] %s 必须为 UTC 秒级时间', p_field)
    );
  END IF;
  RETURN p_value;
END;
$fn$;

CREATE FUNCTION marketreview.require_batch_time(p_request jsonb)
RETURNS text
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
BEGIN
  IF jsonb_typeof(p_request -> 'batch_time') <> 'string' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] batch_time 必须为字符串');
  END IF;
  RETURN marketreview.require_timestamp(p_request ->> 'batch_time', 'batch_time');
END;
$fn$;

CREATE FUNCTION marketreview.require_request_trade_date(p_request jsonb)
RETURNS text
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
BEGIN
  IF jsonb_typeof(p_request -> 'trade_date') <> 'string' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] trade_date 必须为字符串');
  END IF;
  RETURN marketreview.parse_trade_date(p_request ->> 'trade_date', 'trade_date');
END;
$fn$;

CREATE FUNCTION marketreview.json_integer(p_value jsonb, p_field text, p_allow_null boolean)
RETURNS bigint
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
BEGIN
  IF p_value IS NULL OR jsonb_typeof(p_value) = 'null' THEN
    IF p_allow_null THEN
      RETURN NULL;
    END IF;
    PERFORM marketreview.raise_app('INVALID_TYPE', format('[INVALID_TYPE] %s 必须为整数', p_field));
  END IF;
  IF jsonb_typeof(p_value) <> 'number' OR p_value::text !~ '^-?[0-9]+$' THEN
    PERFORM marketreview.raise_app('INVALID_TYPE', format('[INVALID_TYPE] %s 必须为整数', p_field));
  END IF;
  RETURN p_value::text::bigint;
END;
$fn$;

CREATE FUNCTION marketreview.json_int4(p_value jsonb, p_field text, p_allow_null boolean)
RETURNS integer
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  raw bigint;
BEGIN
  raw := marketreview.json_integer(p_value, p_field, p_allow_null);
  IF raw IS NULL THEN
    RETURN NULL;
  END IF;
  IF raw < -2147483648 OR raw > 2147483647 THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', format('[INVALID_REQUEST] %s 超出整数范围', p_field));
  END IF;
  RETURN raw::integer;
END;
$fn$;

CREATE FUNCTION marketreview.json_number(p_value jsonb, p_field text, p_allow_null boolean)
RETURNS double precision
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
BEGIN
  IF p_value IS NULL OR jsonb_typeof(p_value) = 'null' THEN
    IF p_allow_null THEN
      RETURN NULL;
    END IF;
    PERFORM marketreview.raise_app('INVALID_TYPE', format('[INVALID_TYPE] %s 必须为数值', p_field));
  END IF;
  IF jsonb_typeof(p_value) <> 'number' THEN
    PERFORM marketreview.raise_app('INVALID_TYPE', format('[INVALID_TYPE] %s 必须为数值', p_field));
  END IF;
  RETURN p_value::text::double precision;
END;
$fn$;

CREATE FUNCTION marketreview.json_bool(p_value jsonb, p_field text, p_allow_null boolean)
RETURNS boolean
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
BEGIN
  IF p_value IS NULL OR jsonb_typeof(p_value) = 'null' THEN
    IF p_allow_null THEN
      RETURN NULL;
    END IF;
    PERFORM marketreview.raise_app('INVALID_TYPE', format('[INVALID_TYPE] %s 不接受 null', p_field));
  END IF;
  IF jsonb_typeof(p_value) <> 'boolean' THEN
    PERFORM marketreview.raise_app('INVALID_TYPE', format('[INVALID_TYPE] %s 必须为布尔值', p_field));
  END IF;
  RETURN p_value = 'true'::jsonb;
END;
$fn$;

CREATE FUNCTION marketreview.json_text(p_value jsonb, p_field text, p_allow_null boolean)
RETURNS text
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
BEGIN
  IF p_value IS NULL OR jsonb_typeof(p_value) = 'null' THEN
    IF p_allow_null THEN
      RETURN NULL;
    END IF;
    PERFORM marketreview.raise_app('INVALID_TYPE', format('[INVALID_TYPE] %s 必须为字符串', p_field));
  END IF;
  IF jsonb_typeof(p_value) <> 'string' THEN
    PERFORM marketreview.raise_app('INVALID_TYPE', format('[INVALID_TYPE] %s 必须为字符串', p_field));
  END IF;
  RETURN p_value #>> '{}';
END;
$fn$;

CREATE FUNCTION marketreview.require_market(p_value text)
RETURNS text
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
BEGIN
  IF p_value IS NULL OR p_value NOT IN ('sh', 'sz', 'bj') THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] market 必须为 sh、sz 或 bj');
  END IF;
  RETURN p_value;
END;
$fn$;

CREATE FUNCTION marketreview.require_code(p_value text)
RETURNS text
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
BEGIN
  IF p_value IS NULL OR p_value !~ '^[0-9]{6}$' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] code 必须为 6 位数字');
  END IF;
  RETURN p_value;
END;
$fn$;

CREATE FUNCTION marketreview.require_direction(p_value text)
RETURNS text
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
BEGIN
  IF p_value IS NULL OR p_value NOT IN ('up', 'down') THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] direction 必须为 up 或 down');
  END IF;
  RETURN p_value;
END;
$fn$;

CREATE FUNCTION marketreview.require_token(p_request jsonb, p_field text, p_max integer)
RETURNS text
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  value text;
BEGIN
  IF jsonb_typeof(p_request -> p_field) IS DISTINCT FROM 'string' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', format('[INVALID_REQUEST] %s 必须为字符串', p_field));
  END IF;
  value := p_request ->> p_field;
  IF value !~ '^[A-Za-z0-9._:-]+$' OR char_length(value) < 1 OR char_length(value) > p_max THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', format('[INVALID_REQUEST] %s 格式不合法', p_field));
  END IF;
  RETURN value;
END;
$fn$;

CREATE FUNCTION marketreview.require_digest(p_request jsonb)
RETURNS text
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  value text;
BEGIN
  IF jsonb_typeof(p_request -> 'request_digest') IS DISTINCT FROM 'string' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] request_digest 必须为字符串');
  END IF;
  value := p_request ->> 'request_digest';
  IF value !~ '^[A-Za-z0-9._~+/=:-]+$' OR char_length(value) < 1 OR char_length(value) > 256 THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] request_digest 格式不合法');
  END IF;
  RETURN value;
END;
$fn$;

CREATE FUNCTION marketreview.json_string_list(p_value jsonb, p_field text)
RETURNS text[]
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  item jsonb;
  text_item text;
  result text[] := ARRAY[]::text[];
BEGIN
  IF p_value IS NULL OR jsonb_typeof(p_value) = 'null' THEN
    PERFORM marketreview.raise_app('LIST_NULL', format('[LIST_NULL] %s 不能为 null', p_field));
  END IF;
  IF jsonb_typeof(p_value) <> 'array' THEN
    PERFORM marketreview.raise_app('INVALID_TYPE', format('[INVALID_TYPE] %s 必须为数组', p_field));
  END IF;
  FOR item IN SELECT value FROM jsonb_array_elements(p_value) AS element(value)
  LOOP
    IF jsonb_typeof(item) <> 'string' THEN
      PERFORM marketreview.raise_app('INVALID_TYPE', format('[INVALID_TYPE] %s 的元素必须为字符串', p_field));
    END IF;
    text_item := item #>> '{}';
    IF text_item = '' THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', format('[INVALID_REQUEST] %s 不能包含空字符串', p_field));
    END IF;
    IF text_item = ANY(result) THEN
      PERFORM marketreview.raise_app('DUPLICATE_IDENTITY', format('[DUPLICATE_IDENTITY] %s 含重复值', p_field));
    END IF;
    result := result || text_item;
  END LOOP;
  RETURN result;
END;
$fn$;

CREATE FUNCTION marketreview.json_detail_number(p_value jsonb, p_field text)
RETURNS double precision
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  num double precision;
BEGIN
  num := marketreview.json_number(p_value, p_field, true);
  IF num IS NULL THEN
    RETURN NULL;
  END IF;
  IF p_field IN ('previous_close', 'open_price') AND num <= 0 THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', format('[INVALID_REQUEST] %s 必须大于 0', p_field));
  END IF;
  IF p_field IN ('previous_turnover_amount', 'auction_amount', 'turnover_amount', 'turnover_rate')
     AND num < 0 THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', format('[INVALID_REQUEST] %s 不能为负数', p_field));
  END IF;
  RETURN num;
END;
$fn$;

CREATE FUNCTION marketreview.bool_to_smallint(p_value boolean)
RETURNS integer
LANGUAGE sql
IMMUTABLE
AS $fn$
  SELECT CASE WHEN p_value IS NULL THEN NULL WHEN p_value THEN 1 ELSE 0 END;
$fn$;

CREATE FUNCTION marketreview.leader_json(p_value integer)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
AS $fn$
  SELECT CASE
    WHEN p_value IS NULL THEN 'null'::jsonb
    WHEN p_value = 1 THEN 'true'::jsonb
    ELSE 'false'::jsonb
  END;
$fn$;

CREATE FUNCTION marketreview.current_revision()
RETURNS bigint
LANGUAGE sql
STABLE
SET search_path = marketreview, pg_temp
AS $fn$
  SELECT revision FROM marketreview.ledger WHERE ledger_key = 'main';
$fn$;

CREATE FUNCTION marketreview.lock_ledger()
RETURNS bigint
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  rev bigint;
BEGIN
  SELECT revision INTO rev
  FROM marketreview.ledger
  WHERE ledger_key = 'main'
  FOR UPDATE;
  IF NOT FOUND THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 账本缺失');
  END IF;
  RETURN rev;
END;
$fn$;

CREATE FUNCTION marketreview.bump_revision()
RETURNS bigint
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  rev bigint;
BEGIN
  UPDATE marketreview.ledger
  SET revision = revision + 1
  WHERE ledger_key = 'main'
  RETURNING revision INTO rev;
  IF rev IS NULL THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 账本缺失');
  END IF;
  RETURN rev;
END;
$fn$;

CREATE FUNCTION marketreview.insert_history(
  p_kind text,
  p_key jsonb,
  p_revision bigint,
  p_change text,
  p_before boolean,
  p_after boolean,
  p_operation_id text
) RETURNS void
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
BEGIN
  INSERT INTO marketreview.group_change_history (
    group_kind, group_key, revision, change_kind, before_exists, after_exists, operation_id
  ) VALUES (
    p_kind, p_key, p_revision, p_change, p_before, p_after, p_operation_id
  );
END;
$fn$;

CREATE FUNCTION marketreview.write_result(p_revision bigint, p_noop boolean)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
AS $fn$
  SELECT jsonb_build_object(
    'format_version', 1,
    'schema_version', 1,
    'complete', true,
    'noop', p_noop,
    'revision', p_revision
  );
$fn$;

CREATE FUNCTION marketreview.canonical_group_key(p_kind text, p_key jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  key_count integer;
  day text;
  market text;
  code text;
BEGIN
  IF p_key IS NULL OR jsonb_typeof(p_key) <> 'object' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] group_key 必须为对象');
  END IF;
  SELECT count(*) INTO key_count FROM jsonb_object_keys(p_key);
  IF p_kind = 'review' THEN
    IF key_count <> 1 OR NOT jsonb_exists(p_key, 'trade_date') THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 复盘组键只能包含 trade_date');
    END IF;
    IF jsonb_typeof(p_key -> 'trade_date') <> 'string' THEN
      PERFORM marketreview.raise_app('INVALID_TYPE', '[INVALID_TYPE] trade_date 必须为字符串');
    END IF;
    day := marketreview.parse_trade_date(p_key ->> 'trade_date', 'trade_date');
    RETURN jsonb_build_object('trade_date', day);
  ELSIF p_kind = 'event' THEN
    IF key_count <> 3
       OR NOT jsonb_exists(p_key, 'trade_date')
       OR NOT jsonb_exists(p_key, 'market')
       OR NOT jsonb_exists(p_key, 'code') THEN
      PERFORM marketreview.raise_app(
        'INVALID_REQUEST',
        '[INVALID_REQUEST] 事件组键只能包含 trade_date、market、code'
      );
    END IF;
    IF jsonb_typeof(p_key -> 'trade_date') <> 'string'
       OR jsonb_typeof(p_key -> 'market') <> 'string'
       OR jsonb_typeof(p_key -> 'code') <> 'string' THEN
      PERFORM marketreview.raise_app('INVALID_TYPE', '[INVALID_TYPE] 事件组键必须为字符串');
    END IF;
    day := marketreview.parse_trade_date(p_key ->> 'trade_date', 'trade_date');
    market := marketreview.require_market(p_key ->> 'market');
    code := marketreview.require_code(p_key ->> 'code');
    RETURN jsonb_build_object('trade_date', day, 'market', market, 'code', code);
  END IF;
  PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] group_kind 必须为 review 或 event');
  RETURN NULL;
END;
$fn$;

CREATE FUNCTION marketreview.review_json(r marketreview.daily_market_review)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = marketreview, pg_temp
AS $fn$
  SELECT jsonb_build_object(
    'trade_date', r.trade_date,
    'advancing_count', r.advancing_count,
    'avg_stock_price', r.avg_stock_price,
    'cy_index_close', r.cy_index_close,
    'cy_index_prev_close', r.cy_index_prev_close,
    'declining_count', r.declining_count,
    'float_market_cap', r.float_market_cap,
    'margin_balance_bj', r.margin_balance_bj,
    'margin_balance_sh', r.margin_balance_sh,
    'margin_balance_sz', r.margin_balance_sz,
    'median_change_pct', r.median_change_pct,
    'pe_all', r.pe_all,
    'pe_cy', r.pe_cy,
    'pe_sh', r.pe_sh,
    'pe_sz', r.pe_sz,
    'pullback_count', r.pullback_count,
    'sh_index_close', r.sh_index_close,
    'sh_index_prev_close', r.sh_index_prev_close,
    'sz_index_close', r.sz_index_close,
    'sz_index_prev_close', r.sz_index_prev_close,
    'total_market_cap', r.total_market_cap,
    'turnover_amount_bj', r.turnover_amount_bj,
    'turnover_amount_cy', r.turnover_amount_cy,
    'turnover_amount_sh', r.turnover_amount_sh,
    'turnover_amount_sz', r.turnover_amount_sz,
    'created_at', r.created_at,
    'updated_at', r.updated_at
  );
$fn$;

CREATE FUNCTION marketreview.review_json_by_date(p_trade_date text)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = marketreview, pg_temp
AS $fn$
  SELECT marketreview.review_json(r)
  FROM marketreview.daily_market_review AS r
  WHERE r.trade_date = p_trade_date;
$fn$;

CREATE FUNCTION marketreview.event_row_json(r marketreview.daily_price_limit_event)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = marketreview, pg_temp
AS $fn$
  SELECT jsonb_build_object(
    'trade_date', r.trade_date,
    'market', r.market,
    'code', r.code,
    'name', r.name,
    'direction', r.direction,
    'closed_at_limit', r.closed_at_limit = 1,
    'limit_rate_bp', r.limit_rate_bp,
    'streak_height', r.streak_height,
    'created_at', r.created_at,
    'updated_at', r.updated_at
  );
$fn$;

CREATE FUNCTION marketreview.detail_row_json(r marketreview.daily_price_limit_event_detail)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = marketreview, pg_temp
AS $fn$
  SELECT jsonb_build_object(
    'trade_date', r.trade_date,
    'market', r.market,
    'code', r.code,
    'direction', r.direction,
    'previous_turnover_amount', r.previous_turnover_amount,
    'auction_amount', r.auction_amount,
    'previous_close', r.previous_close,
    'open_price', r.open_price,
    'turnover_amount', r.turnover_amount,
    'turnover_rate', r.turnover_rate,
    'is_leader', marketreview.leader_json(r.is_leader),
    'note', r.note,
    'created_at', r.created_at,
    'updated_at', r.updated_at
  );
$fn$;

CREATE FUNCTION marketreview.detail_scalar_json(r marketreview.daily_price_limit_event_detail)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = marketreview, pg_temp
AS $fn$
  SELECT jsonb_build_object(
    'previous_turnover_amount', r.previous_turnover_amount,
    'auction_amount', r.auction_amount,
    'previous_close', r.previous_close,
    'open_price', r.open_price,
    'turnover_amount', r.turnover_amount,
    'turnover_rate', r.turnover_rate,
    'is_leader', marketreview.leader_json(r.is_leader),
    'note', r.note,
    'created_at', r.created_at,
    'updated_at', r.updated_at
  );
$fn$;

CREATE FUNCTION marketreview.ordered_values(
  p_table text,
  p_trade_date text,
  p_market text,
  p_code text,
  p_direction text
) RETURNS jsonb
LANGUAGE plpgsql
STABLE
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  payload jsonb;
BEGIN
  IF p_table NOT IN ('daily_price_limit_event_sector', 'daily_price_limit_event_reason') THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 未知多值表');
  END IF;
  EXECUTE format(
    'SELECT COALESCE(jsonb_agg(item.value ORDER BY item.position), ''[]''::jsonb)
     FROM marketreview.%I AS item
     WHERE item.trade_date = $1 AND item.market = $2 AND item.code = $3 AND item.direction = $4',
    p_table
  ) INTO payload USING p_trade_date, p_market, p_code, p_direction;
  RETURN payload;
END;
$fn$;

CREATE FUNCTION marketreview.replace_text_list(
  p_table text,
  p_trade_date text,
  p_market text,
  p_code text,
  p_direction text,
  p_values text[]
) RETURNS void
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  index integer;
BEGIN
  IF p_table NOT IN ('daily_price_limit_event_sector', 'daily_price_limit_event_reason') THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 未知多值表');
  END IF;
  EXECUTE format(
    'DELETE FROM marketreview.%I
     WHERE trade_date = $1 AND market = $2 AND code = $3 AND direction = $4',
    p_table
  ) USING p_trade_date, p_market, p_code, p_direction;
  IF p_values IS NULL THEN
    RETURN;
  END IF;
  FOR index IN 1..coalesce(array_length(p_values, 1), 0)
  LOOP
    EXECUTE format(
      'INSERT INTO marketreview.%I (trade_date, market, code, direction, position, value)
       VALUES ($1, $2, $3, $4, $5, $6)',
      p_table
    ) USING p_trade_date, p_market, p_code, p_direction, index - 1, p_values[index];
  END LOOP;
END;
$fn$;

CREATE FUNCTION marketreview.sync_direction_json(
  p_trade_date text,
  p_market text,
  p_code text,
  p_direction text
) RETURNS jsonb
LANGUAGE plpgsql
STABLE
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  event_row marketreview.daily_price_limit_event%ROWTYPE;
  detail_row marketreview.daily_price_limit_event_detail%ROWTYPE;
  detail_exists boolean;
BEGIN
  SELECT * INTO event_row
  FROM marketreview.daily_price_limit_event
  WHERE trade_date = p_trade_date AND market = p_market AND code = p_code AND direction = p_direction;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  SELECT * INTO detail_row
  FROM marketreview.daily_price_limit_event_detail
  WHERE trade_date = p_trade_date AND market = p_market AND code = p_code AND direction = p_direction;
  detail_exists := FOUND;
  RETURN jsonb_build_object(
    'direction', event_row.direction,
    'name', event_row.name,
    'closed_at_limit', event_row.closed_at_limit = 1,
    'limit_rate_bp', event_row.limit_rate_bp,
    'streak_height', event_row.streak_height,
    'created_at', event_row.created_at,
    'updated_at', event_row.updated_at,
    'detail_exists', detail_exists,
    'detail', CASE
      WHEN detail_exists THEN marketreview.detail_scalar_json(detail_row)
      ELSE NULL
    END,
    'sectors', marketreview.ordered_values(
      'daily_price_limit_event_sector', p_trade_date, p_market, p_code, p_direction
    ),
    'limit_up_reasons', marketreview.ordered_values(
      'daily_price_limit_event_reason', p_trade_date, p_market, p_code, p_direction
    )
  );
END;
$fn$;

CREATE FUNCTION marketreview.direction_state(
  p_trade_date text,
  p_market text,
  p_code text,
  p_direction text
) RETURNS jsonb
LANGUAGE plpgsql
STABLE
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  event_row marketreview.daily_price_limit_event%ROWTYPE;
  detail_row marketreview.daily_price_limit_event_detail%ROWTYPE;
  detail_exists boolean;
BEGIN
  SELECT * INTO event_row
  FROM marketreview.daily_price_limit_event
  WHERE trade_date = p_trade_date AND market = p_market AND code = p_code AND direction = p_direction;
  IF NOT FOUND THEN
    RETURN jsonb_build_object(
      'exists', false,
      'event', NULL,
      'detail_exists', false,
      'detail', NULL,
      'sectors', '[]'::jsonb,
      'reasons', '[]'::jsonb
    );
  END IF;
  SELECT * INTO detail_row
  FROM marketreview.daily_price_limit_event_detail
  WHERE trade_date = p_trade_date AND market = p_market AND code = p_code AND direction = p_direction;
  detail_exists := FOUND;
  RETURN jsonb_build_object(
    'exists', true,
    'event', marketreview.event_row_json(event_row),
    'detail_exists', detail_exists,
    'detail', CASE
      WHEN detail_exists THEN marketreview.detail_row_json(detail_row)
      ELSE NULL
    END,
    'sectors', marketreview.ordered_values(
      'daily_price_limit_event_sector', p_trade_date, p_market, p_code, p_direction
    ),
    'reasons', marketreview.ordered_values(
      'daily_price_limit_event_reason', p_trade_date, p_market, p_code, p_direction
    )
  );
END;
$fn$;
