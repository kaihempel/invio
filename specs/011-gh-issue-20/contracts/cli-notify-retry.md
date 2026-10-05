# Contract: `invio notify retry`

Module `src/invio/cli/commands/notify.py` exposes `app = typer.Typer(help="E-mail notifications.",
no_args_is_help=True)`. It is auto-discovered as `invio notify`.

## Synopsis

```text
invio notify retry
```

No arguments or options in this issue.

## Behaviour

1. Load settings. A missing `INVIO_DATABASE_URL` causes exit **2** with
   `Configuration error: missing setting database_url`.
2. Check the SMTP configuration. If `INVIO_SMTP_HOST` or `INVIO_SMTP_FROM` is missing, exit **2**
   with `Configuration error: smtp not configured: missing INVIO_SMTP_HOST` (or `…_FROM`). No
   notification row is read or changed.
3. Select the retry candidates:
   - every notification with status `failed`;
   - every notification with status `pending` whose `COALESCE(last_attempt_at, created_at)` is
     more than 1 hour before now.
   They are processed in id order.
4. For each candidate, claim it atomically (see [data-model.md](../data-model.md)).
   - A candidate claimed by another process is silently skipped and not counted.
   - A stale `pending` candidate that already had 5 attempts is set to `skipped` with
     `interrupted: attempt limit reached` without sending, and counted as *given up*.
5. Rebuild the message from the stored digest and `NotificationPayload`, then send it to the
   stored `recipient`.
   - On success: `sent`, with `sent_at` set and `error` cleared.
   - On failure with fewer than 5 attempts: `failed`, with the new error.
   - On failure at 5 attempts: `skipped`, keeping the error; counted as *given up*.
   - Digest deleted, or payload invalid: the attempt fails with `digest no longer available` /
     `invalid notification payload: …`, following the same attempt rules.
6. Logs (JSON, stderr) are emitted inside `run_context(job=<job name>)` per notification and
   carry `notification_id` and `recipient`.

## Output (stdout)

One line per processed notification, then a summary line:

```text
sent      #12 ai-news a@example.org
failed    #13 ai-news b@example.org  SMTPRecipientsRefused: 550 mailbox unavailable
given up  #14 ai-news c@example.org  SMTPConnectError: …
retried 3, sent 1, failed 1, given up 1
```

If there are no candidates, the only output is:

```text
nothing to retry
```

Error texts are already scrubbed (no SMTP password or user).

## Exit codes

| Code | When |
|------|------|
| 0 | No candidates, or every processed notification ended `sent` |
| 1 | At least one processed notification ended `failed` or `skipped` (given up); also an unexpected internal error, which prints `Error: <Type>: <scrubbed message>` with no traceback |
| 2 | Configuration error (database URL, SMTP host or sender missing) |
