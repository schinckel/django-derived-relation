"""Run the standalone behavioral suite without a Django source checkout."""

import argparse
import os

import django
from django.conf import settings
from django.test.runner import DiscoverRunner


def database_config(backend: str) -> dict[str, object]:
    database: dict[str, object]
    if backend == "sqlite":
        database = {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    elif backend in {"mysql", "tidb"}:
        database = {
            "ENGINE": (
                "django_tidb" if backend == "tidb" else "django.db.backends.mysql"
            ),
            "NAME": os.environ.get("MYSQL_DATABASE", "django_derived_relation"),
            "USER": os.environ.get("MYSQL_USER", "root"),
            "PASSWORD": os.environ.get("MYSQL_PASSWORD", ""),
            "HOST": os.environ.get("MYSQL_HOST", "localhost"),
            "PORT": os.environ.get("MYSQL_PORT", "3306"),
            "TEST": {"NAME": "test_django_derived_relation"},
        }
    elif backend == "oracle":
        database = {
            "ENGINE": "django.db.backends.oracle",
            "NAME": os.environ.get("ORACLE_NAME", "localhost:1521/FREEPDB1"),
            "USER": os.environ.get("ORACLE_USER", "system"),
            "PASSWORD": os.environ.get("ORACLE_PASSWORD", ""),
            "TEST": {
                "CREATE_DB": False,
                "USER": "test_django_derived_relation",
                "PASSWORD": "derived-tests",
                "TBLSPACE": "USERS",
                "TBLSPACE_TMP": "TEMP",
            },
        }
    elif backend == "firebird":
        database = {
            "ENGINE": "django_firebird",
            "NAME": os.environ.get(
                "FIREBIRD_DATABASE", "/var/lib/firebird/data/derived.fdb"
            ),
            "USER": "SYSDBA",
            "PASSWORD": os.environ.get("FIREBIRD_PASSWORD", "derived-tests"),
            "HOST": os.environ.get("FIREBIRD_HOST", "localhost"),
            "PORT": os.environ.get("FIREBIRD_PORT", "3050"),
            "TEST": {"NAME": "/var/lib/firebird/data/test_derived.fdb"},
        }
    elif backend == "db2":
        database = {
            "ENGINE": "ibm_db_django",
            "NAME": os.environ.get("DB2_DATABASE", "derived"),
            "USER": os.environ.get("DB2_USER", "db2inst1"),
            "PASSWORD": os.environ.get("DB2_PASSWORD", "derived-tests"),
            "HOST": os.environ.get("DB2_HOST", "localhost"),
            "PORT": os.environ.get("DB2_PORT", "50000"),
            "TEST": {"NAME": "tderived"},
        }
    else:
        engines = {
            "postgresql": "django.db.backends.postgresql",
            "cockroachdb": "django_cockroachdb",
            "yugabytedb": "django_yugabytedb",
        }
        database = {
            "ENGINE": engines[backend],
            "NAME": os.environ.get("PGDATABASE", "postgres"),
            "USER": os.environ.get("PGUSER", ""),
            "PASSWORD": os.environ.get("PGPASSWORD", ""),
            "HOST": os.environ.get("PGHOST", ""),
            "PORT": os.environ.get("PGPORT", ""),
            "TEST": {"NAME": "test_django_derived_relation"},
        }
    return database


def main() -> int:
    parser = argparse.ArgumentParser()
    backends = parser.add_mutually_exclusive_group()
    for backend in (
        "sqlite",
        "mysql",
        "oracle",
        "cockroachdb",
        "yugabytedb",
        "firebird",
        "db2",
        "tidb",
    ):
        backends.add_argument(
            f"--{backend}", dest="backend", action="store_const", const=backend
        )
    parser.set_defaults(backend="postgresql")
    backend = parser.parse_args().backend
    settings.configure(
        DATABASES={"default": database_config(backend)},
        INSTALLED_APPS=["derived_relations"],
        SECRET_KEY="derived-relation-tests",
        DEFAULT_AUTO_FIELD="django.db.models.AutoField",
        DISABLE_COCKROACHDB_TELEMETRY=True,
    )
    django.setup()
    print("Testing Django", django.get_version())
    return DiscoverRunner(verbosity=1, interactive=False).run_tests(
        ["derived_relations"]
    )


if __name__ == "__main__":
    raise SystemExit(main())
