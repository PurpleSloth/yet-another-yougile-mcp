# YouGile MCP

MCP-сервер для работы с [YouGile REST API v2](https://yougile.com/api-v2/).

Сервер запускается в Docker и отдаёт MCP по Streamable HTTP на `/mcp`. API-ключ берётся из переменной окружения `YOUGILE_API_KEY`.

## Быстрый старт

```bash
cp .env.example .env
# впишите YOUGILE_API_KEY в .env
docker compose up --build -d
```

By default the container binds MCP only to `127.0.0.1` on the server. For a quick SSH-tunnel check:

```bash
ssh -L 8000:127.0.0.1:8000 root@your-server
```

Local MCP endpoint:

```text
http://localhost:8000/mcp
```

Для публичного доступа обычно ставят reverse proxy с HTTPS и прокидывают наружу:

```text
https://your-domain.example/mcp
```

## Переменные окружения

`YOUGILE_API_KEY` - постоянный API-ключ YouGile.

`YOUGILE_API_BASE` - базовый адрес YouGile. По умолчанию `https://yougile.com`. Для коробочной установки укажите ваш `mainPageUrl`, например `https://yougile.example.com`.

`MCP_BIND` - адрес публикации Docker-порта на хосте. По умолчанию `127.0.0.1`, чтобы сервер не был открыт в интернет до настройки reverse proxy.

`MCP_PORT` - порт контейнера, по умолчанию `8000`.

`MCP_TRANSPORT` - транспорт MCP, по умолчанию `streamable-http`.

## Как получить ключ

Если ключа ещё нет, сервер содержит инструмент `yougile_create_api_key`, которому нужны `login`, `password`, `company_id` и необязательное имя ключа. После создания ключ лучше положить в `YOUGILE_API_KEY` и больше не передавать пароль через MCP.

## Инструменты

Основные инструменты:

- `yougile_me`
- `yougile_list_projects`, `yougile_get_project`, `yougile_create_project`, `yougile_update_project`
- `yougile_list_boards`, `yougile_create_board`
- `yougile_list_columns`, `yougile_create_column`
- `yougile_list_tasks`, `yougile_get_task`, `yougile_create_task`, `yougile_update_task`
- `yougile_list_users`
- `yougile_send_chat_message`
- `yougile_request` для любого JSON endpoint из YouGile API v2
- `yougile_list_endpoints` и `yougile_get_schema` для просмотра встроенной OpenAPI-схемы

Личные удобные инструменты, чтобы не помнить UUID:

- `yougile_find_project`, `yougile_find_board`, `yougile_find_column`
- `yougile_create_task_at` - создать задачу по названиям проекта/доски/колонки
- `yougile_find_tasks` - найти задачи по названию или коду задачи
- `yougile_task_overview` - компактный обзор задач по проектам, доскам и колонкам
- `yougile_move_task` - перенести задачу в другую колонку
- `yougile_complete_task` - отметить задачу выполненной или вернуть в работу
- `yougile_archive_task` - архивировать или разархивировать задачу
- `yougile_comment_task` - добавить комментарий к задаче

Пример универсального запроса:

```json
{
  "method": "GET",
  "path": "/api-v2/tasks",
  "query": {
    "limit": 50,
    "offset": 0
  }
}
```

Пример создания задачи:

```json
{
  "title": "Проверить MCP",
  "project_title": "Личные проекты",
  "board_title": "Разработка",
  "column_title": "В работе",
  "description": "Проверить создание, перенос и завершение задач через MCP",
  "assign_to_me": true
}
```

Пример обзора всех незавершенных задач:

```json
{
  "include_completed": false,
  "include_archived": false
}
```

Пример переноса задачи:

```json
{
  "task_title": "Проверить MCP",
  "project_title": "Личные проекты",
  "board_title": "Разработка",
  "target_column_title": "Готово"
}
```

## Безопасность

Не публикуйте сервер без HTTPS и контроля доступа. MCP endpoint получает возможность работать с YouGile от имени владельца `YOUGILE_API_KEY`, поэтому на публичном хостинге его стоит закрыть авторизацией на reverse proxy или доступом только из доверенной сети.
