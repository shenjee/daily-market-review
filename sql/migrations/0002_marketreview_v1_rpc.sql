-- 与 0001 同一会话执行，结束 schema version 1 的事务。

CREATE FUNCTION marketreview.patch_review_fields(p_trade_date text, p_fields jsonb)
RETURNS void
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  key text;
  int_fields text[] := ARRAY['advancing_count', 'declining_count', 'pullback_count'];
  float_fields text[] := ARRAY[
    'avg_stock_price', 'cy_index_close', 'cy_index_prev_close', 'float_market_cap',
    'margin_balance_bj', 'margin_balance_sh', 'margin_balance_sz', 'median_change_pct',
    'pe_all', 'pe_cy', 'pe_sh', 'pe_sz', 'sh_index_close', 'sh_index_prev_close',
    'sz_index_close', 'sz_index_prev_close', 'total_market_cap',
    'turnover_amount_bj', 'turnover_amount_cy', 'turnover_amount_sh', 'turnover_amount_sz'
  ];
BEGIN
  IF p_fields IS NULL OR jsonb_typeof(p_fields) <> 'object' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] fields 必须为对象');
  END IF;
  FOR key IN SELECT jsonb_object_keys(p_fields)
  LOOP
    IF key = ANY(int_fields) THEN
      EXECUTE format(
        'UPDATE marketreview.daily_market_review SET %I = $1 WHERE trade_date = $2',
        key
      ) USING marketreview.json_int4(p_fields -> key, key, true), p_trade_date;
    ELSIF key = ANY(float_fields) THEN
      EXECUTE format(
        'UPDATE marketreview.daily_market_review SET %I = $1 WHERE trade_date = $2',
        key
      ) USING marketreview.json_number(p_fields -> key, key, true), p_trade_date;
    ELSE
      PERFORM marketreview.raise_app('UNKNOWN_FIELD', format('[UNKNOWN_FIELD] 未知字段：%s', key));
    END IF;
  END LOOP;
END;
$fn$;

CREATE FUNCTION marketreview.limit_rate(p_value jsonb)
RETURNS integer
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  raw integer;
BEGIN
  raw := marketreview.json_int4(p_value, 'limit_rate_bp', false);
  IF raw NOT IN (1000, 2000, 3000) THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] limit_rate_bp 必须为 1000、2000 或 3000');
  END IF;
  RETURN raw;
END;
$fn$;

CREATE FUNCTION marketreview.streak_height(p_value jsonb)
RETURNS integer
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  raw integer;
BEGIN
  raw := marketreview.json_int4(p_value, 'streak_height', false);
  IF raw < 0 THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] streak_height 不能为负数');
  END IF;
  RETURN raw;
END;
$fn$;

CREATE FUNCTION marketreview.event_group_exists(p_trade_date text, p_market text, p_code text)
RETURNS boolean
LANGUAGE sql
STABLE
SET search_path = marketreview, pg_temp
AS $fn$
  SELECT EXISTS (
    SELECT 1 FROM marketreview.daily_price_limit_event
    WHERE trade_date = p_trade_date AND market = p_market AND code = p_code
  );
$fn$;

