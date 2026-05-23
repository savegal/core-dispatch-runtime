import os
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from db.models import Base, CreatorSource, MonitoredBlogger
from config.settings import get_settings

def migrate():
    settings = get_settings()
    # ИСПРАВЛЕНО: Используем правильное имя атрибута из settings.py (DB_URL вместо DATABASE_URL)
    engine = create_engine(settings.DB_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    print("Starting migration to MonitoredBlogger (v2)...")

    # 1. Create table monitored_bloggers if not exists
    # Using Base.metadata.create_all is safest if model is defined
    Base.metadata.create_all(engine)
    print("Tables ensured.")

    # 2. Check if column exists in creator_sources
    try:
        session.execute(text("SELECT monitored_blogger_id FROM creator_sources LIMIT 1"))
    except Exception:
        print("Adding monitored_blogger_id column...")
        # SQLite limitations: simple ADD COLUMN is supported
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE creator_sources ADD COLUMN monitored_blogger_id INTEGER REFERENCES monitored_bloggers(id) ON DELETE SET NULL"))
            conn.commit()
    
    # 3. Migrate data
    creators = session.query(CreatorSource).filter(CreatorSource.monitored_blogger_id.is_(None)).all()
    print(f"Found {len(creators)} unlinked creators.")

    for creator in creators:
        # Check if MonitoredBlogger exists for this username
        monitored = session.query(MonitoredBlogger).filter(MonitoredBlogger.username == creator.username).first()
        
        if not monitored:
            print(f"Creating MonitoredBlogger for @{creator.username}")
            monitored = MonitoredBlogger(
                username=creator.username,
                last_checked_at=creator.last_checked_at,
                last_seen_video_id=creator.last_seen_video_id
            )
            session.add(monitored)
            session.flush() # get id

        # Link
        creator.monitored_blogger_id = monitored.id
        print(f"Linked CreatorSource {creator.id} to MonitoredBlogger {monitored.id}")
    
    session.commit()
    print("Migration complete.")
    session.close()

if __name__ == "__main__":
    migrate()
