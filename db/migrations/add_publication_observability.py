"""Idempotent migration. Run: python -m db.migrations.add_publication_observability"""
from db.database import sync_engine
from db.models import AccountImportJob, PublicationAttempt, PublicationCheckJob, QuantityAction, TaskRuntime, TaskTrafficBucket


def migrate(engine=sync_engine):
    for model in (PublicationAttempt, TaskRuntime, TaskTrafficBucket, PublicationCheckJob, QuantityAction, AccountImportJob):
        model.__table__.create(engine, checkfirst=True)


if __name__ == "__main__":
    migrate()
    print("Publication observability schema is ready")
