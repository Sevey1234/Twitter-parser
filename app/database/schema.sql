PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS bot_users (
    telegram_user_id INTEGER PRIMARY KEY,
    digest_interval_hours INTEGER NOT NULL CHECK(digest_interval_hours IN (3,6,12,24)),
    monthly_budget_micros INTEGER NOT NULL CHECK(monthly_budget_micros>=0),
    language TEXT NOT NULL DEFAULT 'ru',
    next_due_at TEXT NOT NULL,
    last_digest_at TEXT,
    state TEXT,
    state_data TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS single_owner ON bot_users((1));

CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    x_user_id TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    display_name TEXT NOT NULL,
    tracking_started_at TEXT NOT NULL,
    last_tweet_id TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL,
    checked_at TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS sync_state (
    author_id INTEGER PRIMARY KEY REFERENCES authors(id) ON DELETE CASCADE,
    end_time TEXT NOT NULL,
    pagination_token TEXT,
    page_size INTEGER CHECK(page_size BETWEEN 5 AND 100),
    high_id TEXT,
    seen_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tweets (
    id INTEGER PRIMARY KEY,
    x_tweet_id TEXT NOT NULL UNIQUE,
    author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    processing_status TEXT NOT NULL CHECK(processing_status IN ('staged','pending','queued','sent'))
);
CREATE INDEX IF NOT EXISTS tweets_author_status ON tweets(author_id,processing_status,created_at);
CREATE INDEX IF NOT EXISTS tweets_status ON tweets(processing_status,created_at);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES bot_users(telegram_user_id),
    digest_text TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued','sent'))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_digest ON digests(owner_id) WHERE status='queued';
CREATE INDEX IF NOT EXISTS digests_retention ON digests(status,sent_at);
CREATE TABLE IF NOT EXISTS digest_tweets (
    digest_id INTEGER NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
    tweet_id INTEGER NOT NULL UNIQUE REFERENCES tweets(id) ON DELETE CASCADE,
    PRIMARY KEY(digest_id,tweet_id)
);
CREATE TABLE IF NOT EXISTS digest_parts (
    digest_id INTEGER NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
    part INTEGER NOT NULL,
    html TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','uncertain','sent')),
    message_id INTEGER,
    retry_at TEXT,
    PRIMARY KEY(digest_id,part)
);

CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY,
    endpoint TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    resources_received INTEGER NOT NULL DEFAULT 0,
    estimated_micros INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved'
);
CREATE INDEX IF NOT EXISTS usage_month ON api_usage(requested_at);
CREATE TABLE IF NOT EXISTS rate_limits (
    endpoint TEXT PRIMARY KEY,
    retry_at TEXT NOT NULL
);

-- Store only an ID after an explicit content-removal request, including during pagination.
CREATE TABLE IF NOT EXISTS removed_posts (
    x_tweet_id TEXT PRIMARY KEY
);