CREATE FUNCTION marketreview.probe(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  RETURN jsonb_build_object(
    'format_version', 1,
    'schema_version', 1,
    'complete', true,
    'revision', marketreview.current_revision(),
    'ledger_key', 'main'
  );
END;
$fn$;

CREATE FUNCTION marketreview.get_day(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  day text;
  previous_day text;
  review_payload jsonb;
  events jsonb;
  details jsonb;
  sectors jsonb;
  reasons jsonb;
  previous_events jsonb;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  day := marketreview.require_request_trade_date(p_request);
  IF jsonb_typeof(p_request -> 'previous_trade_date') <> 'string' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] previous_trade_date 必须为字符串');
  END IF;
  previous_day := marketreview.parse_trade_date(p_request ->> 'previous_trade_date', 'previous_trade_date');
  SELECT
    CASE WHEN r.trade_date IS NULL THEN NULL ELSE marketreview.review_json(r) END,
    COALESCE((
      SELECT jsonb_agg(marketreview.event_row_json(e) ORDER BY e.market, e.code, e.direction)
      FROM marketreview.daily_price_limit_event AS e
      WHERE e.trade_date = day
    ), '[]'::jsonb),
    COALESCE((
      SELECT jsonb_agg(marketreview.detail_row_json(d) ORDER BY d.market, d.code, d.direction)
      FROM marketreview.daily_price_limit_event_detail AS d
      WHERE d.trade_date = day
    ), '[]'::jsonb),
    COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'trade_date', s.trade_date, 'market', s.market, 'code', s.code,
        'direction', s.direction, 'position', s.position, 'value', s.value
      ) ORDER BY s.market, s.code, s.direction, s.position)
      FROM marketreview.daily_price_limit_event_sector AS s
      WHERE s.trade_date = day
    ), '[]'::jsonb),
    COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'trade_date', s.trade_date, 'market', s.market, 'code', s.code,
        'direction', s.direction, 'position', s.position, 'value', s.value
      ) ORDER BY s.market, s.code, s.direction, s.position)
      FROM marketreview.daily_price_limit_event_reason AS s
      WHERE s.trade_date = day
    ), '[]'::jsonb),
    COALESCE((
      SELECT jsonb_agg(marketreview.event_row_json(e) ORDER BY e.market, e.code, e.direction)
      FROM marketreview.daily_price_limit_event AS e
      WHERE e.trade_date = previous_day
    ), '[]'::jsonb)
  INTO review_payload, events, details, sectors, reasons, previous_events
  FROM (SELECT 1) AS anchor
  LEFT JOIN marketreview.daily_market_review AS r ON r.trade_date = day;
  RETURN jsonb_build_object(
    'format_version', 1,
    'schema_version', 1,
    'complete', true,
    'trade_date', day,
    'previous_trade_date', previous_day,
    'review', review_payload,
    'events', events,
    'details', details,
    'sectors', sectors,
    'reasons', reasons,
    'previous_events', previous_events,
    'counts', jsonb_build_object(
      'reviews', CASE WHEN review_payload IS NULL THEN 0 ELSE 1 END,
      'events', jsonb_array_length(events),
      'details', jsonb_array_length(details),
      'sectors', jsonb_array_length(sectors),
      'reasons', jsonb_array_length(reasons),
      'previous_events', jsonb_array_length(previous_events)
    )
  );
END;
$fn$;

CREATE FUNCTION marketreview.list_trade_dates(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  start_day text;
  end_day text;
  dates jsonb;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  IF jsonb_exists(p_request, 'start_date') THEN
    IF jsonb_typeof(p_request -> 'start_date') <> 'string' THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] start_date 必须为字符串');
    END IF;
    start_day := marketreview.parse_trade_date(p_request ->> 'start_date', 'start_date');
  END IF;
  IF jsonb_exists(p_request, 'end_date') THEN
    IF jsonb_typeof(p_request -> 'end_date') <> 'string' THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] end_date 必须为字符串');
    END IF;
    end_day := marketreview.parse_trade_date(p_request ->> 'end_date', 'end_date');
  END IF;
  SELECT COALESCE(jsonb_agg(trade_date ORDER BY trade_date), '[]'::jsonb)
  INTO dates
  FROM marketreview.daily_market_review
  WHERE (start_day IS NULL OR trade_date >= start_day)
    AND (end_day IS NULL OR trade_date <= end_day);
  RETURN jsonb_build_object(
    'format_version', 1,
    'schema_version', 1,
    'complete', true,
    'trade_dates', dates,
    'counts', jsonb_build_object('trade_dates', jsonb_array_length(dates))
  );
END;
$fn$;

