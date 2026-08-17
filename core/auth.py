"""Google authorization, scoped to whatever the loaded config actually needs.

`attachments_downloader.py` keeps a read-only Gmail token at state/token.json.
Adding a calendar action means asking for a second scope, and requesting it
would re-consent and overwrite that file. So the token is cached per scope set:
a config with no Google actions asks for gmail.readonly and reuses the existing
token untouched; anything wider gets its own state/token_<hash>.json.
"""

import hashlib
import json
import logging
import sys

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from core.paths import BASE_DIR, CREDENTIALS_PATH, STATE_DIR, TOKEN_PATH

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"


class AuthError(Exception):
    """An authorization problem, carrying user-facing instructions.

    The message is meant to be printed as-is, with no traceback, and to end
    with the exact command that fixes it.
    """


def _boxed(title, body):
    rule = "-" * 74
    return "\n".join(["", rule, title, rule, body.rstrip(), rule])


def reauth_command():
    return f"{sys.executable or 'python'} pipelines_runner.py --reauth"


def _google_reason(err):
    args = getattr(err, "args", ())
    if args and isinstance(args[0], str):
        return args[0]
    return str(err)


def token_path_for(scopes):
    """Where the token for this scope set lives.

    The read-only Gmail scope on its own keeps state/token.json, so a pipelines
    config with only http actions signs in exactly once for both runners.
    """
    scopes = sorted(set(scopes))
    if scopes == [GMAIL_READONLY]:
        return TOKEN_PATH
    digest = hashlib.sha256(" ".join(scopes).encode()).hexdigest()[:12]
    return STATE_DIR / f"token_{digest}.json"


def token_expired_message(err, token_path):
    return _boxed(
        "Google authorization has expired",
        f"""\
Google rejected the saved login in {token_path.name}:

    {_google_reason(err)}

The refresh token can no longer be exchanged for access. The usual causes:

  * The OAuth consent screen is still in "Testing" mode -- Google expires
    those refresh tokens after 7 days. This is by far the most common one.
  * Access was revoked at https://myaccount.google.com/permissions
  * The account password changed, or the token went ~6 months unused.

To fix it, sign in again from a terminal on this machine (a browser window
will open, and the new token is saved for future runs including cron):

    cd {BASE_DIR}
    {reauth_command()}

To stop this from recurring every 7 days, publish the app: Google Cloud
Console -> APIs & Services -> OAuth consent screen -> Publish app. It stays
private to your own account; publishing only lifts the test-mode expiry.""",
    )


def _unreadable_token_message(err, token_path):
    return _boxed(
        "Saved Google login could not be read",
        f"""\
{token_path} exists but is not a usable token file:

    {err}

It was probably truncated or edited by hand. Delete it and sign in again:

    rm {token_path}
    cd {BASE_DIR}
    {reauth_command()}""",
    )


def _missing_credentials_message():
    return _boxed(
        "Missing OAuth client file",
        f"""\
Signing in needs {CREDENTIALS_PATH.name}, which is not at:

    {CREDENTIALS_PATH}

Download an OAuth client ID of type "Desktop app" from Google Cloud Console
(APIs & Services -> Credentials), rename the downloaded JSON file to
{CREDENTIALS_PATH.name}, and save it at that path. README.md section 1 has
the full walkthrough.""",
    )


def _non_interactive_message(why, token_path):
    return _boxed(
        "Google sign-in is needed, but this run cannot open a browser",
        f"""\
{why}

Signing in requires a browser and a terminal you can interact with, so an
unattended run (cron, launchd, a script with no terminal) cannot do it.

Run this once yourself, in a terminal on this machine:

    cd {BASE_DIR}
    {reauth_command()}

The refreshed token is written to {token_path.name}, and the unattended runs
will pick it up on their own from then on.""",
    )


def _scope_change_message(token_path, missing):
    return _boxed(
        "New Google permissions are needed",
        f"""\
This config uses an action that needs access {token_path.name} does not grant:

    {chr(10).join('    ' + s for s in sorted(missing)).strip()}

Sign in again to add it (a browser window will open):

    cd {BASE_DIR}
    {reauth_command()}

Your existing {TOKEN_PATH.name} is left alone -- the wider grant is saved
separately, so the attachments downloader keeps working either way.""",
    )


def get_credentials(scopes, force_reauth=False):
    scopes = sorted(set(scopes))
    token_path = token_path_for(scopes)
    creds = None

    if token_path.exists() and not force_reauth:
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        except (ValueError, json.JSONDecodeError) as e:
            raise AuthError(_unreadable_token_message(e, token_path)) from e

        # From the file, not from `creds.scopes`: passing `scopes` to
        # `from_authorized_user_file` *sets* that attribute, so comparing
        # against it would only ever compare the request to itself.
        try:
            granted = set(json.loads(token_path.read_text()).get("scopes") or [])
        except (OSError, ValueError):
            granted = set(scopes)
        missing = set(scopes) - granted
        if missing and not sys.stdin.isatty():
            raise AuthError(_scope_change_message(token_path, missing))

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logging.info("Access token expired; refreshing it.")
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise AuthError(token_expired_message(e, token_path)) from e
        else:
            creds = _run_consent_flow(scopes, token_path, force_reauth)
        STATE_DIR.mkdir(exist_ok=True)
        token_path.write_text(creds.to_json())

    return creds


def _run_consent_flow(scopes, token_path, force_reauth=False):
    if not CREDENTIALS_PATH.exists():
        raise AuthError(_missing_credentials_message())

    if not sys.stdin.isatty():
        why = (
            "Re-authorization was requested with --reauth."
            if force_reauth
            else f"There is no saved login in {token_path.name} that can be refreshed."
        )
        raise AuthError(_non_interactive_message(why, token_path))

    logging.info(f"Opening a browser to authorize: {', '.join(scopes)}")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), scopes)
    try:
        creds = flow.run_local_server(port=0)
    except GoogleAuthError as e:
        raise AuthError(
            _boxed(
                "Google sign-in did not complete",
                f"""\
The browser sign-in failed:

    {_google_reason(e)}

If the consent screen showed an "access blocked" or "app not verified"
error, check that your Google account is listed as a test user under
APIs & Services -> OAuth consent screen. Then try again:

    cd {BASE_DIR}
    {reauth_command()}""",
            )
        ) from e
    logging.info(f"Authorized. Token saved to {token_path}.")
    return creds


class Services:
    """Lazily-built Google API clients sharing one set of credentials."""

    def __init__(self, credentials):
        self._credentials = credentials
        self._cache = {}

    def _get(self, name, version):
        if name not in self._cache:
            # cache_discovery=False silences googleapiclient's "file_cache is
            # only supported with oauth2client<4.0.0" warning, which is noise
            # on every single run.
            self._cache[name] = build(
                name, version, credentials=self._credentials, cache_discovery=False
            )
        return self._cache[name]

    @property
    def gmail(self):
        return self._get("gmail", "v1")

    @property
    def calendar(self):
        return self._get("calendar", "v3")
