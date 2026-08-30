DO $$ 
DECLARE 
    v_column_names text; 
    v_columns_cast text; 
    v_update_clause text; 
    v_insert_sql text; 
    
    v_start timestamptz := '2026-07-19 20:09:20.753693-07'; 
    v_end timestamptz := '2026-07-20 01:28:44.577008-07'; 
    v_device integer := 1; 
    
    -- Split target parameters or keep plain for catalog lookup compatibility
    v_schema_name text := 'public';
    v_table_name text := 'device_metrics_wide__eg4_18kpv'; 
BEGIN 
    -- STEP 1: Generate column names, data casts, and the conflict update assignments simultaneously 
    SELECT 
        string_agg(quote_ident(sub.metric_name), ', ' ORDER BY sub.metric_name), 
        string_agg( 
            CASE 
                WHEN sub.data_type IN ('double precision', 'real', 'numeric') THEN 
                    format('MAX(CASE WHEN m.metric_name = %L THEN COALESCE(m.metric_value::text, m.metric_ascii) END)::double precision', sub.metric_name) 
                WHEN sub.data_type IN ('integer', 'smallint', 'bigint') THEN 
                    format('MAX(CASE WHEN m.metric_name = %L THEN COALESCE(m.metric_value::text, m.metric_ascii) END)::integer', sub.metric_name) 
                WHEN sub.data_type = 'boolean' THEN 
                    format('CASE WHEN LOWER(MAX(CASE WHEN m.metric_name = %L THEN COALESCE(m.metric_value::text, m.metric_ascii) END)) IN (''1'', ''true'', ''t'', ''on'', ''yes'', ''y'') THEN true WHEN LOWER(MAX(CASE WHEN m.metric_name = %L THEN COALESCE(m.metric_value::text, m.metric_ascii) END)) IN (''0'', ''false'', ''f'', ''off'', ''no'', ''n'') THEN false END', sub.metric_name, sub.metric_name) 
                ELSE 
                    format('MAX(CASE WHEN m.metric_name = %L THEN COALESCE(m.metric_value::text, m.metric_ascii) END)', sub.metric_name) 
            END || format(' AS %I', sub.metric_name), 
            ', ' ORDER BY sub.metric_name 
        ), 
        string_agg(format('%I = EXCLUDED.%I', sub.metric_name, sub.metric_name), ', ' ORDER BY sub.metric_name) 
    INTO v_column_names, v_columns_cast, v_update_clause 
    FROM ( 
        SELECT DISTINCT m.metric_name, c.data_type 
        FROM public.device_metrics_narrow m 
        JOIN information_schema.columns c 
          ON c.table_name = v_table_name  -- Clean name comparison
         AND c.table_schema = v_schema_name 
         AND c.column_name = m.metric_name 
        WHERE m.m_time >= v_start 
          AND m.m_time < v_end 
          AND m.device_info_id = v_device 
    ) sub; 

    -- Safety check: Stop if no metric columns match the wide schema definition 
    IF v_column_names IS NULL THEN 
        RAISE NOTICE 'No narrow rows found matching the destination column schema rules.'; 
        RETURN; 
    END IF; 

    -- STEP 2: Assemble the query with corrected format parameters (%I placeholders for identifiers)
    -- %I (Identifier): Used strictly for SQL Object Names like table names, schema names,
    -- or column headers. It treats the value as a structural database object and automatically wraps it
    -- in double-quotes (") if it contains uppercase letters, spaces, or special characters
    v_insert_sql := format(' 
        INSERT INTO %I.%I (m_time, device_info_id, %s) 
        SELECT m_time, device_info_id, %s 
        FROM public.device_metrics_narrow m 
        WHERE m_time >= %L AND m_time < %L AND device_info_id = %L 
        GROUP BY m_time, device_info_id 
        ON CONFLICT (m_time, device_info_id) 
        DO UPDATE SET %s', 
        v_schema_name,  -- 1st parameter -> replaces 1st %I (Schema)
        v_table_name,   -- 2nd parameter -> replaces 2nd %I (Table Name)
        v_column_names, -- 3rd parameter -> replaces 1st %s (Target Columns)
        v_columns_cast, -- 4th parameter -> replaces 2nd %s (Select Cast Logic)
        v_start,        -- 5th parameter -> replaces 1st %L (start time)
        v_end,          -- 6th parameter -> replaces 2nd %L (end time)
        v_device,       -- 7th parameter -> replaces 3rd %L (device ID)
        v_update_clause -- 8th parameter -> replaces 3rd %s (ON CONFLICT Updates)
    ); 

    -- STEP 3: Execute the conflict-safe insertion statement 
    EXECUTE v_insert_sql; 
    
    RAISE NOTICE 'Data successfully appended/updated in %s.%s.', v_schema_name, v_table_name; 
END $$;