CREATE FUNCTION marketreview.list_reviews(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  dates jsonb;
  reviews jsonb;
BEGIN
  dates := marketreview.list_trade_dates(p_request) -> 'trade_dates';
  SELECT COALESCE(jsonb_agg(marketreview.review_json_by_date(value) ORDER BY value), '[]'::jsonb)
  INTO reviews
  FROM jsonb_array_elements_text(dates) AS item(value);
  RETURN jsonb_build_object(
    'format_version', 1,
    'schema_version', 1,
    'complete', true,
    'reviews', reviews,
    'counts', jsonb_build_object('reviews', jsonb_array_length(reviews))
  );
END;
$fn$;

CREATE FUNCTION marketreview.list_events(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  start_day text;
  end_day text;
  events jsonb;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  IF jsonb_exists(p_request, 'start_date') THEN
    start_day := marketreview.parse_trade_date(p_request ->> 'start_date', 'start_date');
  END IF;
  IF jsonb_exists(p_request, 'end_date') THEN
    end_day := marketreview.parse_trade_date(p_request ->> 'end_date', 'end_date');
  END IF;
  SELECT COALESCE(
    jsonb_agg(marketreview.event_row_json(e) ORDER BY e.trade_date, e.market, e.code, e.direction),
    '[]'::jsonb
  )
  INTO events
  FROM marketreview.daily_price_limit_event AS e
  WHERE (start_day IS NULL OR e.trade_date >= start_day)
    AND (end_day IS NULL OR e.trade_date <= end_day);
  RETURN jsonb_build_object(
    'format_version', 1,
    'schema_version', 1,
    'complete', true,
    'events', events,
    'counts', jsonb_build_object('events', jsonb_array_length(events))
  );
END;
$fn$;

CREATE FUNCTION marketreview.list_event_details(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  start_day text;
  end_day text;
  details jsonb;
  sectors jsonb;
  reasons jsonb;
  events jsonb;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  IF jsonb_exists(p_request, 'start_date') THEN
    start_day := marketreview.parse_trade_date(p_request ->> 'start_date', 'start_date');
  END IF;
  IF jsonb_exists(p_request, 'end_date') THEN
    end_day := marketreview.parse_trade_date(p_request ->> 'end_date', 'end_date');
  END IF;
  SELECT detail_rows.payload, sector_rows.payload, reason_rows.payload, event_rows.payload
  INTO details, sectors, reasons, events
  FROM (SELECT 1) AS anchor
  CROSS JOIN LATERAL (
    SELECT COALESCE(
      jsonb_agg(marketreview.detail_row_json(d) ORDER BY d.trade_date, d.market, d.code, d.direction),
      '[]'::jsonb
    ) AS payload
    FROM marketreview.daily_price_limit_event_detail AS d
    WHERE (start_day IS NULL OR d.trade_date >= start_day)
      AND (end_day IS NULL OR d.trade_date <= end_day)
  ) AS detail_rows
  CROSS JOIN LATERAL (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'trade_date', s.trade_date, 'market', s.market, 'code', s.code,
      'direction', s.direction, 'position', s.position, 'value', s.value
    ) ORDER BY s.trade_date, s.market, s.code, s.direction, s.position), '[]'::jsonb) AS payload
    FROM marketreview.daily_price_limit_event_sector AS s
    WHERE (start_day IS NULL OR s.trade_date >= start_day)
      AND (end_day IS NULL OR s.trade_date <= end_day)
  ) AS sector_rows
  CROSS JOIN LATERAL (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'trade_date', s.trade_date, 'market', s.market, 'code', s.code,
      'direction', s.direction, 'position', s.position, 'value', s.value
    ) ORDER BY s.trade_date, s.market, s.code, s.direction, s.position), '[]'::jsonb) AS payload
    FROM marketreview.daily_price_limit_event_reason AS s
    WHERE (start_day IS NULL OR s.trade_date >= start_day)
      AND (end_day IS NULL OR s.trade_date <= end_day)
  ) AS reason_rows
  CROSS JOIN LATERAL (
    SELECT COALESCE(
      jsonb_agg(marketreview.event_row_json(e) ORDER BY e.trade_date, e.market, e.code, e.direction),
      '[]'::jsonb
    ) AS payload
    FROM marketreview.daily_price_limit_event AS e
    WHERE (start_day IS NULL OR e.trade_date >= start_day)
      AND (end_day IS NULL OR e.trade_date <= end_day)
  ) AS event_rows;
  RETURN jsonb_build_object(
    'format_version', 1,
    'schema_version', 1,
    'complete', true,
    'details', details,
    'sectors', sectors,
    'reasons', reasons,
    'events', events,
    'counts', jsonb_build_object(
      'details', jsonb_array_length(details),
      'sectors', jsonb_array_length(sectors),
      'reasons', jsonb_array_length(reasons),
      'events', jsonb_array_length(events)
    )
  );
END;
$fn$;

