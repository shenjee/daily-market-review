-- 同步提交与授权收尾。与 0001、0002 同一会话执行。

CREATE FUNCTION marketreview.sync_review_keys()
RETURNS text[]
LANGUAGE sql
IMMUTABLE
AS $fn$
  SELECT ARRAY[
    'trade_date', 'advancing_count', 'avg_stock_price', 'cy_index_close', 'cy_index_prev_close',
    'declining_count', 'float_market_cap', 'margin_balance_bj', 'margin_balance_sh',
    'margin_balance_sz', 'median_change_pct', 'pe_all', 'pe_cy', 'pe_sh', 'pe_sz',
    'pullback_count', 'sh_index_close', 'sh_index_prev_close', 'sz_index_close',
    'sz_index_prev_close', 'total_market_cap', 'turnover_amount_bj', 'turnover_amount_cy',
    'turnover_amount_sh', 'turnover_amount_sz', 'created_at', 'updated_at'
  ];
$fn$;

CREATE FUNCTION marketreview.assert_sync_review(p_key jsonb, p_review jsonb)
RETURNS void
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  key text;
  day text;
BEGIN
  IF p_review IS NULL OR jsonb_typeof(p_review) IS DISTINCT FROM 'object' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] review 必须为对象');
  END IF;
  FOR key IN SELECT jsonb_object_keys(p_review)
  LOOP
    IF key <> ALL(marketreview.sync_review_keys()) THEN
      PERFORM marketreview.raise_app('UNKNOWN_FIELD', format('[UNKNOWN_FIELD] 未知字段：%s', key));
    END IF;
  END LOOP;
  IF NOT jsonb_exists_all(p_review, marketreview.sync_review_keys()) THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 同步复盘缺少字段');
  END IF;
  day := p_key ->> 'trade_date';
  IF marketreview.parse_trade_date(p_review ->> 'trade_date', 'trade_date') <> day THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 复盘日期与组键不一致');
  END IF;
END;
$fn$;

CREATE FUNCTION marketreview.insert_sync_review(p_key jsonb, p_review jsonb)
RETURNS void
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  day text;
BEGIN
  PERFORM marketreview.assert_sync_review(p_key, p_review);
  day := p_key ->> 'trade_date';
  INSERT INTO marketreview.daily_market_review (
    trade_date, advancing_count, avg_stock_price, cy_index_close, cy_index_prev_close,
    declining_count, float_market_cap, margin_balance_bj, margin_balance_sh, margin_balance_sz,
    median_change_pct, pe_all, pe_cy, pe_sh, pe_sz, pullback_count, sh_index_close,
    sh_index_prev_close, sz_index_close, sz_index_prev_close, total_market_cap,
    turnover_amount_bj, turnover_amount_cy, turnover_amount_sh, turnover_amount_sz,
    created_at, updated_at
  ) VALUES (
    day,
    marketreview.json_int4(p_review -> 'advancing_count', 'advancing_count', true),
    marketreview.json_number(p_review -> 'avg_stock_price', 'avg_stock_price', true),
    marketreview.json_number(p_review -> 'cy_index_close', 'cy_index_close', true),
    marketreview.json_number(p_review -> 'cy_index_prev_close', 'cy_index_prev_close', true),
    marketreview.json_int4(p_review -> 'declining_count', 'declining_count', true),
    marketreview.json_number(p_review -> 'float_market_cap', 'float_market_cap', true),
    marketreview.json_number(p_review -> 'margin_balance_bj', 'margin_balance_bj', true),
    marketreview.json_number(p_review -> 'margin_balance_sh', 'margin_balance_sh', true),
    marketreview.json_number(p_review -> 'margin_balance_sz', 'margin_balance_sz', true),
    marketreview.json_number(p_review -> 'median_change_pct', 'median_change_pct', true),
    marketreview.json_number(p_review -> 'pe_all', 'pe_all', true),
    marketreview.json_number(p_review -> 'pe_cy', 'pe_cy', true),
    marketreview.json_number(p_review -> 'pe_sh', 'pe_sh', true),
    marketreview.json_number(p_review -> 'pe_sz', 'pe_sz', true),
    marketreview.json_int4(p_review -> 'pullback_count', 'pullback_count', true),
    marketreview.json_number(p_review -> 'sh_index_close', 'sh_index_close', true),
    marketreview.json_number(p_review -> 'sh_index_prev_close', 'sh_index_prev_close', true),
    marketreview.json_number(p_review -> 'sz_index_close', 'sz_index_close', true),
    marketreview.json_number(p_review -> 'sz_index_prev_close', 'sz_index_prev_close', true),
    marketreview.json_number(p_review -> 'total_market_cap', 'total_market_cap', true),
    marketreview.json_number(p_review -> 'turnover_amount_bj', 'turnover_amount_bj', true),
    marketreview.json_number(p_review -> 'turnover_amount_cy', 'turnover_amount_cy', true),
    marketreview.json_number(p_review -> 'turnover_amount_sh', 'turnover_amount_sh', true),
    marketreview.json_number(p_review -> 'turnover_amount_sz', 'turnover_amount_sz', true),
    marketreview.require_timestamp(p_review ->> 'created_at', 'created_at'),
    marketreview.require_timestamp(p_review ->> 'updated_at', 'updated_at')
  );
