#!/usr/bin/env python3
"""Запасной сторож: проверяет один сайт с серверов GitHub и пишет в Telegram, когда сайт упал и когда поднялся.

Зачем: основной сторож живёт на сервере сайта и молчит, если у сервера пропала сеть. Этот живёт отдельно.

Как решает:
- сайт жив, только если страница проверки отвечает 200 и в ответе есть "ok": true (отказ по лимиту запросов тоже жив);
- тревога после двух неудачных запусков подряд (запуски раз в 10 минут, в каждом три попытки);
- пока сайт лежит, напоминание раз в 3 часа; когда поднялся, отбой с длительностью;
- Telegram не принял сообщение: состояние сохраняется, запуск кончается ошибкой (GitHub пришлёт письмо).

Память между запусками: state.json (ветка state). Файл меняется только при событии.
Настройки из окружения: WATCH_URL, WATCH_NAME, WATCH_MAIN, TG_TOKEN, TG_CHAT. Секреты и адрес сайта в журнал не пишутся.
"""
import datetime as dt
import html
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ATTEMPTS = 3                       # попыток за один запуск
PAUSE = 20                         # секунд между попытками
TIMEOUT = 20                       # секунд на ответ сайта
REMIND = dt.timedelta(hours=3)     # как часто напоминать, пока сайт лежит
FLAP = dt.timedelta(hours=1)       # упал снова в течение часа после отбоя: ночью без звука
KEEPALIVE = dt.timedelta(days=7)   # раз в неделю запись в ветку state
MSK = dt.timezone(dt.timedelta(hours=3))

SILENT = 'сайт не отвечает'  # только при этой причине уместна подсказка про пропавшую сеть
BROKEN = 'сайт отвечает ошибкой вместо страницы'


class Config(Exception):
    """Ошибка настроек сторожа: запуск должен стать красным."""


def now_utc():
    fake = os.environ.get('WATCH_NOW')  # только для проверок на своей машине
    return dt.datetime.fromisoformat(fake) if fake else dt.datetime.now(dt.timezone.utc)


def parse(value):
    try:
        at = dt.datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    return at if at is None or at.tzinfo else at.replace(tzinfo=dt.timezone.utc)


def night(at):
    return not 9 <= at.astimezone(MSK).hour < 22


def span(start, end):
    minutes = max(1, int((end - start).total_seconds() // 60))
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f'{minutes} мин'
    return f'{hours} ч {rest} мин' if rest else f'{hours} ч'


def moment(at, other):
    """Время по Москве; дата добавляется, только если события в разные дни."""
    local, ref = at.astimezone(MSK), other.astimezone(MSK)
    return local.strftime('%H:%M') if local.date() == ref.date() else local.strftime('%d.%m %H:%M')


def probe_once(url):
    """(жив, причина простыми словами). Адрес сайта и номера ошибок в причину не попадают."""
    fake = os.environ.get('WATCH_FAKE_PROBE')
    if fake:
        return (True, '') if fake == 'up' else (False, {'down': SILENT, 'broken': BROKEN}.get(fake, SILENT))
    request = urllib.request.Request(url, headers={'User-Agent': 'site-watch'})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            code, body = response.status, response.read(2000).decode('utf-8', 'replace')
    except urllib.error.HTTPError as error:
        code, body = error.code, ''
    except urllib.error.URLError as error:
        cause = error.reason
        if isinstance(cause, ssl.SSLError):
            return False, 'у сайта не в порядке сертификат (замок в адресной строке), браузеры его не откроют'
        if isinstance(cause, socket.gaierror):
            return False, 'адрес сайта не находится в интернете'
        return False, SILENT
    except ssl.SSLError:
        return False, 'у сайта не в порядке сертификат (замок в адресной строке), браузеры его не откроют'
    except Exception:  # обрыв, не ответил вовремя
        return False, SILENT
    if code == 429:
        return True, ''  # сайт сам отказал по лимиту запросов: значит работает
    if code == 200:
        try:
            if json.loads(body).get('ok') is True:
                return True, ''
        except (ValueError, AttributeError):
            pass
    return False, BROKEN  # ошибка сервера, чужая страница вместо сайта, заглушка хостинга


def check_url(url):
    try:
        parts = urllib.parse.urlparse(url)
        good = parts.scheme in ('http', 'https') and bool(parts.hostname) and url.isascii() and ' ' not in url
    except ValueError:
        good = False
    if not good:
        raise Config('адрес сайта в настройках записан неверно')


def probe(url):
    if not os.environ.get('WATCH_FAKE_PROBE'):
        check_url(url)
    reason = ''
    for attempt in range(ATTEMPTS):
        ok, reason = probe_once(url)
        if ok:
            return True, ''
        if attempt + 1 < ATTEMPTS and not os.environ.get('WATCH_FAKE_PROBE') and not os.environ.get('WATCH_NO_PAUSE'):
            time.sleep(PAUSE)
    return False, reason


def send(text, silent):
    """True, если Telegram принял сообщение. Ключ бота в журнал не печатается."""
    fake = os.environ.get('WATCH_FAKE_TG')
    if fake:
        if os.environ.get('WATCH_FAKE_TG_DOWN'):
            return False
        with open(fake, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'text': text, 'silent': silent}, ensure_ascii=False) + '\n')
        return True
    token, chat = os.environ.get('TG_TOKEN', ''), os.environ.get('TG_CHAT', '')
    if not token or not chat:
        print('telegram: нет настроек бота')
        return False
    body = json.dumps({'chat_id': chat, 'text': text, 'parse_mode': 'HTML', 'disable_notification': silent,
                       'link_preview_options': {'is_disabled': True}}).encode()
    request = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', data=body,
                                     headers={'Content-Type': 'application/json'})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if json.load(response).get('ok') is True:
                    return True
        except urllib.error.HTTPError as error:
            print('telegram: отказ, код', error.code)  # 400, 401, 403: повтор не поможет
            return False
        except Exception as error:
            print('telegram: не доставлено,', type(error).__name__)
        if attempt == 0:
            time.sleep(3)
    return False


