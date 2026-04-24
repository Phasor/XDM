from datetime import datetime, timezone
import logging
import os
import time as _time
from functools import wraps
from httpx import HTTPError
from postgrest import APIError
import psycopg
from supabase import create_client, Client


def _retry_on_network_error(max_retries=3, base_delay=2):
    "Retry decorator for transient network errors with exponential backoff"
    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(self, *args, **kwargs)
                except (HTTPError, ConnectionError, OSError) as e:
                    if attempt == max_retries - 1:
                        raise
                    delay = base_delay * (2 ** attempt)
                    self.logger.warning(
                        "Retry %d/%d for %s: %s",
                        attempt + 1, max_retries, func.__name__, e,
                    )
                    _time.sleep(delay)
        return wrapper
    return decorator


class SupaBase:
    """Supabase storage manager with auto schema setup"""

    def __init__(self, config):
        """Initialize clients and ensure schema exists"""
        self.config = config
        self.logger = logging.getLogger("SUPABASE")

        self.conv_table = config["supabase"]["conversations_table"]
        self.msgs_table = config["supabase"]["messages_table"]
        self.drafts_table = config["supabase"].get("drafts_table", "drafts")
        self.limit = config["supabase"]["context_message_limit"]

        url = os.getenv("SUPABASE_URL") or config["supabase"]["project_url"]
        key = os.getenv("SUPABASE__SECRET_KEY") or config["supabase"]["secret_key"]
        self.conn_str = os.getenv("SUPABASE_DB_URL") or config["supabase"]["db_url"]

        self.client: Client = create_client(url, key)
        self.logger.info("Supabase client initialized")

        self._ensure_tables()

    # =========================
    # Setup (psycopg)
    # =========================

    def _ensure_tables(self):
        """Create tables if not exist using psycopg"""
        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.conv_table} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            conversation_id text UNIQUE NOT NULL,
            username text NOT NULL,
            last_message_at timestamptz,
            created_at timestamptz DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS {self.msgs_table} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            conversation_id text NOT NULL
                REFERENCES {self.conv_table}(conversation_id) ON DELETE CASCADE,
            sender text NOT NULL CHECK (sender IN ('user','assistant')),
            message_id text UNIQUE NOT NULL,  -- from X (IMPORTANT)
            message_text text NOT NULL,
            created_at timestamptz DEFAULT now()
        );

        CREATE INDEX IF NOT EXISTS idx_messages_conversation
        ON {self.msgs_table}(conversation_id);

        CREATE TABLE IF NOT EXISTS {self.drafts_table} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            draft_id text UNIQUE NOT NULL,
            character_name text NOT NULL,
            origin text DEFAULT 'compose',
            text text NOT NULL,
            image_prompt text,
            image_path text,
            status text NOT NULL CHECK (status IN
                ('pending','approved','rejected','expired','posted','failed')),
            telegram_chat_id text,
            telegram_message_id text,
            scheduled_for timestamptz,
            expires_at timestamptz,
            posted_at timestamptz,
            tweet_url text,
            failure_reason text,
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now()
        );

        -- Backfill `origin` on existing deployments that pre-date the column.
        ALTER TABLE {self.drafts_table}
            ADD COLUMN IF NOT EXISTS origin text DEFAULT 'compose';

        CREATE INDEX IF NOT EXISTS idx_drafts_status
        ON {self.drafts_table}(status);

        CREATE INDEX IF NOT EXISTS idx_drafts_expires
        ON {self.drafts_table}(expires_at) WHERE status='pending';
        """

        try:
            with psycopg.connect(self.conn_str) as conn:
                with conn.cursor() as cur:
                    cur.execute(create_sql)
                conn.commit()
            self.logger.info("Database tables ensured")
        except psycopg.OperationalError as e:
            self.logger.error("Database connection failed")
            raise ConnectionError("Failed to connect to database") from e
        except psycopg.DatabaseError as e:
            self.logger.error("Table creation failed")
            raise RuntimeError("Failed to create tables") from e

    # =========================
    # Conversations
    # =========================

    @_retry_on_network_error()
    def upsert_conversation(self, conversation_id: str, username: str):
        """Create or update conversation safely"""
        try:
            res = (
                self.client.table(self.conv_table)
                .upsert(
                    {
                        "conversation_id": conversation_id,
                        "username": username,
                    },
                    on_conflict="conversation_id",
                )
                .execute()
            )
            return res.data[0]
        except (APIError, HTTPError) as e:
            self.logger.error("Failed to upsert conversation: %s", e)
            return None

    @_retry_on_network_error()
    def update_last_message_time(self, conversation_id: str):
        """Update last message timestamp"""
        self.logger.debug("Updating last_message_at: %s", conversation_id)
        iso_time = datetime.now(timezone.utc).isoformat()
        try:
            (
                self.client.table(self.conv_table)
                .update({"last_message_at": iso_time})
                .eq("conversation_id", conversation_id)
                .execute()
            )
            self.logger.info("last_message_at updated: %s", conversation_id)
        except (APIError, HTTPError) as e:
            self.logger.error("Failed to update last_message_at: %s", e)

    # =========================
    # Messages
    # =========================

    @_retry_on_network_error()
    def save_message(self, conv_id, message_id, sender, text):
        """Insert message into database (upsert to handle re-processing after crash)"""
        try:
            res = (
                self.client.table(self.msgs_table)
                .upsert(
                    {
                        "conversation_id": conv_id,
                        "message_id": message_id,
                        "sender": sender,
                        "message_text": text,
                    },
                    on_conflict="message_id",
                )
                .execute()
            )
            self.logger.info("Message saved (%s): %s", sender, conv_id)
            return res

        except (APIError, HTTPError) as e:
            self.logger.error("Failed to save message: %s", e)
            return None

    @_retry_on_network_error()
    def get_messages(self, conversation_id, limit=None):
        """Fetch messages for a conversation. Uses config limit by default."""
        try:
            res = (
                self.client.table(self.msgs_table)
                .select("*")
                .eq("conversation_id", conversation_id)
                .order("created_at", desc=True)
                .limit(limit or self.limit)
                .execute()
            )
            res.data.reverse()  # restore chronological order
            self.logger.info("Fetched %s messages: %s", len(res.data), conversation_id)
            return res.data

        except (APIError, HTTPError) as e:
            self.logger.error("Failed to fetch messages: %s", e)
            return []

    # =========================
    # Drafts (outbound tweets)
    # =========================

    @_retry_on_network_error()
    def insert_draft(self, draft):
        "Insert a new draft row. `draft` is a dict matching the drafts schema."
        try:
            res = (
                self.client.table(self.drafts_table)
                .insert(draft)
                .execute()
            )
            self.logger.info("Draft inserted: %s", draft.get("draft_id"))
            return res.data[0] if res.data else None
        except (APIError, HTTPError) as e:
            self.logger.error("Failed to insert draft: %s", e)
            return None

    @_retry_on_network_error()
    def update_draft(self, draft_id, fields):
        "Update arbitrary fields on a draft. Always bumps updated_at."
        fields = dict(fields)
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            res = (
                self.client.table(self.drafts_table)
                .update(fields)
                .eq("draft_id", draft_id)
                .execute()
            )
            return res.data[0] if res.data else None
        except (APIError, HTTPError) as e:
            self.logger.error("Failed to update draft %s: %s", draft_id, e)
            return None

    @_retry_on_network_error()
    def get_draft(self, draft_id):
        "Fetch a single draft by draft_id."
        try:
            res = (
                self.client.table(self.drafts_table)
                .select("*")
                .eq("draft_id", draft_id)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except (APIError, HTTPError) as e:
            self.logger.error("Failed to fetch draft %s: %s", draft_id, e)
            return None

    @_retry_on_network_error()
    def get_draft_by_telegram(self, chat_id, message_id):
        "Find the draft attached to a given Telegram message (for reply-edits)."
        try:
            res = (
                self.client.table(self.drafts_table)
                .select("*")
                .eq("telegram_chat_id", str(chat_id))
                .eq("telegram_message_id", str(message_id))
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except (APIError, HTTPError) as e:
            self.logger.error("Failed to fetch draft by TG msg: %s", e)
            return None

    @_retry_on_network_error()
    def get_pending_expired(self):
        "Return pending drafts whose expires_at has passed."
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            res = (
                self.client.table(self.drafts_table)
                .select("*")
                .eq("status", "pending")
                .lt("expires_at", now_iso)
                .execute()
            )
            return res.data or []
        except (APIError, HTTPError) as e:
            self.logger.error("Failed to fetch expired drafts: %s", e)
            return []

    @_retry_on_network_error()
    def get_approved_due(self):
        "Return approved drafts whose scheduled_for is now or earlier."
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            res = (
                self.client.table(self.drafts_table)
                .select("*")
                .eq("status", "approved")
                .lte("scheduled_for", now_iso)
                .order("scheduled_for", desc=False)
                .execute()
            )
            return res.data or []
        except (APIError, HTTPError) as e:
            self.logger.error("Failed to fetch due drafts: %s", e)
            return []

    @_retry_on_network_error()
    def count_drafts_since(self, character_name, since_iso):
        "Count drafts created at or after `since_iso` for a given character."
        try:
            res = (
                self.client.table(self.drafts_table)
                .select("draft_id", count="exact")
                .eq("character_name", character_name)
                .gte("created_at", since_iso)
                .execute()
            )
            return res.count or 0
        except (APIError, HTTPError) as e:
            self.logger.error("Failed to count drafts: %s", e)
            return 0

    @_retry_on_network_error()
    def get_recent_posted(self, character_name, limit=20):
        "Return the last N posted drafts for a character (for LLM context)."
        try:
            res = (
                self.client.table(self.drafts_table)
                .select("*")
                .eq("character_name", character_name)
                .eq("status", "posted")
                .order("posted_at", desc=True)
                .limit(limit)
                .execute()
            )
            data = res.data or []
            data.reverse()  # chronological
            return data
        except (APIError, HTTPError) as e:
            self.logger.error("Failed to fetch recent posts: %s", e)
            return []

    # =========================
    # Utility
    # =========================

    def health_check(self):
        """Check DB connectivity"""
        try:
            self.client.table(self.conv_table).select("id").limit(1).execute()
            self.logger.info("Health check passed")
            return True
        except psycopg.Error:
            self.logger.error("Health check failed")
            return False