END;
$fn$;

CREATE FUNCTION marketreview.insert_sync_direction(
  p_trade_date text,
  p_market text,
  p_code text,
  p_event jsonb
) RETURNS void
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  direction text;
  detail_exists boolean;
  detail jsonb;
  key text;
  allowed_event text[] := ARRAY[
    'direction', 'name', 'closed_at_limit', 'limit_rate_bp', 'streak_height',
    'created_at', 'updated_at', 'detail_exists', 'detail', 'sectors', 'limit_up_reasons'
  ];
  detail_keys text[] := ARRAY[
    'previous_turnover_amount', 'auction_amount', 'previous_close', 'open_price',
    'turnover_amount', 'turnover_rate', 'is_leader', 'note', 'created_at', 'updated_at'
  ];
  number_fields text[] := ARRAY[
    'previous_turnover_amount', 'auction_amount', 'previous_close', 'open_price',
    'turnover_amount', 'turnover_rate'
  ];
  sectors text[];
  reasons text[];
BEGIN
  IF p_event IS NULL OR jsonb_typeof(p_event) IS DISTINCT FROM 'object' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 同步事件必须为对象');
  END IF;
  FOR key IN SELECT jsonb_object_keys(p_event)
  LOOP
    IF key <> ALL(allowed_event) THEN
      PERFORM marketreview.raise_app('UNKNOWN_FIELD', format('[UNKNOWN_FIELD] 未知字段：%s', key));
    END IF;
  END LOOP;
  IF NOT jsonb_exists_all(p_event, allowed_event) THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 同步事件缺少字段');
  END IF;
  direction := marketreview.require_direction(marketreview.json_text(p_event -> 'direction', 'direction', false));
  IF jsonb_typeof(p_event -> 'detail_exists') IS DISTINCT FROM 'boolean' THEN
    PERFORM marketreview.raise_app('INVALID_TYPE', '[INVALID_TYPE] detail_exists 必须为布尔值');
  END IF;
  detail_exists := p_event -> 'detail_exists' = 'true'::jsonb;
  sectors := marketreview.json_string_list(p_event -> 'sectors', 'sectors');
  reasons := marketreview.json_string_list(p_event -> 'limit_up_reasons', 'limit_up_reasons');
  IF direction = 'down' AND coalesce(array_length(reasons, 1), 0) > 0 THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] down 方向不能带涨停原因');
  END IF;
  IF detail_exists THEN
    detail := p_event -> 'detail';
    IF detail IS NULL OR jsonb_typeof(detail) <> 'object' THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] detail_exists 为真时必须提供明细对象');
    END IF;
    FOR key IN SELECT jsonb_object_keys(detail)
    LOOP
      IF key <> ALL(detail_keys) THEN
        PERFORM marketreview.raise_app('UNKNOWN_FIELD', format('[UNKNOWN_FIELD] 未知明细字段：%s', key));
      END IF;
    END LOOP;
    IF NOT jsonb_exists_all(detail, detail_keys) THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 同步明细缺少字段');
    END IF;
  ELSIF jsonb_typeof(p_event -> 'detail') IS DISTINCT FROM 'null' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 无明细行时 detail 必须为 null');
  END IF;
  INSERT INTO marketreview.daily_price_limit_event (
    trade_date, market, code, name, direction, closed_at_limit, limit_rate_bp, streak_height,
    created_at, updated_at
  ) VALUES (
    p_trade_date,
    p_market,
    p_code,
    marketreview.json_text(p_event -> 'name', 'name', false),
    direction,
    marketreview.bool_to_smallint(marketreview.json_bool(p_event -> 'closed_at_limit', 'closed_at_limit', false)),
    marketreview.limit_rate(p_event -> 'limit_rate_bp'),
    marketreview.streak_height(p_event -> 'streak_height'),
    marketreview.require_timestamp(p_event ->> 'created_at', 'created_at'),
    marketreview.require_timestamp(p_event ->> 'updated_at', 'updated_at')
  );
  IF detail_exists THEN
    INSERT INTO marketreview.daily_price_limit_event_detail (
      trade_date, market, code, direction,
      previous_turnover_amount, auction_amount, previous_close, open_price,
      turnover_amount, turnover_rate, is_leader, note, created_at, updated_at
    ) VALUES (
      p_trade_date, p_market, p_code, direction,
      marketreview.json_detail_number(detail -> 'previous_turnover_amount', 'previous_turnover_amount'),
      marketreview.json_detail_number(detail -> 'auction_amount', 'auction_amount'),
      marketreview.json_detail_number(detail -> 'previous_close', 'previous_close'),
      marketreview.json_detail_number(detail -> 'open_price', 'open_price'),
      marketreview.json_detail_number(detail -> 'turnover_amount', 'turnover_amount'),
      marketreview.json_detail_number(detail -> 'turnover_rate', 'turnover_rate'),
      marketreview.bool_to_smallint(marketreview.json_bool(detail -> 'is_leader', 'is_leader', true)),
      NULLIF(btrim(marketreview.json_text(detail -> 'note', 'note', true)), ''),
      marketreview.require_timestamp(detail ->> 'created_at', 'created_at'),
      marketreview.require_timestamp(detail ->> 'updated_at', 'updated_at')
    );
  END IF;
  PERFORM marketreview.replace_text_list(
    'daily_price_limit_event_sector', p_trade_date, p_market, p_code, direction, sectors
  );
  PERFORM marketreview.replace_text_list(
    'daily_price_limit_event_reason', p_trade_date, p_market, p_code, direction, reasons
  );
