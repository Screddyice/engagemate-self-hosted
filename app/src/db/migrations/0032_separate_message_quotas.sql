ALTER TABLE agent_configs
    ADD COLUMN daily_reel_message_limit integer,
    ADD COLUMN warmup_exempt boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT agent_configs_daily_reel_message_limit_check
        CHECK (daily_reel_message_limit BETWEEN 0 AND 20);
