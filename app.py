"""Local task manager server backed by SQLite (Python standard library only)."""

from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"
DATABASE_PATH = APP_DIR / "tasks.sqlite3"
HOST = "127.0.0.1"
PORT = 8765
MAX_REQUEST_BYTES = 1_000_000
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 5_000
ALLOWED_FILTERS = {"all", "today", "upcoming", "overdue", "completed"}


def local_now() -> datetime:
    """Return a timezone-aware timestamp in the computer's local timezone."""
    return datetime.now().astimezone().replace(microsecond=0)


def timestamp_now() -> str:
    return local_now().isoformat(timespec="seconds")


def normalize_for_search(value: str) -> str:
    """Case-fold text and remove diacritics so searches ignore accents."""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalize_due_date(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("O prazo deve ser uma data no formato AAAA-MM-DD.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Prazo inválido. Use o formato AAAA-MM-DD.") from exc
    if parsed.isoformat() != value:
        raise ValueError("Prazo inválido. Use o formato AAAA-MM-DD.")
    return value


def validate_title(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("O título é obrigatório.")
    title = value.strip()
    if not title:
        raise ValueError("O título é obrigatório.")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValueError(f"O título deve ter no máximo {MAX_TITLE_LENGTH} caracteres.")
    return title


def validate_description(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("A descrição deve ser um texto.")
    description = value.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(
            f"A descrição deve ter no máximo {MAX_DESCRIPTION_LENGTH} caracteres."
        )
    return description


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    with connect_db() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                title        TEXT NOT NULL,
                description  TEXT NOT NULL DEFAULT '',
                due_date     TEXT,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_due_date
                ON tasks (due_date);

            CREATE INDEX IF NOT EXISTS idx_tasks_completed_at
                ON tasks (completed_at);

            CREATE TABLE IF NOT EXISTS history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER,
                task_title TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (
                    event_type IN ('added', 'completed', 'reopened', 'deleted')
                ),
                occurred_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_history_occurred_at
                ON history (occurred_at DESC, id DESC);
            """
        )


def task_due_status(row: sqlite3.Row, today: date) -> str:
    if row["completed_at"] is not None:
        return "completed"
    due_value = row["due_date"]
    if due_value is None:
        return "no_due_date"
    due = date.fromisoformat(due_value)
    if due < today:
        return "overdue"
    if due == today:
        return "today"
    if due <= today + timedelta(days=7):
        return "upcoming"
    return "later"


def task_to_dict(row: sqlite3.Row, today: date) -> dict[str, object]:
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "due_date": row["due_date"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "status": task_due_status(row, today),
        "completed": row["completed_at"] is not None,
    }


def add_history_event(
    connection: sqlite3.Connection,
    *,
    task_id: int,
    task_title: str,
    event_type: str,
    occurred_at: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO history (task_id, task_title, event_type, occurred_at)
        VALUES (?, ?, ?, ?)
        """,
        (task_id, task_title, event_type, occurred_at or timestamp_now()),
    )


class TaskHandler(BaseHTTPRequestHandler):
    server_version = "TarefasLocal/1.0"

    def log_message(self, format_string: str, *args: object) -> None:
        # Keep the local terminal useful while avoiding noisy default formatting.
        super().log_message(format_string, *args)

    def send_json(self, payload: object, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json({"error": message}, status=status)

    def read_json_body(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Cabeçalho Content-Length inválido.") from exc
        if length <= 0:
            raise ValueError("Envie um corpo JSON.")
        if length > MAX_REQUEST_BYTES:
            raise ValueError("A solicitação excede o tamanho máximo permitido.")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("JSON inválido.") from exc
        if not isinstance(body, dict):
            raise ValueError("O corpo JSON deve ser um objeto.")
        return body

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path == "/api/tasks":
            self.handle_list_tasks(parse_qs(parsed.query, keep_blank_values=True))
            return
        if path == "/api/history":
            self.handle_list_history()
            return
        if path == "/":
            self.serve_file(WEB_DIR / "index.html")
            return
        if path.startswith("/web/"):
            relative_path = path.removeprefix("/web/")
            self.serve_web_asset(relative_path)
            return
        if path.startswith("/static/"):
            relative_path = path.removeprefix("/static/")
            self.serve_web_asset(relative_path)
            return
        self.send_error_json(404, "Página ou recurso não encontrado.")

    def handle_list_tasks(self, query: dict[str, list[str]]) -> None:
        selected_filter = query.get("filter", ["all"])[0]
        if selected_filter not in ALLOWED_FILTERS:
            self.send_error_json(
                400,
                "Filtro inválido. Use all, today, upcoming, overdue ou completed.",
            )
            return

        search_term = query.get("search", [""])[0].strip()
        normalized_search = normalize_for_search(search_term)
        today = local_now().date()

        with connect_db() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY id DESC"
            ).fetchall()

        matching_rows = []
        for row in rows:
            haystack = normalize_for_search(f"{row['title']}\n{row['description']}")
            if normalized_search and normalized_search not in haystack:
                continue
            matching_rows.append(row)

        counts = {filter_name: 0 for filter_name in ALLOWED_FILTERS}
        for row in matching_rows:
            status = task_due_status(row, today)
            if status == "completed":
                counts["completed"] += 1
            else:
                counts["all"] += 1
                if status in {"today", "upcoming", "overdue"}:
                    counts[status] += 1

        if selected_filter == "completed":
            selected_rows = [row for row in matching_rows if row["completed_at"]]
            selected_rows.sort(key=lambda row: row["completed_at"] or "", reverse=True)
        else:
            selected_rows = [row for row in matching_rows if not row["completed_at"]]
            if selected_filter != "all":
                selected_rows = [
                    row
                    for row in selected_rows
                    if task_due_status(row, today) == selected_filter
                ]
            selected_rows.sort(key=lambda row: self.task_sort_key(row, today))

        self.send_json(
            {
                "tasks": [task_to_dict(row, today) for row in selected_rows],
                "counts": counts,
                "filter": selected_filter,
                "search": search_term,
                "today": today.isoformat(),
            }
        )

    @staticmethod
    def task_sort_key(row: sqlite3.Row, today: date) -> tuple[int, str, str, int]:
        status = task_due_status(row, today)
        priority = {"overdue": 0, "today": 1, "upcoming": 2, "later": 3, "no_due_date": 4}
        due_date = row["due_date"] or "9999-12-31"
        return (priority.get(status, 5), due_date, row["created_at"], row["id"])

    def handle_list_history(self) -> None:
        with connect_db() as connection:
            rows = connection.execute(
                """
                SELECT id, task_id, task_title, event_type, occurred_at
                FROM history
                ORDER BY occurred_at DESC, id DESC
                """
            ).fetchall()
        self.send_json(
            {
                "events": [
                    {
                        "id": row["id"],
                        "task_id": row["task_id"],
                        "task_title": row["task_title"],
                        "event_type": row["event_type"],
                        "occurred_at": row["occurred_at"],
                    }
                    for row in rows
                ]
            }
        )

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/api/tasks":
            self.send_error_json(404, "Endpoint não encontrado.")
            return
        try:
            body = self.read_json_body()
            title = validate_title(body.get("title"))
            description = validate_description(body.get("description", ""))
            due_date = normalize_due_date(body.get("due_date"))
        except ValueError as exc:
            self.send_error_json(400, str(exc))
            return

        created_at = timestamp_now()
        with connect_db() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tasks (title, description, due_date, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (title, description, due_date, created_at, created_at),
            )
            task_id = int(cursor.lastrowid)
            add_history_event(
                connection,
                task_id=task_id,
                task_title=title,
                event_type="added",
                occurred_at=created_at,
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()

        self.send_json({"task": task_to_dict(row, local_now().date())}, status=201)

    def do_PATCH(self) -> None:  # noqa: N802
        match = re.fullmatch(r"/api/tasks/(\d+)", urlsplit(self.path).path)
        if not match:
            self.send_error_json(404, "Endpoint não encontrado.")
            return
        task_id = int(match.group(1))
        try:
            body = self.read_json_body()
            unknown_fields = set(body) - {"title", "description", "due_date", "completed"}
            if unknown_fields:
                raise ValueError("A solicitação contém campos desconhecidos.")
            if "completed" in body and not isinstance(body["completed"], bool):
                raise ValueError("O campo completed deve ser verdadeiro ou falso.")
        except ValueError as exc:
            self.send_error_json(400, str(exc))
            return

        with connect_db() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                self.send_error_json(404, "Tarefa não encontrada.")
                return

            try:
                title = validate_title(body["title"]) if "title" in body else row["title"]
                description = (
                    validate_description(body["description"])
                    if "description" in body
                    else row["description"]
                )
                due_date = (
                    normalize_due_date(body["due_date"])
                    if "due_date" in body
                    else row["due_date"]
                )
            except ValueError as exc:
                self.send_error_json(400, str(exc))
                return

            completed_at = row["completed_at"]
            was_completed = completed_at is not None
            is_completed = body.get("completed", was_completed)
            event_type = None
            event_time = None
            if is_completed and not was_completed:
                event_time = timestamp_now()
                completed_at = event_time
                event_type = "completed"
            elif not is_completed and was_completed:
                event_time = timestamp_now()
                completed_at = None
                event_type = "reopened"

            updated_at = timestamp_now()
            connection.execute(
                """
                UPDATE tasks
                SET title = ?, description = ?, due_date = ?,
                    completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (title, description, due_date, completed_at, updated_at, task_id),
            )
            if event_type is not None:
                add_history_event(
                    connection,
                    task_id=task_id,
                    task_title=title,
                    event_type=event_type,
                    occurred_at=event_time,
                )
            updated_row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()

        self.send_json({"task": task_to_dict(updated_row, local_now().date())})

    def do_DELETE(self) -> None:  # noqa: N802
        match = re.fullmatch(r"/api/tasks/(\d+)", urlsplit(self.path).path)
        if not match:
            self.send_error_json(404, "Endpoint não encontrado.")
            return
        task_id = int(match.group(1))
        with connect_db() as connection:
            row = connection.execute(
                "SELECT id, title FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                self.send_error_json(404, "Tarefa não encontrada.")
                return
            deleted_at = timestamp_now()
            add_history_event(
                connection,
                task_id=task_id,
                task_title=row["title"],
                event_type="deleted",
                occurred_at=deleted_at,
            )
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.send_json({"deleted": True, "id": task_id})

    def serve_web_asset(self, relative_path: str) -> None:
        candidate = (WEB_DIR / relative_path).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            self.send_error_json(404, "Recurso não encontrado.")
            return
        if not candidate.is_file():
            self.send_error_json(404, "Recurso não encontrado.")
            return
        self.serve_file(candidate)

    def serve_file(self, file_path: Path) -> None:
        if not file_path.is_file():
            self.send_error_json(
                404,
                "A interface ainda não foi criada. Esperado: web/index.html.",
            )
            return
        try:
            contents = file_path.read_bytes()
        except OSError:
            self.send_error_json(500, "Não foi possível ler o arquivo solicitado.")
            return
        content_type, _ = mimetypes.guess_type(str(file_path))
        if content_type is None:
            content_type = "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(contents)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(contents)

    def log_error(self, format_string: str, *args: object) -> None:
        super().log_error(format_string, *args)


class LocalTaskServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    initialize_database()
    server = LocalTaskServer((HOST, PORT), TaskHandler)
    print(f"App de tarefas disponível em http://{HOST}:{PORT}")
    print("Pressione Ctrl+C para encerrar o servidor.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando o app de tarefas...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
