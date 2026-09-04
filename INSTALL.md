# Установка Telegram- и MAX-ботов «Маршрут построен»

Инструкция рассчитана на следующую схему:

- основной сайт и PostgreSQL работают на существующем сервере;
- Telegram- и MAX-боты запускаются одним Python-сервисом на отдельной VPS;
- Docker не используется;
- VPS работает под Ubuntu 24.04;
- для webhook используется отдельный HTTPS-домен, например `bot.example.ru`.

Исходный код:

- сайт: `C:\Proj\guessboss`;
- боты: `C:\Proj\marsrut_bot`.

Все значения в угловых скобках необходимо заменить своими. Угловые скобки в итоговых командах и `.env` оставлять нельзя.

## 1. Что необходимо получить заранее

Потребуются:

- IP-адрес VPS бота;
- домен или поддомен для webhook, например `bot.example.ru`;
- токен Telegram-бота;
- токен прошедшего модерацию MAX-бота;
- Telegram ID первого владельца;
- MAX ID первого владельца;
- SSH-доступ к VPS бота;
- доступ к серверу сайта и его production `.env`;
- имя systemd-сервиса сайта.

## 2. Создание Telegram-бота

1. Открыть `@BotFather` в Telegram.
2. Выполнить `/newbot`.
3. Задать название и username.
4. Сохранить выданный токен.
5. Написать созданному боту `/start`.
6. До регистрации webhook получить свой Telegram ID:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_TOKEN>/getUpdates"
```

Нужное значение находится в:

```text
result[].message.from.id
```

Оно будет использовано как `TELEGRAM_OWNER_IDS`.

## 3. Создание MAX-бота

Для MAX необходим верифицированный профиль организации, ИП или самозанятого.

1. Открыть платформу MAX для партнёров.
2. Перейти в раздел «Чат-боты».
3. Создать и отправить бота на модерацию.
4. После модерации открыть «Расширенные настройки» и скопировать токен.
5. Открыть бота в MAX и нажать «Начать».
6. До регистрации webhook получить свой MAX ID:

```bash
export MAX_BOT_TOKEN='<MAX_TOKEN>'

curl -s \
  -H "Authorization: $MAX_BOT_TOKEN" \
  "https://platform-api2.max.ru/updates"
```

В событии `bot_started` найти:

```text
user.user_id
```

Это значение будет использовано как `MAX_OWNER_IDS`.

## 4. Настройка DNS

У DNS-провайдера создать запись:

```text
Тип: A
Имя: bot
Значение: <IP_VPS>
TTL: 300
```

Проверить распространение записи:

```bash
nslookup bot.example.ru
```

или:

```bash
dig +short bot.example.ru
```

Команда должна вернуть IP-адрес VPS.

## 5. Создание секретов

Сгенерировать четыре разных секрета:

```bash
openssl rand -hex 32
openssl rand -hex 32
openssl rand -hex 32
openssl rand -hex 32
```

Назначение значений:

```text
BOT_API_SECRET
APP_SECRET
TELEGRAM_WEBHOOK_SECRET
MAX_WEBHOOK_SECRET
```

`BOT_API_SECRET` должен быть одинаковым на сервере сайта и VPS бота. Остальные секреты между собой совпадать не должны.

# Установка изменений на сервер сайта

## 6. Резервная копия сайта и базы данных

Перед обновлением создать snapshot сервера или резервную копию через панель хостинга.

Для PostgreSQL можно использовать:

```bash
cd <SITE_DIRECTORY>
pg_dump "$DATABASE_URL" > ~/guessboss-before-bot-$(date +%F-%H%M).sql
```

Убедиться, что файл создан и не пустой:

```bash
ls -lh ~/guessboss-before-bot-*.sql
```

## 7. Загрузка обновлённого сайта

Перенести изменения из `C:\Proj\guessboss` на сервер сайта существующим способом: Git, CI/CD, SCP или rsync.

Production `.env`, каталог загрузок и пользовательские данные заменять локальными файлами нельзя.

## 8. Настройка API сайта

Добавить в production `.env` сайта:

```dotenv
BOT_API_KEY_ID=marsrut-bot-v1
BOT_API_SECRET=<BOT_API_SECRET_ИЗ_РАЗДЕЛА_5>
```

## 9. Сборка и миграция сайта

В каталоге сайта выполнить:

```bash
cd <SITE_DIRECTORY>
npm ci
npx prisma generate
npm run build
```

Если сборка успешна, применить миграцию:

```bash
npx prisma migrate deploy
npx prisma migrate status
```

Перезапустить сайт:

```bash
sudo systemctl restart <SITE_SERVICE>
sudo systemctl status <SITE_SERVICE>
```

Проверить публичный адрес:

```bash
curl -I https://маршрут-построен.рф/
```

Закрытый API без HMAC-подписи должен отклонять запрос:

```bash
curl -i https://маршрут-построен.рф/api/integrations/bot/health
```

Ожидаемый результат — `401 Unauthorized`. Это подтверждает, что интеграционный API не открыт публично.

# Установка Python-сервиса на VPS

## 10. Подготовка Ubuntu

Подключиться к VPS:

```bash
ssh root@<IP_VPS>
```

Установить обновления и пакеты:

```bash
apt update
apt upgrade -y

