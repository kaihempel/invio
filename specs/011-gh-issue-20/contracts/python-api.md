# Contract: `invio.notify` Python API

Public names are re-exported from `invio.notify`. Signatures are normative; bodies are not.

```python
MAX_ATTEMPTS: Final = 5
STALE_PENDING_AFTER: Final = timedelta(hours=1)
CHANNEL_EMAIL: Final = "email"


class SmtpMailer:
    """Sends prepared messages over one reusable SMTP connection (aiosmtplib)."""

    def __init__(
        self, settings: Settings, *, tls_context: ssl.SSLContext | None = None
    ) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc: object) -> None: ...  # quits; never raises
    async def send(self, message: EmailMessage) -> None:
        ...  # raises SMTP/OSError errors;
        # reconnects if disconnected


def missing_smtp_settings(settings: Settings) -> list[str]:
    ...
    # e.g. ["INVIO_SMTP_HOST"]; empty when delivery is possible


async def deliver_digest(
    session_factory: sessionmaker[Session],
    digest_id: int,
    *,
    settings: Settings,
    mailer_factory: Callable[[Settings], SmtpMailer] = SmtpMailer,
    clock: Callable[[], datetime] = utcnow,
) -> DeliveryOutcome:
    ...
    # Loads digest, job (JobConfig), run; applies send_if_empty; builds the payload; creates
    # one row per distinct recipient; THEN renders once; sends per recipient; marks rows.
    # NEVER raises. Any exception after the rows exist (rendering, SMTP, …) marks every row
    # still `pending` as failed. An exception before the recipients are known (digest missing,
    # stored job config invalid, payload invalid) creates no rows and returns
    # DeliveryOutcome(sent=0, failed=0, error="<scrubbed text>"), so the run still ends
    # `partial`. Missing SMTP settings → all rows failed with "smtp not configured: missing …".


async def retry_failed(
    session_factory: sessionmaker[Session],
    *,
    settings: Settings,
    mailer_factory: Callable[[Settings], SmtpMailer] = SmtpMailer,
    clock: Callable[[], datetime] = utcnow,
) -> RetryOutcome:
    ...
    # Implements contracts/cli-notify-retry.md steps 3–6. Callers check SMTP config first.


def run_status_after_delivery(planned: RunStatus, outcome: DeliveryOutcome) -> RunStatus:
    ...
    # SUCCEEDED + (outcome.failed > 0 or outcome.error is not None) → PARTIAL;
    # otherwise planned.


# invio.notify.render
def markdown_to_safe_html(markdown: str) -> str: ...
def render_subject(template: str, *, job_name: str, date: dt.date) -> str: ...
def render_mail(payload: NotificationPayload, digest_markdown: str) -> RenderedMail: ...
def build_message(
    mail: RenderedMail, *, sender: str, recipient: str, now: datetime
) -> EmailMessage: ...


# invio.notify.email
def scrub_secret(text: str, settings: Settings) -> str: ...  # also truncates to 2,000 chars
```

## Repository additions (`invio.db.repositories`)

```python
class DigestRepository:
    def get(self, digest_id: int) -> Digest | None: ...


class NotificationRepository:
    def begin_attempt(self, notification: Notification, *, now: datetime) -> Notification:
        ...
        # attempts += 1, last_attempt_at = now, status stays pending

    def retry_candidates(
        self, *, now: datetime, stale_after: timedelta
    ) -> builtins.list[Notification]: ...
    def claim_for_retry(
        self, notification_id: int, *, now: datetime, stale_after: timedelta
    ) -> bool:
        ...
        # single conditional UPDATE; True iff exactly one row changed

    # mark(): unchanged signature; for status SENT it now also clears error
```

## Transactions

- Each row creation and each `begin_attempt` / claim is committed *before* the network send, so
  a crash leaves a `pending` row with a counted attempt.
- Each `mark` is committed right after its send.
- No DB transaction is held open across an SMTP call.
