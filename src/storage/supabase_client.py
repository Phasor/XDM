from datetime import datetime, timezone
import logging
import os
from httpx import HTTPError
from postgrest import APIError
import psycopg
from supabase import create_client, Client


class SupaBase:
    """Supabase storage manager with auto schema setup"""

    def __init__(self, config):
        """Initialize clients and ensure schema exists"""
        self.config = config
        self.logger = logging.getLogger("SUPABASE")

        self.conv_table = config["supabase"]["conversations_table"]
        self.msgs_table = config["supabase"]["messages_table"]
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
            raise RuntimeError(f"Failed to upsert conversation: {e}") from e

    def update_last_message_time(self, conversation_id: str):
        """Update last message timestamp"""
        self.logger.debug("Updating last_message_at: %s", conversation_id)
        iso_time = datetime.now(timezone.utc).isoformat()  # <-- convert to string
        try:
            (
                self.client.table(self.conv_table)
                .update({"last_message_at": iso_time})
                .eq("conversation_id", conversation_id)
                .execute()
            )
            self.logger.info("last_message_at updated: %s", conversation_id)
        except (APIError, HTTPError) as e:
            raise RuntimeError(f"Failed to update last_message_at: {e}") from e

    # =========================
    # Messages
    # =========================

    def save_message(self, conv_id, message_id, sender, text):
        """Insert message into database"""
        try:
            res = (
                self.client.table(self.msgs_table)
                .insert(
                    {
                        "conversation_id": conv_id,
                        "message_id": message_id,
                        "sender": sender,
                        "message_text": text,
                    }
                )
                .execute()
            )
            self.logger.info("Message saved (%s): %s", sender, conv_id)
            return res

        except (APIError, HTTPError) as e:
            raise RuntimeError(f"Failed to save message: {e}") from e

    def get_messages(self, conversation_id):
        """Fetch recent messages for a conversation"""
        try:
            res = (
                self.client.table(self.msgs_table)
                .select("*")
                .eq("conversation_id", conversation_id)
                .order("created_at")
                .limit(self.limit)
                .execute()
            )
            self.logger.info("Fetched %s messages: %s", len(res.data), conversation_id)
            return res.data

        except (APIError, HTTPError) as e:
            raise RuntimeError(f"Failed to fetch messages: {e}") from e

    def get_latest_message_id(self, conversation_id: str):
        """Return latest message_id for a conversation"""
        try:
            res = (
                self.client.table(self.msgs_table)
                .select("message_id")
                .eq("conversation_id", conversation_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
        except (APIError, HTTPError) as e:
            raise RuntimeError(f"Failed to fetch latest message ID: {e}") from e

        if res.data:
            msg_id = res.data[0]["message_id"]
        else:
            msg_id = None

        self.logger.debug("Latest user message ID: %s", msg_id)
        return msg_id

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
