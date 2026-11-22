# Database migrations

The database schema is currently initialized by `signalstack.infrastructure.db.init_db()`.
When a schema change requires altering existing data, add a versioned migration here and
document its execution before changing the initialized schema.
