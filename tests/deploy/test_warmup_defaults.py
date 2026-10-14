"""New configurations ramp; migration must not raise existing user limits."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg


def test_defaults_migration_preserves_old_rows_and_zero_overrides():
    database = f"warmup_defaults_{uuid4().hex[:12]}"
    subprocess.run(["createdb", database], check=True, capture_output=True)
    url = os.environ.get("TEST_DATABASE_URL_TEMPLATE", "postgresql:///%s") % database
    try:
        with psycopg.connect(url) as connection:
            connection.execute("""
                CREATE TABLE agent_configs (
                    id serial PRIMARY KEY, enabled boolean DEFAULT false,
                    daily_like_limit integer DEFAULT 0,
                    daily_reply_limit integer DEFAULT 0,
                    daily_dm_limit integer DEFAULT 0,
                    auto_unfollow_enabled boolean DEFAULT true,
                    daily_view_limit integer DEFAULT 0
                );
                INSERT INTO agent_configs DEFAULT VALUES;
                INSERT INTO agent_configs (daily_like_limit, daily_view_limit) VALUES (3, 50);
            """)
            migration = Path(__file__).resolve().parents[2] / "app/src/db/migrations/0016_automatic_warmup_defaults.sql"
            connection.execute(migration.read_text())
            connection.execute("INSERT INTO agent_configs DEFAULT VALUES")
            connection.execute("INSERT INTO agent_configs (daily_dm_limit) VALUES (0)")
            rows = connection.execute("""
                SELECT enabled, daily_like_limit, daily_reply_limit, daily_dm_limit, daily_view_limit
                FROM agent_configs ORDER BY id
            """).fetchall()
            assert rows == [
                (False, 0, 0, 0, 0), (False, 3, 0, 0, 50),
                (False, 20, 5, 2, 1000), (False, 20, 5, 0, 1000),
            ]
        with psycopg.connect(url) as connection:
            migration = Path(__file__).resolve().parents[2] / "app/src/db/migrations/0027_opt_in_unfollow_default.sql"
            connection.execute(migration.read_text())
            connection.execute("INSERT INTO agent_configs DEFAULT VALUES")
            assert connection.execute("SELECT daily_reply_limit,daily_dm_limit,auto_unfollow_enabled FROM agent_configs ORDER BY id DESC LIMIT 1").fetchone() == (10, 0, False)
            assert connection.execute("SELECT count(*) FROM agent_configs WHERE daily_dm_limit <> 0").fetchone()[0] == 0
        with psycopg.connect(url) as connection:
            connection.execute("ALTER TABLE agent_configs ADD COLUMN follow_after_engagement boolean DEFAULT false")
            connection.execute("ALTER TABLE agent_configs ADD COLUMN follow_reel_message_enabled boolean DEFAULT false")
            before = connection.execute("SELECT * FROM agent_configs ORDER BY id").fetchall()
            migration = Path(__file__).resolve().parents[2] / "app/src/db/migrations/0034_likes_only_onboarding.sql"
            connection.execute(migration.read_text())
            assert connection.execute("SELECT * FROM agent_configs ORDER BY id").fetchall() == before
            connection.execute("INSERT INTO agent_configs DEFAULT VALUES")
            assert connection.execute("SELECT daily_like_limit,daily_reply_limit,daily_dm_limit,daily_view_limit,follow_after_engagement,follow_reel_message_enabled,auto_unfollow_enabled FROM agent_configs ORDER BY id DESC LIMIT 1").fetchone() == (20,0,0,0,False,False,False)
        probe = Path(__file__).resolve().parents[2] / "deploy/healthcheck.py"
        result = subprocess.run(
            [sys.executable, str(probe), "warmup-defaults"],
            env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        with psycopg.connect(url) as connection:
            connection.execute("ALTER TABLE agent_configs ALTER COLUMN daily_like_limit SET DEFAULT 0")
        result = subprocess.run(
            [sys.executable, str(probe), "warmup-defaults"],
            env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True, check=False,
        )
        assert result.returncode != 0
        assert "0034" in result.stderr
    finally:
        subprocess.run(["dropdb", "--if-exists", database], check=True, capture_output=True)