END;
$fn$;

CREATE FUNCTION marketreview.assert_sync_direction(p_event jsonb)
RETURNS void
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  direction text;
  detail_exists boolean;
  detail jsonb;
  key text;
  allowed_event text[] := ARRAY[
    'direction', 'name', 'closed_at_limit', 'limit_rate_bp', 'streak_height',
    'created_at', 'updated_at', 'detail_exists', 'detail', 'sectors', 'limit_up_reasons'
  ];
  detail_keys text[] := ARRAY[
    'previous_turnover_amount', 'auction_amount', 'previous_close', 'open_price',
    'turnover_amount', 'turnover_rate', 'is_leader', 'note', 'created_at', 'updated_at'
  ];
  reasons text[];
BEGIN
  IF p_event IS NULL OR jsonb_typeof(p_event) IS DISTINCT FROM 'object' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 同步事件必须为对象');
  END IF;
  FOR key IN SELECT jsonb_object_keys(p_event)
  LOOP
    IF key <> ALL(allowed_event) THEN
      PERFORM marketreview.raise_app('UNKNOWN_FIELD', format('[UNKNOWN_FIELD] 未知字段：%s', key));
    END IF;
  END LOOP;
  IF NOT jsonb_exists_all(p_event, allowed_event) THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 同步事件缺少字段');
  END IF;
  direction := marketreview.require_direction(marketreview.json_text(p_event -> 'direction', 'direction', false));
  IF jsonb_typeof(p_event -> 'detail_exists') IS DISTINCT FROM 'boolean' THEN
    PERFORM marketreview.raise_app('INVALID_TYPE', '[INVALID_TYPE] detail_exists 必须为布尔值');
  END IF;
  detail_exists := p_event -> 'detail_exists' = 'true'::jsonb;
  PERFORM marketreview.json_string_list(p_event -> 'sectors', 'sectors');
  reasons := marketreview.json_string_list(p_event -> 'limit_up_reasons', 'limit_up_reasons');
  IF direction = 'down' AND coalesce(array_length(reasons, 1), 0) > 0 THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] down 方向不能带涨停原因');
  END IF;
  IF detail_exists THEN
    detail := p_event -> 'detail';
    IF detail IS NULL OR jsonb_typeof(detail) IS DISTINCT FROM 'object' THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] detail_exists 为真时必须提供明细对象');
    END IF;
    FOR key IN SELECT jsonb_object_keys(detail)
    LOOP
      IF key <> ALL(detail_keys) THEN
        PERFORM marketreview.raise_app('UNKNOWN_FIELD', format('[UNKNOWN_FIELD] 未知明细字段：%s', key));
      END IF;
    END LOOP;
    IF NOT jsonb_exists_all(detail, detail_keys) THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 同步明细缺少字段');
    END IF;
  ELSIF jsonb_typeof(p_event -> 'detail') IS DISTINCT FROM 'null' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 无明细行时 detail 必须为 null');
  END IF;
  PERFORM marketreview.json_text(p_event -> 'name', 'name', false);
  PERFORM marketreview.json_bool(p_event -> 'closed_at_limit', 'closed_at_limit', false);
  PERFORM marketreview.limit_rate(p_event -> 'limit_rate_bp');
  PERFORM marketreview.streak_height(p_event -> 'streak_height');
  PERFORM marketreview.require_timestamp(p_event ->> 'created_at', 'created_at');
  PERFORM marketreview.require_timestamp(p_event ->> 'updated_at', 'updated_at');
