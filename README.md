# FB Marketplace Bot

Telegram-бот для сбора объявлений Facebook Marketplace.

**Репозиторий:** https://github.com/callumcox819-svg/fbbot88

## Меню

- **▶️ Старт поиска** — запрос токена аккаунта FB → сбор JSON
- **⚙️ Настройки** — прокси, категории, лимит JSON, страна (CH/FI)
- **⏹ Стоп поиск** — остановка
- **👑 Админ панель** — статистика, выдача/отзыв доступа

Бот **закрыт** до выдачи доступа админом.

## Токен аккаунта Facebook

Строка как в VOID (это **ваш аккаунт FB**, не «токен парсера»):

```
61588728046344|1%3ABchMU-VZ1HYFRQ%3A2%3A...|LyT_aa36...|LyT_afeF...|1MOxHY0sKRRe...
```

| Часть | Поле |
|-------|------|
| 1 | `c_user` |
| 2 | `xs` |
| 3 | `datr` |
| 4 | `fr` |
| 5 | access token |

При старте: вставить строку или **♻️ Использовать последний токен**.

## Куда вписать токен Telegram-бота

**Не в код и не в GitHub** — только в файл `.env` локально или в секреты хостинга.

Путь:

`fb-marketplace-bot/.env` (создай из `.env.example`)

```env
BOT_TOKEN=123456789:AAH...твой_токен_от_BotFather...
ADMIN_IDS=твой_telegram_id
```

```powershell
copy .env.example .env
notepad .env
```

`.env` в `.gitignore` — в репозиторий **не коммить**.

### Деплой на Railway (рекомендуется)

1. Залей код в https://github.com/callumcox819-svg/fbbot88 (без `.env`).
2. [railway.app](https://railway.app) → New Project → Deploy from GitHub → выбери `fbbot88`.
3. **Variables** → добавь:
   - `BOT_TOKEN` = токен @BotFather
   - `ADMIN_IDS` = твой Telegram ID
4. Deploy. Start command уже в `railway.toml`: `python bot.py`.

### Деплой на Render / VPS

Те же переменные `BOT_TOKEN` и `ADMIN_IDS`. Команда: `python bot.py`.

## Запуск

```powershell
cd c:\Users\user\Desktop\fb-marketplace-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# BOT_TOKEN и ADMIN_IDS
python bot.py
```

## Настройки пользователя

- **Прокси** — свои, по одному на строку: `host:port:user:pass`
- **Категории** — готовые (до 7) или своя ссылка
- **Лимит JSON** — сколько объявлений в файл (1–500)
- **Страна** — 🇨🇭 Швейцария / 🇫🇮 Финляндия (🟢 = фильтр включён)
