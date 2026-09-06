"""The environment's database, for the rows only the server can turn into a
notification.

Section 3's five variants are decided by five fields the server signs into the
notification token, and those come from rows in `purchases`. The token is
signed, so the only honest way to reach those variants is to put the rows there
and let the server do its own arithmetic.

Nothing here guesses a schema. `--inspect` reads the real one first, because a
seeder written against an imagined column list fails in a way that looks like a
product bug. Run the inspection once, and the seed shapes below are filled in
from what it prints.

Credentials come from the same names the QA automation already uses, so an
existing `.env` works unchanged:

    DB_HOST DB_PORT DB_USER DB_PASSWORD DB_NAME
    DB_SSH_HOST DB_SSH_USER DB_SSH_PORT DB_SSH_KEY_PATH   (bastion, optional)
"""
import os
from contextlib import contextmanager

from bring import config as cfg

try:
    import pymysql
    HAVE_MYSQL = True
except ImportError:
    pymysql = None
    HAVE_MYSQL = False

try:
    import boto3
    HAVE_BOTO = True
except ImportError:
    boto3 = None
    HAVE_BOTO = False

try:
    from sshtunnel import SSHTunnelForwarder
    HAVE_TUNNEL = True
except ImportError:
    SSHTunnelForwarder = None
    HAVE_TUNNEL = False


class NoDatabase(RuntimeError):
    """No usable database. The tests that need one skip; nothing fails."""


# Temporary environments do not share production's database, and they do not
# each get their own server either: the deployer puts them all on one RDS
# instance kept for the purpose, and gives each environment a database on it
# named after itself. Verified against the environments that exist today —
# `mySqlSolflare-temp` holds `dbname: solflare_temp` on this host.
#
# The credentials live in Secrets Manager, which is where the backend itself
# reads them (utils/dataHandler.ts, `DB_SECRET_ARN`). Taking them from there
# rather than from a .env means no database password is written to disk, and
# no chance of pointing this at production by editing the wrong line.
SHARED_SECRET = os.getenv("BRING_DB_SECRET", "mySqlSharedDevRds")

_cached_secret = None


def secret() -> dict:
    """Credentials for the temporary-environment RDS, from Secrets Manager."""
    global _cached_secret
    if _cached_secret is not None:
        return _cached_secret
    if not HAVE_BOTO:
        raise NoDatabase("boto3 is required to read the database credentials")
    client = boto3.client("secretsmanager", region_name=cfg.AWS_REGION)
    import json
    _cached_secret = json.loads(
        client.get_secret_value(SecretId=SHARED_SECRET)["SecretString"])
    return _cached_secret


def configured() -> bool:
    """Whether this machine has enough to reach the database at all."""
    if not HAVE_MYSQL:
        return False
    if os.getenv("DB_HOST") and os.getenv("DB_USER"):
        return True
    try:
        return bool(secret().get("host"))
    except Exception:
        return False


def _params() -> dict:
    """Connection parameters: the secret by default, .env when it overrides."""
    host = os.getenv("DB_HOST")
    if host:
        user, password = os.getenv("DB_USER"), os.getenv("DB_PASSWORD", "")
        port = int(os.getenv("DB_PORT", "3306"))
    else:
        values = secret()
        host, port = values["host"], int(values.get("port", 3306))
        user, password = values["username"], values["password"]

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "connect_timeout": 15,
        "cursorclass": pymysql.cursors.DictCursor,
    }


@contextmanager
def connect(database: str = None):
    """A connection to *database*, through the bastion when one is configured.

    The tunnel is opened per connection rather than held open for the run: the
    seeding happens a handful of times, and a forwarder left running across a
    twenty-minute suite is one more thing to notice has died.
    """
    if not configured():
        raise NoDatabase(
            "No database configured. Set DB_HOST, DB_USER, DB_PASSWORD in .env "
            "(and pip install pymysql) to seed notification rows; without it "
            "the section 3 variant tests skip.")

    params = _params()
    tunnel = None

    ssh_host, ssh_user = os.getenv("DB_SSH_HOST"), os.getenv("DB_SSH_USER")
    if ssh_host and ssh_user:
        if not HAVE_TUNNEL:
            raise NoDatabase("DB_SSH_HOST is set but sshtunnel is not installed")
        options = {
            "ssh_address_or_host": (ssh_host, int(os.getenv("DB_SSH_PORT", "22"))),
            "ssh_username": ssh_user,
            "remote_bind_address": (params["host"], params["port"]),
            "local_bind_address": ("127.0.0.1", 0),
        }
        key = os.getenv("DB_SSH_KEY_PATH")
        if key and os.path.exists(key):
            options["ssh_pkey"] = key
        elif os.getenv("DB_SSH_PASSWORD"):
            options["ssh_password"] = os.getenv("DB_SSH_PASSWORD")
        tunnel = SSHTunnelForwarder(**options)
        tunnel.start()
        params["host"], params["port"] = "127.0.0.1", tunnel.local_bind_port

    if database:
        params["database"] = database

    connection = None
    try:
        connection = pymysql.connect(**params)
        yield connection
    finally:
        if connection:
            try:
                connection.close()
            except Exception:
                pass
        if tunnel:
            try:
                tunnel.stop()
            except Exception:
                pass


