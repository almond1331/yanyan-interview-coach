from __future__ import annotations

import io
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from ai_client import AIServiceError, analyze_interview_answer, generate_interview_question, provider_status
from db import fetch_all, fetch_one, log_event, transaction


INTERVIEW_TYPES = ("科研面", "英语面", "专业面", "行为面")
COUNT_COLUMNS = {
    "科研面": "research_count",
    "英语面": "english_count",
    "专业面": "professional_count",
    "行为面": "behavior_count",
}
DIMENSION_LABELS = {
    "logic_score": "逻辑表达",
    "completeness_score": "完整度",
    "accuracy_score": "专业准确性",
    "clarity_score": "表达清晰度",
    "response_score": "临场反应",
}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_EXTRACTED_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_ANSWER_CHARS = 3000
MAX_FEEDBACK_CHARS = 500
MAX_FILES_PER_CONFIG = 3
MAX_MATERIALS_PER_VISITOR = 20
DAILY_SESSION_LIMIT = 3
GLOBAL_DAILY_SESSION_LIMIT = 30
AI_DAILY_CALL_LIMIT_PER_VISITOR = 20
AI_DAILY_CALL_LIMIT_GLOBAL = 120
ALLOWED_UPLOAD_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}
MATERIAL_TYPES = {"简历", "专业资料", "院校面试真题", "其他资料"}


class UsageLimitError(ValueError):
    pass


def _owned_session(conn: Any, session_id: int, visitor_id: str) -> Any:
    session = conn.execute(
        "SELECT * FROM interview_sessions WHERE session_id = ? AND visitor_id = ?",
        (session_id, visitor_id),
    ).fetchone()
    if not session:
        raise ValueError("面试会话不存在或无权访问")
    return session


def _china_day_window() -> tuple[datetime, datetime]:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _sql_timestamp(value: datetime) -> str:
    return value.isoformat(sep=" ", timespec="seconds")


def _clean_file_name(file_name: str) -> tuple[str, str]:
    clean_name = Path(file_name.replace("\\", "/")).name
    clean_name = re.sub(r"[\x00-\x1f\x7f]", "", clean_name).strip()
    if not clean_name or len(clean_name) > 200:
        raise ValueError("文件名不能为空，且不能超过 200 字")
    suffix = Path(clean_name).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise ValueError("仅支持 PDF、DOCX、TXT、MD 文件")
    return clean_name, suffix


def extract_text(file_name: str, data: bytes) -> tuple[str, str]:
    suffix = Path(file_name).suffix.lower()
    try:
        if suffix in {".txt", ".md", ".csv"}:
            return data.decode("utf-8", errors="ignore")[:30000], "success"
        if suffix == ".docx":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                document = archive.getinfo("word/document.xml")
                if document.file_size > MAX_EXTRACTED_DOCUMENT_BYTES:
                    return "", "failed"
                xml = archive.read(document)
            root = ET.fromstring(xml)
            text = "\n".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
            return text[:30000], "success"
        if suffix == ".pdf":
            return "", "metadata_only"
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return "", "failed"
    return "", "metadata_only"