END;
$fn$;

CREATE FUNCTION marketreview.assert_sync_group(p_group jsonb)
RETURNS void
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  kind text;
  gkey jsonb;
  exists_flag boolean;
  event_item jsonb;
  seen_directions text[] := ARRAY[]::text[];
  direction text;
BEGIN
  IF p_group IS NULL OR jsonb_typeof(p_group) IS DISTINCT FROM 'object' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 同步组必须为对象');
  END IF;
  IF jsonb_typeof(p_group -> 'group_kind') IS DISTINCT FROM 'string' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] group_kind 必须为字符串');
  END IF;
  kind := p_group ->> 'group_kind';
  gkey := marketreview.canonical_group_key(kind, p_group -> 'group_key');
  IF jsonb_typeof(p_group -> 'exists') IS DISTINCT FROM 'boolean' THEN
    PERFORM marketreview.raise_app('INVALID_TYPE', '[INVALID_TYPE] exists 必须为布尔值');
  END IF;
  exists_flag := p_group -> 'exists' = 'true'::jsonb;
  IF kind = 'review' THEN
    IF jsonb_exists(p_group, 'events') THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 复盘组不能包含 events');
    END IF;
    IF exists_flag THEN
      PERFORM marketreview.assert_sync_review(gkey, p_group -> 'review');
    ELSIF jsonb_exists(p_group, 'review') THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 删除提交不能携带业务内容');
    END IF;
    RETURN;
  END IF;
  IF jsonb_exists(p_group, 'review') THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 事件组不能包含 review');
  END IF;
  IF NOT exists_flag THEN
    IF jsonb_exists(p_group, 'events') THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 删除提交不能携带业务内容');
    END IF;
    RETURN;
  END IF;
  IF jsonb_typeof(p_group -> 'events') IS DISTINCT FROM 'array' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 存在的事件组至少要有一个方向');
  END IF;
  IF jsonb_array_length(p_group -> 'events') < 1 THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 存在的事件组至少要有一个方向');
  END IF;
  FOR event_item IN SELECT value FROM jsonb_array_elements(p_group -> 'events') AS element(value)
  LOOP
    PERFORM marketreview.assert_sync_direction(event_item);
    direction := event_item ->> 'direction';
    IF direction = ANY(seen_directions) THEN
      PERFORM marketreview.raise_app('DUPLICATE_IDENTITY', '[DUPLICATE_IDENTITY] 同步事件方向重复');
    END IF;
    seen_directions := seen_directions || direction;
  END LOOP;
END;
$fn$;

