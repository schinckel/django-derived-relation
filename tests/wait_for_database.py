"""Wait for a disposable CI database to accept native backend queries."""

import sys
import time

import django
from django.conf import settings
from django.db import DatabaseError, connection
from runtests import database_config


def main() -> None:
    backend = sys.argv[1]
    settings.configure(
        DATABASES={"default": database_config(backend)},
        INSTALLED_APPS=[],
        DISABLE_COCKROACHDB_TELEMETRY=True,
    )
    django.setup()
    statement = {
        "firebird": "SELECT 1 FROM RDB$DATABASE",
        "db2": "SELECT 1 FROM SYSIBM.SYSDUMMY1",
    }.get(backend, "SELECT 1")
    for attempt in range(90):
        try:
            with connection.cursor() as cursor:
                cursor.execute(statement)
                assert cursor.fetchone()[0] == 1
            print(f"{backend} ready with Django {django.get_version()}")
            connection.close()
            return
        except DatabaseError:
            connection.close()
            if attempt == 89:
                raise
            time.sleep(5)


if __name__ == "__main__":
    main()