CREATE FUNCTION marketreview.replace_direction_preimage(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  day text;
  market text;
  code text;
  old_direction text;
  new_direction text;
  old_state jsonb;
  new_state jsonb;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  day := marketreview.require_request_trade_date(p_request);
  market := marketreview.require_market(marketreview.json_text(p_request -> 'market', 'market', false));
  code := marketreview.require_code(marketreview.json_text(p_request -> 'code', 'code', false));
  old_direction := marketreview.require_direction(
    marketreview.json_text(p_request -> 'old_direction', 'old_direction', false)
  );
  new_direction := marketreview.require_direction(
    marketreview.json_text(p_request -> 'new_direction', 'new_direction', false)
  );
  IF old_direction = new_direction THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 方向未变化');
  END IF;
  SELECT marketreview.direction_state(day, market, code, old_direction),
         marketreview.direction_state(day, market, code, new_direction)
  INTO old_state, new_state;
  RETURN jsonb_build_object(
    'format_version', 1,
    'schema_version', 1,
    'complete', true,
    'revision', marketreview.current_revision(),
    'trade_date', day,
    'market', market,
    'code', code,
    'old_direction', old_direction,
    'new_direction', new_direction,
    'old', old_state,
    'new', new_state
  );
END;
$fn$;

CREATE FUNCTION marketreview.save_review(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  day text;
  batch text;
  fields jsonb;
  before_exists boolean;
  new_revision bigint;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  day := marketreview.require_request_trade_date(p_request);
  batch := marketreview.require_batch_time(p_request);
  fields := p_request -> 'fields';
  IF fields IS NULL OR jsonb_typeof(fields) <> 'object' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] fields 必须为对象');
  END IF;
  IF fields = '{}'::jsonb THEN
    RETURN marketreview.write_result(marketreview.current_revision(), true);
  END IF;
  PERFORM marketreview.lock_ledger();
  SELECT EXISTS (
    SELECT 1 FROM marketreview.daily_market_review WHERE trade_date = day
  ) INTO before_exists;
  INSERT INTO marketreview.daily_market_review (trade_date, created_at, updated_at)
  VALUES (day, batch, batch)
  ON CONFLICT ON CONSTRAINT daily_market_review_pkey DO NOTHING;
  PERFORM marketreview.patch_review_fields(day, fields);
  UPDATE marketreview.daily_market_review SET updated_at = batch WHERE trade_date = day;
  new_revision := marketreview.bump_revision();
  PERFORM marketreview.insert_history(
    'review',
    jsonb_build_object('trade_date', day),
    new_revision,
    'update',
    before_exists,
    true,
    NULL
  );
  RETURN marketreview.write_result(new_revision, false);
END;
$fn$;

CREATE FUNCTION marketreview.parse_event_body(p_event jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  key text;
  allowed text[] := ARRAY[
    'market', 'code', 'name', 'direction', 'closed_at_limit', 'limit_rate_bp', 'streak_height'
  ];
BEGIN
  IF p_event IS NULL OR jsonb_typeof(p_event) <> 'object' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 事件必须为对象');
  END IF;
  FOR key IN SELECT jsonb_object_keys(p_event)
  LOOP
    IF key <> ALL(allowed) THEN
      PERFORM marketreview.raise_app('UNKNOWN_FIELD', format('[UNKNOWN_FIELD] 未知字段：%s', key));
    END IF;
  END LOOP;
  IF NOT jsonb_exists_all(p_event, allowed) THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 事件缺少必填字段');
  END IF;
  RETURN jsonb_build_object(
    'market', marketreview.require_market(marketreview.json_text(p_event -> 'market', 'market', false)),
    'code', marketreview.require_code(marketreview.json_text(p_event -> 'code', 'code', false)),
    'name', marketreview.json_text(p_event -> 'name', 'name', false),
    'direction', marketreview.require_direction(marketreview.json_text(p_event -> 'direction', 'direction', false)),
    'closed_at_limit', marketreview.bool_to_smallint(
      marketreview.json_bool(p_event -> 'closed_at_limit', 'closed_at_limit', false)
    ),
    'limit_rate_bp', marketreview.limit_rate(p_event -> 'limit_rate_bp'),
    'streak_height', marketreview.streak_height(p_event -> 'streak_height')
  );
END;
$fn$;

CREATE FUNCTION marketreview.save_events(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  day text;
  batch text;
  events jsonb;
  item jsonb;
  parsed jsonb;
  normalized jsonb := '[]'::jsonb;
  seen text[] := ARRAY[]::text[];
  identity text;
  group_key text;
  seen_groups text[] := ARRAY[]::text[];
  groups jsonb := '[]'::jsonb;
  market text;
  code text;
  before_exists boolean;
  new_revision bigint;
  history_item jsonb;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  day := marketreview.require_request_trade_date(p_request);
  batch := marketreview.require_batch_time(p_request);
  events := p_request -> 'events';
  IF events IS NULL OR jsonb_typeof(events) <> 'array' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] events 必须为数组');
  END IF;
  IF jsonb_array_length(events) = 0 THEN
    RETURN marketreview.write_result(marketreview.current_revision(), true);
  END IF;
  FOR item IN SELECT value FROM jsonb_array_elements(events) AS element(value)
  LOOP
    parsed := marketreview.parse_event_body(item);
    identity := (parsed ->> 'market') || '|' || (parsed ->> 'code') || '|' || (parsed ->> 'direction');
    IF identity = ANY(seen) THEN
      PERFORM marketreview.raise_app('DUPLICATE_IDENTITY', '[DUPLICATE_IDENTITY] 同一批写入存在重复事件');
    END IF;
    seen := seen || identity;
    normalized := normalized || jsonb_build_array(parsed);
  END LOOP;
  PERFORM marketreview.lock_ledger();
  FOR item IN SELECT value FROM jsonb_array_elements(normalized) AS element(value)
  LOOP
    market := item ->> 'market';
    code := item ->> 'code';
    group_key := market || '|' || code;
    IF group_key <> ALL(seen_groups) THEN
      seen_groups := seen_groups || group_key;
      before_exists := marketreview.event_group_exists(day, market, code);
      groups := groups || jsonb_build_array(jsonb_build_object(
        'market', market,
        'code', code,
        'before_exists', before_exists
      ));
    END IF;
    INSERT INTO marketreview.daily_price_limit_event (
      trade_date, market, code, name, direction, closed_at_limit, limit_rate_bp, streak_height,
      created_at, updated_at
    ) VALUES (
      day, market, code, item ->> 'name', item ->> 'direction',
      (item ->> 'closed_at_limit')::integer,
      (item ->> 'limit_rate_bp')::integer,
      (item ->> 'streak_height')::integer,
      batch, batch
    )
    ON CONFLICT ON CONSTRAINT daily_price_limit_event_pkey DO UPDATE SET
      name = EXCLUDED.name,
      closed_at_limit = EXCLUDED.closed_at_limit,
      limit_rate_bp = EXCLUDED.limit_rate_bp,
      streak_height = EXCLUDED.streak_height,
      updated_at = EXCLUDED.updated_at;
  END LOOP;
  new_revision := marketreview.bump_revision();
  FOR history_item IN SELECT value FROM jsonb_array_elements(groups) AS element(value)
  LOOP
    PERFORM marketreview.insert_history(
      'event',
      jsonb_build_object(
        'trade_date', day,
        'market', history_item ->> 'market',
        'code', history_item ->> 'code'
      ),
      new_revision,
      'update',
      (history_item ->> 'before_exists')::boolean,
      true,
      NULL
    );
  END LOOP;
  RETURN marketreview.write_result(new_revision, false);
END;
$fn$;

CREATE FUNCTION marketreview.apply_detail_patch(
  p_trade_date text,
  p_patch jsonb,
  p_batch text
) RETURNS void
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  market text;
  code text;
  direction text;
  key text;
  allowed text[] := ARRAY[
    'market', 'code', 'direction', 'previous_turnover_amount', 'auction_amount',
    'previous_close', 'open_price', 'turnover_amount', 'turnover_rate', 'is_leader',
    'note', 'sectors', 'limit_up_reasons'
  ];
  scalar_fields text[] := ARRAY[
    'previous_turnover_amount', 'auction_amount', 'previous_close', 'open_price',
    'turnover_amount', 'turnover_rate', 'is_leader', 'note'
  ];
  number_fields text[] := ARRAY[
    'previous_turnover_amount', 'auction_amount', 'previous_close', 'open_price',
    'turnover_amount', 'turnover_rate'
  ];
  sectors text[];
  reasons text[];
BEGIN
  IF p_patch IS NULL OR jsonb_typeof(p_patch) <> 'object' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 明细必须为对象');
  END IF;
  FOR key IN SELECT jsonb_object_keys(p_patch)
  LOOP
    IF key <> ALL(allowed) THEN
      IF key IN ('auction_ratio', 'open_change') THEN
        PERFORM marketreview.raise_app('UNKNOWN_FIELD', format('[UNKNOWN_FIELD] %s 是读取派生字段，不能写入', key));
      END IF;
      PERFORM marketreview.raise_app('UNKNOWN_FIELD', format('[UNKNOWN_FIELD] 未知字段：%s', key));
    END IF;
  END LOOP;
  market := marketreview.require_market(marketreview.json_text(p_patch -> 'market', 'market', false));
  code := marketreview.require_code(marketreview.json_text(p_patch -> 'code', 'code', false));
  direction := marketreview.require_direction(marketreview.json_text(p_patch -> 'direction', 'direction', false));
  IF jsonb_exists(p_patch, 'limit_up_reasons') AND direction <> 'up' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] limit_up_reasons 只允许写入 direction=up 的事件');
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM marketreview.daily_price_limit_event AS event_row
    WHERE event_row.trade_date = p_trade_date
      AND event_row.market = market
      AND event_row.code = code
      AND event_row.direction = direction
  ) THEN
    PERFORM marketreview.raise_app('PARENT_EVENT_MISSING', '[PARENT_EVENT_MISSING] 父事件不存在');
  END IF;
  IF jsonb_exists_any(p_patch, scalar_fields) THEN
    INSERT INTO marketreview.daily_price_limit_event_detail (
      trade_date, market, code, direction, created_at, updated_at
    ) VALUES (p_trade_date, market, code, direction, p_batch, p_batch)
    ON CONFLICT ON CONSTRAINT daily_price_limit_event_detail_pkey DO NOTHING;
    FOREACH key IN ARRAY scalar_fields
    LOOP
      IF NOT jsonb_exists(p_patch, key) THEN
        CONTINUE;
      END IF;
      IF key = ANY(number_fields) THEN
        EXECUTE format(
          'UPDATE marketreview.daily_price_limit_event_detail
           SET %I = $1, updated_at = $2
           WHERE trade_date = $3 AND market = $4 AND code = $5 AND direction = $6',
          key
        ) USING marketreview.json_detail_number(p_patch -> key, key), p_batch,
          p_trade_date, market, code, direction;
      ELSIF key = 'is_leader' THEN
        UPDATE marketreview.daily_price_limit_event_detail AS detail_row
        SET is_leader = marketreview.bool_to_smallint(
              marketreview.json_bool(p_patch -> 'is_leader', 'is_leader', true)
            ),
            updated_at = p_batch
        WHERE detail_row.trade_date = p_trade_date
          AND detail_row.market = market
          AND detail_row.code = code
          AND detail_row.direction = direction;
      ELSE
        UPDATE marketreview.daily_price_limit_event_detail AS detail_row
        SET note = NULLIF(btrim(marketreview.json_text(p_patch -> 'note', 'note', true)), ''),
            updated_at = p_batch
        WHERE detail_row.trade_date = p_trade_date
          AND detail_row.market = market
          AND detail_row.code = code
          AND detail_row.direction = direction;
      END IF;
    END LOOP;
  END IF;
  IF jsonb_exists(p_patch, 'sectors') THEN
    sectors := marketreview.json_string_list(p_patch -> 'sectors', 'sectors');
    PERFORM marketreview.replace_text_list(
      'daily_price_limit_event_sector', p_trade_date, market, code, direction, sectors
    );
  END IF;
  IF jsonb_exists(p_patch, 'limit_up_reasons') THEN
    reasons := marketreview.json_string_list(p_patch -> 'limit_up_reasons', 'limit_up_reasons');
    PERFORM marketreview.replace_text_list(
      'daily_price_limit_event_reason', p_trade_date, market, code, direction, reasons
    );
  END IF;