CREATE FUNCTION marketreview.apply_sync_group(p_group jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  kind text;
  gkey jsonb;
  exists_flag boolean;
  before_exists boolean;
  event_item jsonb;
  seen_directions text[] := ARRAY[]::text[];
  direction text;
  public_events jsonb;
BEGIN
  PERFORM marketreview.assert_sync_group(p_group);
  IF jsonb_typeof(p_group -> 'group_kind') IS DISTINCT FROM 'string' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] group_kind 必须为字符串');
  END IF;
  kind := p_group ->> 'group_kind';
  gkey := marketreview.canonical_group_key(kind, p_group -> 'group_key');
  IF jsonb_typeof(p_group -> 'exists') IS DISTINCT FROM 'boolean' THEN
    PERFORM marketreview.raise_app('INVALID_TYPE', '[INVALID_TYPE] exists 必须为布尔值');
  END IF;
  exists_flag := p_group -> 'exists' = 'true'::jsonb;
  IF kind = 'review' THEN
    IF jsonb_exists(p_group, 'events') THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 复盘组不能包含 events');
    END IF;
    IF exists_flag THEN
      PERFORM marketreview.assert_sync_review(gkey, p_group -> 'review');
    ELSIF jsonb_exists(p_group, 'review') THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 删除提交不能携带业务内容');
    END IF;
    SELECT EXISTS (
      SELECT 1 FROM marketreview.daily_market_review WHERE trade_date = gkey ->> 'trade_date'
    ) INTO before_exists;
    DELETE FROM marketreview.daily_market_review WHERE trade_date = gkey ->> 'trade_date';
    IF exists_flag THEN
      PERFORM marketreview.insert_sync_review(gkey, p_group -> 'review');
      RETURN jsonb_build_object(
        'public_group', jsonb_build_object(
          'group_kind', 'review',
          'group_key', gkey,
          'exists', true,
          'review', marketreview.review_json_by_date(gkey ->> 'trade_date')
        ),
        'history', jsonb_build_object(
          'group_kind', 'review',
          'group_key', gkey,
          'before_exists', before_exists,
          'after_exists', true,
          'change_kind', 'sync'
        )
      );
    END IF;
    RETURN jsonb_build_object(
      'public_group', jsonb_build_object('group_kind', 'review', 'group_key', gkey, 'exists', false),
      'history', jsonb_build_object(
        'group_kind', 'review',
        'group_key', gkey,
        'before_exists', before_exists,
        'after_exists', false,
        'change_kind', 'delete'
      )
    );
  END IF;
  IF jsonb_exists(p_group, 'review') THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 事件组不能包含 review');
  END IF;
  before_exists := marketreview.event_group_exists(gkey ->> 'trade_date', gkey ->> 'market', gkey ->> 'code');
  IF NOT exists_flag THEN
    IF jsonb_exists(p_group, 'events') THEN
      PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 删除提交不能携带业务内容');
    END IF;
    DELETE FROM marketreview.daily_price_limit_event
    WHERE trade_date = gkey ->> 'trade_date'
      AND market = gkey ->> 'market'
      AND code = gkey ->> 'code';
    RETURN jsonb_build_object(
      'public_group', jsonb_build_object('group_kind', 'event', 'group_key', gkey, 'exists', false),
      'history', jsonb_build_object(
        'group_kind', 'event',
        'group_key', gkey,
        'before_exists', before_exists,
        'after_exists', false,
        'change_kind', 'delete'
      )
    );
  END IF;
  IF jsonb_typeof(p_group -> 'events') IS DISTINCT FROM 'array' THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 存在的事件组至少要有一个方向');
  END IF;
  IF jsonb_array_length(p_group -> 'events') < 1 THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] 存在的事件组至少要有一个方向');
  END IF;
  DELETE FROM marketreview.daily_price_limit_event
  WHERE trade_date = gkey ->> 'trade_date'
    AND market = gkey ->> 'market'
    AND code = gkey ->> 'code';
  FOR event_item IN SELECT value FROM jsonb_array_elements(p_group -> 'events') AS element(value)
  LOOP
    direction := marketreview.require_direction(
      marketreview.json_text(event_item -> 'direction', 'direction', false)
    );
    IF direction = ANY(seen_directions) THEN
      PERFORM marketreview.raise_app('DUPLICATE_IDENTITY', '[DUPLICATE_IDENTITY] 同步事件方向重复');
    END IF;
    seen_directions := seen_directions || direction;
    PERFORM marketreview.insert_sync_direction(
      gkey ->> 'trade_date', gkey ->> 'market', gkey ->> 'code', event_item
    );
  END LOOP;
  SELECT COALESCE(
    jsonb_agg(marketreview.sync_direction_json(
      gkey ->> 'trade_date', gkey ->> 'market', gkey ->> 'code', e.direction
    ) ORDER BY e.direction),
    '[]'::jsonb
  )
  INTO public_events
  FROM marketreview.daily_price_limit_event AS e
  WHERE e.trade_date = gkey ->> 'trade_date'
    AND e.market = gkey ->> 'market'
    AND e.code = gkey ->> 'code';
  RETURN jsonb_build_object(
    'public_group', jsonb_build_object(
      'group_kind', 'event',
      'group_key', gkey,
      'exists', true,
      'events', public_events
    ),
    'history', jsonb_build_object(
      'group_kind', 'event',
      'group_key', gkey,
      'before_exists', before_exists,
      'after_exists', true,
      'change_kind', 'sync'
    )
  );
