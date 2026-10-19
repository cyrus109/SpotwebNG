"""Alembic environment. Migrations run on a connection handed in by app.migrate,
so they work under the app's async engine and never need an alembic.ini."""

from alembic import context

from app.models import Base

connection = context.config.attributes["connection"]
context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    render_as_batch=connection.dialect.name == "sqlite",
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
