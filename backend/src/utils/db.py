import os
import time
import json
import logging
import traceback
from contextlib import contextmanager
from typing import List, Dict, Optional
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import bcrypt
import urllib.parse
import csv
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# Recommended: ensure DATABASE_URL includes sslmode=require for Neon,
# e.g. postgres://user:pass@host:5432/dbname?sslmode=require
DATABASE_URL = os.getenv("DATABASE_URL")
# Configuration: tune these to your environment
POOL_MINCONN = int(os.getenv("PG_POOL_MIN", "1"))
POOL_MAXCONN = int(os.getenv("PG_POOL_MAX", "10"))
USE_POOLING = os.getenv("PG_USE_POOL", "true").lower() in ("1", "true", "yes")
HEALTHCHECK_SQL = "SELECT 1"

class PGDB:
    """
    Neon-optimized Postgres helper.
    Features:
    - Uses ThreadedConnectionPool (optionally disabled)
    - Health-checks connections before handing them out
    - Auto-recreates dead connections
    - Provides a contextmanager .conn() to safely get a connection + cursor
    - Small retry logic for transient SSL/connection-close errors
    """
    _instance = None
    _pool: Optional[pool.ThreadedConnectionPool] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL environment variable is not set")
        self.dsn = DATABASE_URL
        self.use_pooling = USE_POOLING
        if self.use_pooling:
            # Use ThreadedConnectionPool instead of SimpleConnectionPool
            # Threaded pool plays nicer in multi-threaded servers.
            try:
                PGDB._pool = pool.ThreadedConnectionPool(
                    POOL_MINCONN,
                    POOL_MAXCONN,
                    dsn=self.dsn
                )
                logging.info(f"Postgres pool created: {POOL_MINCONN}-{POOL_MAXCONN}")
            except Exception:
                logging.exception("Failed to create Postgres pool, falling back to no-pool.")
                PGDB._pool = None
                self.use_pooling = False

        self.create_users_table()
        self.create_call_history_table()
        self.create_appointments_table()
        self.create_user_prompts_table()
        self.create_contacts_table()

        self._initialized = True

    # ------------- Connection helpers -----------------
    def _connect_fresh(self):
        """Create a new psycopg2 connection (fresh, not pooled)."""
        # psycopg2 will accept connection params in the dsn (e.g. keepalives) if present.
        return psycopg2.connect(self.dsn)

    def _health_check_conn(self, conn) -> bool:
        """Return True if connection appears healthy (runs a simple query)."""
        try:
            # set a short timeout for a quick health check
            with conn.cursor() as cur:
                cur.execute(HEALTHCHECK_SQL)
                cur.fetchone()
            return True
        except Exception:
            return False

    def _get_from_pool(self):
        """Get connection from pool, running a healthcheck and recreating if necessary."""
        conn = None
        try:
            conn = PGDB._pool.getconn()
            if conn is None:
                raise RuntimeError("Pool returned None connection")
            # Quick healthcheck; if it fails, close and create a fresh connection
            if not self._health_check_conn(conn):
                try:
                    conn.close()
                except Exception:
                    pass
                # create new fresh connection (not added to pool) and return it
                return self._connect_fresh(), False # False -> not from pool (caller must not putconn)
            return conn, True # True -> from pool
        except Exception as e:
            logging.warning(f"Pool getconn failed: {e}; creating fresh connection.")
            # if pool broken, fallback to fresh connection
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            return self._connect_fresh(), False

    # Public: get a raw connection (plus a flag indicating whether it came from pool)
    def _acquire_connection(self):
        """
        Returns: (conn, from_pool: bool)
        Caller MUST either putconn(conn) back to pool if from_pool=True OR close(conn) if from_pool=False.
        """
        if self.use_pooling and PGDB._pool:
            return self._get_from_pool()
        else:
            return self._connect_fresh(), False

    # ------------ Context manager --------------
    @contextmanager
    def conn(self, dict_cursor: bool = True, retries: int = 2, retry_delay: float = 0.2):
        """
        Usage:
            with db.conn() as (conn, cur):
                cur.execute(...)
                conn.commit()
        This will always release the connection back to pool or close it.
        Retries brief transient errors (like 'SSL connection has been closed unexpectedly').
        """
        attempt = 0
        last_exc = None
        while attempt <= retries:
            conn = None
            from_pool = False
            cur = None
            try:
                conn, from_pool = self._acquire_connection()
                cur = conn.cursor(cursor_factory=RealDictCursor if dict_cursor else None)
                yield conn, cur
                # caller will usually commit; if not, leaving it to them is fine
                return
            except (psycopg2.OperationalError, psycopg2.DatabaseError) as e:
                last_exc = e
                msg = str(e).lower()
                logging.warning(f"DB connection error (attempt {attempt}/{retries}): {e}")
                # Common transient phrases from Neon: "SSL connection has been closed unexpectedly"
                transient = ("ssl connection has been closed unexpectedly" in msg) or ("connection reset by peer" in msg) or ("server closed the connection unexpectedly" in msg)
                try:
                    if conn:
                        try:
                            conn.close()
                        except Exception:
                            pass
                finally:
                    # If connection was from pool, we must not put a broken conn back
                    if from_pool and PGDB._pool:
                        try:
                            PGDB._pool.putconn(conn, close=True)
                        except Exception:
                            pass
                if transient and attempt < retries:
                    attempt += 1
                    time.sleep(retry_delay * (attempt)) # small backoff
                    continue
                else:
                    logging.exception("Non-transient DB error or retries exhausted")
                    raise
            except Exception:
                logging.exception("Unexpected error inside DB context")
                raise
            finally:
                # On normal exit from `with` block, the yield returned and we executed `return`,
                # so finalization happens here only if an exception bubbled up or after the yield's block.
                if cur:
                    try:
                        cur.close()
                    except Exception:
                        pass
                if conn:
                    if from_pool and PGDB._pool:
                        try:
                            PGDB._pool.putconn(conn)
                        except Exception:
                            # ensure closure if putconn failed
                            try:
                                conn.close()
                            except Exception:
                                pass
                    else:
                        # direct connection: close it
                        try:
                            conn.close()
                        except Exception:
                            pass
        # If we get here, retries exhausted
        raise last_exc or RuntimeError("Failed to get DB connection")

    # ----------------- Convenience helpers -----------------
    def execute(self, sql: str, params: tuple = None, fetchone: bool = False, fetchall: bool = False, commit: bool = False):
        """
        Small helper to run an SQL statement quickly.
        """
        with self.conn() as (conn, cur):
            cur.execute(sql, params or ())
            if commit:
                conn.commit()
            if fetchone:
                return cur.fetchone()
            if fetchall:
                return cur.fetchall()
            return None

    def close_pool(self):
        """Close and discard the pool (useful on shutdown)."""
        if PGDB._pool:
            try:
                PGDB._pool.closeall()
                PGDB._pool = None
                logging.info("Postgres pool closed")
            except Exception:
                logging.exception("Error closing pool")

    def create_user_prompts_table(self):
        """
        Create table for multiple named prompts per user.
        Each user can have many prompts with unique names.
        """
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS user_prompts (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        prompt_name VARCHAR(255) NOT NULL,
                        system_prompt TEXT NOT NULL,
                        is_default BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, prompt_name)
                    );
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_user_prompts_user_id
                    ON user_prompts(user_id);
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_user_prompts_default
                    ON user_prompts(user_id, is_default) WHERE is_default = TRUE;
                """)
                conn.commit()
                logging.info("✅ user_prompts table created/updated for multiple prompts")
            except Exception as e:
                logging.error(f"Error creating user_prompts table: {e}")

    def create_default_user_prompt(self, user_id: int):
        """
        Create default prompt for a new user.
        Called automatically on user registration.
        """
        default_prompt = """You are **PrimerAIBot**, an AI Appointment Specialist for a local Primerica representative.
        ====================================================================
        ## 1. IDENTITY & BEHAVIOR
        - You speak in a calm, low-and-slow tone.
        - You are assertive, confident, and professional.
        - You do NOT sell products.
        - You do NOT give financial advice.
        - You do NOT debate prospects.
        - Your only mission is to book a **Qualified Appointment** for a complimentary Financial Needs Analysis (FNA).
        A Qualified Appointment means:
        1. Both spouses / financial decision-makers must be present.
        2. A specific date AND specific time must be confirmed.
        Anything less = failed appointment.
        ====================================================================
        ## 2. WHAT YOU ARE OFFERING (FNA)
        You offer a **complimentary, confidential, customized, 30-minute Financial Needs Analysis**.
        The FNA provides a financial “snapshot” addressing:
        - Debt strategy
        - Retirement planning needs
        - Income protection analysis
        - Education funding projections
        You NEVER explain products, prices, or give recommendations.
        All product/advice questions must be deflected into the appointment with:
        “That’s an excellent question, and it’s exactly the kind of thing we’ll cover in your personalized FNA.
        To make sure I answer that properly for you, what’s better for you… weekends or weekdays?”
        ====================================================================
        ## 3. MANDATORY CALL STRUCTURE
        ### STEP 1 — INTRODUCTION (ALWAYS)
        “Hi, may I speak with [PROSPECT NAME]?”
        (wait)
        “Hi [Name], this is **[Agent Name]** with Primerica Financial Services.
        I know I’m calling completely out of the blue… do you have 30 seconds for me to tell you why I’m calling?”
        ### STEP 2 — PURPOSE STATEMENT
        “I’m a local financial professional, and my firm offers a complimentary 30-minute Financial Needs Analysis.
        It helps families create a clear strategy to eliminate debt faster, save properly for retirement, and make sure their income is protected.”
        “To respect your time, I just have two quick questions to see if this is even relevant for you.”
        ### STEP 3 — TWO CRITICAL QUALIFIERS
        Q1 — Spouse/Decision-Maker Frame:
        “When it comes to big-picture financial decisions—retirement, insurance, planning—do you handle that alone, or is it a team effort with your spouse/partner?”
        (Store as `spouse_role`.)
        Q2 — Advisor Frame:
        “Do you already have a financial advisor you’re 100% happy with, or are you mostly handling things on your own?”
        (Store as `advisor_status`.)
        ====================================================================
        ## 4. PATH-BASED CLOSE (USE THEIR ANSWERS)
        If `advisor_status` = “I have an advisor”:
        → Skip the appointment ask.
        → Proceed immediately to the **Second Opinion rebuttal**.
        If `spouse_role` = “We do it together”:
        “Perfect. This analysis only works with both decision-makers present.
        So for that 30-minute session, when are you both normally together—weekends or weeknights?”
        If `spouse_role` = “I handle it myself”:
        “Great. For that complimentary 30-minute snapshot, what works better… weekends or weekdays?”
        ====================================================================
        ## 5. OBJECTION HANDLING MATRIX (MANDATORY SCRIPTS)
        ### “I already have an advisor.” → Second Opinion Pivot
        Affirm → Answer → Close
        Use the arm/doctor analogy, wait for “second opinion,” then close:
        “So for that free second opinion, what’s better… weekends or weekdays?”
        ### “I’m too busy.” → Time Objection Scripts
        Use “15-minute guarantee” or Feel–Felt–Found, then close:
        “What’s better… weekends or weekdays?”
        ### “Not interested.” → Referral Pivot
        Affirm → Answer → Close:
        “For 15 minutes, if not for you, then for the people you care about.
        What works better… weekends or weekdays?”
        ### “Need to talk to spouse.” → Hypothetical Isolation
        If they agree hypothetically, pencil them in:
        “Let’s pencil in Saturday at 10 AM.
        If that doesn’t work, call me back with 2–3 other times.”
        ### “Send me information.”
        Explain that the FNA is custom-only.
        Then close with weekends/weekdays.
        ### “What is it exactly?” / “What do you sell?”
        Affirm → Answer → Close:
        “We help people save on debt, taxes, and insurance, and redirect savings efficiently.
        So to show you that, what works better… weekends or weekdays?”
        ### “Is this a pyramid scheme?”
        Affirm → Answer (Primerica facts: NYSE PRI, A+ BBB, commissions from product sales),
        then close.
        ### “Is this Primerica?”
        Affirm → Answer confidently, then immediately close.
        Every objection ends with:
        “What works better… weekends or weekdays?”
        ====================================================================
        ## 6. APPOINTMENT CONFIRMATION PROTOCOL (MANDATORY)
        After a date/time is chosen:
        ### Step 1 — Spouse Confirmation
        Confirm both decision-makers will be present.
        If not → appointment invalid → return to earlier steps.
        ### Step 2 — Homework
        Ask them to have:
        - Recent pay stubs
        - Insurance policies
        - Retirement/savings statements
        on the table for accurate numbers.
        ### Step 3 — Calendar Lock
        “Please put this on your calendar right now while we’re on the phone.”
        ### Step 4 — Reschedule Mandate
        “If an emergency comes up, please **call** me directly.
        Don’t text or email; I may not see those in time.
        Is that okay?”
        ### Step 5 — Sign-Off
        Strong, confident farewell.
        ====================================================================
        ## 7. GLOBAL RULES
        - Never sell products.
        - Never give advice.
        - Never debate.
        - Always use **Affirm → Answer → Close**.
        - All product/advice questions must be deflected into booking the appointment.
        - If spouse is unavailable, appointment is not qualified.
        - Appointments must have a **specific time and date**.
        ====================================================================
        You must follow this structure, tone, and logic with zero deviation."""
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                # ✅ FIX: Add prompt_name and set is_default=TRUE
                cursor.execute("""
                    INSERT INTO user_prompts (user_id, prompt_name, system_prompt, is_default)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (user_id, prompt_name) DO NOTHING;
                """, (user_id, "Default Prompt", default_prompt, True))
                conn.commit()
                logging.info(f"✅ Created default prompt for user {user_id}")
            except Exception as e:
                conn.rollback()
                logging.error(f"Error creating default prompt: {e}")
                raise

    def get_user_prompt(self, user_id: int) -> dict:
        """
        Get the user's current system prompt.
        Returns None if not found (creates default if missing).
        """
        with self.conn() as (conn, cursor):
            try:
                cursor.execute("""
                    SELECT
                        id,
                        user_id,
                        system_prompt,
                        created_at,
                        updated_at
                    FROM user_prompts
                    WHERE user_id = %s;
                """, (user_id,))
                result = cursor.fetchone()
               
                # If no prompt exists, create default
                if not result:
                    self.create_default_user_prompt(user_id)
                    cursor.execute("""
                        SELECT
                            id,
                            user_id,
                            system_prompt,
                            created_at,
                            updated_at
                        FROM user_prompts
                        WHERE user_id = %s;
                    """, (user_id,))
                    result = cursor.fetchone()
               
                return result
            except Exception as e:
                logging.error(f"Error getting user prompt: {e}")
                raise

    def update_user_system_prompt(self, user_id: int, system_prompt: str):
        """
        Update user's system prompt.
        Stores exactly what is provided - no parsing.
        """
        with self.conn() as (conn, cursor):
            try:
                cursor.execute("""
                    UPDATE user_prompts
                    SET system_prompt = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = %s
                    RETURNING
                        id,
                        user_id,
                        system_prompt,
                        created_at,
                        updated_at;
                """, (system_prompt, user_id))
                result = cursor.fetchone()
           
                conn.commit()
                logging.info(f"✅ Updated prompt for user {user_id}")
                return result
           
            except Exception as e:
                conn.rollback()
                logging.error(f"Error updating user prompt: {e}")
                raise

    def reset_user_prompt_to_default(self, user_id: int):
        """
        Reset user's prompt to default text.
        """
        default_prompt = """You are Paul, a professional AI assistant for business services.
    Your responsibilities:
    - Help schedule appointments and meetings
    - Answer questions about services
    - Be respectful, patient, and adapt to the business's communication style
    - Always confirm important details before proceeding
    Tone: Professional and friendly"""
       
        return self.update_user_system_prompt(user_id, default_prompt)

    def get_user_customization_dict(self, user_id: int) -> dict:
        """
        Get user's system prompt for use in call initiation.
       
        Returns:
            dict with key: system_prompt (full text)
        """
        prompt_data = self.get_user_prompt(user_id)
       
        if not prompt_data:
            # Return default if not found
            return {
                "system_prompt": """You are Paul, a professional AI assistant for business services.
    Your responsibilities:
    - Help schedule appointments and meetings
    - Answer questions about services
    - Be respectful, patient, and adapt to the business's communication style
    - Always confirm important details before proceeding
    Tone: Professional and friendly"""
            }
       
        return {
            "system_prompt": prompt_data['system_prompt']
        }

    

    
    
    # ==================== MODIFIED: REGISTER USER (AUTO-CREATE PROMPT) ====================
    def register_user(self, user_data):
        with self.conn() as (conn, cursor):
            try:
                # Check if email already exists
                cursor.execute("SELECT id FROM users WHERE email = %s", (user_data['email'],))
                if cursor.fetchone():
                    raise ValueError("Email already registered.")
                # Hash the password
                hashed_password = bcrypt.hashpw(user_data['password'].encode('utf-8'), bcrypt.gensalt())
                # Insert user
                cursor.execute("""
                    INSERT INTO users (username, email, password_hash, is_admin)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, username, email, created_at, is_admin;
                """, (
                    user_data['username'],
                    user_data['email'],
                    hashed_password.decode('utf-8'),
                    user_data.get('is_admin', False)
                ))
                row = cursor.fetchone()
                user_id = row["id"]
                conn.commit()
                # ✅ AUTO-CREATE DEFAULT PROMPT FOR NEW USER
                self.create_default_user_prompt(user_id)
                return {
                    "id": row["id"],
                    "username": row["username"],
                    "email": row["email"],
                    "created_at": row["created_at"]
                }
            except Exception as e:
                conn.rollback()
                logging.error(f"Error in register_user: {e}")
                raise

    def create_users_table(self):
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        username VARCHAR(100),
                        email VARCHAR(100) UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        first_name VARCHAR(100),
                        last_name VARCHAR(100),
                        is_admin BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                conn.commit()
            except Exception as e:
                logging.error(f"Error creating users table: {e}")

    def create_call_history_table(self):
        """
        Create call_history table to store call details
        """
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS call_history (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        call_id TEXT NOT NULL UNIQUE,
                        status TEXT,
                        duration DOUBLE PRECISION,
                        transcript JSONB,
                        summary TEXT,
                        recording_url TEXT,
                        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                        started_at TIMESTAMPTZ NULL,
                        ended_at TIMESTAMPTZ NULL,
                        voice_id TEXT,
                        voice_name TEXT,
                        from_number TEXT NULL,
                        to_number TEXT NULL,
                        transcript_url TEXT, -- ADDED
                        transcript_blob TEXT, -- ADDED
                        recording_blob TEXT, -- ADDED
                        events_log JSONB DEFAULT '[]', -- ADDED: For webhooks
                        agent_events JSONB DEFAULT '[]' -- ADDED: For agent reports
                    );
                """)
                # Add indexes if missing (idempotent)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_call_history_events_log ON call_history USING GIN (events_log);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_call_history_agent_events ON call_history USING GIN (agent_events);")
                conn.commit()
            except Exception as e:
                logging.error(f"Error creating call_history table: {e}")

    # ============================= USERS LOGIC START =============================
    def register_user(self, user_data):
        with self.conn() as (conn, cursor):
            try:
                # Check if email already exists
                cursor.execute("SELECT id FROM users WHERE email = %s", (user_data['email'],))
                if cursor.fetchone():
                    raise ValueError("Email already registered.")
                # Hash the password
                hashed_password = bcrypt.hashpw(user_data['password'].encode('utf-8'), bcrypt.gensalt())
                # Insert user
                cursor.execute("""
                    INSERT INTO users (username, email, password_hash, is_admin)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, username, email, created_at, is_admin;
                """, (
                    user_data['username'],
                    user_data['email'],
                    hashed_password.decode('utf-8'),
                    user_data.get('is_admin', False)
                ))
                row = cursor.fetchone()
                user_id = row["id"]
                conn.commit()
                # ✅ AUTO-CREATE DEFAULT PROMPT FOR NEW USER
                self.create_default_user_prompt(user_id)
                logging.info(f"✅ Created default prompt for new user {user_id}")
                return {
                    "id": row["id"],
                    "username": row["username"],
                    "email": row["email"],
                    "created_at": row["created_at"]
                }
            except Exception as e:
                conn.rollback()
                logging.error(f"Error in register_user: {e}")
                raise

    def login_user(self, user_data):
        """Verify user credentials by username or email and return user info."""
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute("""
                    SELECT id, username, email, password_hash,first_name,last_name,created_at,is_admin
                    FROM users
                    WHERE username = %s OR email = %s
                    LIMIT 1
                """, (user_data.get("username"), user_data['email']))
                result = cursor.fetchone()
                if result and bcrypt.checkpw(user_data['password'].encode('utf-8'), result[3].encode('utf-8')):
                    return {
                        "id": result[0],
                        "username": result[1],
                        "email": result[2],
                        # "first_name": result[4],
                        # "last_name": result[5],
                        "created_at": result[6],
                        "is_admin": result[7]
                    }
                else:
                    raise ValueError("Invalid username or password.")
            except Exception as e:
                logging.error(f"Error during login: {str(e)}")
                raise

    def get_user_by_id(self, user_id: int):
        """Get user by ID"""
        with self.conn() as (conn, cursor):
            try:
                cursor.execute(
                    "SELECT id,first_name,last_name,username,email,is_admin,created_at FROM users WHERE id = %s",
                    (user_id,)
                )
                return cursor.fetchone()
            except Exception as e:
                logging.error(f"Error getting user by id: {e}")
                raise

    def delete_user_by_id(self, user_id):
        """
        delete user by id
        """
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute(
                    """
                    DELETE FROM users WHERE id = %s
                    """,
                    (user_id,)
                )
                conn.commit()
                return True
            except Exception as e:
                conn.rollback()
                logging.error(f"Error deleting user {user_id}: {e}")
                return False

    def update_user_name_fields(self, user_id: int, first_name: str, last_name: str):
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute("""
                    UPDATE users
                    SET first_name = %s,
                        last_name = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (first_name, last_name, user_id))
                conn.commit()
                return True
            except Exception as e:
                logging.error(f"Error updating name fields: {e}")
                return False

    def change_user_password(self, user_id: int, current_password: str, new_password: str):
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute("""
                    SELECT password_hash FROM users WHERE id = %s
                """, (user_id,))
                result = cursor.fetchone()
                if not result:
                    raise ValueError("User not found.")
                # Verify current password
                if not bcrypt.checkpw(current_password.encode(), result[0].encode()):
                    raise ValueError("Current password is incorrect.")
                # Hash new password
                new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
                # Update
                cursor.execute("""
                    UPDATE users SET password_hash = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (new_hash, user_id))
                conn.commit()
                return True
            except Exception as e:
                logging.error(f"Password change error: {e}")
                raise

    def get_all_users(self):
        query = """
            SELECT id, first_name, last_name, username, email, is_admin, created_at
            FROM users
            WHERE is_admin = FALSE
            ORDER BY created_at DESC
        """
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute(query)
                result = cursor.fetchall()
                return [
                    {
                        "id": row[0],
                        # "first_name": row[1],
                        # "last_name": row[2],
                        "username": row[3],
                        "email": row[4],
                        "is_admin": row[5],
                        "created_at": row[6],
                    }
                    for row in result
                ]
            except Exception as e:
                logging.error(f"Error getting all users: {e}")
                raise

    def get_all_users_paginated(self, page: int = 1, page_size: int = 10):
        query_total = "SELECT COUNT(*) FROM users WHERE is_admin = FALSE"
        query_data = """
            SELECT id, first_name, last_name, username, email, is_admin, created_at
            FROM users
            -- WHERE is_admin = FALSE
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                # Total user count
                cursor.execute(query_total)
                total_users = cursor.fetchone()[0]
                # Paginated data
                offset = (page - 1) * page_size
                cursor.execute(query_data, (page_size, offset))
                rows = cursor.fetchall()
                users = [
                    {
                        "id": row[0],
                        # "first_name": row[1],
                        # "last_name": row[2],
                        "username": row[3],
                        "email": row[4],
                        "is_admin": row[5],
                        "created_at": row[6],
                    }
                    for row in rows
                ]
                return {
                    "users": users,
                    "total": total_users
                }
            except Exception as e:
                print(f"Error fetching paginated users: {e}")
                return {"users": [], "total": 0}

    def insert_call_history(
        self,
        user_id: int,
        call_id: str,
        status: str = None,
        voice_id: str = None,
        voice_name: str = None,
        to_number: str = None
    ):
        """
        Insert a new call history record with initial data.
        Other fields (transcript, summary, duration, etc.) will be updated later.
        """
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                values = (
                    user_id, call_id, status,
                    voice_id, voice_name, to_number
                )
                cursor.execute("""
                    INSERT INTO call_history (
                        user_id, call_id, status,
                        voice_id, voice_name, to_number
                    )
                    VALUES (%s,%s,%s,%s,%s,%s)
                    RETURNING id;
                """, values)
                row = cursor.fetchone()
                conn.commit()
                return row[0] if row else None
            except Exception as e:
                conn.rollback()
                logging.error(f"Error inserting call history: {e}")
                raise

    def update_call_history(self, call_id: str, updates: dict):
        """
        Update specific fields in the call_history record based on the call_id.
        Args:
            call_id (str): The unique identifier for the call.
            updates (dict): A dictionary where keys are column names and values
                            are the new values to set. e.g., {"status": "completed", "duration": 120.5}
        """
        if not updates:
            logging.warning("update_call_history called with no updates.")
            return None # Or raise an error
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                # Build the SET part of the SQL query dynamically
                set_clauses = []
                param_values = []
                for key, value in updates.items():
                   
                    # ==========================================================
                    # ⭐️ BUG FIX IS HERE ⭐️
                    # The old logic ('if not key.isalnum() and key != '_'') was
                    # rejecting valid keys like 'transcript_url'.
                    # This new logic allows keys with underscores.
                    # ==========================================================
                    if not key.replace('_', '').isalnum():
                        logging.error(f"Invalid column name detected: {key}")
                        raise ValueError(f"Invalid column name: {key}")
                    # Handle JSON data specifically
                    if key == 'transcript' and value is not None:
                        set_clauses.append(f"{key} = %s")
                        param_values.append(json.dumps(value))
                    else:
                        set_clauses.append(f"{key} = %s")
                        param_values.append(value)
                if not set_clauses:
                    logging.warning("No valid fields to update.")
                    return None
                set_sql = ", ".join(set_clauses)
                sql = f"UPDATE call_history SET {set_sql} WHERE call_id = %s RETURNING id;"
               
                # Add call_id to the parameters list
                param_values.append(call_id)
                logging.debug(f"Executing SQL: {sql} with params: {param_values}") # Optional: Log SQL for debugging
                cursor.execute(sql, tuple(param_values))
                row = cursor.fetchone()
                conn.commit()
                logging.info(f"Updated call_history for call_id {call_id}. Updated fields: {list(updates.keys())}")
                return row[0] if row else None
            except Exception as e:
                conn.rollback()
                logging.error(f"Error updating call history for call_id={call_id}: {e}")
                traceback.print_exc() # Print full traceback
                raise # Re-raise the exception

    def get_call_history_by_user_id(self, user_id: int, page: int = 1, page_size: int = 10):
        with self.conn() as (conn, cursor):
            try:
                # Count total records
                cursor.execute("SELECT COUNT(*) FROM call_history WHERE user_id = %s", (user_id,))
                total = cursor.fetchone()["count"]
                
                # Count completed
                cursor.execute("""
                    SELECT COUNT(*) FROM call_history
                    WHERE user_id = %s AND status = 'completed'
                """, (user_id,))
                completed_calls = cursor.fetchone()["count"]
                not_completed_calls = total - completed_calls
                
                # ✅ MAKE SURE recording_blob IS IN SELECT
                offset = (page - 1) * page_size
                cursor.execute("""
                    SELECT ch.id, ch.call_id, ch.status, ch.duration, ch.transcript,
                        ch.summary, ch.recording_url, ch.created_at, ch.started_at, ch.ended_at,
                        ch.voice_id, ch.voice_name, ch.from_number, ch.to_number,
                        ch.recording_blob,  -- ✅ ADD THIS
                        u.id AS user_id, u.username, u.email
                    FROM call_history ch
                    JOIN users u ON ch.user_id = u.id
                    WHERE ch.user_id = %s
                    ORDER BY ch.created_at DESC
                    LIMIT %s OFFSET %s
                """, (user_id, page_size, offset))
                rows = cursor.fetchall()
                
                # Ensure transcript is JSON
                for row in rows:
                    if isinstance(row["transcript"], str):
                        try:
                            row["transcript"] = json.loads(row["transcript"])
                        except Exception:
                            logging.warning(f"Invalid JSON in transcript for call_id={row['call_id']}")
                
                return {
                    "calls": rows,
                    "total": total,
                    "completed_calls": completed_calls,
                    "not_completed_calls": not_completed_calls,
                    "page": page,
                    "page_size": page_size
                }
            except Exception as e:
                logging.error(f"Error fetching call history for user_id={user_id}: {e}")
                raise

    def create_appointment(
        self,
        user_id: int,
        appointment_date: str,
        start_time: str,
        end_time: str,
        attendee_email: str,
        attendee_name: str,
        title: str,
        description: str = "",
        notes: str = ""
    ):
        """Create a new appointment"""
        query = """
            INSERT INTO appointments
            (user_id, appointment_date, start_time, end_time, attendee_email, attendee_name, title, description, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute(query, (
                    user_id, appointment_date, start_time, end_time,
                    attendee_email, attendee_name, title, description, notes
                ))
                appointment_id = cursor.fetchone()[0]
                conn.commit()
                return appointment_id
            except Exception as e:
                conn.rollback()
                logging.error(f"Error creating appointment: {e}")
                raise

    def create_appointments_table(self):
        """
        Create appointments table to store meeting scheduling data
        """
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS appointments (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        appointment_date DATE NOT NULL,
                        start_time TIME NOT NULL,
                        end_time TIME NOT NULL,
                        attendee_email VARCHAR(255) NOT NULL,
                        attendee_name VARCHAR(255),
                        title TEXT NOT NULL,
                        description TEXT,
                        notes TEXT, -- ✅ NEW FIELD
                        status VARCHAR(50) DEFAULT 'scheduled',
                        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                conn.commit()
            except Exception as e:
                logging.error(f"Error creating appointments table: {e}")

    def get_user_appointments(self, user_id: int, from_date: str = None):
        """Get all appointments for a user from a specific date onwards"""
        if from_date is None:
            from_date = datetime.now().strftime("%Y-%m-%d")
       
        query = """
            SELECT id, appointment_date, start_time, end_time, attendee_email,
                attendee_name, title, description, status, created_at
            FROM appointments
            WHERE user_id = %s AND appointment_date >= %s
            ORDER BY appointment_date, start_time
        """
        with self.conn() as (conn, cursor):
            try:
                cursor.execute(query, (user_id, from_date))
                return cursor.fetchall()
            except Exception as e:
                logging.error(f"Error getting appointments: {e}")
                raise

    def check_appointment_conflict(
        self,
        user_id: int,
        appointment_date: str,
        start_time: str,
        end_time: str
    ) -> bool:
        """Check if there's a conflicting appointment"""
        query = """
            SELECT COUNT(*) as conflict_count
            FROM appointments
            WHERE user_id = %s
            AND appointment_date = %s
            AND status = 'scheduled'
            AND (
                (start_time <= %s AND end_time > %s) OR
                (start_time < %s AND end_time >= %s) OR
                (start_time >= %s AND end_time <= %s)
            )
        """
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute(query, (
                    user_id, appointment_date,
                    start_time, start_time,
                    end_time, end_time,
                    start_time, end_time
                ))
                result = cursor.fetchone()
                return result[0] > 0
            except Exception as e:
                logging.error(f"Error checking conflict: {e}")
                raise

    def get_available_slots(
        self,
        user_id: int,
        appointment_date: str,
        business_hours_start: str = "08:00",
        business_hours_end: str = "18:00",
        slot_duration_minutes: int = 60
    ):
        """Get available time slots for a given date"""
        query = """
            SELECT start_time, end_time
            FROM appointments
            WHERE user_id = %s AND appointment_date = %s AND status = 'scheduled'
            ORDER BY start_time
        """
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute(query, (user_id, appointment_date))
                booked_slots = cursor.fetchall()
           
                return {
                    "date": appointment_date,
                    "booked_slots": [{"start": slot[0], "end": slot[1]} for slot in booked_slots]
                }
            except Exception as e:
                logging.error(f"Error getting available slots: {e}")
                raise

    def get_call_by_id(self, call_id: str, user_id: int):
        """Get a specific call by ID for a user"""
        query = """
            SELECT id, call_id, status, duration, transcript, recording_url,
                transcript_url, transcript_blob, recording_blob,
                created_at, started_at, ended_at,
                from_number, to_number, voice_name
            FROM call_history
            WHERE call_id = %s AND user_id = %s
        """
        with self.conn() as (conn, cursor):
            try:
                cursor.execute(query, (call_id, user_id))
                result = cursor.fetchone()
               
                if result and isinstance(result.get("transcript"), str):
                    try:
                        result["transcript"] = json.loads(result["transcript"])
                    except:
                        pass
               
                return result
            except Exception as e:
                logging.error(f"Error getting call by ID: {e}")
                raise

    def add_call_event(self, call_id: str, event_type: str, event_data: dict = None):
        """Add a unique event entry into call_history.events_log"""
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                # Fetch existing events
                cursor.execute("SELECT events_log FROM call_history WHERE call_id = %s", (call_id,))
                row = cursor.fetchone()
                if not row:
                    logging.warning(f"Call {call_id} not found for event {event_type}")
                    return
               
                events_log = row[0] or []
                if isinstance(events_log, str):
                    try:
                        events_log = json.loads(events_log)
                    except Exception:
                        events_log = []
                # Check for duplicate event
                if any(ev.get("event") == event_type for ev in events_log):
                    logging.info(f"Duplicate event {event_type} ignored for {call_id}")
                    return
                # Append event
                events_log.append({
                    "event": event_type,
                    "timestamp": datetime.utcnow().isoformat(),
                    "data": event_data or {}
                })
                # Update DB
                cursor.execute(
                    "UPDATE call_history SET events_log = %s WHERE call_id = %s",
                    (json.dumps(events_log), call_id)
                )
                conn.commit()
                logging.info(f"Event '{event_type}' added to call {call_id}")
            except Exception as e:
                conn.rollback()
                logging.error(f"Error adding call event: {e}")

    def add_agent_event(self, call_id: str, event_type: str, event_data: dict = None, timestamp: str = None):
        """Add a unique agent event entry into call_history.agent_events"""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).isoformat()
       
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                # Fetch existing events
                cursor.execute("SELECT agent_events FROM call_history WHERE call_id = %s", (call_id,))
                row = cursor.fetchone()
                if not row:
                    logging.warning(f"Call {call_id} not found for agent event {event_type}")
                    return
               
                events_log = row[0] or []
                if isinstance(events_log, str):
                    try:
                        events_log = json.loads(events_log)
                    except Exception:
                        events_log = []
                # Check for duplicate (within 5s timestamp tolerance)
                now = datetime.now(timezone.utc)
                for ev in events_log:
                    if (ev.get("event_type") == event_type and
                        abs((now - datetime.fromisoformat(ev.get("timestamp").replace("Z", "+00:00"))).total_seconds()) < 5):
                        logging.info(f"Duplicate agent event {event_type} ignored for {call_id}")
                        return
                # Append event
                events_log.append({
                    "event_type": event_type,
                    "event_data": event_data or {},
                    "timestamp": timestamp,
                    "received_at": datetime.now(timezone.utc).isoformat()
                })
                # Update DB
                cursor.execute(
                    "UPDATE call_history SET agent_events = %s WHERE call_id = %s",
                    (json.dumps(events_log), call_id)
                )
                conn.commit()
                logging.info(f"Agent event '{event_type}' added to call {call_id}")
            except Exception as e:
                conn.rollback()
                logging.error(f"Error adding agent event: {e}")
                traceback.print_exc()
                raise

    # Add to your PGDB class in db.py
    def create_contacts_table(self):
        """Create contacts table with proper constraints"""
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS contacts (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        first_name VARCHAR(100),
                        last_name VARCHAR(100),
                        phone_number VARCHAR(50) NOT NULL,
                        email VARCHAR(255),
                        call_status VARCHAR(50) DEFAULT 'pending',
                        uploaded_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id, phone_number) -- Prevent duplicate phone numbers per user
                    );
                   
                    CREATE INDEX IF NOT EXISTS idx_contacts_user_id ON contacts(user_id);
                    CREATE INDEX IF NOT EXISTS idx_contacts_phone ON contacts(phone_number);
                """)
                conn.commit()
                logging.info("✅ contacts table created/updated")
            except Exception as e:
                logging.error(f"Error creating contacts table: {e}")

    def get_contacts_simple(self, user_id: int) -> List[Dict]:
        """
        Get all contacts for a user (name + phone only, for quick frontend display)
        No pagination - returns all contacts
       
        Returns:
            List of dicts with: id, name, phone_number
        """
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                cursor.execute("""
                    SELECT id, first_name, last_name, phone_number, call_status
                    FROM contacts
                    WHERE user_id = %s
                    ORDER BY uploaded_at DESC
                """, (user_id,))
                   
                rows = cursor.fetchall()
                   
                return [
                    {
                        "id": row[0],
                        "name": f"{row[1] or ''} {row[2] or ''}".strip() or "Unknown",
                        "phone_number": row[3],
                        "call_status": row[4]
                    }
                    for row in rows
                ]
                   
            except Exception as e:
                logging.error(f"Error getting contacts (simple): {e}")
                raise

    def save_contacts_bulk(self, user_id: int, contacts: list) -> dict:
        """
        Fast bulk insert using execute_values (fastest method for PostgreSQL)
        """
        from psycopg2.extras import execute_values
       
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                # Prepare data
                values = [
                    (
                        user_id,
                        contact.get("first_name", "")[:100],
                        contact.get("last_name", "")[:100],
                        contact.get("phone_number", "").strip(),
                        contact.get("email") if contact.get("email") else None
                    )
                    for contact in contacts
                    if contact.get("phone_number", "").strip()
                ]
               
                if not values:
                    return {"inserted": 0, "duplicates": 0, "errors": 0}
               
                # Fast bulk insert using execute_values
                execute_values(
                    cursor,
                    """
                    INSERT INTO contacts (user_id, first_name, last_name, phone_number, email)
                    VALUES %s
                    ON CONFLICT (user_id, phone_number) DO NOTHING
                    """,
                    values,
                    page_size=1000 # Insert 1000 rows at a time
                )
               
                # Get count of actually inserted rows
                cursor.execute("""
                    SELECT COUNT(*) FROM contacts
                    WHERE user_id = %s AND uploaded_at >= NOW() - INTERVAL '10 seconds'
                """, (user_id,))
               
                inserted = cursor.fetchone()[0]
               
                conn.commit()
           
                duplicates = len(values) - inserted
           
                logging.info(f"✅ Bulk insert: {inserted} inserted, {duplicates} duplicates")
           
                return {
                    "inserted": inserted,
                    "duplicates": duplicates,
                    "errors": 0
                }
           
            except Exception as e:
                conn.rollback()
                logging.error(f"Bulk insert error: {e}")
                traceback.print_exc()
                raise

    def create_prompt(self, user_id: int, prompt_name: str, system_prompt: str) -> dict:
        """
        Create a new named prompt for user.
       
        Args:
            user_id: User ID
            prompt_name: Name/heading for the prompt
            system_prompt: The actual prompt text
       
        Returns:
            dict with prompt data
        """
        with self.conn() as (conn, cursor):
            try:
                # Check if name already exists for this user
                cursor.execute("""
                    SELECT id FROM user_prompts
                    WHERE user_id = %s AND prompt_name = %s
                """, (user_id, prompt_name))
               
                if cursor.fetchone():
                    raise ValueError(f"Prompt with name '{prompt_name}' already exists")
               
                cursor.execute("""
                    INSERT INTO user_prompts (user_id, prompt_name, system_prompt, is_default)
                    VALUES (%s, %s, %s, FALSE)
                    RETURNING id, user_id, prompt_name, system_prompt, is_default, created_at, updated_at;
                """, (user_id, prompt_name, system_prompt))
               
                result = cursor.fetchone()
                conn.commit()
                logging.info(f"✅ Created prompt '{prompt_name}' for user {user_id}")
                return result
            except ValueError:
                raise
            except Exception as e:
                conn.rollback()
                logging.error(f"Error creating prompt: {e}")
                raise

    def get_all_user_prompts(self, user_id: int) -> list:
        """
        Get all prompts for a user with their names.
       
        Returns:
            List of dicts with: id, prompt_name, system_prompt, is_default, created_at, updated_at
        """
        with self.conn() as (conn, cursor):
            try:
                cursor.execute("""
                    SELECT id, user_id, prompt_name, system_prompt, is_default, created_at, updated_at
                    FROM user_prompts
                    WHERE user_id = %s
                    ORDER BY is_default DESC, created_at DESC;
                """, (user_id,))
                return cursor.fetchall()
            except Exception as e:
                logging.error(f"Error getting all user prompts: {e}")
                raise

    def get_prompt_by_id(self, user_id: int, prompt_id: int) -> dict:
        """
        Get a specific prompt by ID (with user verification).
        """
        with self.conn() as (conn, cursor):
            try:
                cursor.execute("""
                    SELECT id, user_id, prompt_name, system_prompt, is_default, created_at, updated_at
                    FROM user_prompts
                    WHERE id = %s AND user_id = %s;
                """, (prompt_id, user_id))
                return cursor.fetchone()
            except Exception as e:
                logging.error(f"Error getting prompt by id: {e}")
                raise

    def update_prompt(self, user_id: int, prompt_id: int, prompt_name: str = None, system_prompt: str = None) -> dict:
        """
        Update an existing prompt.
        Can update name and/or prompt text.
        """
        with self.conn() as (conn, cursor):
            try:
                # Verify ownership
                cursor.execute("""
                    SELECT id FROM user_prompts WHERE id = %s AND user_id = %s
                """, (prompt_id, user_id))
               
                if not cursor.fetchone():
                    raise ValueError("Prompt not found or access denied")
               
                # Build dynamic update
                updates = []
                params = []
               
                if prompt_name is not None:
                    updates.append("prompt_name = %s")
                    params.append(prompt_name)
               
                if system_prompt is not None:
                    updates.append("system_prompt = %s")
                    params.append(system_prompt)
               
                if not updates:
                    raise ValueError("No fields to update")
               
                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.extend([prompt_id, user_id])
               
                cursor.execute(f"""
                    UPDATE user_prompts
                    SET {', '.join(updates)}
                    WHERE id = %s AND user_id = %s
                    RETURNING id, user_id, prompt_name, system_prompt, is_default, created_at, updated_at;
                """, params)
               
                result = cursor.fetchone()
                conn.commit()
                logging.info(f"✅ Updated prompt {prompt_id}")
                return result
            except ValueError:
                raise
            except Exception as e:
                conn.rollback()
                logging.error(f"Error updating prompt: {e}")
                raise

    def delete_prompt(self, user_id: int, prompt_id: int) -> bool:
        """
        Delete a prompt (cannot delete default).
        """
        with self.conn(dict_cursor=False) as (conn, cursor):
            try:
                # Check if default
                cursor.execute("""
                    SELECT is_default FROM user_prompts
                    WHERE id = %s AND user_id = %s
                """, (prompt_id, user_id))
               
                row = cursor.fetchone()
                if not row:
                    raise ValueError("Prompt not found")
               
                if row[0]: # is_default
                    raise ValueError("Cannot delete default prompt")
               
                cursor.execute("""
                    DELETE FROM user_prompts
                    WHERE id = %s AND user_id = %s
                """, (prompt_id, user_id))
           
                conn.commit()
                logging.info(f"✅ Deleted prompt {prompt_id}")
                return True
            except ValueError:
                raise
            except Exception as e:
                conn.rollback()
                logging.error(f"Error deleting prompt: {e}")
                raise

    def set_default_prompt(self, user_id: int, prompt_id: int) -> dict:
        """
        Set a prompt as the default (unsets others).
        """
        with self.conn() as (conn, cursor):
            try:
                # Verify ownership
                cursor.execute("""
                    SELECT id FROM user_prompts WHERE id = %s AND user_id = %s
                """, (prompt_id, user_id))
               
                if not cursor.fetchone():
                    raise ValueError("Prompt not found")
               
                # Unset all defaults
                cursor.execute("""
                    UPDATE user_prompts SET is_default = FALSE WHERE user_id = %s
                """, (user_id,))
               
                # Set new default
                cursor.execute("""
                    UPDATE user_prompts
                    SET is_default = TRUE, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND user_id = %s
                    RETURNING id, user_id, prompt_name, system_prompt, is_default, created_at, updated_at;
                """, (prompt_id, user_id))
               
                result = cursor.fetchone()
                conn.commit()
                logging.info(f"✅ Set prompt {prompt_id} as default")
                return result
            except ValueError:
                raise
            except Exception as e:
                conn.rollback()
                logging.error(f"Error setting default: {e}")
                raise