END;
$fn$;

CREATE FUNCTION marketreview.expected_revision(p_request jsonb)
RETURNS bigint
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  raw numeric;
BEGIN
  IF NOT marketreview.json_type_is(p_request -> 'expected_revision', 'number')
     OR COALESCE((p_request ->> 'expected_revision') !~ '^[0-9]+$', true) THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] expected_revision 必须为非负整数');
  END IF;
  raw := (p_request ->> 'expected_revision')::numeric;
  IF raw > 9223372036854775807 THEN
    PERFORM marketreview.raise_app('INVALID_REQUEST', '[INVALID_REQUEST] expected_revision 超出范围');
  END IF;
  RETURN raw::bigint;
END;
$fn$;

CREATE FUNCTION marketreview.sync_snapshot(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  result jsonb;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  SELECT jsonb_build_object(
    'format_version', 1,
    'schema_version', s.schema_version,
    'complete', true,
    'revision', l.revision,
    'ledger_key', l.ledger_key,
    'reviews', review_rows.payload,
    'events', event_rows.payload,
    'details', detail_rows.payload,
    'sectors', sector_rows.payload,
    'reasons', reason_rows.payload,
    'history', history_rows.payload,
    'sync_results', result_rows.payload,
    'counts', jsonb_build_object(
      'reviews', jsonb_array_length(review_rows.payload),
      'events', jsonb_array_length(event_rows.payload),
      'details', jsonb_array_length(detail_rows.payload),
      'sectors', jsonb_array_length(sector_rows.payload),
      'reasons', jsonb_array_length(reason_rows.payload),
      'history', jsonb_array_length(history_rows.payload),
      'sync_results', jsonb_array_length(result_rows.payload)
    )
  )
  INTO result
  FROM marketreview.schema_meta AS s
  JOIN marketreview.ledger AS l ON l.ledger_key = 'main'
  CROSS JOIN LATERAL (
    SELECT COALESCE(jsonb_agg(marketreview.review_json(r) ORDER BY r.trade_date), '[]'::jsonb) AS payload
    FROM marketreview.daily_market_review AS r
  ) AS review_rows
  CROSS JOIN LATERAL (
    SELECT COALESCE(
      jsonb_agg(marketreview.event_row_json(e) ORDER BY e.trade_date, e.market, e.code, e.direction),
      '[]'::jsonb
    ) AS payload
    FROM marketreview.daily_price_limit_event AS e
  ) AS event_rows
  CROSS JOIN LATERAL (
    SELECT COALESCE(
      jsonb_agg(marketreview.detail_row_json(d) ORDER BY d.trade_date, d.market, d.code, d.direction),
      '[]'::jsonb
    ) AS payload
    FROM marketreview.daily_price_limit_event_detail AS d
  ) AS detail_rows
  CROSS JOIN LATERAL (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'trade_date', sector.trade_date, 'market', sector.market, 'code', sector.code,
      'direction', sector.direction, 'position', sector.position, 'value', sector.value
    ) ORDER BY sector.trade_date, sector.market, sector.code, sector.direction, sector.position), '[]'::jsonb) AS payload
    FROM marketreview.daily_price_limit_event_sector AS sector
  ) AS sector_rows
  CROSS JOIN LATERAL (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'trade_date', reason.trade_date, 'market', reason.market, 'code', reason.code,
      'direction', reason.direction, 'position', reason.position, 'value', reason.value
    ) ORDER BY reason.trade_date, reason.market, reason.code, reason.direction, reason.position), '[]'::jsonb) AS payload
    FROM marketreview.daily_price_limit_event_reason AS reason
  ) AS reason_rows
  CROSS JOIN LATERAL (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'group_kind', h.group_kind,
      'group_key', h.group_key,
      'revision', h.revision,
      'change_kind', h.change_kind,
      'before_exists', h.before_exists,
      'after_exists', h.after_exists,
      'operation_id', h.operation_id
    ) ORDER BY h.revision, h.group_kind, h.group_key::text), '[]'::jsonb) AS payload
    FROM marketreview.group_change_history AS h
  ) AS history_rows
  CROSS JOIN LATERAL (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'operation_id', stored.operation_id,
      'project_id', stored.project_id,
      'ledger_id', stored.ledger_id,
      'request_digest', stored.request_digest,
      'expected_revision', stored.expected_revision,
      'committed_revision', stored.committed_revision,
      'result_version', stored.result_version,
      'result_payload', stored.result_payload
    ) ORDER BY stored.operation_id), '[]'::jsonb) AS payload
    FROM marketreview.sync_commit_result AS stored
  ) AS result_rows
  WHERE s.id = 1
    AND s.schema_version = (p_request ->> 'schema_version')::integer;
  IF result IS NULL THEN
    PERFORM marketreview.raise_app('SCHEMA_VERSION_MISMATCH', '[SCHEMA_VERSION_MISMATCH] schema 版本不一致');
  END IF;
  RETURN result;
