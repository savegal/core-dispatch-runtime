"""
Migration: Adding country_code column to proxies table.

This script adds the country_code column if it doesn't exist.
Safe to run multiple times (checks for column existence).
"""
import sys
import os

# Add root directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from pathlib import Path
from config.settings import get_settings

settings = get_settings()

def migrate():
    """Adds country_code column to proxies table."""
    db_url = settings.DB_URL
    
    if not db_url.startswith("sqlite:///"):
        print(f"[WARNING] Migration supports SQLite only. Current DB_URL: {db_url}")
        print("   For PostgreSQL use ALTER TABLE manually or Alembic migrations.")
        return False
    
    # Extract database file path
    db_path = db_url.replace("sqlite:///", "")
    if not Path(db_path).exists():
        print(f"[ERROR] Database file not found: {db_path}")
        return False
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if country_code column exists
        cursor.execute("PRAGMA table_info(proxies)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if "country_code" in columns:
            print("[OK] Column country_code already exists in proxies table")
            conn.close()
            return True
        
        # Add column
        print("[+] Adding country_code column to proxies table...")
        cursor.execute("""
            ALTER TABLE proxies 
            ADD COLUMN country_code VARCHAR(2) NULL
        """)
        
        # Create index for query optimization
        print("[+] Creating index for country_code...")
        try:
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS ix_proxies_country_code 
                ON proxies(country_code)
            """)
        except sqlite3.OperationalError as e:
            if "already exists" not in str(e).lower():
                print(f"[WARNING] Warning while creating index: {e}")
        
        conn.commit()
        conn.close()
        
        print("[OK] Migration completed successfully!")
        print(f"   Added country_code column to proxies table")
        print(f"   Created index for query optimization")
        return True
        
    except Exception as e:
        print(f"[ERROR] Error during migration: {e}")
        return False


if __name__ == "__main__":
    print("Migration: Adding country_code column to proxies table...")
    success = migrate()
    exit(0 if success else 1)