def query(sql: str, args=None, database: str = None) -> list:
    with connect(database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, args or ())
            return list(cursor.fetchall())


# The temporary environment shares an RDS instance with production, and the
# credentials that reach one reach the other. A seeder is a writer, and a
# writer pointed at the wrong database by a blank DB_NAME or a failed lookup
# would be inventing purchases for real users. So writes name the database and
# the name is checked; reads are left alone.
PROTECTED = ("prod", "production", "live")


def refuse_if_production(database: str) -> None:
    """Raise unless *database* is safely a temporary environment's own."""
    if not database:
        raise NoDatabase(
            "A write needs an explicit database name. Blank means 'whatever the "
            "connection defaults to', and that is production here.")
    lowered = database.lower()
    for marker in PROTECTED:
        if marker in lowered:
            raise NoDatabase(
                f"Refusing to write to '{database}': it looks like production, "
                f"and seeding purchases there would create rewards for real "
                f"users.\nPoint DB_NAME at the temporary environment's own "
                f"database — `python -m bring.db --inspect` lists them.")


def execute(sql: str, args=None, database: str = None) -> int:
    refuse_if_production(database)
    with connect(database) as connection:
        with connection.cursor() as cursor:
            affected = cursor.execute(sql, args or ())
        connection.commit()
        return affected


def database_for(env_name: str = None) -> str:
    """The database this environment's backend writes to.

    `DB_NAME` wins when it is set. Otherwise the environment's own database is
    looked up by name, because a temporary environment gets one of its own on
    the shared instance and writing into the wrong one would be seeding
    production-shaped data into somebody else's run.
    """
    explicit = os.getenv("DB_NAME")
    if explicit:
        return explicit

    env_name = env_name or cfg.ENV_NAME
    # The deployer's own naming: `qa-e2e` becomes `qa_e2e_temp`, the way
    # `solflare` became `solflare_temp`. Derived rather than searched for, so a
    # missing database says "the environment is not deployed" instead of
    # quietly matching some other environment's.
    expected = env_name.replace("-", "_").lower()
    if not expected.endswith("_temp"):
        expected += "_temp"

    candidates = [list(row.values())[0] for row in query("SHOW DATABASES")]
    if expected in candidates:
        return expected

    others = [name for name in candidates
              if name.endswith("_temp")
              and not any(m in name.lower() for m in PROTECTED)]
    raise NoDatabase(
        f"'{env_name}' has no database yet - the deployer creates "
        f"`{expected}` when the environment is deployed, and it is not there.\n"
        f"Deploy the environment first, or point DB_NAME at one that exists.\n"
        f"Temporary environments on this server: "
        f"{', '.join(sorted(others)) or '(none)'}")


# ── inspection ──────────────────────────────────────────────────────

def describe(table: str, database: str = None) -> list:
    return query(f"DESCRIBE `{table}`", database=database)


def inspect(env_name: str = None) -> None:
    """Print what is actually there, so the seed can be written against it."""
    print("Databases visible to this user:")
    for row in query("SHOW DATABASES"):
        print("  ", list(row.values())[0])

    try:
        database = database_for(env_name)
    except NoDatabase as e:
        print(f"\n{e}")
        return
    print(f"\nUsing database: {database}")

    for table in ("purchases", "platforms", "testWallets", "clicks", "reminders"):
        try:
            columns = describe(table, database=database)
        except Exception as e:
            print(f"\n{table}: not readable ({e})")
            continue
        print(f"\n{table}:")
        for column in columns:
            null = "NULL" if column.get("Null") == "YES" else "NOT NULL"
            default = column.get("Default")
            print(f"   {column['Field']:<28} {column['Type']:<24} {null}"
                  + (f"  default={default!r}" if default is not None else ""))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Look at the environment's database")
    parser.add_argument("--env", default=cfg.ENV_NAME)
    parser.add_argument("--inspect", action="store_true",
                        help="print the databases and the tables the seed needs "
                             "(the default, and the only thing this does)")
    args = parser.parse_args()
    try:
        inspect(args.env)
    except NoDatabase as e:
        raise SystemExit(str(e))