END;
$fn$;

CREATE FUNCTION marketreview.sync_precheck(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  snapshot jsonb;
BEGIN
  snapshot := marketreview.sync_snapshot(p_request);
  RETURN jsonb_build_object(
    'format_version', 1,
    'schema_version', snapshot -> 'schema_version',
    'complete', true,
    'revision', snapshot -> 'revision',
    'ledger_key', snapshot -> 'ledger_key',
    'counts', snapshot -> 'counts'
  );
END;
$fn$;

CREATE FUNCTION marketreview.sync_commit(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  operation_id text;
  project_id text;
  ledger_id text;
  digest text;
  stored_digest text;
  stored_payload jsonb;
  groups jsonb;
  expected bigint;
  locked bigint;
  applied jsonb;
  public_groups jsonb := '[]'::jsonb;
  histories jsonb := '[]'::jsonb;
  item jsonb;
  new_revision bigint;
  distinct_keys integer;
  group_count integer;
  payload jsonb;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  IF octet_length(p_request::text) > 8388608 THEN
    PERFORM marketreview.raise_app('REQUEST_TOO_LARGE', '[REQUEST_TOO_LARGE] 提交超过上限，整次失败');
  END IF;
  operation_id := marketreview.require_token(p_request, 'operation_id', 128);
  project_id := marketreview.require_token(p_request, 'project_id', 128);
  ledger_id := marketreview.require_token(p_request, 'ledger_id', 128);
  digest := marketreview.require_digest(p_request);
  PERFORM pg_advisory_xact_lock(hashtextextended(operation_id, 0));
  SELECT request_digest, result_payload
  INTO stored_digest, stored_payload
  FROM marketreview.sync_commit_result
  WHERE sync_commit_result.operation_id = operation_id;
  IF FOUND THEN
    IF stored_digest = digest THEN
      RETURN stored_payload;
    END IF;
    PERFORM marketreview.raise_app(
      'OPERATION_DIGEST_MISMATCH',
      '[OPERATION_DIGEST_MISMATCH] 同一 operation_id 的摘要不一致'
    );
  END IF;
  groups := p_request -> 'groups';
  IF groups IS NULL OR jsonb_typeof(groups) <> 'array' OR jsonb_array_length(groups) = 0 THEN
    PERFORM marketreview.raise_app('EMPTY_COMMIT', '[EMPTY_COMMIT] 空提交集合不写');
  END IF;
  SELECT count(*), count(DISTINCT marketreview.canonical_group_key(value ->> 'group_kind', value -> 'group_key'))
  INTO group_count, distinct_keys
  FROM jsonb_array_elements(groups) AS element(value);
  IF distinct_keys <> group_count THEN
    PERFORM marketreview.raise_app('DUPLICATE_IDENTITY', '[DUPLICATE_IDENTITY] 提交集合有重复组');
  END IF;
  expected := marketreview.expected_revision(p_request);
  locked := marketreview.lock_ledger();
  IF locked <> expected THEN
    PERFORM marketreview.raise_app('REVISION_CONFLICT', '[REVISION_CONFLICT] 预期云端版本已变化，整批已回滚');
  END IF;
  FOR item IN SELECT value FROM jsonb_array_elements(groups) AS element(value)
  LOOP
    applied := marketreview.apply_sync_group(item);
    public_groups := public_groups || jsonb_build_array(applied -> 'public_group');
    histories := histories || jsonb_build_array(applied -> 'history');
  END LOOP;
  new_revision := marketreview.bump_revision();
  FOR item IN SELECT value FROM jsonb_array_elements(histories) AS element(value)
  LOOP
    PERFORM marketreview.insert_history(
      item ->> 'group_kind',
      item -> 'group_key',
      new_revision,
      item ->> 'change_kind',
      (item ->> 'before_exists')::boolean,
      (item ->> 'after_exists')::boolean,
      operation_id
    );
  END LOOP;
  payload := jsonb_build_object(
    'format_version', 1,
    'schema_version', 1,
    'operation_id', operation_id,
    'project_id', project_id,
    'ledger_id', ledger_id,
    'request_digest', digest,
    'expected_revision', expected,
    'committed_revision', new_revision,
    'groups', public_groups
  );
  INSERT INTO marketreview.sync_commit_result (
    operation_id, project_id, ledger_id, request_digest, expected_revision,
    committed_revision, result_version, result_payload
  ) VALUES (
    operation_id, project_id, ledger_id, digest, expected, new_revision, 1, payload
  );
  RETURN payload;
END;
$fn$;

CREATE FUNCTION marketreview.sync_result(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = marketreview, pg_temp
AS $fn$
#variable_conflict use_variable
DECLARE
  operation_id text;
  payload jsonb;
BEGIN
  PERFORM marketreview.assert_schema(p_request);
  operation_id := marketreview.require_token(p_request, 'operation_id', 128);
  SELECT result_payload INTO payload
  FROM marketreview.sync_commit_result
  WHERE sync_commit_result.operation_id = operation_id;
  IF NOT FOUND THEN
    PERFORM marketreview.raise_app(
      'OPERATION_NOT_FOUND',
      '[OPERATION_NOT_FOUND] 未找到提交结果，不能据此推断已回滚'
    );
  END IF;
  RETURN payload;
END;
$fn$;

DO $wrap$
DECLARE
  fn text;
BEGIN
  FOREACH fn IN ARRAY ARRAY[
    'probe', 'get_day', 'list_reviews', 'list_trade_dates', 'list_events', 'list_event_details',
    'replace_direction_preimage', 'save_review', 'save_events', 'save_event_details',
    'delete_event', 'replace_direction', 'delete_review', 'delete_price_limit_events',
    'sync_snapshot', 'sync_precheck', 'sync_commit', 'sync_result'
  ]
  LOOP
    EXECUTE format(
      'CREATE OR REPLACE FUNCTION public.marketreview_%s(p_request jsonb)
       RETURNS jsonb
       LANGUAGE sql
       SECURITY INVOKER
       SET search_path = marketreview, pg_temp
       AS %L',
      fn,
      format('SELECT marketreview.%I(p_request)', fn)
    );
    EXECUTE format(
      'REVOKE ALL ON FUNCTION public.marketreview_%s(jsonb) FROM PUBLIC, anon, authenticated',
      fn
    );
    EXECUTE format(
      'GRANT EXECUTE ON FUNCTION public.marketreview_%s(jsonb) TO service_role',
      fn
    );
  END LOOP;
END;
$wrap$;

REVOKE ALL ON SCHEMA marketreview FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA marketreview TO service_role;
REVOKE ALL ON ALL TABLES IN SCHEMA marketreview FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA marketreview TO service_role;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA marketreview FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA marketreview TO service_role;

COMMIT;