END;
$fn$;

CREATE FUNCTION marketreview.save_event_details(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  day text;
  batch text;
  details jsonb;
  item jsonb;
  seen text[] := ARRAY[]::text[];
  identity text;
  seen_groups text[] := ARRAY[]::text[];
  groups jsonb := '[]'::jsonb;
  market text;
  code text;
  new_revision bigint;
  history_item jsonb;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  day := marketreview.require_request_trade_date(p_request);
  batch := marketreview.require_batch_time(p_request);
  details := p_request -> 'details';
  IF details IS NULL OR jsonb_typeof(details) <> 'array' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] details 必须为数组');
  END IF;
  IF jsonb_array_length(details) = 0 THEN
    RETURN marketreview.write_result(marketreview.current_revision(), true);
  END IF;
  FOR item IN SELECT value FROM jsonb_array_elements(details) AS element(value)
  LOOP
    market := marketreview.require_market(marketreview.json_text(item -> 'market', 'market', false));
    code := marketreview.require_code(marketreview.json_text(item -> 'code', 'code', false));
    PERFORM marketreview.require_direction(marketreview.json_text(item -> 'direction', 'direction', false));
    identity := market || '|' || code || '|' || (item ->> 'direction');
    IF identity = ANY(seen) THEN
      PERFORM marketreview.raise_app('DUPLICATE_IDENTITY', '[DUPLICATE_IDENTITY] 同一批写入存在重复事件扩展');
    END IF;
    seen := seen || identity;
  END LOOP;
  PERFORM marketreview.lock_ledger();
  FOR item IN SELECT value FROM jsonb_array_elements(details) AS element(value)
  LOOP
    PERFORM marketreview.apply_detail_patch(day, item, batch);
    market := item ->> 'market';
    code := item ->> 'code';
    identity := market || '|' || code;
    IF identity <> ALL(seen_groups) THEN
      seen_groups := seen_groups || identity;
      groups := groups || jsonb_build_array(jsonb_build_object('market', market, 'code', code));
    END IF;
  END LOOP;
  new_revision := marketreview.bump_revision();
  FOR history_item IN SELECT value FROM jsonb_array_elements(groups) AS element(value)
  LOOP
    PERFORM marketreview.insert_history(
      'event',
      jsonb_build_object(
        'trade_date', day,
        'market', history_item ->> 'market',
        'code', history_item ->> 'code'
      ),
      new_revision,
      'update',
      true,
      true,
      NULL
    );
  END LOOP;
  RETURN marketreview.write_result(new_revision, false);
