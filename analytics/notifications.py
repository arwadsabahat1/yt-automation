"""Optional Telegram delivery, invoked only by an explicit --send/--telegram command."""
import os
from pathlib import Path

import requests


def deliver(path: Path, send=False, post=None):
    message = path.read_text(encoding='utf-8')
    if not send:
        return {'sent': False, 'draft': str(path), 'characters': len(message)}
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat:
        raise ValueError('Telegram delivery needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID; the local report is already saved')
    try:
        response = (post or requests.post)(f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat, 'text': message[:4000], 'disable_web_page_preview': True}, timeout=20)
        result = response.json()
        if response.status_code != 200 or not result.get('ok'):
            raise RuntimeError('Delivery failed')
    except Exception:
        # requests exceptions can embed the bot token in their URL.
        raise ValueError('Telegram delivery was not confirmed; local report is saved. Check bot access before retrying.') from None
    return {'sent': True, 'message_id': result['result']['message_id']}
