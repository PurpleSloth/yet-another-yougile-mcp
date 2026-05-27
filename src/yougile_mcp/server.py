from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP

HttpMethod = Literal["GET", "POST", "PUT", "DELETE"]

OPENAPI_CANDIDATES = [
    Path.cwd() / "yougile.openapi.json",
    Path(__file__).resolve().parents[2] / "yougile.openapi.json",
]
if os.getenv("YOUGILE_OPENAPI_PATH"):
    OPENAPI_CANDIDATES.insert(0, Path(os.environ["YOUGILE_OPENAPI_PATH"]))
DEFAULT_API_BASE = "https://yougile.com"
ALLOWED_METHODS = {"GET", "POST", "PUT", "DELETE"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_openapi() -> dict[str, Any]:
    for path in OPENAPI_CANDIDATES:
        if path and path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {"paths": {}, "components": {"schemas": {}}}


OPENAPI = _load_openapi()

mcp = FastMCP(
    "YouGile MCP",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "8000")),
    stateless_http=_env_bool("MCP_STATELESS_HTTP", True),
    json_response=_env_bool("MCP_JSON_RESPONSE", True),
)


class YouGileError(RuntimeError):
    pass


def _api_base() -> str:
    return os.getenv("YOUGILE_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _api_key(required: bool = True) -> str | None:
    token = os.getenv("YOUGILE_API_KEY")
    if required and not token:
        raise YouGileError("YOUGILE_API_KEY is not set")
    return token


def _normalize_path(path: str, path_params: dict[str, Any] | None = None) -> str:
    cleaned = path.strip()
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        raise YouGileError("Use an API-relative path, not a full URL")

    if not cleaned.startswith("/"):
        cleaned = f"/api-v2/{cleaned.lstrip('/')}"
    elif not cleaned.startswith("/api-v2/"):
        cleaned = f"/api-v2{cleaned}"

    for key, value in (path_params or {}).items():
        cleaned = cleaned.replace("{" + key + "}", str(value))

    if "{" in cleaned or "}" in cleaned:
        raise YouGileError(f"Unresolved path parameter in {cleaned!r}")
    if not cleaned.startswith("/api-v2/"):
        raise YouGileError("Only /api-v2 paths are allowed")
    return cleaned


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("content"), list):
        return [item for item in payload["content"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _matches(value: Any, query: str | None, *, exact: bool = False) -> bool:
    if query is None:
        return True
    text = str(value or "").casefold()
    needle = query.casefold()
    return text == needle if exact else needle in text


def _compact_entity(entity: dict[str, Any], *extra_keys: str) -> dict[str, Any]:
    keys = ["id", "title", "name", "columnId", "completed", "archived", "idTaskProject", "idTaskCommon"]
    result = {key: entity[key] for key in [*keys, *extra_keys] if key in entity}
    return result or entity


async def _search_entities(
    path: str,
    *,
    title: str | None = None,
    query: dict[str, Any] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    params = {"limit": limit, "offset": 0}
    if query:
        params.update({key: value for key, value in query.items() if value is not None})
    if title:
        params["title"] = title
    payload = await _request("GET", path, query=params)
    items = _items(payload)
    if title:
        title_matches = [item for item in items if _matches(item.get("title"), title)]
        return title_matches or items
    return items


async def _one_entity(
    entity_name: str,
    path: str,
    *,
    title: str | None = None,
    query: dict[str, Any] | None = None,
    exact: bool = False,
) -> dict[str, Any]:
    candidates = await _search_entities(path, title=title, query=query)
    if title:
        filtered = [item for item in candidates if _matches(item.get("title"), title, exact=exact)]
        candidates = filtered or candidates
    if not candidates:
        raise YouGileError(f"{entity_name} not found")
    if len(candidates) > 1:
        exact_matches = [item for item in candidates if title and _matches(item.get("title"), title, exact=True)]
        if len(exact_matches) == 1:
            return exact_matches[0]
        shown = [_compact_entity(item) for item in candidates[:10]]
        raise YouGileError(f"Ambiguous {entity_name}. Narrow the query. Candidates: {shown}")
    return candidates[0]


async def _resolve_project(project_id: str | None, project_title: str | None) -> dict[str, Any] | None:
    if project_id:
        payload = await _request("GET", "/api-v2/projects/{id}", path_params={"id": project_id})
        return payload if isinstance(payload, dict) else None
    if project_title:
        return await _one_entity("project", "/api-v2/projects", title=project_title)
    return None


async def _resolve_board(
    board_id: str | None,
    board_title: str | None,
    *,
    project_id: str | None = None,
    project_title: str | None = None,
) -> dict[str, Any] | None:
    if board_id:
        payload = await _request("GET", "/api-v2/boards/{id}", path_params={"id": board_id})
        return payload if isinstance(payload, dict) else None
    if not board_title:
        return None
    project = await _resolve_project(project_id, project_title)
    query = {"projectId": project["id"]} if project else None
    return await _one_entity("board", "/api-v2/boards", title=board_title, query=query)


async def _resolve_column(
    column_id: str | None,
    column_title: str | None,
    *,
    project_id: str | None = None,
    project_title: str | None = None,
    board_id: str | None = None,
    board_title: str | None = None,
) -> dict[str, Any]:
    if column_id:
        payload = await _request("GET", "/api-v2/columns/{id}", path_params={"id": column_id})
        if isinstance(payload, dict):
            return payload
        raise YouGileError("Column response is not an object")
    if not column_title:
        raise YouGileError("column_id or column_title is required")
    board = await _resolve_board(
        board_id, board_title, project_id=project_id, project_title=project_title
    )
    query = {"boardId": board["id"]} if board else None
    return await _one_entity("column", "/api-v2/columns", title=column_title, query=query)


async def _find_tasks(
    *,
    task_title: str | None = None,
    project_id: str | None = None,
    project_title: str | None = None,
    board_id: str | None = None,
    board_title: str | None = None,
    column_id: str | None = None,
    column_title: str | None = None,
    include_completed: bool = True,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    if column_id or column_title:
        column = await _resolve_column(
            column_id,
            column_title,
            project_id=project_id,
            project_title=project_title,
            board_id=board_id,
            board_title=board_title,
        )
        columns = [column]
    else:
        board = await _resolve_board(
            board_id, board_title, project_id=project_id, project_title=project_title
        )
        if board:
            columns = await _search_entities("/api-v2/columns", query={"boardId": board["id"]}, limit=1000)
        else:
            project = await _resolve_project(project_id, project_title)
            boards = await _search_entities(
                "/api-v2/boards",
                query={"projectId": project["id"]} if project else None,
                limit=1000,
            )
            columns = []
            for item in boards:
                columns.extend(
                    await _search_entities("/api-v2/columns", query={"boardId": item["id"]}, limit=1000)
                )

    tasks: list[dict[str, Any]] = []
    for column in columns:
        found = await _search_entities(
            "/api-v2/tasks",
            title=task_title,
            query={"columnId": column["id"], "includeDeleted": False},
            limit=1000,
        )
        for task in found:
            if task_title and not (
                _matches(task.get("title"), task_title)
                or _matches(task.get("idTaskProject"), task_title, exact=True)
                or _matches(task.get("idTaskCommon"), task_title, exact=True)
            ):
                continue
            if not include_completed and task.get("completed"):
                continue
            if not include_archived and task.get("archived"):
                continue
            task["_columnTitle"] = column.get("title")
            task["_columnId"] = column.get("id")
            tasks.append(task)
    return tasks


async def _one_task(**kwargs: Any) -> dict[str, Any]:
    candidates = await _find_tasks(**kwargs)
    if not candidates:
        raise YouGileError("Task not found")
    title = kwargs.get("task_title")
    exact_matches = [
        task
        for task in candidates
        if title
        and (
            _matches(task.get("title"), title, exact=True)
            or _matches(task.get("idTaskProject"), title, exact=True)
            or _matches(task.get("idTaskCommon"), title, exact=True)
        )
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(candidates) > 1:
        shown = [_compact_entity(task, "_columnTitle", "_columnId") for task in candidates[:10]]
        raise YouGileError(f"Ambiguous task. Narrow the query. Candidates: {shown}")
    return candidates[0]


async def _request(
    method: HttpMethod,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    json_body: dict[str, Any] | list[Any] | None = None,
    path_params: dict[str, Any] | None = None,
    auth_required: bool = True,
) -> dict[str, Any] | list[Any] | str:
    method = method.upper()  # type: ignore[assignment]
    if method not in ALLOWED_METHODS:
        raise YouGileError(f"Unsupported HTTP method: {method}")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    token = _api_key(required=auth_required)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{_api_base()}{_normalize_path(path, path_params)}"
    async with httpx.AsyncClient(timeout=float(os.getenv("YOUGILE_TIMEOUT", "30"))) as client:
        response = await client.request(method, url, params=query, json=json_body, headers=headers)

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        payload: dict[str, Any] | list[Any] | str = response.json()
    else:
        payload = response.text

    if response.is_error:
        raise YouGileError(
            f"YouGile API returned {response.status_code} for {method} {url}: {payload}"
        )
    return payload


def _path_summary(path: str, method: str, operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": method.upper(),
        "path": path,
        "operationId": operation.get("operationId"),
        "summary": operation.get("summary"),
        "tags": operation.get("tags", []),
        "query": [
            {
                "name": p.get("name"),
                "required": p.get("required", False),
                "description": p.get("description"),
                "schema": p.get("schema", {}),
            }
            for p in operation.get("parameters", [])
            if p.get("in") == "query"
        ],
        "pathParams": [
            {
                "name": p.get("name"),
                "required": p.get("required", False),
                "schema": p.get("schema", {}),
            }
            for p in operation.get("parameters", [])
            if p.get("in") == "path"
        ],
        "bodySchema": (
            operation.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema")
        ),
    }


@mcp.tool()
def yougile_list_endpoints(filter_text: str | None = None) -> list[dict[str, Any]]:
    """List YouGile API v2 endpoints from the bundled OpenAPI schema."""
    pattern = re.compile(re.escape(filter_text), re.IGNORECASE) if filter_text else None
    endpoints: list[dict[str, Any]] = []
    for path, methods in OPENAPI.get("paths", {}).items():
        for method, operation in methods.items():
            item = _path_summary(path, method, operation)
            haystack = json.dumps(item, ensure_ascii=False)
            if pattern is None or pattern.search(haystack):
                endpoints.append(item)
    return endpoints


@mcp.tool()
def yougile_get_schema(schema_name: str) -> dict[str, Any]:
    """Return a DTO schema from the bundled YouGile OpenAPI file."""
    schemas = OPENAPI.get("components", {}).get("schemas", {})
    if schema_name not in schemas:
        names = sorted(name for name in schemas if schema_name.lower() in name.lower())
        raise YouGileError(f"Schema {schema_name!r} not found. Similar schemas: {names[:20]}")
    return schemas[schema_name]


@mcp.tool()
async def yougile_request(
    method: HttpMethod,
    path: str,
    query: dict[str, Any] | None = None,
    json_body: dict[str, Any] | list[Any] | None = None,
    path_params: dict[str, Any] | None = None,
) -> dict[str, Any] | list[Any] | str:
    """Call any YouGile REST API v2 JSON endpoint."""
    return await _request(method, path, query=query, json_body=json_body, path_params=path_params)


@mcp.tool()
async def yougile_create_api_key(
    login: str,
    password: str,
    company_id: str,
    name: str | None = None,
) -> dict[str, Any] | list[Any] | str:
    """Create a YouGile API key from login, password, and company ID."""
    body: dict[str, Any] = {"login": login, "password": password, "companyId": company_id}
    if name:
        body["name"] = name
    return await _request("POST", "/api-v2/auth/keys", json_body=body, auth_required=False)


@mcp.tool()
async def yougile_me() -> dict[str, Any] | list[Any] | str:
    """Get the current YouGile user for YOUGILE_API_KEY."""
    return await _request("GET", "/api-v2/users/me")


@mcp.tool()
async def yougile_list_projects(
    title: str | None = None,
    include_deleted: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any] | list[Any] | str:
    """List projects, optionally filtering by title."""
    query = {"limit": limit, "offset": offset}
    if title is not None:
        query["title"] = title
    if include_deleted is not None:
        query["includeDeleted"] = include_deleted
    return await _request("GET", "/api-v2/projects", query=query)


@mcp.tool()
async def yougile_get_project(project_id: str) -> dict[str, Any] | list[Any] | str:
    """Get a project by ID."""
    return await _request("GET", "/api-v2/projects/{id}", path_params={"id": project_id})


@mcp.tool()
async def yougile_create_project(body: dict[str, Any]) -> dict[str, Any] | list[Any] | str:
    """Create a project. Body follows CreateProjectDto."""
    return await _request("POST", "/api-v2/projects", json_body=body)


@mcp.tool()
async def yougile_update_project(
    project_id: str, body: dict[str, Any]
) -> dict[str, Any] | list[Any] | str:
    """Update a project. Body follows UpdateProjectDto."""
    return await _request("PUT", "/api-v2/projects/{id}", path_params={"id": project_id}, json_body=body)


@mcp.tool()
async def yougile_list_boards(
    project_id: str | None = None,
    title: str | None = None,
    include_deleted: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any] | list[Any] | str:
    """List boards, optionally filtering by project ID or title."""
    query = {"limit": limit, "offset": offset}
    if project_id is not None:
        query["projectId"] = project_id
    if title is not None:
        query["title"] = title
    if include_deleted is not None:
        query["includeDeleted"] = include_deleted
    return await _request("GET", "/api-v2/boards", query=query)


@mcp.tool()
async def yougile_create_board(body: dict[str, Any]) -> dict[str, Any] | list[Any] | str:
    """Create a board. Body follows CreateBoardDto."""
    return await _request("POST", "/api-v2/boards", json_body=body)


@mcp.tool()
async def yougile_list_columns(
    board_id: str | None = None,
    title: str | None = None,
    include_deleted: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any] | list[Any] | str:
    """List columns, optionally filtering by board ID or title."""
    query = {"limit": limit, "offset": offset}
    if board_id is not None:
        query["boardId"] = board_id
    if title is not None:
        query["title"] = title
    if include_deleted is not None:
        query["includeDeleted"] = include_deleted
    return await _request("GET", "/api-v2/columns", query=query)


@mcp.tool()
async def yougile_create_column(body: dict[str, Any]) -> dict[str, Any] | list[Any] | str:
    """Create a column. Body follows CreateColumnDto."""
    return await _request("POST", "/api-v2/columns", json_body=body)


@mcp.tool()
async def yougile_list_tasks(
    column_id: str | None = None,
    title: str | None = None,
    include_deleted: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any] | list[Any] | str:
    """List tasks, optionally filtering by column ID or title."""
    query = {"limit": limit, "offset": offset}
    if column_id is not None:
        query["columnId"] = column_id
    if title is not None:
        query["title"] = title
    if include_deleted is not None:
        query["includeDeleted"] = include_deleted
    return await _request("GET", "/api-v2/tasks", query=query)


@mcp.tool()
async def yougile_get_task(task_id: str) -> dict[str, Any] | list[Any] | str:
    """Get a task by ID."""
    return await _request("GET", "/api-v2/tasks/{id}", path_params={"id": task_id})


@mcp.tool()
async def yougile_create_task(body: dict[str, Any]) -> dict[str, Any] | list[Any] | str:
    """Create a task. Body follows CreateTaskDto."""
    return await _request("POST", "/api-v2/tasks", json_body=body)


@mcp.tool()
async def yougile_update_task(task_id: str, body: dict[str, Any]) -> dict[str, Any] | list[Any] | str:
    """Update a task. Body follows UpdateTaskDto."""
    return await _request("PUT", "/api-v2/tasks/{id}", path_params={"id": task_id}, json_body=body)


@mcp.tool()
async def yougile_list_users(
    email: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any] | list[Any] | str:
    """List company users."""
    query = {"limit": limit, "offset": offset}
    if email is not None:
        query["email"] = email
    if project_id is not None:
        query["projectId"] = project_id
    return await _request("GET", "/api-v2/users", query=query)


@mcp.tool()
async def yougile_find_project(title: str) -> dict[str, Any]:
    """Find one project by title and return its ID and main fields."""
    return _compact_entity(await _one_entity("project", "/api-v2/projects", title=title))


@mcp.tool()
async def yougile_find_board(
    title: str,
    project_title: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Find one board by title, optionally inside a project."""
    board = await _resolve_board(None, title, project_id=project_id, project_title=project_title)
    if not board:
        raise YouGileError("Board not found")
    return _compact_entity(board, "projectId")


@mcp.tool()
async def yougile_find_column(
    title: str,
    project_title: str | None = None,
    project_id: str | None = None,
    board_title: str | None = None,
    board_id: str | None = None,
) -> dict[str, Any]:
    """Find one column by title, optionally inside a board or project."""
    column = await _resolve_column(
        None,
        title,
        project_id=project_id,
        project_title=project_title,
        board_id=board_id,
        board_title=board_title,
    )
    return _compact_entity(column, "boardId")


@mcp.tool()
async def yougile_create_task_at(
    title: str,
    column_title: str | None = None,
    description: str | None = None,
    project_title: str | None = None,
    board_title: str | None = None,
    column_id: str | None = None,
    assign_to_me: bool = False,
    completed: bool = False,
    archived: bool = False,
    deadline_ms: int | None = None,
) -> dict[str, Any] | list[Any] | str:
    """Create a task by human-readable project/board/column names."""
    column = await _resolve_column(
        column_id,
        column_title,
        project_title=project_title,
        board_title=board_title,
    )
    body: dict[str, Any] = {
        "title": title,
        "columnId": column["id"],
        "completed": completed,
        "archived": archived,
    }
    if description:
        body["description"] = description
    if deadline_ms is not None:
        body["deadline"] = {"deadline": deadline_ms}
    if assign_to_me:
        me = await _request("GET", "/api-v2/users/me")
        if isinstance(me, dict) and me.get("id"):
            body["assigned"] = [me["id"]]
    return await _request("POST", "/api-v2/tasks", json_body=body)


@mcp.tool()
async def yougile_find_tasks(
    task_title: str | None = None,
    project_title: str | None = None,
    board_title: str | None = None,
    column_title: str | None = None,
    include_completed: bool = True,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Find tasks by title/code and optional project, board, or column names."""
    tasks = await _find_tasks(
        task_title=task_title,
        project_title=project_title,
        board_title=board_title,
        column_title=column_title,
        include_completed=include_completed,
        include_archived=include_archived,
    )
    return [_compact_entity(task, "_columnTitle", "_columnId", "description") for task in tasks]


@mcp.tool()
async def yougile_task_overview(
    project_title: str | None = None,
    include_completed: bool = False,
    include_archived: bool = False,
    max_projects: int = 50,
) -> dict[str, Any]:
    """Return a compact task overview grouped by project, board, and column."""
    projects = (
        [await _one_entity("project", "/api-v2/projects", title=project_title)]
        if project_title
        else await _search_entities("/api-v2/projects", limit=max_projects)
    )
    overview: dict[str, Any] = {"projects": []}
    for project in projects[:max_projects]:
        project_entry = {"id": project.get("id"), "title": project.get("title"), "boards": []}
        boards = await _search_entities("/api-v2/boards", query={"projectId": project["id"]}, limit=1000)
        for board in boards:
            board_entry = {"id": board.get("id"), "title": board.get("title"), "columns": []}
            columns = await _search_entities("/api-v2/columns", query={"boardId": board["id"]}, limit=1000)
            for column in columns:
                tasks = await _search_entities(
                    "/api-v2/tasks",
                    query={"columnId": column["id"], "includeDeleted": False},
                    limit=1000,
                )
                visible_tasks = [
                    _compact_entity(task)
                    for task in tasks
                    if (include_completed or not task.get("completed"))
                    and (include_archived or not task.get("archived"))
                ]
                if visible_tasks:
                    board_entry["columns"].append(
                        {"id": column.get("id"), "title": column.get("title"), "tasks": visible_tasks}
                    )
            if board_entry["columns"]:
                project_entry["boards"].append(board_entry)
        overview["projects"].append(project_entry)
    return overview


@mcp.tool()
async def yougile_move_task(
    task_title: str,
    target_column_title: str,
    project_title: str | None = None,
    board_title: str | None = None,
    source_column_title: str | None = None,
) -> dict[str, Any] | list[Any] | str:
    """Move a task found by title/code to another column found by title."""
    task = await _one_task(
        task_title=task_title,
        project_title=project_title,
        board_title=board_title,
        column_title=source_column_title,
    )
    target = await _resolve_column(
        None,
        target_column_title,
        project_title=project_title,
        board_title=board_title,
    )
    return await _request(
        "PUT",
        "/api-v2/tasks/{id}",
        path_params={"id": task["id"]},
        json_body={"columnId": target["id"]},
    )


@mcp.tool()
async def yougile_complete_task(
    task_title: str,
    project_title: str | None = None,
    board_title: str | None = None,
    column_title: str | None = None,
    completed: bool = True,
) -> dict[str, Any] | list[Any] | str:
    """Mark a task as completed or not completed by title/code."""
    task = await _one_task(
        task_title=task_title,
        project_title=project_title,
        board_title=board_title,
        column_title=column_title,
        include_completed=True,
    )
    return await _request(
        "PUT",
        "/api-v2/tasks/{id}",
        path_params={"id": task["id"]},
        json_body={"completed": completed},
    )


@mcp.tool()
async def yougile_archive_task(
    task_title: str,
    project_title: str | None = None,
    board_title: str | None = None,
    column_title: str | None = None,
    archived: bool = True,
) -> dict[str, Any] | list[Any] | str:
    """Archive or unarchive a task by title/code."""
    task = await _one_task(
        task_title=task_title,
        project_title=project_title,
        board_title=board_title,
        column_title=column_title,
        include_archived=True,
    )
    return await _request(
        "PUT",
        "/api-v2/tasks/{id}",
        path_params={"id": task["id"]},
        json_body={"archived": archived},
    )


@mcp.tool()
async def yougile_comment_task(
    task_title: str,
    text: str,
    project_title: str | None = None,
    board_title: str | None = None,
    column_title: str | None = None,
    label: str = "",
) -> dict[str, Any] | list[Any] | str:
    """Add a comment to a task found by title/code."""
    task = await _one_task(
        task_title=task_title,
        project_title=project_title,
        board_title=board_title,
        column_title=column_title,
    )
    body = {"text": text, "textHtml": f"<p>{text}</p>", "label": label}
    return await _request(
        "POST",
        "/api-v2/chats/{chatId}/messages",
        path_params={"chatId": task["id"]},
        json_body=body,
    )


@mcp.tool()
async def yougile_send_chat_message(
    chat_id: str,
    text: str,
    text_html: str | None = None,
    label: str = "",
) -> dict[str, Any] | list[Any] | str:
    """Send a message to a YouGile chat."""
    body = {"text": text, "textHtml": text_html or f"<p>{text}</p>", "label": label}
    return await _request(
        "POST", "/api-v2/chats/{chatId}/messages", path_params={"chatId": chat_id}, json_body=body
    )


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