def texts(name, main, host):
    name = html.escape(name)
    where = f'в {html.escape(main)} тревоги нет' if main else 'основной сторож молчит'
    look = f'\nПроверь сам: открой {html.escape(host)} с телефона.' if host else ''

    def alert(reason, since, at, late, again=False):
        lines = [f'<b>🔴 {name}: сайт {"снова " if again else ""}не открывается</b>',
                 f'Запасной сторож: {reason}. Длится {span(since, at)}.']
        if reason == SILENT:
            lines.append(f'Если {where}, скорее всего у сервера пропала сеть: оттуда сообщение не отправить.')
        text = '\n'.join(lines) + look + f'\nРазбор: скажи Claude «разбери сбой сайта {name}».'
        return text + (f'\n<i>Пришло с опозданием: сбой с {moment(since, at)}.</i>' if late else '')

    def remind(since, at):
        return (f'<b>🔴 {name}: сайт всё ещё не открывается</b>\n'
                f'Запасной сторож: длится {span(since, at)}, с {moment(since, at)}.')

    def green(since, at):
        return (f'<b>🟢 {name}: сайт снова открывается</b>\n'
                f'Запасной сторож: сбой длился {span(since, at)} ({moment(since, at)} – {moment(at, since)}).')

    def drill(ok, reason):
        state = 'Сайт сейчас открывается.' if ok else f'Сейчас {reason}.'
        other = f'не в {html.escape(main)}' if main else 'отдельно от основного сторожа'
        silent = f'{html.escape(main)} замолчит' if main else 'основной сторож замолчит'
        return (f'<b>🧪 Проба запасного сторожа: он работает</b>\n{state}\n'
                f'Запасной сторож живёт не на сервере сайта и пишет сюда, {other}: '
                f'если у сервера пропадёт сеть, {silent}, а запасной нет.\n'
                f'Настоящая тревога начнётся со слов «🔴 {name}: сайт не открывается».')

    return alert, remind, green, drill


def run(state, at, url, name, main):
    """Меняет state. Возвращает (надо сохранить, Telegram подвёл)."""
    host = urllib.parse.urlparse(url).hostname or ''
    alert, remind, green, _ = texts(name, main, host)
    ok, reason = probe(url)
    print('check:', 'ok' if ok else 'fail')
    suspect, down = parse(state.get('suspect')), parse(state.get('down_since'))
    alerted = parse(state.get('alerted'))
    changed = failed = False
    forget = ('suspect', 'down_since', 'alerted', 'last_alert', 'up_since')

    if alerted and not down:  # память повреждена руками: начинаем с чистого листа
        for key in forget:
            state.pop(key, None)
        suspect = alerted = None
        changed = True
    up = parse(state.get('up_since'))
    if up and down:  # сайт уже оживал, но отбой тогда не ушёл: сначала досылаем его, прошлый сбой закрыт
        if send(green(down, up), night(at)):
            for key in forget:
                state.pop(key, None)
            state['last_recovery'] = up.isoformat()
            suspect = down = alerted = None
            changed = True
        else:
            return changed, True

    if ok:
        if down and alerted:
            if send(green(down, at), night(at)):
                state.update(last_recovery=at.isoformat())
            else:
                state['up_since'] = at.isoformat()
                changed = failed = True
        if not failed and (down or suspect):  # тревога до человека не дошла: отбой не шлём, просто забываем
            for key in forget:
                state.pop(key, None)
            changed = True
    elif not suspect and not down:
        state['suspect'] = at.isoformat()  # первая неудача: ждём второй запуск, пока молчим
        changed = True
    elif not alerted:
        since = down or suspect
        state['down_since'] = since.isoformat()
        state.pop('suspect', None)
        recovered = parse(state.get('last_recovery'))
        flapping = recovered is not None and since - recovered < FLAP
        if send(alert(reason, since, at, late=down is not None, again=flapping), flapping and night(at)):
            state['alerted'] = state['last_alert'] = at.isoformat()
        else:
            failed = True
        changed = True
    else:
        last = parse(state.get('last_alert')) or alerted
        if at - last >= REMIND:
            if send(remind(down, at), night(at)):
                state['last_alert'] = at.isoformat()
                changed = True
            else:
                failed = True

    alive = parse(state.get('keepalive'))
    if alive is None or at - alive >= KEEPALIVE:
        state['keepalive'] = at.isoformat()
        changed = True
    return changed, failed


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'state.json'
    url, name = os.environ.get('WATCH_URL', ''), os.environ.get('WATCH_NAME', '') or 'Сайт'
    main_bot = os.environ.get('WATCH_MAIN', '')
    if not url and not os.environ.get('WATCH_FAKE_PROBE'):
        print('настройки: нет адреса сайта')
        return 1
    try:
        if os.environ.get('WATCH_DRILL') == '1':  # проба: настоящая проверка, одно сообщение, память не трогаем
            ok, reason = probe(url)
            print('drill:', 'ok' if ok else 'fail')
            host = urllib.parse.urlparse(url).hostname or ''
            return 0 if send(texts(name, main_bot, host)[3](ok, reason), False) else 1
        try:
            with open(path, encoding='utf-8') as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        changed, failed = run(state, now_utc(), url, name, main_bot)
    except Config as error:
        print('настройки:', error)
        return 1
    if changed:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write('\n')
        print('state: записано')
    if failed:
        print('telegram: сообщение не ушло, запуск помечен ошибкой')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
