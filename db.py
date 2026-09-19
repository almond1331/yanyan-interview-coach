from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "yanyan.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"
POSTGRES_SCHEMA_PATH = BASE_DIR / "schema_postgres.sql"

SUPABASE_SECRET_NAMES = (
    "SUPABASE_DB_HOST",
    "SUPABASE_DB_PORT",
    "SUPABASE_DB_NAME",
    "SUPABASE_DB_USER",
    "SUPABASE_DB_PASSWORD",
)


SEED_QUESTIONS = {
    "科研面": [
        "请用两分钟介绍一段你最有代表性的科研经历，并说明你的具体贡献。",
        "你的研究问题是如何提出的？为什么值得研究？",
        "请解释项目中采用的研究方法，以及你如何验证结果的可靠性。",
        "科研过程中遇到的最大困难是什么？你如何解决？",
        "如果继续推进这项研究，你下一步会做什么？",
    ],
    "英语面": [
        "Please introduce yourself and explain your research interests in English.",
        "Please describe one project you are proud of and your contribution to it.",
        "Why do you want to pursue postgraduate study at our university?",
        "What is your greatest academic strength, and how have you demonstrated it?",
        "Please explain your future research plan in English.",
    ],
    "专业面": [
        "请解释权责发生制与收付实现制的区别，并各举一个例子。",
        "企业应如何判断一项支出应当资本化还是费用化？",
        "请说明资产负债表、利润表和现金流量表之间的勾稽关系。",
        "收入确认的核心原则是什么？请结合履约义务进行说明。",
        "请分析存货计价方法对利润和资产的可能影响。",
    ],
    "行为面": [
        "请讲述一次你在团队中推动分歧达成共识的经历。",
        "请举例说明你如何在压力下安排多个重要任务。",
        "你为什么选择当前申请方向？这个选择经历了怎样的思考？",
        "请讲述一次失败经历，以及它如何改变了你的做事方式。",
        "如果研究生阶段的进展不及预期，你会如何调整？",
    ],
}


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str


def _secret(name: str) -> str:
    env_value = os.getenv(name, "").strip()
    if env_value:
        return env_value
    try:
        import streamlit as st

        value = st.secrets.get(name, "")
        return str(value).strip() if value is not None else ""
    except (ImportError, FileNotFoundError, RuntimeError):
        return ""


def postgres_settings() -> PostgresSettings | None:
    values = {name: _secret(name) for name in SUPABASE_SECRET_NAMES}
    configured = [name for name, value in values.items() if value]
    if not configured:
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"Supabase Secrets 配置不完整，缺少：{', '.join(missing)}")
    try:
        port = int(values["SUPABASE_DB_PORT"])
    except ValueError as exc:
        raise RuntimeError("SUPABASE_DB_PORT 必须是数字，Transaction pooler 通常使用 6543。") from exc
    if "://" in values["SUPABASE_DB_HOST"] or "/" in values["SUPABASE_DB_HOST"]:
        raise RuntimeError("SUPABASE_DB_HOST 只能填写 Host，不能填写完整连接字符串。")
    if not 1 <= port <= 65535:
        raise RuntimeError("SUPABASE_DB_PORT 不是有效端口。")
    return PostgresSettings(
        host=values["SUPABASE_DB_HOST"],
        port=port,
        dbname=values["SUPABASE_DB_NAME"],
        user=values["SUPABASE_DB_USER"],
        password=values["SUPABASE_DB_PASSWORD"],
    )


class DatabaseConnection:
    def __init__(self, raw: Any, backend: str) -> None:
        self.raw = raw
        self.backend = backend

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.backend == "postgres" else sql

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        return self.raw.execute(self._sql(sql), params)

    def executemany(self, sql: str, params: list[tuple[Any, ...]]) -> Any:
        return self.raw.executemany(self._sql(sql), params)

    def executescript(self, script: str) -> None:
        if self.backend == "sqlite":
            self.raw.executescript(script)
            return
        for statement in script.split(";"):
            if statement.strip():
                self.raw.execute(statement)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


def database_backend() -> str:
    return "postgres" if postgres_settings() else "sqlite"


def connect() -> DatabaseConnection:
    settings = postgres_settings()
    if settings:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("缺少 psycopg 依赖，请重新部署应用。") from exc
        raw = psycopg.connect(
            host=settings.host,
            port=settings.port,
            dbname=settings.dbname,
            user=settings.user,
            password=settings.password,
            sslmode="require",
            connect_timeout=10,
            prepare_threshold=None,
            row_factory=dict_row,
        )
        return DatabaseConnection(raw, "postgres")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(DB_PATH)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return DatabaseConnection(raw, "sqlite")


@contextmanager
def transaction() -> Iterator[DatabaseConnection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _seed_questions(conn: DatabaseConnection) -> None:
    count_row = conn.execute("SELECT COUNT(*) AS count FROM questions").fetchone()
    if int(count_row["count"]) == 0:
        rows = [
            (kind, text, "中等", "会计学" if kind == "专业面" else "通用")
            for kind, questions in SEED_QUESTIONS.items()
            for text in questions
        ]
        conn.executemany(
            "INSERT INTO questions(interview_type, question_text, difficulty, major_scope) VALUES (?, ?, ?, ?)",
            rows,
        )


def init_db() -> None:
    with transaction() as conn:
        if conn.backend == "postgres":
            conn.execute("SELECT pg_advisory_xact_lock(989447321)").fetchone()
            conn.executescript(POSTGRES_SCHEMA_PATH.read_text(encoding="utf-8"))
            _seed_questions(conn)
            return

        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for table_name in ("interview_sessions", "materials", "event_logs"):
            table_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
            if "visitor_id" not in table_columns:
                conn.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN visitor_id TEXT NOT NULL DEFAULT 'legacy'"
                )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(session_questions)")}
        if "parent_session_question_id" not in columns:
            conn.execute("ALTER TABLE session_questions ADD COLUMN parent_session_question_id INTEGER")
        if "generation_method" not in columns:
            conn.execute("ALTER TABLE session_questions ADD COLUMN generation_method TEXT NOT NULL DEFAULT 'rule'")
        answer_columns = {row[1] for row in conn.execute("PRAGMA table_info(answers)")}
        if "evaluation_method" not in answer_columns:
            conn.execute("ALTER TABLE answers ADD COLUMN evaluation_method TEXT NOT NULL DEFAULT 'rule'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_visitor_created "
            "ON interview_sessions(visitor_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_materials_visitor ON materials(visitor_id, uploaded_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_visitor_time "
            "ON event_logs(visitor_id, created_at DESC)"
        )
        _seed_questions(conn)


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    conn = connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> Any | None:
    conn = connect()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def log_event(
    event_name: str,
    visitor_id: str,
    session_id: int | None = None,
    page_name: str | None = None,
    params: dict[str, Any] | None = None,
) -> None:
    with transaction() as conn:
        if session_id is not None:
            owner = conn.execute(
                "SELECT visitor_id FROM interview_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if not owner or owner["visitor_id"] != visitor_id:
                raise ValueError("不能为其他访客的会话记录事件")
        conn.execute(
            """
            INSERT INTO event_logs(session_id, visitor_id, event_name, page_name, event_params)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, visitor_id, event_name, page_name, json.dumps(params or {}, ensure_ascii=False)),
        )
