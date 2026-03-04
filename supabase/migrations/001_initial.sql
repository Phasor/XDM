-- X users who have DM'd the agent
CREATE TABLE x_users (
  id TEXT PRIMARY KEY,                    -- X user ID (numeric string)
  username TEXT,
  display_name TEXT,
  free_messages_used INTEGER DEFAULT 0,
  chat_access_until TIMESTAMPTZ,          -- NULL = no paid access
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ
);

-- Conversation history (LLM context)
CREATE TABLE messages (
  id BIGSERIAL PRIMARY KEY,
  x_user_id TEXT NOT NULL REFERENCES x_users(id),
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL,
  tool_calls JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX messages_user_created ON messages(x_user_id, created_at DESC);

-- Content library
CREATE TABLE content_items (
  id TEXT PRIMARY KEY,                    -- slug, e.g. 'pack-01-video'
  type TEXT NOT NULL CHECK (type IN ('image', 'video', 'voice')),
  title TEXT NOT NULL,
  description TEXT,
  bunny_path TEXT NOT NULL,               -- Storage path or Stream video ID
  price_usd NUMERIC(10,2) NOT NULL,
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Payment records
CREATE TABLE payments (
  id TEXT PRIMARY KEY,
  x_user_id TEXT NOT NULL REFERENCES x_users(id),
  item_type TEXT NOT NULL CHECK (item_type IN ('chat', 'content')),
  item_id TEXT,
  amount_usd NUMERIC(10,2),
  usdc_amount NUMERIC(20,6),
  wallet_address TEXT,
  tx_hash TEXT,
  status TEXT DEFAULT 'pending' CHECK (status IN ('pending','confirmed','expired')),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  confirmed_at TIMESTAMPTZ
);
CREATE INDEX payments_user ON payments(x_user_id, status);

-- Content access grants (after payment confirmed)
CREATE TABLE content_access (
  x_user_id TEXT NOT NULL REFERENCES x_users(id),
  item_id TEXT NOT NULL REFERENCES content_items(id),
  granted_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (x_user_id, item_id)
);

-- Agent configuration (one row for MVP single-account)
CREATE TABLE agent_config (
  id TEXT PRIMARY KEY DEFAULT 'default',
  x_account_handle TEXT,
  model TEXT DEFAULT 'anthropic/claude-sonnet-4-6',
  system_prompt TEXT,
  context_about TEXT,
  context_faq TEXT,
  context_offers TEXT,
  context_style TEXT,
  free_message_limit INTEGER DEFAULT 3,
  chat_session_price_usd NUMERIC(10,2),
  chat_session_hours INTEGER DEFAULT 24,
  max_context_messages INTEGER DEFAULT 20,
  active BOOLEAN DEFAULT TRUE,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- DM poll cursor
CREATE TABLE poll_state (
  agent_id TEXT PRIMARY KEY DEFAULT 'default',
  last_polled_at TIMESTAMPTZ,
  last_dm_id TEXT
);

-- Seed default config row
INSERT INTO agent_config (id) VALUES ('default') ON CONFLICT DO NOTHING;
INSERT INTO poll_state (agent_id) VALUES ('default') ON CONFLICT DO NOTHING;
