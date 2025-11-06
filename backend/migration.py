# migration_create_user_prompts.py
# Run this ONCE after deploying to create prompts for existing users

import os
import logging
from dotenv import load_dotenv
from src.utils.db import PGDB

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate_existing_users():
    """
    Create default prompts for all existing users who don't have one.
    Safe to run multiple times (uses ON CONFLICT DO NOTHING).
    """
    db = PGDB()
    
    logger.info("🔄 Starting migration: Creating prompts for existing users")
    
    # Get all users
    conn = db.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, username FROM users ORDER BY id")
            users = cursor.fetchall()
            
        logger.info(f"📊 Found {len(users)} users")
        
        # Create default prompt for each user
        created_count = 0
        skipped_count = 0
        
        for user_id, username in users:
            try:
                # Check if prompt already exists
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM user_prompts WHERE user_id = %s",
                        (user_id,)
                    )
                    exists = cursor.fetchone()
                
                if exists:
                    logger.info(f"⏭️  User {user_id} ({username}) already has prompt")
                    skipped_count += 1
                    continue
                
                # Create default prompt
                db.create_default_user_prompt(user_id)
                logger.info(f"✅ Created prompt for user {user_id} ({username})")
                created_count += 1
                
            except Exception as e:
                logger.error(f"❌ Failed for user {user_id}: {e}")
        
        logger.info("=" * 60)
        logger.info(f"✅ Migration complete!")
        logger.info(f"   Created: {created_count}")
        logger.info(f"   Skipped: {skipped_count}")
        logger.info(f"   Total:   {len(users)}")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate_existing_users()