"""Engine and session factories plus URL handling for the persistence layer.

The database URL usually contains a password. This module never logs it; use :func:`redact`
before showing any text that might echo it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote, quote_plus

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker

_MYSQL_BACKENDS = ("mysql", "mariadb")
# time_zone only affects NOW()/TIMESTAMP (UTCDateTime binds naive UTC itself); it keeps raw SQL
# and any future TIMESTAMP column consistent with the stored UTC values.
_INIT_COMMAND = (
    "SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION', time_zone='+00:00'"
)


def normalize_url(url: str | URL) -> URL:
    """Parse ``url``; for mysql/mariadb force ``charset=utf8mb4``.

    Raises :class:`sqlalchemy.exc.ArgumentError` for unparsable URLs.
    """
    try:
        parsed = make_url(url)
    except ValueError as exc:  # e.g. a non-numeric port
        raise ArgumentError(f"Could not parse SQLAlchemy URL: {exc}") from exc
    if parsed.get_backend_name() in _MYSQL_BACKENDS:
        parsed = parsed.update_query_dict({"charset": "utf8mb4"})
    return parsed


def connect_args_for(url: URL) -> dict[str, str]:
    """Return driver connect arguments (strict SQL mode, UTC session) for mysql/mariadb."""
    if url.get_backend_name() in _MYSQL_BACKENDS:
        return {"init_command": _INIT_COMMAND}
    return {}


def create_db_engine(url: str | URL, **kwargs: Any) -> Engine:
    """Create an engine that follows invio's connection rules.

    mysql/mariadb: utf8mb4, strict mode, UTC session, ``pool_pre_ping``, ``pool_recycle`` and
    ``READ COMMITTED`` isolation, so a statement sees rows other transactions committed after
    this one began (``ItemRepository.add`` relies on it to return a concurrent writer's row).
    sqlite: ``PRAGMA foreign_keys=ON`` on every connection, and SQLAlchemy emits ``BEGIN``
    itself. pysqlite only opens a transaction implicitly before DML, so a SAVEPOINT issued
    after mere SELECTs would become the outermost transaction and its RELEASE would commit.
    """
    parsed = normalize_url(url)
    backend = parsed.get_backend_name()
    if backend in _MYSQL_BACKENDS:
        kwargs.setdefault("pool_pre_ping", True)
        kwargs.setdefault("pool_recycle", 3600)
        kwargs.setdefault("isolation_level", "READ COMMITTED")
        connect_args = {**connect_args_for(parsed), **kwargs.pop("connect_args", {})}
        engine = create_engine(parsed, connect_args=connect_args, **kwargs)
    else:
        engine = create_engine(parsed, **kwargs)
    if backend == "sqlite":

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_connection: Any, _record: Any) -> None:  # Any: DBAPI
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()
            dbapi_connection.isolation_level = None  # disable pysqlite's implicit BEGIN

        @event.listens_for(engine, "begin")
        def _begin(connection: Connection) -> None:
            connection.exec_driver_sql("BEGIN")

    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory with ``expire_on_commit=False``."""
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, roll back on any exception (re-raised), always close."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def redact(text: str, url: str | URL) -> str:
    """Replace the URL's password and the URL's string forms in ``text`` with ``***``.

    Accepts unparsable URLs (the raw string is replaced) and URLs without a password.
    Every occurrence of the password is replaced, even inside other words: with a very short
    password the message gets harder to read, which is preferred over risking a leak.
    """
    needles: set[str] = set()
    raw = url if isinstance(url, str) else None
    try:
        parsed = make_url(url)
    except (ArgumentError, ValueError):  # ValueError: e.g. non-numeric port
        parsed = None
    if raw:
        needles.add(raw)
    if parsed is not None:
        needles.add(parsed.render_as_string(hide_password=False))
        needles.add(normalize_url(parsed).render_as_string(hide_password=False))
        password = parsed.password
        if password:
            needles.update({password, quote_plus(password), quote(password, safe="")})
    for needle in sorted((n for n in needles if n), key=len, reverse=True):
        text = text.replace(needle, "***")
    return text


class DatabaseConfigError(ValueError):
    """The database URL is unparsable, names an unknown dialect or a missing driver.

    The message is already scrubbed of the URL and its password.
    """


def scrub_database_url(text: str, raw: str) -> str:
    """Mask ``raw`` (the configured database URL) in all its string forms within ``text``."""
    if not raw:
        return text
    text = redact(text, raw)
    try:
        return redact(text, normalize_url(raw))
    except ArgumentError:
        return text


def check_database_url(raw: str) -> URL:
    """Parse ``raw`` and import its DBAPI module without connecting.

    Raises :class:`DatabaseConfigError` for an unparsable URL or unknown dialect/driver
    (``ArgumentError``/``NoSuchModuleError``) and for an uninstalled DBAPI (``ImportError``).
    """
    try:
        url = normalize_url(raw)
        url.get_dialect().import_dbapi()
    except ArgumentError as exc:
        message = scrub_database_url(str(exc), raw)
        raise DatabaseConfigError(f"invalid database URL: {message}") from None
    except ImportError as exc:
        message = scrub_database_url(str(exc), raw)
        raise DatabaseConfigError(f"database driver not installed: {message}") from None
    return url


def checked_session_factory(raw: str) -> sessionmaker[Session]:
    """Return a session factory on ``raw`` after :func:`check_database_url` accepted it.

    Raises :class:`DatabaseConfigError` like :func:`check_database_url`; nothing connects yet.
    """
    return session_factory(create_db_engine(check_database_url(raw)))
