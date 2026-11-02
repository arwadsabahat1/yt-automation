"""Read-only Analytics OAuth. Existing upload credentials are never overwritten."""
import json
from pathlib import Path

from core.config import DATA_DIR
from publishing.uploader import save

PRIVATE = DATA_DIR / 'private' / 'analytics'
READ = 'https://www.googleapis.com/auth/youtube.readonly'
ANALYTICS = 'https://www.googleapis.com/auth/yt-analytics.readonly'
MONETARY = 'https://www.googleapis.com/auth/yt-analytics-monetary.readonly'


class AnalyticsUnavailable(ValueError):
    pass


def accept_scope_response(flow, warning, required):
    """Google may include permissions previously granted to the same desktop client."""
    granted = set(getattr(warning, 'new_scope', []))
    token = getattr(warning, 'token', None)
    if not token or not set(required).issubset(granted):
        raise AnalyticsUnavailable('Google did not grant all required read-only permissions; reconnect and select both') from None
    flow.oauth2session.token = token
    flow.oauth2session.scope = sorted(granted)
    return flow.credentials


def authenticate(client_secrets: Path, channel: str, monetary=False):
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    scopes = [READ, ANALYTICS] + ([MONETARY] if monetary else [])
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), scopes)
    try:
        creds = flow.run_local_server(host='127.0.0.1', port=0, open_browser=True, timeout_seconds=300,
            authorization_prompt_message='Approve the read-only Analytics connection in your browser.',
            success_message='Consent received. Return to Codex while the connection is verified.')
    except Warning as warning:
        creds = accept_scope_response(flow, warning, scopes)
    if creds.granted_scopes is not None and not set(scopes).issubset(creds.granted_scopes):
        raise AnalyticsUnavailable('Google did not grant all requested read-only permissions')
    data = build('youtube', 'v3', credentials=creds, cache_discovery=False)
    verify_channel(data, channel)
    save(PRIVATE / 'token.json', json.loads(creds.to_json()))


def verify_channel(data, expected):
    actual = [r['id'] for r in data.channels().list(part='id', mine=True).execute().get('items', [])]
    if actual != [expected]:
        raise AnalyticsUnavailable('Analytics connection belongs to a different channel; reconnect with YT Automation selected')


def services(channel):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    token = PRIVATE / 'token.json'
    if not token.exists():
        raise AnalyticsUnavailable('Read-only Analytics consent needed: main.py analytics auth --client-secrets PATH')
    creds = Credentials.from_authorized_user_file(str(token))
    if not creds.has_scopes([READ, ANALYTICS]):
        raise AnalyticsUnavailable('Read-only Analytics scopes missing; run analytics auth again')
    if not creds.valid:
        if not creds.refresh_token:
            raise AnalyticsUnavailable('Analytics consent expired; run analytics auth again')
        creds.refresh(Request())
        save(token, json.loads(creds.to_json()))
    data = build('youtube', 'v3', credentials=creds, cache_discovery=False)
    verify_channel(data, channel)
    return data, build('youtubeAnalytics', 'v2', credentials=creds, cache_discovery=False), creds.has_scopes([MONETARY])


def error_status(exc):
    """Never surface HttpError URLs, OAuth responses or other credential-bearing exceptions."""
    code = getattr(getattr(exc, 'resp', None), 'status', None)
    if code == 403:
        try:
            error = json.loads(getattr(exc, 'content', b'{}')).get('error', {})
            reasons = {entry.get('reason') for entry in error.get('errors', [])}
            if reasons & {'accessNotConfigured', 'serviceDisabled'}:
                return 'unavailable', 'YouTube Analytics API is disabled in the Google Cloud project; enable youtubeanalytics.googleapis.com'
        except (ValueError, TypeError):
            pass
        return 'unavailable', 'Access denied: check Analytics API enablement, consent and (for revenue) YPP eligibility'
    if code == 401:
        return 'unavailable', 'Analytics consent expired; reconnect'
    if code == 400:
        return 'unsupported', 'YouTube did not accept this report combination'
    return 'error', f'{type(exc).__name__}: refresh incomplete; previous reports remain available'


def query(api, channel, start, end, metrics, dimensions=None, video=None):
    args = {'ids': f'channel=={channel}', 'startDate': str(start), 'endDate': str(end), 'metrics': metrics,
            'maxResults': 200}
    if dimensions:
        args['dimensions'] = dimensions
    if video:
        args['filters'] = f'video=={video}'
    result = []
    while True:
        response = api.reports().query(**args).execute()
        keys = [h['name'] for h in response.get('columnHeaders', [])]
        rows = response.get('rows', [])
        if any(len(r) != len(keys) for r in rows):
            raise ValueError('Malformed Analytics report')
        result.extend(dict(zip(keys, row)) for row in rows)
        if len(rows) < args['maxResults']:
            return result
        args['startIndex'] = args.get('startIndex', 1) + len(rows)
