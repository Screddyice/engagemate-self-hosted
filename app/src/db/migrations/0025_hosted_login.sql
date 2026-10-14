-- Temporary paid lease reservations for isolated hosted sign-in browsers.
-- Account IDs are provisional until verified completion, so have no FK.
CREATE TABLE hosted_login_reservations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id uuid NOT NULL,
    username text NOT NULL,
    expected_ig_user_id text,
    proxy_id uuid NOT NULL REFERENCES proxies(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '12 minutes',
    released_at timestamptz,
    CHECK (expires_at > created_at)
);
CREATE UNIQUE INDEX hosted_login_active_user ON hosted_login_reservations (user_id)
    WHERE released_at IS NULL;
CREATE UNIQUE INDEX hosted_login_active_proxy ON hosted_login_reservations (proxy_id)
    WHERE released_at IS NULL;
CREATE INDEX hosted_login_expiry ON hosted_login_reservations (expires_at)
    WHERE released_at IS NULL;