apt install -y \
  python3.12 \
  python3.12-venv \
  python3-pip \
  nginx \
  certbot \
  python3-certbot-nginx \
  ca-certificates \
  curl \
  jq \
  tar
```

Проверить синхронизацию часов. Она необходима для HMAC-подписи запросов к сайту:

```bash
timedatectl status
```

Должно быть указано:

```text
System clock synchronized: yes
```

Если синхронизация выключена:

```bash
timedatectl set-ntp true
```

Настроить firewall:

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw enable
ufw status
```

## 11. Системный пользователь

Создать пользователя сервиса:

```bash
useradd \
  --system \
  --home-dir /opt/marsrut-bot \
  --shell /usr/sbin/nologin \
  marsrut-bot
```

Если пользователь уже существует, повторно создавать его не нужно.

Создать каталоги:

```bash
mkdir -p /opt/marsrut-bot
mkdir -p /opt/marsrut-bot/data
mkdir -p /opt/marsrut-bot/logs
mkdir -p /opt/marsrut-bot/backups
chown -R marsrut-bot:marsrut-bot /opt/marsrut-bot
```

## 12. Перенос кода с Windows

В локальном PowerShell создать архив без виртуального окружения и кэшей:

```powershell
tar `
  --exclude=.venv `
  --exclude=.pytest_cache `
  --exclude=.ruff_cache `
  --exclude=__pycache__ `
  -czf C:\Proj\marsrut_bot.tar.gz `
  -C C:\Proj\marsrut_bot .
```

Отправить архив:

```powershell
scp C:\Proj\marsrut_bot.tar.gz root@<IP_VPS>:/tmp/
```

На VPS распаковать:

```bash
tar -xzf /tmp/marsrut_bot.tar.gz -C /opt/marsrut-bot
chown -R marsrut-bot:marsrut-bot /opt/marsrut-bot
rm /tmp/marsrut_bot.tar.gz
```

## 13. Python-окружение

Создать виртуальное окружение:

```bash
sudo -u marsrut-bot \
  python3.12 -m venv /opt/marsrut-bot/.venv
```

Установить зависимости:

```bash
sudo -u marsrut-bot \
  /opt/marsrut-bot/.venv/bin/pip install --upgrade pip

sudo -u marsrut-bot \
  /opt/marsrut-bot/.venv/bin/pip install \
  -r /opt/marsrut-bot/requirements.txt
```

Проверить Python-файлы:

```bash
sudo -u marsrut-bot \
  /opt/marsrut-bot/.venv/bin/python \
  -m compileall -q /opt/marsrut-bot/app /opt/marsrut-bot/scripts
```

## 14. Настройка `.env` бота

Создать production-конфигурацию:

```bash
cp /opt/marsrut-bot/.env.example /opt/marsrut-bot/.env
nano /opt/marsrut-bot/.env
```

Заполнить:

