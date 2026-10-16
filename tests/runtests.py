"""Run the standalone behavioral suite without a Django source checkout."""

import argparse
import os

import django
from django.conf import settings
from django.test.runner import DiscoverRunner


def main() -> int:
    parser = argparse.ArgumentParser()
    backends = parser.add_mutually_exclusive_group()
    for backend in ("sqlite", "mysql", "oracle"):
        backends.add_argument(
            f"--{backend}", dest="backend", action="store_const", const=backend
        )
    parser.set_defaults(backend="postgresql")
    backend = parser.parse_args().backend
    if backend == "sqlite":
        database = {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    elif backend == "mysql":
        database = {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ.get("MYSQL_DATABASE", "django_table_expressions"),
            "USER": os.environ.get("MYSQL_USER", "root"),
            "PASSWORD": os.environ.get("MYSQL_PASSWORD", ""),
            "HOST": os.environ.get("MYSQL_HOST", "localhost"),
            "PORT": os.environ.get("MYSQL_PORT", "3306"),
            "TEST": {"NAME": "test_django_table_expressions"},
        }
    elif backend == "oracle":
        database = {
            "ENGINE": "django.db.backends.oracle",
            "NAME": os.environ.get("ORACLE_NAME", "localhost:1521/FREEPDB1"),
            "USER": os.environ.get("ORACLE_USER", "system"),
            "PASSWORD": os.environ.get("ORACLE_PASSWORD", ""),
            "TEST": {
                "CREATE_DB": False,
                "USER": "test_django_table_expressions",
                "PASSWORD": "derived-tests",
                "TBLSPACE": "USERS",
                "TBLSPACE_TMP": "TEMP",
            },
        }
    else:
        database = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "postgres"),
            "USER": os.environ.get("PGUSER", ""),
            "PASSWORD": os.environ.get("PGPASSWORD", ""),
            "HOST": os.environ.get("PGHOST", ""),
            "PORT": os.environ.get("PGPORT", ""),
            "TEST": {"NAME": "test_django_table_expressions"},
        }
    settings.configure(
        DATABASES={"default": database},
        INSTALLED_APPS=["derived_relations"],
        SECRET_KEY="derived-relation-tests",
        DEFAULT_AUTO_FIELD="django.db.models.AutoField",
    )
    django.setup()
    print("Testing Django", django.get_version())
    return DiscoverRunner(verbosity=1, interactive=False).run_tests(
        ["derived_relations"]
    )


if __name__ == "__main__":
    raise SystemExit(main())