END;
$fn$;

CREATE FUNCTION marketreview.delete_event(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  day text;
  market text;
  code text;
  direction text;
  group_count integer;
  direction_count integer;
  new_revision bigint;
  after_exists boolean;
  change_kind text;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  day := marketreview.require_request_trade_date(p_request);
  PERFORM marketreview.require_batch_time(p_request);
  market := marketreview.require_market(marketreview.json_text(p_request -> 'market', 'market', false));
  code := marketreview.require_code(marketreview.json_text(p_request -> 'code', 'code', false));
  direction := marketreview.require_direction(marketreview.json_text(p_request -> 'direction', 'direction', false));
  PERFORM marketreview.lock_ledger();
  SELECT count(*) INTO direction_count
  FROM marketreview.daily_price_limit_event AS event_row
  WHERE event_row.trade_date = day
    AND event_row.market = market
    AND event_row.code = code
    AND event_row.direction = direction;
  IF direction_count = 0 THEN
    RETURN marketreview.write_result(marketreview.current_revision(), true);
  END IF;
  SELECT count(*) INTO group_count
  FROM marketreview.daily_price_limit_event AS event_row
  WHERE event_row.trade_date = day
    AND event_row.market = market
    AND event_row.code = code;
  DELETE FROM marketreview.daily_price_limit_event AS event_row
  WHERE event_row.trade_date = day
    AND event_row.market = market
    AND event_row.code = code
    AND event_row.direction = direction;
  after_exists := group_count > direction_count;
  change_kind := CASE WHEN after_exists THEN 'update' ELSE 'delete' END;
  new_revision := marketreview.bump_revision();
  PERFORM marketreview.insert_history(
    'event',
    jsonb_build_object('trade_date', day, 'market', market, 'code', code),
    new_revision,
    change_kind,
    true,
    after_exists,
    NULL
  );
  RETURN marketreview.write_result(new_revision, false);
END;
$fn$;

CREATE FUNCTION marketreview.delete_review(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  day text;
  deleted integer;
  new_revision bigint;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  day := marketreview.require_request_trade_date(p_request);
  PERFORM marketreview.require_batch_time(p_request);
  PERFORM marketreview.lock_ledger();
  DELETE FROM marketreview.daily_market_review WHERE trade_date = day;
  GET DIAGNOSTICS deleted = ROW_COUNT;
  IF deleted = 0 THEN
    RETURN marketreview.write_result(marketreview.current_revision(), true);
  END IF;
  new_revision := marketreview.bump_revision();
  PERFORM marketreview.insert_history(
    'review', jsonb_build_object('trade_date', day), new_revision, 'delete', true, false, NULL
  );
  RETURN marketreview.write_result(new_revision, false);
END;
$fn$;

CREATE FUNCTION marketreview.delete_price_limit_events(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  day text;
  groups jsonb;
  new_revision bigint;
  item jsonb;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  day := marketreview.require_request_trade_date(p_request);
  PERFORM marketreview.require_batch_time(p_request);
  PERFORM marketreview.lock_ledger();
  SELECT COALESCE(jsonb_agg(DISTINCT jsonb_build_object('market', market, 'code', code)), '[]'::jsonb)
  INTO groups
  FROM marketreview.daily_price_limit_event
  WHERE trade_date = day;
  IF groups = '[]'::jsonb THEN
    RETURN marketreview.write_result(marketreview.current_revision(), true);
  END IF;
  DELETE FROM marketreview.daily_price_limit_event WHERE trade_date = day;
  new_revision := marketreview.bump_revision();
  FOR item IN SELECT value FROM jsonb_array_elements(groups) AS element(value)
  LOOP
    PERFORM marketreview.insert_history(
      'event',
      jsonb_build_object('trade_date', day, 'market', item ->> 'market', 'code', item ->> 'code'),
      new_revision,
      'delete',
      true,
      false,
      NULL
    );
  END LOOP;
  RETURN marketreview.write_result(new_revision, false);
END;
$fn$;

CREATE FUNCTION marketreview.replace_direction(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  day text;
  batch text;
  market text;
  code text;
  old_direction text;
  event_body jsonb;
  new_direction text;
  detail_row marketreview.daily_price_limit_event_detail%ROWTYPE;
  detail_exists boolean;
  sectors text[];
  reasons text[];
  new_revision bigint;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  day := marketreview.require_request_trade_date(p_request);
  batch := marketreview.require_batch_time(p_request);
  market := marketreview.require_market(marketreview.json_text(p_request -> 'market', 'market', false));
  code := marketreview.require_code(marketreview.json_text(p_request -> 'code', 'code', false));
  old_direction := marketreview.require_direction(
    marketreview.json_text(p_request -> 'old_direction', 'old_direction', false)
  );
  event_body := marketreview.parse_event_body(p_request -> 'event');
  IF (event_body ->> 'market') <> market OR (event_body ->> 'code') <> code THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 替换事件的 market/code 必须与目标一致');
  END IF;
  new_direction := event_body ->> 'direction';
  IF new_direction = old_direction THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 方向未变化');
  END IF;
  PERFORM marketreview.lock_ledger();
  IF NOT EXISTS (
    SELECT 1 FROM marketreview.daily_price_limit_event AS event_row
    WHERE event_row.trade_date = day
      AND event_row.market = market
      AND event_row.code = code
      AND event_row.direction = old_direction
  ) THEN
    PERFORM marketreview.raise_app('REPLACE_SOURCE_MISSING', '[REPLACE_SOURCE_MISSING] 被替换事件不存在');
  END IF;
  IF EXISTS (
    SELECT 1 FROM marketreview.daily_price_limit_event AS event_row
    WHERE event_row.trade_date = day
      AND event_row.market = market
      AND event_row.code = code
      AND event_row.direction = new_direction
  ) THEN
    PERFORM marketreview.raise_app('REPLACE_TARGET_EXISTS', '[REPLACE_TARGET_EXISTS] 目标方向已存在');
  END IF;
  SELECT * INTO detail_row
  FROM marketreview.daily_price_limit_event_detail AS detail_src
  WHERE detail_src.trade_date = day
    AND detail_src.market = market
    AND detail_src.code = code
    AND detail_src.direction = old_direction;
  detail_exists := FOUND;
  SELECT COALESCE(array_agg(sector_row.value ORDER BY sector_row.position), ARRAY[]::text[])
  INTO sectors
  FROM marketreview.daily_price_limit_event_sector AS sector_row
  WHERE sector_row.trade_date = day
    AND sector_row.market = market
    AND sector_row.code = code
    AND sector_row.direction = old_direction;
  SELECT COALESCE(array_agg(reason_row.value ORDER BY reason_row.position), ARRAY[]::text[])
  INTO reasons
  FROM marketreview.daily_price_limit_event_reason AS reason_row
  WHERE reason_row.trade_date = day
    AND reason_row.market = market
    AND reason_row.code = code
    AND reason_row.direction = old_direction;
  DELETE FROM marketreview.daily_price_limit_event AS event_row
  WHERE event_row.trade_date = day
    AND event_row.market = market
    AND event_row.code = code
    AND event_row.direction = old_direction;
  INSERT INTO marketreview.daily_price_limit_event (
    trade_date, market, code, name, direction, closed_at_limit, limit_rate_bp, streak_height,
    created_at, updated_at
  ) VALUES (
    day, market, code, event_body ->> 'name', new_direction,
    (event_body ->> 'closed_at_limit')::integer,
    (event_body ->> 'limit_rate_bp')::integer,
    (event_body ->> 'streak_height')::integer,
    batch, batch
  );
  IF detail_exists THEN
    INSERT INTO marketreview.daily_price_limit_event_detail (
      trade_date, market, code, direction,
      previous_turnover_amount, auction_amount, previous_close, open_price,
      turnover_amount, turnover_rate, is_leader, note, created_at, updated_at
    ) VALUES (
      day, market, code, new_direction,
      detail_row.previous_turnover_amount, detail_row.auction_amount, detail_row.previous_close,
      detail_row.open_price, detail_row.turnover_amount, detail_row.turnover_rate,
      detail_row.is_leader, detail_row.note, batch, batch
    );
  END IF;
  PERFORM marketreview.replace_text_list(
    'daily_price_limit_event_sector', day, market, code, new_direction, sectors
  );
  IF new_direction = 'up' THEN
    PERFORM marketreview.replace_text_list(
      'daily_price_limit_event_reason', day, market, code, new_direction, reasons
    );
  END IF;
  new_revision := marketreview.bump_revision();
  PERFORM marketreview.insert_history(
    'event',
    jsonb_build_object('trade_date', day, 'market', market, 'code', code),
    new_revision,
    'direction_replace',
    true,
    true,
    NULL
  );
  RETURN marketreview.write_result(new_revision, false);
END;
$fn$;
