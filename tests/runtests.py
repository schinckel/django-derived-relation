"""Run the standalone behavioral suite without a Django source checkout."""

import os
import sys

import django
from django.conf import settings
from django.test.runner import DiscoverRunner


def main() -> int:
    sqlite = "--sqlite" in sys.argv
    database = (
        {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
        if sqlite
        else {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "postgres"),
            "USER": os.environ.get("PGUSER", ""),
            "PASSWORD": os.environ.get("PGPASSWORD", ""),
            "HOST": os.environ.get("PGHOST", ""),
            "PORT": os.environ.get("PGPORT", ""),
            "TEST": {"NAME": "test_django_derived_relation"},
        }
    )
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