```dotenv
APP_ENV=production
APP_HOST=127.0.0.1
APP_PORT=8080
APP_SECRET=<APP_SECRET>

PUBLIC_BASE_URL=https://bot.example.ru

SITE_BASE_URL=https://xn----7sbqzieaghadljej2f.xn--p1ai
SITE_PUBLIC_URL=https://xn----7sbqzieaghadljej2f.xn--p1ai
PRIVACY_URL=https://xn----7sbqzieaghadljej2f.xn--p1ai/privacy-policy

DATABASE_PATH=/opt/marsrut-bot/data/bot.sqlite3
LOG_LEVEL=INFO
TIMEZONE=Europe/Moscow

BOT_API_KEY_ID=marsrut-bot-v1
BOT_API_SECRET=<ТОТ_ЖЕ_BOT_API_SECRET_ЧТО_НА_САЙТЕ>

TELEGRAM_BOT_TOKEN=<TELEGRAM_TOKEN>
TELEGRAM_WEBHOOK_SECRET=<TELEGRAM_WEBHOOK_SECRET>
TELEGRAM_BOT_USERNAME=<USERNAME_БЕЗ_СИМВОЛА_СОБАКИ>
TELEGRAM_OWNER_IDS=<TELEGRAM_USER_ID>

MAX_BOT_TOKEN=<MAX_TOKEN>
MAX_WEBHOOK_SECRET=<MAX_WEBHOOK_SECRET>
MAX_BOT_USERNAME=<MAX_USERNAME_БЕЗ_СИМВОЛА_СОБАКИ>
MAX_OWNER_IDS=<MAX_USER_ID>

EVENT_POLL_INTERVAL_SECONDS=5
REMINDER_POLL_INTERVAL_SECONDS=60
DAILY_DIGEST_HOUR=9
OUTBOX_RETENTION_DAYS=30
```

Для нескольких аварийных владельцев ID перечисляются через запятую:

```dotenv
TELEGRAM_OWNER_IDS=123456789,987654321
MAX_OWNER_IDS=111111111,222222222
```

Ограничить доступ к конфигурации:

```bash
chown marsrut-bot:marsrut-bot /opt/marsrut-bot/.env
chmod 600 /opt/marsrut-bot/.env
```

Проверить конфигурацию без печати секретов:

```bash
cd /opt/marsrut-bot

sudo -u marsrut-bot .venv/bin/python -c \
"from app.config import load_settings; s=load_settings(); print('Telegram:', s.telegram_enabled, 'MAX:', s.max_enabled)"
```

Ожидается:

```text
Telegram: True MAX: True
```

### Сертификаты для API MAX

MAX требует актуальную доверенную цепочку сертификатов. Сначала выполнить:

```bash
apt install --reinstall -y ca-certificates
update-ca-certificates
```

Если соединение с `platform-api2.max.ru` всё равно выдаёт SSL-ошибку, установить официальный сертификат Минцифры в системное хранилище согласно актуальной документации MAX. Не следует скачивать корневые сертификаты из неофициальных источников.

# Настройка HTTPS

## 15. Временный Nginx-конфиг

Создать файл:

```bash
nano /etc/nginx/sites-available/marsrut-bot
```

Содержимое:

```nginx
server {
    listen 80;
    listen [::]:80;

    server_name bot.example.ru;

    location / {
        return 404;
    }
}
```

Включить конфигурацию:

```bash
ln -s /etc/nginx/sites-available/marsrut-bot \
  /etc/nginx/sites-enabled/marsrut-bot

nginx -t
systemctl reload nginx
```

Если символическая ссылка уже существует, повторно создавать её не нужно.

## 16. Сертификат Let's Encrypt

Получить сертификат:

```bash
certbot --nginx -d bot.example.ru
```

Выбрать перенаправление HTTP на HTTPS.

Проверить продление:

```bash
certbot renew --dry-run
```

## 17. Финальный Nginx-конфиг

Скопировать подготовленный конфиг:

```bash
cp /opt/marsrut-bot/deploy/nginx.conf.example \
  /etc/nginx/sites-available/marsrut-bot
```

Открыть файл:

```bash
nano /etc/nginx/sites-available/marsrut-bot
```

Заменить `bot.example.ru` реальным доменом и проверить пути:

```nginx
ssl_certificate /etc/letsencrypt/live/bot.example.ru/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/bot.example.ru/privkey.pem;
```

Применить:

```bash
nginx -t
systemctl reload nginx
```

# Запуск сервиса

## 18. Установка systemd units

