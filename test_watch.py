#!/usr/bin/env python3
"""Проверка сторожа на придуманных сбоях: без интернета и без Telegram, с подставным временем.
Запуск: python3 -I test_watch.py
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
failed = []


def step(tmp, at, probe=None, tg_down=False, drill=False, url=None, expect=0):
    env = {'PATH': os.environ['PATH'], 'WATCH_NOW': at, 'WATCH_NAME': 'Проба', 'WATCH_MAIN': 'ГЛАВНЫЙ_БОТ',
           'WATCH_FAKE_TG': os.path.join(tmp, 'tg.jsonl'), 'WATCH_NO_PAUSE': '1'}
    if probe:
        env['WATCH_FAKE_PROBE'] = probe
    if url:
        env['WATCH_URL'] = url
    if tg_down:
        env['WATCH_FAKE_TG_DOWN'] = '1'
    if drill:
        env['WATCH_DRILL'] = '1'
    result = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'watch.py'), os.path.join(tmp, 'state.json')],
                            env=env, capture_output=True, text=True)
    assert result.returncode == expect, (result.returncode, result.stdout + result.stderr)
    return result.stdout


def messages(tmp):
    path = os.path.join(tmp, 'tg.jsonl')
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f]


def state(tmp):
    with open(os.path.join(tmp, 'state.json'), encoding='utf-8') as f:
        return json.load(f)


def check(name, condition):
    print('OK  ' if condition else 'FAIL', name)
    if not condition:
        failed.append(name)


def case_outage():
    with tempfile.TemporaryDirectory() as tmp:
        step(tmp, '2026-11-13T20:47:00+00:00', 'up')
        check('сайт в порядке: сообщений нет', messages(tmp) == [])
        step(tmp, '2026-11-13T20:57:00+00:00', 'down')  # 23:57 по Москве, ночь
        check('первая неудача: пока молчит', messages(tmp) == [] and 'suspect' in state(tmp))
        step(tmp, '2026-11-13T21:07:00+00:00', 'down')
        m = messages(tmp)
        check('вторая неудача подряд: красная тревога', len(m) == 1 and m[0]['text'].startswith('<b>🔴 Проба: сайт не открывается</b>'))
        check('в тревоге: сколько длится, считая с первой неудачи', 'сайт не отвечает. Длится 10 мин.' in m[0]['text'])
        check('в тревоге: подсказка про главного бота и пропавшую сеть', 'Если в ГЛАВНЫЙ_БОТ тревоги нет' in m[0]['text'])
        check('в тревоге: что сказать Claude', 'скажи Claude «разбери сбой сайта Проба»' in m[0]['text'])
        check('тревога звонит и ночью', m[0]['silent'] is False)
        step(tmp, '2026-11-13T21:17:00+00:00', 'down')
        step(tmp, '2026-11-14T00:00:00+00:00', 'down')
        check('меньше 3 часов после тревоги: молчит', len(messages(tmp)) == 1)
        step(tmp, '2026-11-14T00:07:00+00:00', 'down')
        m = messages(tmp)
        check('через 3 часа: напоминание', len(m) == 2 and 'всё ещё не открывается' in m[1]['text'] and 'длится 3 ч 10 мин' in m[1]['text'])
        check('напоминание ночью без звука', m[1]['silent'] is True)
        step(tmp, '2026-11-15T10:27:00+00:00', 'up')
        m = messages(tmp)
        check('поднялся: зелёное с длительностью', m[-1]['text'].startswith('<b>🟢 Проба: сайт снова открывается</b>')
              and 'Запасной сторож: сбой длился 37 ч 30 мин (13.11 23:57 – 15.11 13:27).' in m[-1]['text'])
        check('после отбоя сбой забыт', not {'down_since', 'suspect', 'alerted'} & set(state(tmp)))
        before = len(messages(tmp))
        step(tmp, '2026-11-15T10:37:00+00:00', 'up')
        check('дальше тихо', len(messages(tmp)) == before)
        check('длинного тире в сообщениях нет', all(chr(8212) not in x['text'] for x in messages(tmp)))


def case_blip():
    with tempfile.TemporaryDirectory() as tmp:
        step(tmp, '2026-11-14T23:00:00+00:00', 'down')
        step(tmp, '2026-11-14T23:10:00+00:00', 'up')
        check('одна неудача и сайт ожил: ни одного сообщения', messages(tmp) == [] and 'suspect' not in state(tmp))


def case_telegram_down():
    with tempfile.TemporaryDirectory() as tmp:
        step(tmp, '2026-11-14T09:00:00+00:00', 'down')
        step(tmp, '2026-11-14T09:10:00+00:00', 'down', tg_down=True, expect=1)
        check('Telegram не принял тревогу: запуск красный, сбой запомнен',
              state(tmp).get('down_since') and not state(tmp).get('alerted'))
        step(tmp, '2026-11-14T09:20:00+00:00', 'down')
        m = messages(tmp)
        check('следующий запуск досылает полную тревогу', len(m) == 1 and 'скажи Claude' in m[0]['text'])
        check('в досланной сказано, что опоздала', 'Пришло с опозданием: сбой с 12:00.' in m[0]['text'])
        step(tmp, '2026-11-14T09:30:00+00:00', 'up', tg_down=True, expect=1)
        check('отбой не дошёл: запуск красный, сбой не забыт', 'down_since' in state(tmp))
        step(tmp, '2026-11-14T09:40:00+00:00', 'up')
        check('отбой дослан', messages(tmp)[-1]['text'].startswith('<b>🟢') and 'down_since' not in state(tmp))


def case_alert_never_delivered():
    with tempfile.TemporaryDirectory() as tmp:
        step(tmp, '2026-11-14T09:00:00+00:00', 'down')
        step(tmp, '2026-11-14T09:10:00+00:00', 'down', tg_down=True, expect=1)
        step(tmp, '2026-11-14T09:20:00+00:00', 'up')
        check('тревога не дошла и сайт ожил: отбоя про неизвестный сбой нет', messages(tmp) == [])
        check('сбой забыт', 'down_since' not in state(tmp))


def case_drill():
    with tempfile.TemporaryDirectory() as tmp:
        step(tmp, '2026-11-15T11:00:00+00:00', 'up', drill=True)
        m = messages(tmp)
        check('проба: одно сообщение, с первой строки ясно, что проба',
              len(m) == 1 and m[0]['text'].startswith('<b>🧪 Проба запасного сторожа') and 'Сайт сейчас открывается.' in m[0]['text'])
        check('проба не трогает память', not os.path.exists(os.path.join(tmp, 'state.json')))
        step(tmp, '2026-11-15T11:00:00+00:00', 'down')
        step(tmp, '2026-11-15T11:10:00+00:00', 'down')
        step(tmp, '2026-11-15T11:12:00+00:00', 'down', drill=True)
        m = messages(tmp)
        check('проба во время настоящего сбоя говорит правду', 'Сейчас сайт не отвечает.' in m[-1]['text'])
        check('и настоящий сбой не сбит', 'down_since' in state(tmp))
        step(tmp, '2026-11-15T11:13:00+00:00', 'up', drill=True, tg_down=True, expect=1)
        check('проба при сломанном Telegram: запуск красный', True)


def case_flap():
    with tempfile.TemporaryDirectory() as tmp:
        for at, probe in (('2026-11-14T22:00:00+00:00', 'down'), ('2026-11-14T22:10:00+00:00', 'down'),
                          ('2026-11-14T22:20:00+00:00', 'up'), ('2026-11-14T22:30:00+00:00', 'down'),
                          ('2026-11-14T22:40:00+00:00', 'down')):
            step(tmp, at, probe)
        m = messages(tmp)
        check('мигание ночью: вторая тревога в течение часа без звука',
              len(m) == 3 and m[0]['silent'] is False and m[2]['silent'] is True
              and m[2]['text'].startswith('<b>🔴 Проба: сайт снова не открывается</b>'))


def case_green_lost_then_down_again():
    with tempfile.TemporaryDirectory() as tmp:
        step(tmp, '2026-11-14T10:00:00+00:00', 'down')
        step(tmp, '2026-11-14T10:10:00+00:00', 'down')
        step(tmp, '2026-11-14T10:20:00+00:00', 'up', tg_down=True, expect=1)
        check('отбой не ушёл: время оживления запомнено', 'up_since' in state(tmp))
        step(tmp, '2026-11-14T15:00:00+00:00', 'down')
        m = messages(tmp)
        check('сайт упал снова: сначала дослан отбой прошлого сбоя с верным концом',
              len(m) == 2 and 'сбой длился 20 мин (13:00 – 13:20)' in m[1]['text'])
        check('новый сбой начат заново, с первой неудачи', state(tmp).get('suspect', '').startswith('2026-11-14T15:00')
              and 'down_since' not in state(tmp))
        step(tmp, '2026-11-14T15:10:00+00:00', 'down')
        check('новая тревога считает длительность нового сбоя', 'Длится 10 мин.' in messages(tmp)[-1]['text'])


def case_late_alert_then_reminder():
    with tempfile.TemporaryDirectory() as tmp:
        step(tmp, '2026-11-14T09:00:00+00:00', 'down')
        for minute in ('10', '20', '30'):
            step(tmp, f'2026-11-14T09:{minute}:00+00:00', 'down', tg_down=True, expect=1)
        step(tmp, '2026-11-14T09:40:00+00:00', 'down')
        step(tmp, '2026-11-14T12:30:00+00:00', 'down')
        check('после досланной тревоги 3 часа тихо', len(messages(tmp)) == 1)
        step(tmp, '2026-11-14T12:40:00+00:00', 'down')
        m = messages(tmp)
        check('напоминание через 3 часа после досланной тревоги', len(m) == 2 and 'длится 3 ч 40 мин' in m[1]['text'])


def case_half_broken_state():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, 'state.json'), 'w') as f:
            json.dump({'alerted': '2026-11-14T08:00:00+00:00', 'last_alert': '2026-11-14T08:00:00+00:00'}, f)
        step(tmp, '2026-11-14T09:00:00+00:00', 'down')
        step(tmp, '2026-11-14T09:10:00+00:00', 'down')
        check('повреждённая память: не падает и шлёт обычную тревогу', len(messages(tmp)) == 1)


def case_broken():
    with tempfile.TemporaryDirectory() as tmp:
        step(tmp, '2026-11-14T09:00:00+00:00', 'broken')
        step(tmp, '2026-11-14T09:10:00+00:00', 'broken')
        text = messages(tmp)[0]['text']
        check('сайт отвечает ошибкой: без номера ошибки и без подсказки про сеть',
              'сайт отвечает ошибкой вместо страницы. Длится 10 мин.' in text and 'пропала сеть' not in text and not any(c in text for c in ('500', '502', '503')))


def case_state_garbage():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, 'state.json'), 'w') as f:
            f.write('[]')
        step(tmp, '2026-11-14T09:00:00+00:00', 'up')
        check('битая память: начинает с чистого листа', isinstance(state(tmp), dict))


def case_keepalive():
    with tempfile.TemporaryDirectory() as tmp:
        step(tmp, '2026-11-01T00:00:00+00:00', 'up')
        first = state(tmp)['keepalive']
        step(tmp, '2026-11-05T00:00:00+00:00', 'up')
        check('меньше недели: файл не трогается', state(tmp)['keepalive'] == first)
        step(tmp, '2026-11-08T00:10:00+00:00', 'up')
        check('через неделю: отметка обновилась', state(tmp)['keepalive'] != first)


class Handler(http.server.BaseHTTPRequestHandler):
    pages = {'/ok': (200, '{"ok":true}'), '/notok': (200, '{"ok":false}'), '/stub': (200, '<html>заглушка</html>'),
             '/404': (404, 'нет'), '/403': (403, 'нет'), '/429': (429, '{"ok":false}'), '/503': (503, '{"ok":false}')}

    def do_GET(self):
        code, body = self.pages.get(self.path, (404, ''))
        data = body.encode()
        self.send_response(code)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def case_real_probe():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    for path, alive in (('/ok', True), ('/429', True), ('/notok', False), ('/stub', False), ('/404', False),
                        ('/403', False), ('/503', False)):
        with tempfile.TemporaryDirectory() as tmp:
            out = step(tmp, '2026-11-14T09:00:00+00:00', url=base + path)
            check(f'настоящая проверка {path}: {"жив" if alive else "сбой"}', ('check: ok' in out) == alive)
    server.shutdown()
    with tempfile.TemporaryDirectory() as tmp:
        out = step(tmp, '2026-11-14T09:00:00+00:00', url='http://127.0.0.1:9/')
        check('соединение отклонено: сбой', 'check: fail' in out)
    for bad in ('example.invalid/без-https', 'https://', 'htps://example.invalid/x', 'https://example.invalid/путь'):
        with tempfile.TemporaryDirectory() as tmp:
            out = step(tmp, '2026-11-14T09:00:00+00:00', url=bad, expect=1)
            check(f'кривой адрес в настройках ({bad[:8]}...): запуск красный, памяти не касается',
                  'настройки' in out and not os.path.exists(os.path.join(tmp, 'state.json')))
    with tempfile.TemporaryDirectory() as tmp:
        out = step(tmp, '2026-11-14T09:00:00+00:00', url='example.invalid/без-https', expect=1)
        check('адрес сайта в журнал не печатается', 'example.invalid' not in out and '127.0.0.1' not in out)


for case in (case_outage, case_blip, case_telegram_down, case_alert_never_delivered, case_drill, case_flap,
             case_green_lost_then_down_again, case_late_alert_then_reminder, case_half_broken_state, case_broken, case_state_garbage, case_keepalive, case_real_probe):
    case()
print('ИТОГ:', 'всё прошло' if not failed else f'упало {len(failed)}: ' + '; '.join(failed))
sys.exit(1 if failed else 0)
