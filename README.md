# NEZEFIR Widget

Публичный счётчик долгов канала [@NEZEFIR](https://t.me/NEZEFIR), оформленный
как Telegram Mini App в боте [@nezefir_debt_bot](https://t.me/nezefir_debt_bot).

## Архитектура

```
Google Sheets (приватная таблица NEZEFIR)
        │
        ▼
GitHub Actions (cron 15 мин · push · workflow_dispatch · repository_dispatch)
        │
        ▼
scripts/build.py (gspread + Service Account)
   ├─ читает листы Долги и Выплаты
   ├─ дропает приватные колонки (ФИО, user_id, link-токены)
   └─ пишет:
       ├─ dist/index.html  ← inline-data, мгновенный рендер
       └─ dist/data.json   ← same-origin polling
        │
        ▼
Cloudflare Pages (nezefir-widget.pages.dev)
        │
        ▼
TG WebApp (Mini App в @nezefir_debt_bot)
```

## Локальная разработка

```bash
# Положи service_account.json в корень или прописать GOOGLE_SA_JSON env
python3 scripts/build.py
open dist/index.html
```

## Деплой

Push в `main` → GH Actions сам собирает и деплоит на CF Pages.

## Секреты в GitHub Actions

- `GOOGLE_SA_JSON` — содержимое service_account.json одной строкой
- `CLOUDFLARE_API_TOKEN` — токен Cloudflare с правами Pages:Edit
- `CLOUDFLARE_ACCOUNT_ID` — ID аккаунта Cloudflare

## Безопасность

В публичный `dist/data.json` попадают **только**:
- Из листа `Долги`: ник, тип, сумма, ставка, даты, статус, выплаченные%
- Из листа `Выплаты`: всё (там и так только ники)

Колонки реального имени, TelegramUserID, username, LinkCode, LinkURL —
никогда не выводятся. Фильтрация в `scripts/build.py:fetch_debts()`.
