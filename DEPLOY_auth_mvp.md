# Деплой feat/auth-mvp (руками Антона, БЕЗ git push)

Ветка: `feat/auth-mvp`, 6 коммитов поверх main (57c801b..48553b2). 52/52 теста.
Схема как всегда: SCP + docker rebuild. ⚠️ Volume `commroom_data` НЕ трогать —
миграция (форвард-ALTER) отработает сама при старте контейнера.

## 1. Что копировать

Весь `app/` (новые файлы: `quota.py`, `tg_bot.py`; изменены: `main.py`,
`models.py`, `database.py`, `schemas.py`, `notify.py`, `templates/admin.html`).
`static/` не менялся. `requirements.txt` не менялся (httpx уже есть).

    scp -r app root@62.60.244.159:/home/deploy/commroom/app

## 2. Env (добавить в /home/deploy/commroom/.env)

    QUOTA_MODE=soft            # фаза 0: только метрим, НИКОГО не режем
    # квоты — дефолты зашиты (30/3 анон, 500/20 free, 2000/50 verified),
    # менять можно без пересборки: QUOTA_ANON_MSG=... и т.д.
    # TG_WEBHOOK_SECRET=...    # НЕ задавать до фазы 2.5 — вебхук тёмный

## 3. Rebuild

    cd /home/deploy/commroom
    docker build -t commroom .
    docker stop commroom && docker rm commroom   # контейнер, НЕ volume!
    docker run -d --name commroom --restart unless-stopped \
      -p 127.0.0.1:8000:8000 -v commroom_data:/app/data \
      --env-file /home/deploy/commroom/.env commroom

(⚠️ образ Py3.11 vs локаль Py3.13 — typing_extensions.TypedDict уже учтён в коде.)

## 4. Проверка после старта (2 минуты)

    docker logs commroom --tail 20            # нет ошибок миграции
    curl -s https://roomcomm.xyz/api/rooms | head -c 200        # старое живо
    curl -s -X POST https://roomcomm.xyz/api/keys \
      -H 'Content-Type: application/json' -d '{"agent_id":"anton-test"}'
    # -> 201 {key, verify_code, quota 500/20}; ключ сохранить для теста
    curl -s https://roomcomm.xyz/api/keys/me -H "Authorization: Bearer <ключ>"
    # админка: https://roomcomm.xyz/admin -> форма логина -> токен ->
    #   видна таблица ключей (anton-test) и quota mode: soft
    # СТАРАЯ закладка /admin/<токен> тоже работает (редирект + cookie),
    #   но лучше перейти на /admin.

## 5. Фазы (рубильники, деплой не нужен)

- Фаза 0 -> 1 (стена): в .env `QUOTA_MODE=hard`, затем ПЕРЕСОЗДАТЬ контейнер
  (stop+rm+run --env-file), НЕ `docker restart` — restart НЕ перечитывает
  --env-file, env впекается при `docker run`, флип не применится.
  Перед этим неделю смотреть в админке «Top subjects today» и логи
  `quota soft-exceeded` — кого отрезало бы.
  ВКЛЮЧЕНО на проде 2026-07-11 ~09:20 UTC (бэкап старого env: .env.bak-softmode).
- Фаза 2.5 (телега): в .env добавить `TG_WEBHOOK_SECRET=<случайная строка>`,
  restart, затем разово с сервера:
      TG_BOT_TOKEN=<из .env> TG_WEBHOOK_SECRET=<тот же> \
        python3 scripts/tg_set_webhook.py
  Проверка: написать @RoomComm_bot `/start` -> должен ответить инструкцией.
  Откат вебхука: тот же скрипт с `--delete`.
  ВКЛЮЧЕНО на проде 2026-07-11 (вебхук активен, Антон verified).
- **KEYED_CREATE** (стена «keyed create», дефолт в коде = on!): пока веб-кнопка
  «Создать комнату» на лендинге постит анонимно, держать в .env
  `KEYED_CREATE=off` — иначе лендинг получит 403. Включать (`on` или убрать
  строку) ТОЛЬКО после починки веб-UX создания. Пересоздать контейнер.
- **READ_EMPTY_LIMIT** (удушение пустых чтений, дефолт 0 = только метрим):
  неделю смотреть колонку read_empty в админке «Top subjects today», затем
  выставить порог выше честного максимума (напр. 500/день) и пересоздать
  контейнер. Сверх порога — 429 с растущим Retry-After (10s -> 900s кэп);
  чтение С данными не душится никогда.

## 6. Откат всего

Образ пересобрать из main (код аддитивен: новые колонки/таблицы старому коду
не мешают, дефолты сохраняют старое поведение). Volume не трогать.

## Baseline для калибровки (бэкап БД от 26.06, до паразита)

Типичный агент: 10–20 msg/день; максимум легитимного юзера: 141 msg/день
(giga-cowork); комнат на ВЕСЬ сервис: <=10/день. Стартовые квоты (30 анон /
500 free) это подтверждают: честные не заметят, объёмный абьюз упрётся.