```bash
cp /opt/marsrut-bot/systemd/marsrut-bot.service \
  /etc/systemd/system/

cp /opt/marsrut-bot/systemd/marsrut-bot-backup.service \
  /etc/systemd/system/

cp /opt/marsrut-bot/systemd/marsrut-bot-backup.timer \
  /etc/systemd/system/

systemctl daemon-reload
```

Запустить сервис:

```bash
systemctl enable --now marsrut-bot.service
systemctl enable --now marsrut-bot-backup.timer
```

Проверить состояние:

```bash
systemctl status marsrut-bot.service
systemctl status marsrut-bot-backup.timer
```

Посмотреть логи:

```bash
journalctl -u marsrut-bot.service -n 100 --no-pager
```

Для непрерывного просмотра:

```bash
journalctl -u marsrut-bot.service -f
```

## 19. Health-check

Проверить приложение напрямую:

```bash
curl http://127.0.0.1:8080/health
```

Проверить через Nginx:

```bash
curl https://bot.example.ru/health
```

Ожидаемый ответ:

```json
{"status":"ok"}
```

Расширенная диагностика доступна локально и требует `APP_SECRET`:

```bash
set -a
source /opt/marsrut-bot/.env
set +a

curl \
  -H "X-Health-Token: $APP_SECRET" \
  http://127.0.0.1:8080/health/details
```

В ответе должны присутствовать состояние API сайта и статистика очереди сообщений.

# Регистрация webhook

## 20. Регистрация Telegram и MAX

Webhook регистрируются только после успешного ответа публичного health-check:

```bash
cd /opt/marsrut-bot

sudo -u marsrut-bot \
  .venv/bin/python -m scripts.register_webhooks
```

Ожидаемый вывод:

```text
Telegram webhook registered
MAX webhook registered
```

## 21. Проверка Telegram webhook

```bash
set -a
source /opt/marsrut-bot/.env
set +a

curl -s \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" \
  | jq
```

Проверить:

```text
url = https://bot.example.ru/webhooks/telegram
pending_update_count = 0
```

`last_error_message` должен отсутствовать.

## 22. Проверка MAX webhook

```bash
curl -s \
  -H "Authorization: $MAX_BOT_TOKEN" \
  "https://platform-api2.max.ru/subscriptions" \
  | jq
```

В списке должен присутствовать:

```text
https://bot.example.ru/webhooks/max
```

# Первый запуск

## 23. Проверка владельца

В Telegram отправить:

```text
/start
/admin
```

В MAX нажать «Начать», затем отправить:

```text
/admin
```

Должна открыться панель администратора.

Если показывается обычное меню, перепроверить `TELEGRAM_OWNER_IDS` и `MAX_OWNER_IDS`, затем выполнить:

```bash
systemctl restart marsrut-bot
```

## 24. Добавление администраторов

Владелец открывает:

```text
Панель администратора
→ Администраторы
→ Добавить
→ Выбрать роль
```

Бот создаст одноразовый код и ссылку. Будущий администратор открывает ссылку или отправляет:

```text
/start ADM-XXXXXXXX
```

Код действует 30 минут и может быть использован только один раз.

Чтобы связать Telegram и MAX одного администратора:

```text
Администраторы
→ Открыть администратора
→ Привязать второй мессенджер
```

### Редактор сообщений и возможностей

Открыть в боте:

```text
Панель администратора → Тексты, кнопки и изображения
```

Ответы сгруппированы по разделам. Для каждого ответа доступны изменение и сброс текста,
до 10 изображений по прямым HTTPS-ссылкам, переименование/скрытие кнопок, предпросмотр и создание
новой кнопки с отдельным сообщением. Маркер `{default}` внутри изменённого текста сохраняет
динамическую часть исходного ответа. В разделе «Включение возможностей» пользовательскую функцию
можно отключить целиком; тогда исчезают её актуальные кнопки и перестают работать старые.

Редактирование доступно владельцам и администраторам. Наблюдатели не видят кнопку редактора.

# Приёмочное тестирование

## 25. Обязательный сценарий проверки