def save_material(
    *,
    visitor_id: str,
    session_id: int | None,
    material_type: str,
    file_name: str,
    data: bytes,
) -> int:
    if not data:
        raise ValueError("上传文件为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("单个文件不能超过 5MB")
    if material_type not in MATERIAL_TYPES:
        raise ValueError("无效的资料类型")
    clean_name, suffix = _clean_file_name(file_name)
    content_text, parse_status = extract_text(clean_name, data)
    with transaction() as conn:
        if session_id is not None:
            _owned_session(conn, session_id, visitor_id)
        material_count = conn.execute(
            "SELECT COUNT(*) AS count FROM materials WHERE visitor_id=?",
            (visitor_id,),
        ).fetchone()["count"]
        if int(material_count) >= MAX_MATERIALS_PER_VISITOR:
            raise UsageLimitError("当前匿名体验最多保留 20 份资料，请先清除旧数据或创建新体验。")
        cursor = conn.execute(
            """
            INSERT INTO materials(
                session_id, visitor_id, material_type, file_name, file_type,
                file_size, content_text, parse_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING material_id
            """,
            (
                session_id,
                visitor_id,
                material_type,
                clean_name,
                suffix,
                len(data),
                content_text,
                parse_status,
            ),
        )
        material_id = int(cursor.fetchone()["material_id"])
    log_event(
        "material_upload_success",
        visitor_id,
        session_id,
        "配置页" if session_id else "面试题库",
        {"material_type": material_type, "file_name": clean_name, "file_size": len(data)},
    )
    return material_id


def create_session(
    visitor_id: str,
    selection: str,
    user_major: str,
    counts: dict[str, int],
    extra_requirement: str,
) -> int:
    if selection not in {"全流程面试", *INTERVIEW_TYPES}:
        raise ValueError("无效的面试类型")
    cleaned_major = user_major.strip()
    cleaned_requirement = extra_requirement.strip()
    if not cleaned_major or len(cleaned_major) > 80:
        raise ValueError("目标专业需填写，且不能超过 80 字")
    if len(cleaned_requirement) > 500:
        raise ValueError("附加要求不能超过 500 字")
    practice_mode = "全流程面试" if selection == "全流程面试" else "单项面试"
    interview_type = None if practice_mode == "全流程面试" else selection
    if practice_mode == "全流程面试":
        counts = {kind: 1 for kind in INTERVIEW_TYPES}
    elif not 1 <= int(counts.get(selection, 0)) <= 5:
        raise ValueError("单项面试题量需为 1 到 5 题")
    started_at = datetime.now().isoformat(timespec="seconds")
    day_start, day_end = _china_day_window()
    with transaction() as conn:
        today_count = conn.execute(
            """
            SELECT COUNT(*) AS count FROM interview_sessions
            WHERE visitor_id = ? AND created_at >= ? AND created_at < ?
            """,
            (visitor_id, _sql_timestamp(day_start), _sql_timestamp(day_end)),
        ).fetchone()["count"]
        if int(today_count) >= DAILY_SESSION_LIMIT:
            raise UsageLimitError("今天已完成 3 次体验，请明天再来练习。")
        global_today_count = conn.execute(
            "SELECT COUNT(*) AS count FROM interview_sessions WHERE created_at >= ? AND created_at < ?",
            (_sql_timestamp(day_start), _sql_timestamp(day_end)),
        ).fetchone()["count"]
        if int(global_today_count) >= GLOBAL_DAILY_SESSION_LIMIT:
            raise UsageLimitError("今天的公开体验名额已用完，请明天再来。")
        cursor = conn.execute(
            """
            INSERT INTO interview_sessions(
                visitor_id, practice_mode, interview_type, user_major, research_count, english_count,
                professional_count, behavior_count, extra_requirement, status, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?)
            RETURNING session_id
            """,
            (
                visitor_id,
                practice_mode,
                interview_type,
                cleaned_major,
                counts.get("科研面", 0),
                counts.get("英语面", 0),
                counts.get("专业面", 0),
                counts.get("行为面", 0),
                cleaned_requirement,
                started_at,
            ),
        )
        return int(cursor.fetchone()["session_id"])


def _material_for_type(materials: list[Any], interview_type: str) -> Any | None:
    by_type: dict[str, list[Any]] = {}
    for material in materials:
        by_type.setdefault(material["material_type"], []).append(material)
    if by_type.get("院校面试真题"):
        return by_type["院校面试真题"][0]
    if interview_type == "专业面" and by_type.get("专业资料"):
        return by_type["专业资料"][0]
    if interview_type in {"科研面", "英语面", "行为面"} and by_type.get("简历"):
        return by_type["简历"][0]
    return None


def _reserve_ai_call(conn: Any, visitor_id: str, session_id: int, task: str) -> bool:
    day_start, day_end = _china_day_window()
    if conn.backend == "postgres":
        conn.execute("SELECT pg_advisory_xact_lock(989447322)").fetchone()
    usage = conn.execute(
        """
        SELECT COUNT(*) AS global_used,
               COALESCE(SUM(CASE WHEN visitor_id=? THEN 1 ELSE 0 END), 0) AS visitor_used
        FROM event_logs
        WHERE event_name='ai_request_reserved' AND created_at >= ? AND created_at < ?
        """,
        (visitor_id, _sql_timestamp(day_start), _sql_timestamp(day_end)),
    ).fetchone()
    visitor_used = int(usage["visitor_used"])
    global_used = int(usage["global_used"])
    limit_scope = None
    if visitor_used >= AI_DAILY_CALL_LIMIT_PER_VISITOR:
        limit_scope = "visitor"
    elif global_used >= AI_DAILY_CALL_LIMIT_GLOBAL:
        limit_scope = "global"
    event_name = "ai_limit_reached" if limit_scope else "ai_request_reserved"
    conn.execute(
        """
        INSERT INTO event_logs(session_id, visitor_id, event_name, page_name, event_params)
        VALUES (?, ?, ?, 'AI 服务', ?)
        """,
        (session_id, visitor_id, event_name, json.dumps({"task": task, "scope": limit_scope}, ensure_ascii=False)),
    )
    return limit_scope is None


def _ai_call_allowed(conn: Any, visitor_id: str, session_id: int, task: str) -> bool:
    enabled, _ = provider_status()
    if not enabled:
        return False
    if conn.backend == "postgres":
        # Keep the advisory lock short; never hold it during the external API request.
        with transaction() as budget_conn:
            return _reserve_ai_call(budget_conn, visitor_id, session_id, task)
    return _reserve_ai_call(conn, visitor_id, session_id, task)


def ai_usage_status(visitor_id: str) -> dict[str, int]:
    day_start, day_end = _china_day_window()
    row = fetch_one(
        """
        SELECT COUNT(*) AS global_used,
               COALESCE(SUM(CASE WHEN visitor_id=? THEN 1 ELSE 0 END), 0) AS visitor_used
        FROM event_logs
        WHERE event_name='ai_request_reserved' AND created_at >= ? AND created_at < ?
        """,
        (visitor_id, _sql_timestamp(day_start), _sql_timestamp(day_end)),
    )
    visitor_used = int(row["visitor_used"]) if row else 0
    global_used = int(row["global_used"]) if row else 0
    return {
        "visitor_used": visitor_used,
        "visitor_limit": AI_DAILY_CALL_LIMIT_PER_VISITOR,
        "visitor_remaining": max(0, AI_DAILY_CALL_LIMIT_PER_VISITOR - visitor_used),
        "global_used": global_used,
        "global_limit": AI_DAILY_CALL_LIMIT_GLOBAL,
        "global_remaining": max(0, AI_DAILY_CALL_LIMIT_GLOBAL - global_used),
    }


def remaining_material_capacity(visitor_id: str) -> int:
    row = fetch_one("SELECT COUNT(*) AS count FROM materials WHERE visitor_id=?", (visitor_id,))
    used = int(row["count"]) if row else 0
    return max(0, MAX_MATERIALS_PER_VISITOR - used)


def _material_question(material: Any, interview_type: str, index: int) -> str:
    source_text = (material["content_text"] or "").strip()
    lines = [
        re.sub(r"^[\d一二三四五六七八九十、.()（）\-\s]+", "", line).strip()
        for line in source_text.splitlines()
        if len(line.strip()) >= 8
    ]
    if material["material_type"] == "院校面试真题" and lines:
        candidate = lines[index % len(lines)]
        return candidate if candidate.endswith(("？", "?")) else f"结合院校往年考查方向，请回答：{candidate}"
    keyword = lines[index % len(lines)][:36] if lines else Path(material["file_name"]).stem
    templates = {
        "科研面": f"结合你资料中提到的“{keyword}”，请说明研究动机、方法和你的具体贡献。",
        "英语面": f'Please explain the experience related to "{keyword}" and what you learned from it.',
        "专业面": f"结合专业资料中的“{keyword}”，请解释其核心概念、适用场景与一个具体例子。",
        "行为面": f"围绕“{keyword}”，请讲述一次具体经历，并说明你的行动、结果与反思。",
    }
    return templates[interview_type]


def generate_questions_from_materials(
    conn: Any,
    session_id: int,
    visitor_id: str,
    requested_types: Iterable[str],
    user_major: str = "",
    extra_requirement: str = "",
) -> list[dict[str, Any]]:
    """Generate material-based questions with DeepSeek and a deterministic fallback."""
    materials = conn.execute(
        """
        SELECT * FROM materials
        WHERE visitor_id = ? AND (session_id = ? OR session_id IS NULL)
        ORDER BY CASE WHEN session_id = ? THEN 0 ELSE 1 END, uploaded_at DESC
        """,
        (visitor_id, session_id, session_id),
    ).fetchall()
    generated: list[dict[str, Any]] = []
    type_indices: Counter[str] = Counter()
    ai_question_attempted = False
    for interview_type in requested_types:
        index = type_indices[interview_type]
        type_indices[interview_type] += 1
        material = _material_for_type(materials, interview_type)
        if material:
            generation_method = "rule"
            question_text = _material_question(material, interview_type, index)
            should_use_ai = material["material_type"] != "院校面试真题" and not ai_question_attempted
            if should_use_ai and _ai_call_allowed(conn, visitor_id, session_id, "question_generation"):
                ai_question_attempted = True
                try:
                    question_text = generate_interview_question(
                        interview_type=interview_type,
                        material_type=material["material_type"],
                        material_name=material["file_name"],
                        material_text=material["content_text"] or material["file_name"],
                        user_major=user_major,
                        extra_requirement=extra_requirement,
                    )
                    generation_method = "deepseek"
                except AIServiceError as exc:
                    conn.execute(
                        """
                        INSERT INTO event_logs(session_id, visitor_id, event_name, page_name, event_params)
                        VALUES (?, ?, 'ai_fallback_used', '抽题服务', ?)
                        """,
                        (
                            session_id,
                            visitor_id,
                            json.dumps(
                                {"task": "question_generation", "reason": str(exc)[:200]},
                                ensure_ascii=False,
                            ),
                        ),
                    )
            generated.append(
                {
                    "interview_type": interview_type,
                    "question_text": question_text,
                    "source_scope": "user_material",
                    "source_type": material["material_type"],
                    "source_question_id": None,
                    "source_material_id": material["material_id"],
                    "generation_method": generation_method,
                }
            )
            continue
        rows = conn.execute(
            "SELECT * FROM questions WHERE interview_type = ? AND is_active = 1 ORDER BY RANDOM()",
            (interview_type,),
        ).fetchall()
        if not rows:
            raise ValueError(f"系统题库缺少 {interview_type} 题目")
        row = rows[index % len(rows)]
        generated.append(
            {
                "interview_type": interview_type,
                "question_text": row["question_text"],
                "source_scope": "system",
                "source_type": "系统题库",
                "source_question_id": row["question_id"],
                "source_material_id": None,
                "generation_method": "system_bank",
            }
        )
    return generated


def generate_session_questions(session_id: int, visitor_id: str) -> list[Any]:
    with transaction() as conn:
        session = _owned_session(conn, session_id, visitor_id)
        existing = conn.execute(
            "SELECT * FROM session_questions WHERE session_id = ? ORDER BY sequence_no", (session_id,)
        ).fetchall()
        if existing:
            return existing
        requested_types: list[str] = []
        if session["practice_mode"] == "全流程面试":
            requested_types = list(INTERVIEW_TYPES)
        else:
            interview_type = session["interview_type"]
            count = max(1, int(session[COUNT_COLUMNS[interview_type]]))
            requested_types = [interview_type] * count
        questions = generate_questions_from_materials(
            conn,
            session_id,
            visitor_id,
            requested_types,
            user_major=session["user_major"],
            extra_requirement=session["extra_requirement"] or "",
        )
        for sequence_no, question in enumerate(questions, start=1):
            conn.execute(
                """
                INSERT INTO session_questions(
                    session_id, sequence_no, interview_type, question_text, source_scope,
                    source_type, source_question_id, source_material_id, generation_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    sequence_no,
                    question["interview_type"],
                    question["question_text"],
                    question["source_scope"],
                    question["source_type"],
                    question["source_question_id"],
                    question["source_material_id"],
                    question["generation_method"],
                ),
            )
    rows = get_session_questions(session_id, visitor_id)
    for row in rows:
        log_event(
            "ai_question_generated",
            visitor_id,
            session_id,
            "模拟面试",
            {"question_id": row["session_question_id"], "question_type": row["interview_type"], "source_type": row["source_type"]},
        )
    return rows


def get_session_questions(session_id: int, visitor_id: str) -> list[Any]:
    return fetch_all(
        """
        SELECT sq.* FROM session_questions sq
        JOIN interview_sessions s ON s.session_id = sq.session_id
        WHERE sq.session_id = ? AND s.visitor_id = ?
        ORDER BY sq.sequence_no
        """,
        (session_id, visitor_id),
    )


def evaluate_answer(answer_text: str, interview_type: str) -> dict[str, Any]:
    text = answer_text.strip()
    length = len(text)
    structure_hits = sum(word in text for word in ("首先", "其次", "最后", "背景", "任务", "行动", "结果", "反思"))
    evidence_hits = sum(word in text for word in ("例如", "比如", "%", "数据", "结果", "提升", "降低", "验证"))
    logic = min(94, 55 + min(length // 12, 22) + structure_hits * 4)
    completeness = min(94, 52 + min(length // 10, 28) + evidence_hits * 3)
    accuracy = min(92, 58 + min(length // 15, 18) + evidence_hits * 3)
    clarity = min(95, 58 + min(length // 13, 24) + structure_hits * 3)
    response = min(92, 60 + min(length // 16, 22) + (4 if length >= 80 else 0))
    if interview_type == "英语面" and re.search(r"[A-Za-z]{4,}", text):
        clarity = min(95, clarity + 5)
        accuracy = min(95, accuracy + 3)
    scores = {
        "logic_score": float(logic),
        "completeness_score": float(completeness),
        "accuracy_score": float(accuracy),
        "clarity_score": float(clarity),
        "response_score": float(response),
    }
    weak_key = min(scores, key=scores.get)
    weak_dimension = DIMENSION_LABELS[weak_key]
    overall = round(sum(scores.values()) / len(scores), 1)
    suggestions = {
        "逻辑表达": "先用一句话给出结论，再按背景、行动、结果分层展开，避免信息并列堆叠。",
        "完整度": "补充关键背景、你的具体职责、可验证结果和复盘，形成完整闭环。",
        "专业准确性": "增加专业概念、方法选择依据和验证过程，避免只描述过程。",
        "表达清晰度": "缩短长句，减少模糊指代，用关键词标记回答的三个层次。",
        "临场反应": "先确认问题重点，停顿一秒组织框架，再用结论先行的方式作答。",
    }
    diagnosis = (
        f"回答已覆盖主要内容，当前最需要提升的是{weak_dimension}。"
        if length >= 60
        else f"回答偏简略，信息证据不足，主要薄弱维度为{weak_dimension}。"
    )
    return {
        **scores,
        "overall_score": overall,
        "weak_dimension": weak_dimension,
        "diagnosis": diagnosis,
        "suggestion": suggestions[weak_dimension],
        "reference_structure": "结论（1句）→ 背景/目标 → 你的关键行动 → 量化或可验证结果 → 反思与迁移。",
        "evaluation_method": "rule",
    }


def submit_answer(
    session_id: int,
    session_question_id: int,
    answer_text: str,
    visitor_id: str,
    duration: int = 0,
) -> dict[str, Any]:
    cleaned_answer = answer_text.strip()
    if not cleaned_answer:
        raise ValueError("回答不能为空")
    if len(cleaned_answer) > MAX_ANSWER_CHARS:
        raise ValueError("回答不能超过 3000 字")
    question = fetch_one(
        """
        SELECT sq.* FROM session_questions sq
        JOIN interview_sessions s ON s.session_id = sq.session_id
        WHERE sq.session_question_id = ? AND sq.session_id = ? AND s.visitor_id = ?
        """,
        (session_question_id, session_id, visitor_id),
    )
    if not question:
        raise ValueError("题目不存在")
    existing = fetch_one(
        "SELECT * FROM answers WHERE session_id=? AND session_question_id=?",
        (session_id, session_question_id),
    )
    if existing and existing["answer_text"] == cleaned_answer:
        return {
            "overall_score": existing["overall_score"],
            "logic_score": existing["logic_score"],
            "completeness_score": existing["completeness_score"],
            "accuracy_score": existing["accuracy_score"],
            "clarity_score": existing["clarity_score"],
            "response_score": existing["response_score"],
            "weak_dimension": existing["weak_dimension"],
            "diagnosis": existing["diagnosis"],
            "suggestion": existing["suggestion"],
            "reference_structure": existing["reference_structure"],
            "evaluation_method": existing["evaluation_method"],
        }
    result = evaluate_answer(answer_text, question["interview_type"])
    with transaction() as conn:
        ai_allowed = _ai_call_allowed(conn, visitor_id, session_id, "answer_evaluation")
    if ai_allowed:
        try:
            result = analyze_interview_answer(
                question_text=question["question_text"],
                answer_text=cleaned_answer,
                interview_type=question["interview_type"],
            )
        except AIServiceError as exc:
            log_event(
                "ai_fallback_used",
                visitor_id,
                session_id,
                "评分服务",
                {"task": "answer_evaluation", "question_id": session_question_id, "reason": str(exc)[:200]},
            )
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO answers(
                session_question_id, session_id, answer_text, answer_duration, overall_score,
                logic_score, completeness_score, accuracy_score, clarity_score, response_score,
                weak_dimension, diagnosis, suggestion, reference_structure, evaluation_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_question_id) DO UPDATE SET
                answer_text=excluded.answer_text, answer_duration=excluded.answer_duration,
                overall_score=excluded.overall_score, logic_score=excluded.logic_score,
                completeness_score=excluded.completeness_score, accuracy_score=excluded.accuracy_score,
                clarity_score=excluded.clarity_score, response_score=excluded.response_score,
                weak_dimension=excluded.weak_dimension, diagnosis=excluded.diagnosis,
                suggestion=excluded.suggestion, reference_structure=excluded.reference_structure,
                evaluation_method=excluded.evaluation_method
            """,
            (
                session_question_id,
                session_id,
                cleaned_answer,
                duration,
                result["overall_score"],
                result["logic_score"],
                result["completeness_score"],
                result["accuracy_score"],
                result["clarity_score"],
                result["response_score"],
                result["weak_dimension"],
                result["diagnosis"],
                result["suggestion"],
                result["reference_structure"],
                result["evaluation_method"],
            ),
        )
    log_event(
        "answer_submitted",
        visitor_id,
        session_id,
        "模拟面试",
        {"question_id": session_question_id, "score": result["overall_score"]},
    )
    return result


def maybe_generate_followup(
    session_id: int,
    session_question_id: int,
    answer_text: str,
    evaluation: dict[str, Any],
    visitor_id: str,
) -> int | None:
    """Insert one rule-based follow-up immediately after a weak/short base answer."""
    with transaction() as conn:
        _owned_session(conn, session_id, visitor_id)
        question = conn.execute(
            "SELECT * FROM session_questions WHERE session_id=? AND session_question_id=?",
            (session_id, session_question_id),
        ).fetchone()
        if not question or question["is_followup"]:
            return None
        existing = conn.execute(
            "SELECT session_question_id FROM session_questions WHERE parent_session_question_id=?",
            (session_question_id,),
        ).fetchone()
        needs_followup = len(answer_text.strip()) < 80 or float(evaluation["overall_score"]) < 68
        if existing or not needs_followup:
            return None
        sequence_no = int(question["sequence_no"]) + 1
        conn.execute(
            "UPDATE session_questions SET sequence_no=sequence_no+1000 WHERE session_id=? AND sequence_no>=?",
            (session_id, sequence_no),
        )
        conn.execute(
            "UPDATE session_questions SET sequence_no=sequence_no-999 WHERE session_id=? AND sequence_no>=1000",
            (session_id,),
        )
        prompts = {
            "逻辑表达": "追问：请先用一句话概括你的结论，再按两个关键步骤展开。",
            "完整度": "追问：你能补充当时的具体背景、你的行动和最终结果吗？",
            "专业准确性": "追问：请补充你选择该方法的专业依据，以及如何验证它有效。",
            "表达清晰度": "追问：请用三句话重新概括，分别说明目标、行动和结果。",
            "临场反应": "追问：如果条件发生变化，你会如何调整方案？请说明判断依据。",
        }
        cursor = conn.execute(
            """
            INSERT INTO session_questions(
                session_id, sequence_no, interview_type, question_text, source_scope, source_type,
                source_question_id, source_material_id, parent_session_question_id, generation_method, is_followup
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'rule_followup', 1)
            RETURNING session_question_id
            """,
            (
                session_id,
                sequence_no,
                question["interview_type"],
                prompts[evaluation["weak_dimension"]],
                question["source_scope"],
                question["source_type"],
                question["source_question_id"],
                question["source_material_id"],
                session_question_id,
            ),
        )
        followup_id = int(cursor.fetchone()["session_question_id"])
    log_event(
        "ai_followup_generated",
        visitor_id,
        session_id,
        "模拟面试",
        {"question_id": followup_id, "parent_question_id": session_question_id, "weak_dimension": evaluation["weak_dimension"]},
    )
    return followup_id


def finish_session(session_id: int, visitor_id: str) -> None:
    session = fetch_one(
        "SELECT started_at FROM interview_sessions WHERE session_id=? AND visitor_id=?",
        (session_id, visitor_id),
    )
    if not session:
        raise ValueError("面试会话不存在或无权访问")
    started_at = datetime.fromisoformat(session["started_at"]) if session and session["started_at"] else datetime.now()
    finished_at = datetime.now()
    duration = max(0, int((finished_at - started_at).total_seconds()))
    with transaction() as conn:
        conn.execute(
            """
            UPDATE interview_sessions
            SET status='completed', finished_at=?, duration_seconds=?
            WHERE session_id=? AND visitor_id=?
            """,
            (finished_at.isoformat(timespec="seconds"), duration, session_id, visitor_id),
        )
    answered = fetch_one("SELECT COUNT(*) AS count FROM answers WHERE session_id=?", (session_id,))["count"]
    log_event("interview_finished", visitor_id, session_id, "模拟面试", {"completed_question_count": answered})


def get_report(session_id: int, visitor_id: str) -> dict[str, Any] | None:
    session = fetch_one(
        "SELECT * FROM interview_sessions WHERE session_id=? AND visitor_id=?",
        (session_id, visitor_id),
    )
    if not session:
        return None
    summary = fetch_one(
        """
        SELECT COUNT(DISTINCT sq.session_question_id) AS total_questions,
               COUNT(DISTINCT a.answer_id) AS answered_questions,
               ROUND(AVG(a.overall_score),1) AS overall_score,
               ROUND(AVG(a.logic_score),1) AS logic_score,
               ROUND(AVG(a.completeness_score),1) AS completeness_score,
               ROUND(AVG(a.accuracy_score),1) AS accuracy_score,
               ROUND(AVG(a.clarity_score),1) AS clarity_score,
               ROUND(AVG(a.response_score),1) AS response_score
        FROM session_questions sq
        LEFT JOIN answers a ON a.session_question_id=sq.session_question_id
        WHERE sq.session_id=?
        """,
        (session_id,),
    )
    details = fetch_all(
        """
        SELECT sq.*, a.answer_text, a.overall_score, a.weak_dimension, a.diagnosis,
               a.suggestion, a.reference_structure, a.evaluation_method
        FROM session_questions sq
        LEFT JOIN answers a ON a.session_question_id=sq.session_question_id
        WHERE sq.session_id=? ORDER BY sq.sequence_no
        """,
        (session_id,),
    )
    weak_rows = fetch_all(
        "SELECT weak_dimension, COUNT(*) AS count FROM answers WHERE session_id=? GROUP BY weak_dimension ORDER BY count DESC, weak_dimension",
        (session_id,),
    )
    return {"session": session, "summary": summary, "details": details, "weaknesses": weak_rows}


def get_session_feedback(session_id: int, visitor_id: str) -> Any | None:
    return fetch_one(
        """
        SELECT f.* FROM session_feedback f
        JOIN interview_sessions s ON s.session_id=f.session_id
        WHERE f.session_id=? AND s.visitor_id=?
        """,
        (session_id, visitor_id),
    )


def save_session_feedback(
    session_id: int,
    visitor_id: str,
    helpful_score: int,
    relevance_score: int,
    comment: str = "",
) -> None:
    if helpful_score not in range(1, 6) or relevance_score not in range(1, 6):
        raise ValueError("反馈评分必须为 1 到 5 分")
    cleaned_comment = comment.strip()
    if len(cleaned_comment) > MAX_FEEDBACK_CHARS:
        raise ValueError("反馈建议不能超过 500 字")
    with transaction() as conn:
        session = _owned_session(conn, session_id, visitor_id)
        if session["status"] != "completed":
            raise ValueError("完成面试后才能提交反馈")
        conn.execute(
            """
            INSERT INTO session_feedback(session_id, helpful_score, relevance_score, comment)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                helpful_score=excluded.helpful_score,
                relevance_score=excluded.relevance_score,
                comment=excluded.comment,
                updated_at=CURRENT_TIMESTAMP
            """,
            (session_id, helpful_score, relevance_score, cleaned_comment),
        )
    log_event(
        "report_feedback_submitted",
        visitor_id,
        session_id,
        "面试报告",
        {"helpful_score": helpful_score, "relevance_score": relevance_score},
    )


def list_sessions(visitor_id: str) -> list[Any]:
    return fetch_all(
        """
        SELECT s.*, COUNT(DISTINCT sq.session_question_id) AS question_count,
               COUNT(DISTINCT a.answer_id) AS answer_count, ROUND(AVG(a.overall_score),1) AS overall_score
        FROM interview_sessions s
        LEFT JOIN session_questions sq ON sq.session_id=s.session_id
        LEFT JOIN answers a ON a.session_id=s.session_id
        WHERE s.visitor_id = ?
        GROUP BY s.session_id ORDER BY s.created_at DESC
        """,
        (visitor_id,),
    )


def list_materials(visitor_id: str) -> list[Any]:
    return fetch_all(
        "SELECT * FROM materials WHERE visitor_id = ? ORDER BY uploaded_at DESC",
        (visitor_id,),
    )


def list_questions(interview_type: str | None = None) -> list[Any]:
    if interview_type and interview_type != "全部":
        return fetch_all("SELECT * FROM questions WHERE interview_type=? ORDER BY question_id", (interview_type,))
    return fetch_all("SELECT * FROM questions ORDER BY interview_type, question_id")


def dashboard_metrics(visitor_id: str) -> dict[str, Any]:
    row = fetch_one(
        """
        SELECT (SELECT COUNT(*) FROM interview_sessions WHERE visitor_id=?) AS sessions,
               COALESCE((
                   SELECT ROUND(AVG(a.overall_score),1) FROM answers a
                   JOIN interview_sessions s ON s.session_id=a.session_id
                   WHERE s.visitor_id=?
               ),0) AS avg_score,
               COALESCE((SELECT SUM(duration_seconds) FROM interview_sessions WHERE visitor_id=?),0) AS total_seconds
        """,
        (visitor_id, visitor_id, visitor_id),
    )
    return dict(row) if row else {"sessions": 0, "avg_score": 0, "total_seconds": 0}


def recent_events(visitor_id: str, limit: int = 100) -> list[Any]:
    return fetch_all(
        "SELECT * FROM event_logs WHERE visitor_id=? ORDER BY event_id DESC LIMIT ?",
        (visitor_id, limit),
    )


def daily_session_count(visitor_id: str) -> int:
    day_start, day_end = _china_day_window()
    row = fetch_one(
        """
        SELECT COUNT(*) AS count
        FROM interview_sessions
        WHERE visitor_id = ? AND created_at >= ? AND created_at < ?
        """,
        (visitor_id, _sql_timestamp(day_start), _sql_timestamp(day_end)),
    )
    return int(row["count"]) if row else 0


def clear_visitor_data(visitor_id: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM interview_sessions WHERE visitor_id=?", (visitor_id,))
        conn.execute("DELETE FROM materials WHERE visitor_id=?", (visitor_id,))
        conn.execute(
            """
            DELETE FROM event_logs
            WHERE visitor_id=? AND event_name NOT IN ('ai_request_reserved', 'ai_limit_reached')
            """,
            (visitor_id,),
        )
        conn.execute(
            """
            UPDATE event_logs
            SET visitor_id='deleted', event_params='{"retained_for":"daily_cost_limit"}'
            WHERE visitor_id=? AND event_name IN ('ai_request_reserved', 'ai_limit_reached')
            """,
            (visitor_id,),
        )
