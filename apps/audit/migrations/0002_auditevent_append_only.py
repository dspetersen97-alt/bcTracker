"""
Enforce the append-only guarantee for the audit table in the database.

The plan called for revoking UPDATE and DELETE from the application role, but
that would not actually hold: in this deployment the application connects as the
role that owns the schema, and a table owner always retains full privileges on
its own tables. A REVOKE would look like protection while providing none.

A trigger holds regardless of ownership. Removing it requires a deliberate DDL
statement, which is a very different thing from an accidental ``.update()`` or a
SQL injection reaching this table.

The Python-level guards in AuditEvent.save/delete stay as well, so a mistake
fails with a clear message in development instead of a database error.

Note on tests: this fires on row-level UPDATE and DELETE only. Django's test
teardown uses TRUNCATE, which does not fire row triggers, so the suite is
unaffected.
"""

from django.db import migrations

CREATE = """
CREATE OR REPLACE FUNCTION audit_auditevent_append_only()
    RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'audit_auditevent is append-only; % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER audit_auditevent_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_auditevent
    FOR EACH ROW
    EXECUTE FUNCTION audit_auditevent_append_only();
"""

DROP = """
DROP TRIGGER IF EXISTS audit_auditevent_no_update_delete ON audit_auditevent;
DROP FUNCTION IF EXISTS audit_auditevent_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE, reverse_sql=DROP),
    ]
