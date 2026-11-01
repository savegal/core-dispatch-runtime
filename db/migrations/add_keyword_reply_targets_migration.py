"""Create keyword-order receipts without rewriting existing orders."""
from sqlalchemy import inspect
from db.database import sync_engine
from db.models import KeywordReplyTarget


def migrate(verbose: bool = False) -> bool:
    with sync_engine.begin() as connection:
        if inspect(connection).has_table(KeywordReplyTarget.__tablename__):
            return False
        KeywordReplyTarget.__table__.create(connection, checkfirst=True)
    return True
