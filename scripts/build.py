"""
Сборка статической версии виджета долгов NEZEFIR.

Тянет данные из 2 листов Google Sheets (Долги, Выплаты) через gspread+
service-account, фильтрует приватные колонки (реальное имя, user_id,
link-токены) и встраивает безопасный JSON прямо в HTML как inline-data.

После сборки страница рендерится мгновенно из inline-data, без походов
в sheets.googleapis.com. Браузер потом фоном опрашивает /data.json
(same-origin) каждые 60 сек для свежих обновлений.

Запуск локально:
    python3 scripts/build.py

В CI: переменная GOOGLE_SA_JSON содержит JSON SA, читается напрямую.
Локально: читается ../service_account.json или ./service_account.json.
"""
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

ROOT = Path(__file__).resolve().parent.parent
SRC_HTML = ROOT / 'index.html'
DST_DIR = ROOT / 'dist'
DST_HTML = DST_DIR / 'index.html'

SHEET_ID = '1TssnHiSjja9nFSP1-kh1Ed2MhIkVt1eFOZNq8f7u4Ak'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']

# Карта типов: в Sheets русские, в виджете технические
TYPE_MAP = {
    'долг':   'static',
    'займ':   'loan',
    'кредит': 'credit',
}


def get_credentials() -> Credentials:
    sa_json_str = os.environ.get('GOOGLE_SA_JSON')
    if sa_json_str:
        return Credentials.from_service_account_info(json.loads(sa_json_str), scopes=SCOPES)
    # Локальный поиск JSON в типичных местах
    candidates = [
        ROOT / 'service_account.json',
        Path.home() / 'Desktop' / 'nezefir_debt_bot' / 'service_account.json',
    ]
    for p in candidates:
        if p.exists():
            return Credentials.from_service_account_file(str(p), scopes=SCOPES)
    raise SystemExit(
        'Не найден service_account.json. Положи в репо или задай переменную '
        'GOOGLE_SA_JSON со содержимым JSON.'
    )


def safe_float(v) -> float:
    """ru-RU локаль: '46078,24' → 46078.24. Числа из UNFORMATTED_VALUE приходят
    числами, но защищаемся от текста."""
    if v is None or v == '':
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(' ', '').replace(' ', '').replace(',', '.'))
    except (ValueError, TypeError):
        return 0.0


def normalize_type(t: str) -> str:
    return TYPE_MAP.get((t or '').strip().lower(), 'static')


def fetch_debts(sh) -> list[dict]:
    """Долги!A2:N — но в JSON выводим ТОЛЬКО публичные поля.
    Дропаем колонку A (реальное имя) и K-N (user_id, username, токены).
    """
    ws = sh.worksheet('Долги')
    rows = ws.get('A2:N', value_render_option='UNFORMATTED_VALUE')
    out = []
    for r in rows:
        # Пад до 14 колонок
        r = (list(r) + [''] * 14)[:14]
        nick = (str(r[1]) if r[1] is not None else '').strip()
        # Дропаем строки без ника — там либо мусор, либо приватные данные
        if not nick:
            continue
        out.append({
            'name':         nick,                                      # B (ник, публичный)
            'type':         normalize_type(str(r[2]) if r[2] else ''),  # C
            'amount':       safe_float(r[3]),                           # D
            'rate':         safe_float(r[4]),                           # E
            'startDate':    str(r[5]) if r[5] else '',                  # F
            'endDate':      str(r[6]) if r[6] else '',                  # G
            'note':         str(r[7]) if r[7] else '',                  # H
            'status':       (str(r[8]) if r[8] else 'active').lower().strip(),  # I
            'paidInterest': safe_float(r[9]),                            # J
            # K-N (user_id, username, link_code, link_url) — НЕ выводим, приватка
        })
    return out


def fetch_payments(sh) -> list[dict]:
    ws = sh.worksheet('Выплаты')
    rows = ws.get('A2:F', value_render_option='UNFORMATTED_VALUE')
    out = []
    for r in rows:
        r = (list(r) + [''] * 6)[:6]
        creditor = (str(r[1]) if r[1] else '').strip()
        if not creditor:
            continue
        date_val = r[0]
        # UNFORMATTED_VALUE для дат отдаёт serial-number — но мы хранили даты как
        # текст ISO, так что обычно тут строка "2026-04-29".
        date_str = str(date_val) if date_val else ''
        out.append({
            'date':      date_str,
            'creditor':  creditor,
            'amount':    safe_float(r[2]),
            'type':      str(r[3]) if r[3] else '',
            'remainder': safe_float(r[4]),
            'note':      str(r[5]) if r[5] else '',
        })
    # Хронологически: новые сначала
    out.reverse()
    return out


def main() -> int:
    print('Авторизация в Google Sheets…')
    creds = get_credentials()
    sh = gspread.authorize(creds).open_by_key(SHEET_ID)

    print('Тянем Долги и Выплаты…')
    debts = fetch_debts(sh)
    payments = fetch_payments(sh)

    data = {
        'debts': debts,
        'payments': payments,
        'built_at': datetime.now().isoformat(timespec='seconds'),
    }

    if not debts:
        print('ВНИМАНИЕ: список долгов пустой — что-то не так с парсингом', file=sys.stderr)
        return 2

    print('Встраиваем в HTML…')
    if not SRC_HTML.exists():
        print(f'ОШИБКА: {SRC_HTML} не найден', file=sys.stderr)
        return 3

    html = SRC_HTML.read_text(encoding='utf-8')
    pattern = re.compile(
        r'<script id="data"[^>]*>.*?</script>',
        re.DOTALL,
    )
    inline = (
        '<script id="data" type="application/json">'
        + json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        + '</script>'
    )
    # ВАЖНО: lambda вместо строки-замены, иначе re.subn интерпретирует \n / \1
    # внутри inline JSON как backreferences и превращает в реальные newlines.
    new_html, n = pattern.subn(lambda _m: inline, html, count=1)
    if n == 0:
        print(
            'ОШИБКА: в index.html не найден placeholder <script id="data">. '
            'Добавь его перед основным <script>.',
            file=sys.stderr,
        )
        return 4

    DST_DIR.mkdir(parents=True, exist_ok=True)
    DST_HTML.write_text(new_html, encoding='utf-8')

    # Same-origin JSON для periodic poll'а из браузера
    (DST_DIR / 'data.json').write_text(
        json.dumps(data, ensure_ascii=False, separators=(',', ':')),
        encoding='utf-8',
    )

    total_debt = sum(d['amount'] for d in debts if d['status'] != 'closed')
    total_paid = sum(p['amount'] for p in payments)
    print(f'Готово: {DST_HTML}')
    print(f'  долгов: {len(debts)} (активных тело: {total_debt:,.0f} ₽)')
    print(f'  выплат: {len(payments)} (всего выплачено: {total_paid:,.0f} ₽)')
    print(f'  data.json: {(DST_DIR / "data.json").stat().st_size} байт')
    return 0


if __name__ == '__main__':
    sys.exit(main())
