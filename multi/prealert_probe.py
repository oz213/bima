"""Operator-run Telegram multipart screenshot smoke test before enabling gate.

Requires TELEGRAM_BOT_TOKEN and BIMA_PREALERT_REVIEWER_CHAT_ID in environment.
Never sends an alert to the BIMA endpoint or modifies the crawler database.
"""
import base64
import os
import sys
from selective_prealert import _send_document

# Valid 1x1 transparent PNG solely for testing multipart upload connectivity.
SMOKE_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBg'
    'AAAABQABpfZFQAAAAABJRU5ErkJggg==')


def main():
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.environ.get('BIMA_PREALERT_REVIEWER_CHAT_ID', '').strip()
    if not token or not chat.isdecimal():
        print('Configure TELEGRAM_BOT_TOKEN and numeric private reviewer ID', file=sys.stderr)
        return 2
    try:
        result = _send_document(token,chat,SMOKE_PNG,
                                'BIMA Pre-Alert | TEST upload screenshot (bukan kasus judol)',
                                {'inline_keyboard': []})
    except Exception as error:
        print('Telegram upload failed:',type(error).__name__,file=sys.stderr)
        return 1
    if not (result.get('document') or {}).get('file_id'):
        print('Telegram accepted call but document file_id missing',file=sys.stderr)
        return 1
    print('Telegram sendDocument POST succeeded; screenshot received')
    return 0


if __name__ == '__main__':
    sys.exit(main())
