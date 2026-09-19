from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "yanyan.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"


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


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with transaction() as conn:
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
        count = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        if count == 0:
            rows = [
                (kind, text, "中等", "会计学" if kind == "专业面" else "通用")
                for kind, questions in SEED_QUESTIONS.items()
                for text in questions
            ]
            conn.executemany(
                "INSERT INTO questions(interview_type, question_text, difficulty, major_scope) VALUES (?, ?, ?, ?)",
                rows,
            )


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
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
