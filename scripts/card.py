#!/usr/bin/env python3
"""Карточка для закрепа канала НЕЗЕФИР: картинка dist/card.png и подпись dist/card.json.

Берёт уже собранный dist/data.json (scripts/build.py), считает цифры так же, как
мини-приложение (calcCurrentDebt в index.html), рисует карточку в «Чертеже»
через Chrome без окна. Бот на сервере раз в несколько минут читает card.json и
card.png из /var/www/lintu-debts и обновляет закреплённый пост.

Запуск:  python3 scripts/card.py            (после build.py)
Сбой здесь не должен ронять выкладку мини-приложения — шаг в workflow
помечен continue-on-error.
"""
import html
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / 'dist'
MSK = timezone(timedelta(hours=3))
W, H = 400, 314          # CSS-пиксели; картинка в 3 раза крупнее
SCALE = 3
MONTHS_GEN = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля',
              'августа', 'сентября', 'октября', 'ноября', 'декабря']
MONTHS_SHORT = ['ЯНВ', 'ФЕВ', 'МАР', 'АПР', 'МАЙ', 'ИЮН', 'ИЮЛ', 'АВГ', 'СЕН', 'ОКТ', 'НОЯ', 'ДЕК']
NB = ' '


def parse_dt(s):
    """Как new Date(s) в JS на сервере сборки (UTC): дата без времени — полночь UTC."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def current_debt(d, now):
    """Копия calcCurrentDebt из index.html — цифры карточки и приложения обязаны совпадать."""
    amount = float(d.get('amount') or 0)
    rate = float(d.get('rate') or 0)
    if not rate:
        return amount
    base = parse_dt(d.get('lastAccrual'))
    if base:
        days = max(0, math.floor((now - base).total_seconds() / 86400))
        return amount + max(0.0, float(d.get('carriedInterest') or 0) + amount * (rate / 100) * (days / 365))
    start = parse_dt(d.get('startDate'))
    if not start:
        return amount
    days = max(0, math.floor((now - start).total_seconds() / 86400))
    accrued = amount * (rate / 100) * (days / 365)
    return amount + max(0.0, accrued - float(d.get('paidInterest') or 0))


def money(n):
    return f'{round(n):,}'.replace(',', NB)


def short_mln(n):
    if n >= 1_000_000:
        return f'{n / 1_000_000:.2f}'.replace('.', ',') + f'{NB}млн'
    if n >= 1000:
        return f'{round(n / 1000)}{NB}тыс'
    return money(n)


def ddmm(s):
    d = parse_dt(s)
    return d.strftime('%d.%m') if d else str(s)


def compute(data, now):
    debts = data.get('debts') or []
    # В data.json выплаты от новых к старым; переворачиваем, чтобы в один день порядок остался верным
    pays = sorted(reversed(data.get('payments') or []), key=lambda p: str(p.get('date') or ''))
    open_ = [d for d in debts if str(d.get('status') or 'active').lower() != 'closed']
    total = sum(current_debt(d, now) for d in open_)
    paid = sum(float(p.get('amount') or 0) for p in pays)
    denom = total + paid
    per_day = sum(float(d.get('amount') or 0) * float(d.get('rate') or 0) / 100 / 365 for d in open_)
    reserved = sum(float(d.get('reserved') or 0) for d in open_)

    # Выплаты по месяцам: последние 7 месяцев до текущего включительно
    today = now.astimezone(MSK)
    months = []
    y, m = today.year, today.month
    for _ in range(7):
        months.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    first = parse_dt(pays[0]['date']) if pays else None
    if first:
        months = [ym for ym in months if ym >= (first.year, first.month)] or months[-1:]
    by_month = {ym: 0.0 for ym in months}
    for p in pays:
        d = parse_dt(p.get('date'))
        if d and (d.year, d.month) in by_month:
            by_month[(d.year, d.month)] += float(p.get('amount') or 0)

    last = pays[-1] if pays else None
    last_full = next((p for p in reversed(pays) if str(p.get('type', '')).startswith('полн')), None)
    avg = None
    if first and paid > 0:
        days = max(1, (today.date() - first.astimezone(MSK).date()).days + 1)
        v = paid / days
        avg = round(v, -3) if v >= 1000 else round(v, -2)

    # Выплаты месяца для свёрнутой цитаты: текущий месяц, а если в нём пусто — последний с выплатами
    cur = (today.year, today.month)
    month_pays = [p for p in pays if (lambda d: d and (d.year, d.month) == cur)(parse_dt(p.get('date')))]
    if not month_pays and last:
        ld = parse_dt(last['date'])
        cur = (ld.year, ld.month)
        month_pays = [p for p in pays if (lambda d: d and (d.year, d.month) == cur)(parse_dt(p.get('date')))]

    return {
        'now': today, 'total': total, 'paid': paid, 'pct': round(paid / denom * 100) if denom else 0,
        'closed': len(debts) - len(open_), 'count': len(debts), 'per_day': per_day,
        'reserved': reserved, 'months': [(ym, by_month[ym]) for ym in months],
        'last': last, 'last_full': last_full, 'avg': avg, 'first': first,
        'month': cur, 'month_pays': list(reversed(month_pays)),
    }


def caption(c):
    e = html.escape
    lines = []
    if c['last']:
        p = c['last']
        lines.append(f"📉 Последняя выплата — <b>{ddmm(p['date'])} · {money(float(p['amount']))}{NB}₽</b> · {e(p.get('creditor') or '')}")
    if c['last_full']:
        p = c['last_full']
        lines.append(f"✅ Последний закрытый долг — <b>{ddmm(p['date'])}</b> · {e(p.get('creditor') or '')}")
    if c['reserved'] >= 1:
        lines.append(f"💼 Отложено на выплаты — <b>{money(c['reserved'])}{NB}₽</b>")
    if c['avg'] and c['first']:
        lines.append(f"📊 Возвращаю <b>≈{NB}{money(c['avg'])}{NB}₽ в день</b> с {c['first'].strftime('%d.%m')}")
    out = '\n'.join(lines)
    mp = c['month_pays']
    if mp:
        y, m = c['month']
        s = sum(float(p['amount']) for p in mp)
        rows = []
        for p in mp[:12]:
            full = ' ✓' if str(p.get('type', '')).startswith('полн') else ''
            rows.append(f"{ddmm(p['date'])} · {e(p.get('creditor') or '')} · {money(float(p['amount']))}{full}")
        if len(mp) > 12:
            rows.append(f'…и ещё {len(mp) - 12}')
        out += (f"\n<blockquote expandable><b>Все выплаты {MONTHS_GEN[m - 1]} · {money(s)}{NB}₽</b>\n"
                + '\n'.join(rows) + '</blockquote>')
    return out


def card_html(c):
    top = max([v for _, v in c['months']] + [1])
    cur = c['months'][-1][0] if c['months'] else None
    bars, labels = [], []
    for ym, v in c['months']:
        h = max(4, round(v / top * 100)) if v > 0 else 3
        cls = 'bar now' if ym == cur else 'bar'
        bars.append(f'<div class="{cls}" style="height:{h}%"></div>')
        labels.append(f'<span{" class=hi" if ym == cur else ""}>{MONTHS_SHORT[ym[1] - 1]}</span>')
    ncols = len(c['months']) or 1
    pct = min(100, max(0, c['pct']))
    per_day = f"+{round(c['per_day'] / 1000)}{NB}тыс/сут" if c['per_day'] >= 1000 else f"+{money(c['per_day'])}{NB}₽/сут"
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Onest:wght@500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
html,body{{margin:0;width:{W}px;height:{H}px;overflow:hidden;background:#0A0D12}}
.card{{box-sizing:border-box;width:{W}px;height:{H}px;padding:16px 18px 14px;color:#E8ECF2;font-family:Onest,Arial,sans-serif;
background-color:#0A0D12;background-image:linear-gradient(rgba(120,160,255,.09) 1px,transparent 1px),linear-gradient(90deg,rgba(120,160,255,.09) 1px,transparent 1px),linear-gradient(rgba(120,160,255,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(120,160,255,.045) 1px,transparent 1px);
background-size:80px 80px,80px 80px,16px 16px,16px 16px;display:flex;flex-direction:column;gap:9px}}
.m{{font-family:'JetBrains Mono',monospace}}
.cl{{font-family:'JetBrains Mono',monospace;font-size:9.5px;letter-spacing:.08em;color:rgba(232,236,242,.56)}}
.row{{display:flex;justify-content:space-between}}
.sum{{display:flex;align-items:flex-start;gap:6px}}
.sum b{{font-size:40px;font-weight:600;letter-spacing:-.03em;line-height:1}}
.sum i{{font-style:normal;font-family:'JetBrains Mono',monospace;font-size:13px;color:rgba(232,236,242,.56);padding-top:4px}}
.rl{{position:relative;height:28px;margin-top:4px}}
.t1{{position:absolute;left:0;right:0;top:6px;height:5px;background-image:linear-gradient(90deg,rgba(232,236,242,.30) 1px,transparent 1px);background-size:2% 100%}}
.t2{{position:absolute;left:0;right:0;top:0;height:11px;background-image:linear-gradient(90deg,rgba(232,236,242,.22) 1px,transparent 1px);background-size:10% 100%;border-right:1px solid rgba(232,236,242,.22)}}
.fill{{position:absolute;left:0;width:{pct}%;top:13px;height:3px;background:#3D7BFF;box-shadow:0 0 6px 1px rgba(61,123,255,.85)}}
.ptr{{position:absolute;left:{pct}%;top:-3px;width:1px;height:21px;background:#FF6A3D}}
.rlab{{position:absolute;left:0;right:0;top:19px;display:flex;justify-content:space-between;font-size:8.5px;color:rgba(232,236,242,.36)}}
.tiles{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}}
.tile{{border:1px solid rgba(232,236,242,.13);border-radius:11px;padding:7px 9px;background:rgba(13,17,24,.6)}}
.tile div:last-child{{font-size:16px;font-weight:600;margin-top:2px;white-space:nowrap}}
.tile.or{{border-color:rgba(255,106,61,.5)}} .tile.or .cl{{color:#FF6A3D}}
.bars{{display:grid;grid-template-columns:repeat({ncols},minmax(0,1fr));gap:9px;align-items:end;height:62px}}
.bar{{width:100%;background:#3D7BFF;box-shadow:0 0 8px rgba(61,123,255,.7);border-radius:2px 2px 0 0}}
.bar.now{{background:transparent;border:1px dashed #8FB2FF;border-bottom:none;box-shadow:none;box-sizing:border-box}}
.lab{{display:grid;grid-template-columns:repeat({ncols},minmax(0,1fr));gap:9px;font-size:8.5px;color:rgba(232,236,242,.36);text-align:center}}
.hi{{color:#8FB2FF}}
</style></head><body><div class="card">
<div class="cl row"><span>НЕЗЕФИР · СЧЁТЧИК ДОЛГОВ</span><span>{c['now'].strftime('%d.%m')}</span></div>
<div class="sum"><b>{money(c['total'])}</b><i>₽</i></div>
<div class="rl"><div class="t1"></div><div class="t2"></div><div class="fill"></div><div class="ptr"></div>
<div class="rlab m"><span>ПОГАШЕНО {pct}{NB}%</span><span>100{NB}%</span></div></div>
<div class="tiles">
<div class="tile"><div class="cl">ВЫПЛАЧЕНО</div><div>{short_mln(c['paid'])}</div></div>
<div class="tile"><div class="cl">ЗАКРЫТО</div><div>{c['closed']}{NB}/{NB}{c['count']}</div></div>
<div class="tile or"><div class="cl">ПРОЦЕНТЫ</div><div>{per_day}</div></div>
</div>
<div class="cl" style="margin-top:2px">ВЫПЛАТЫ ПО МЕСЯЦАМ</div>
<div class="bars">{''.join(bars)}</div>
<div class="lab m">{''.join(labels)}</div>
</div></body></html>'''


def find_chrome():
    for name in (os.environ.get('CHROME'), 'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser',
                 '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'):
        if name and (shutil.which(name) or Path(name).exists()):
            return shutil.which(name) or name
    return None


def main():
    src = DIST / 'data.json'
    data = json.loads(src.read_text(encoding='utf-8'))
    now = datetime.now(timezone.utc)
    c = compute(data, now)
    page = DIST / 'card.html'
    page.write_text(card_html(c), encoding='utf-8')
    meta = {'built_at': data.get('built_at'), 'rendered_at': now.isoformat(timespec='seconds'),
            'caption': caption(c), 'total': round(c['total'])}
    chrome = find_chrome()
    if not chrome:
        print('card: Chrome не найден — картинки не будет', file=sys.stderr)
        return 1
    out = DIST / 'card.png'
    cmd = [chrome, '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
           f'--window-size={W},{H}', f'--force-device-scale-factor={SCALE}',
           '--virtual-time-budget=6000', f'--screenshot={out}', page.as_uri()]
    subprocess.run(cmd, check=True, timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not out.exists() or out.stat().st_size < 10_000:
        print('card: картинка не получилась', file=sys.stderr)
        return 1
    # card.json пишем последним: бот берёт картинку, только если подпись свежая
    (DIST / 'card.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    print(f"card: {out.stat().st_size // 1024} КБ, сумма {money(c['total'])}, подпись {len(meta['caption'])} знаков")
    return 0


if __name__ == '__main__':
    sys.exit(main())