1. Отправить заявку через форму сайта.
2. Убедиться, что сайт показал номер вида `MP-260903-ABC123`.
3. Проверить появление заявки в web-админке.
4. Проверить уведомление владельца в Telegram и MAX.
5. В боте открыть «Мои заявки → Привязать заявку с сайта».
6. Ввести номер заявки и телефон.
7. Создать отдельную заявку непосредственно в Telegram.
8. Проверить её появление на сайте и у администраторов.
9. Создать заявку непосредственно в MAX.
10. Взять заявку в работу.
11. Назначить ответственного.
12. Изменить статус и проверить уведомление пользователя.
13. Отправить сообщение заявителю из административной карточки.
14. Ответить со стороны пользователя.
15. Добавить внутренний комментарий.
16. Установить напоминание.
17. Открыть «Панель администратора → Тексты, кнопки и изображения» и изменить тестовый ответ.
18. Проверить переименование/скрытие кнопки, добавление изображения и дополнительной страницы.
19. Выполнить ручную рассылку тестовой публикации и проверить явное число получателей в предпросмотре.
20. Проверить CSV-выгрузку.
21. Проверить удаление данных тестового пользователя.

## 26. Отключение старых уведомлений сайта

Только после успешного приёмочного тестирования очистить старые переменные Telegram на сервере сайта:

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Перезапустить сайт:

```bash
sudo systemctl restart <SITE_SERVICE>
```

Это предотвратит дублирование уведомлений о новых заявках.

# Эксплуатация

## 27. Основные команды

Состояние:

```bash
systemctl status marsrut-bot
```

Перезапуск:

```bash
systemctl restart marsrut-bot
```

Последние логи:

```bash
journalctl -u marsrut-bot -n 200 --no-pager
```

Логи в реальном времени:

```bash
journalctl -u marsrut-bot -f
```

Публичная проверка:

```bash
curl https://bot.example.ru/health
```

## 28. Резервные копии

Ручной запуск:

```bash
cd /opt/marsrut-bot

sudo -u marsrut-bot \
  .venv/bin/python -m scripts.backup
```

Проверка таймера и файлов:

```bash
systemctl list-timers marsrut-bot-backup.timer
ls -lah /opt/marsrut-bot/backups
```

## 29. Обновление бота

Перед обновлением создать резервную копию SQLite и текущего кода:

```bash
cd /opt/marsrut-bot
sudo -u marsrut-bot .venv/bin/python -m scripts.backup

systemctl stop marsrut-bot
cp -a /opt/marsrut-bot /opt/marsrut-bot-rollback-$(date +%F-%H%M)
```

При загрузке новой версии нельзя заменять:

```text
.env
data/
backups/
.venv/
```

После обновления:

```bash
chown -R marsrut-bot:marsrut-bot /opt/marsrut-bot

sudo -u marsrut-bot \
  /opt/marsrut-bot/.venv/bin/pip install \
  -r /opt/marsrut-bot/requirements.txt

systemctl start marsrut-bot
systemctl status marsrut-bot
curl https://bot.example.ru/health
```

Повторная регистрация webhook при обычном обновлении не требуется. Она нужна при смене домена, URL, токена или webhook-секрета.

## 30. Быстрая диагностика проблем

### Бот не запускается

```bash
systemctl status marsrut-bot
journalctl -u marsrut-bot -n 200 --no-pager
```

Чаще всего причина — незаполненная переменная `.env`, короткий секрет или неправильные права на `/opt/marsrut-bot`.

### Telegram не присылает события

```bash
curl -s \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" \
  | jq
```

Проверить URL, `pending_update_count` и `last_error_message`.

### MAX не присылает события

```bash
curl -s \
  -H "Authorization: $MAX_BOT_TOKEN" \
  "https://platform-api2.max.ru/subscriptions" \
  | jq
```

Также проверить доверенную цепочку сертификатов и логи Nginx.

### Бот не может получить заявки с сайта

```bash
curl \
  -H "X-Health-Token: $APP_SECRET" \
  http://127.0.0.1:8080/health/details
```

Проверить:

- совпадают ли `BOT_API_KEY_ID` и `BOT_API_SECRET` на двух серверах;
- синхронизированы ли часы;
- доступен ли `SITE_BASE_URL` с VPS;
- применена ли Prisma-миграция;
- перезапущен ли сайт после изменения `.env`.

### Ошибка Nginx

```bash
nginx -t
journalctl -u nginx -n 100 --no-pager
tail -n 100 /var/log/nginx/error.log
```

### Проверка свободного места

```bash
df -h
du -sh /opt/marsrut-bot/data /opt/marsrut-bot/backups
```
