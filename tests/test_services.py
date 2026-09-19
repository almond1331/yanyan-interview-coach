from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import db
import services
from ai_client import _parse_json


class YanyanMvpTests(unittest.TestCase):
    VISITOR_A = "visitor-a"
    VISITOR_B = "visitor-b"

    def setUp(self) -> None:
        os.environ["AI_DISABLED"] = "1"
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DATA_DIR = Path(self.temp_dir.name)
        db.UPLOAD_DIR = db.DATA_DIR / "uploads"
        db.DB_PATH = db.DATA_DIR / "test.db"
        db.init_db()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("AI_DISABLED", None)

    def test_full_process_generates_exactly_four_types(self) -> None:
        session_id = services.create_session(self.VISITOR_A, "全流程面试", "计算机", {}, "")
        rows = services.generate_session_questions(session_id, self.VISITOR_A)
        self.assertEqual(4, len(rows))
        self.assertEqual(set(services.INTERVIEW_TYPES), {row["interview_type"] for row in rows})

    def test_professional_material_beats_system_bank(self) -> None:
        session_id = services.create_session(self.VISITOR_A, "专业面", "计算机", {"专业面": 2}, "")
        services.save_material(
            visitor_id=self.VISITOR_A,
            session_id=session_id,
            material_type="专业资料",
            file_name="机器学习笔记.txt",
            data="支持向量机的间隔最大化原理\n神经网络的反向传播算法".encode("utf-8"),
        )
        rows = services.generate_session_questions(session_id, self.VISITOR_A)
        self.assertTrue(all(row["source_type"] == "专业资料" for row in rows))

    def test_school_exam_has_highest_priority_for_every_type(self) -> None:
        session_id = services.create_session(self.VISITOR_A, "全流程面试", "会计学", {}, "")
        services.save_material(visitor_id=self.VISITOR_A, session_id=session_id, material_type="简历", file_name="简历.txt", data=b"project")
        services.save_material(visitor_id=self.VISITOR_A, session_id=session_id, material_type="专业资料", file_name="笔记.txt", data=b"accounting")
        services.save_material(
            visitor_id=self.VISITOR_A,
            session_id=session_id,
            material_type="院校面试真题",
            file_name="真题.txt",
            data="请介绍你的研究计划？\n为什么选择本校？".encode("utf-8"),
        )
        rows = services.generate_session_questions(session_id, self.VISITOR_A)
        self.assertTrue(all(row["source_type"] == "院校面试真题" for row in rows))

    def test_resume_does_not_override_professional_system_bank(self) -> None:
        session_id = services.create_session(self.VISITOR_A, "专业面", "会计学", {"专业面": 1}, "")
        services.save_material(visitor_id=self.VISITOR_A, session_id=session_id, material_type="简历", file_name="简历.txt", data=b"project")
        rows = services.generate_session_questions(session_id, self.VISITOR_A)
        self.assertEqual("系统题库", rows[0]["source_type"])

    def test_answer_and_report_round_trip(self) -> None:
        session_id = services.create_session(self.VISITOR_A, "科研面", "人工智能", {"科研面": 1}, "")
        question = services.generate_session_questions(session_id, self.VISITOR_A)[0]
        result = services.submit_answer(
            session_id,
            question["session_question_id"],
            "首先我负责数据清洗与模型评估；其次通过消融实验验证方法，最后准确率提升了8%。",
            self.VISITOR_A,
        )
        services.finish_session(session_id, self.VISITOR_A)
        report = services.get_report(session_id, self.VISITOR_A)
        self.assertGreater(result["overall_score"], 50)
        self.assertEqual(1, report["summary"]["answered_questions"])
        self.assertEqual("completed", report["session"]["status"])

    def test_short_answer_creates_one_followup(self) -> None:
        session_id = services.create_session(self.VISITOR_A, "科研面", "人工智能", {"科研面": 1}, "")
        question = services.generate_session_questions(session_id, self.VISITOR_A)[0]
        evaluation = services.submit_answer(session_id, question["session_question_id"], "我做了一个项目。", self.VISITOR_A)
        followup_id = services.maybe_generate_followup(
            session_id, question["session_question_id"], "我做了一个项目。", evaluation, self.VISITOR_A
        )
        rows = services.get_session_questions(session_id, self.VISITOR_A)
        self.assertIsNotNone(followup_id)
        self.assertEqual(2, len(rows))
        self.assertEqual(1, rows[1]["is_followup"])
        second_attempt = services.maybe_generate_followup(
            session_id, question["session_question_id"], "我做了一个项目。", evaluation, self.VISITOR_A
        )
        self.assertIsNone(second_attempt)

    def test_deepseek_material_question_is_saved(self) -> None:
        session_id = services.create_session(self.VISITOR_A, "科研面", "人工智能", {"科研面": 1}, "关注实验设计")
        services.save_material(
            visitor_id=self.VISITOR_A,
            session_id=session_id,
            material_type="简历",
            file_name="简历.txt",
            data="我负责医学影像分割项目的数据清洗与消融实验。".encode("utf-8"),
        )
        with (
            patch.object(services, "provider_status", return_value=(True, "DeepSeek 已配置")),
            patch.object(services, "generate_interview_question", return_value="你如何设计消融实验以验证各模块贡献？"),
        ):
            rows = services.generate_session_questions(session_id, self.VISITOR_A)
        self.assertEqual("deepseek", rows[0]["generation_method"])
        self.assertIn("消融实验", rows[0]["question_text"])

    def test_deepseek_answer_evaluation_is_saved(self) -> None:
        session_id = services.create_session(self.VISITOR_A, "科研面", "人工智能", {"科研面": 1}, "")
        question = services.generate_session_questions(session_id, self.VISITOR_A)[0]
        ai_result = {
            "logic_score": 81.0,
            "completeness_score": 78.0,
            "accuracy_score": 84.0,
            "clarity_score": 82.0,
            "response_score": 79.0,
            "overall_score": 80.8,
            "weak_dimension": "完整度",
            "diagnosis": "回答说明了实验过程，但没有交代你的个人职责边界。",
            "suggestion": "补充你独立负责的步骤，并给出一个量化结果。",
            "reference_structure": "研究目标、个人职责、关键行动、实验结果、复盘。",
            "evaluation_method": "deepseek",
        }
        with (
            patch.object(services, "provider_status", return_value=(True, "DeepSeek 已配置")),
            patch.object(services, "analyze_interview_answer", return_value=ai_result),
        ):
            result = services.submit_answer(session_id, question["session_question_id"], "我完成了实验。", self.VISITOR_A)
        report = services.get_report(session_id, self.VISITOR_A)
        self.assertEqual("deepseek", result["evaluation_method"])
        self.assertEqual("deepseek", report["details"][0]["evaluation_method"])

    def test_identical_answer_reuses_saved_evaluation(self) -> None:
        session_id = services.create_session(self.VISITOR_A, "科研面", "人工智能", {"科研面": 1}, "")
        question = services.generate_session_questions(session_id, self.VISITOR_A)[0]
        answer = "我负责数据处理，并通过消融实验验证不同模块的贡献。"
        first = services.submit_answer(session_id, question["session_question_id"], answer, self.VISITOR_A)
        with patch.object(services, "analyze_interview_answer", side_effect=AssertionError("不应重复调用 API")):
            second = services.submit_answer(session_id, question["session_question_id"], answer, self.VISITOR_A)
        self.assertEqual(first["overall_score"], second["overall_score"])

    def test_visitors_cannot_read_or_answer_each_others_sessions(self) -> None:
        session_id = services.create_session(self.VISITOR_A, "科研面", "人工智能", {"科研面": 1}, "")
        question = services.generate_session_questions(session_id, self.VISITOR_A)[0]

        self.assertIsNone(services.get_report(session_id, self.VISITOR_B))
        self.assertEqual([], services.get_session_questions(session_id, self.VISITOR_B))
        self.assertEqual([], services.list_sessions(self.VISITOR_B))
        with self.assertRaises(ValueError):
            services.submit_answer(
                session_id,
                question["session_question_id"],
                "这是访客 B 的回答。",
                self.VISITOR_B,
            )

    def test_library_materials_are_visitor_scoped_and_not_saved_to_disk(self) -> None:
        services.save_material(
            visitor_id=self.VISITOR_A,
            session_id=None,
            material_type="专业资料",
            file_name="note.txt",
            data=b"private study note",
        )
        self.assertEqual(1, len(services.list_materials(self.VISITOR_A)))
        self.assertEqual([], services.list_materials(self.VISITOR_B))
        self.assertEqual([], list(db.UPLOAD_DIR.glob("**/*")))

    def test_daily_limit_rejects_fourth_session(self) -> None:
        for _ in range(services.DAILY_SESSION_LIMIT):
            services.create_session(self.VISITOR_A, "行为面", "通用", {"行为面": 1}, "")
        with self.assertRaises(services.UsageLimitError):
            services.create_session(self.VISITOR_A, "行为面", "通用", {"行为面": 1}, "")

    def test_ai_call_budget_limits_visitor_and_global_usage(self) -> None:
        session_a = services.create_session(self.VISITOR_A, "科研面", "人工智能", {"科研面": 1}, "")
        session_b = services.create_session(self.VISITOR_B, "科研面", "人工智能", {"科研面": 1}, "")
        with (
            patch.object(services, "AI_DAILY_CALL_LIMIT_PER_VISITOR", 1),
            patch.object(services, "AI_DAILY_CALL_LIMIT_GLOBAL", 2),
            db.transaction() as conn,
        ):
            self.assertTrue(services._reserve_ai_call(conn, self.VISITOR_A, session_a, "test"))
            self.assertFalse(services._reserve_ai_call(conn, self.VISITOR_A, session_a, "test"))
            self.assertTrue(services._reserve_ai_call(conn, self.VISITOR_B, session_b, "test"))
            self.assertFalse(services._reserve_ai_call(conn, "visitor-c", session_b, "test"))

        usage = services.ai_usage_status(self.VISITOR_A)
        self.assertEqual(1, usage["visitor_used"])
        events = db.fetch_all("SELECT event_name FROM event_logs ORDER BY event_id")
        self.assertEqual(2, sum(row["event_name"] == "ai_request_reserved" for row in events))
        self.assertEqual(2, sum(row["event_name"] == "ai_limit_reached" for row in events))

    def test_ai_limit_falls_back_without_interrupting_interview(self) -> None:
        session_id = services.create_session(self.VISITOR_A, "科研面", "人工智能", {"科研面": 2}, "")
        services.save_material(
            visitor_id=self.VISITOR_A,
            session_id=session_id,
            material_type="简历",
            file_name="resume.txt",
            data=b"research project experience",
        )
        with (
            patch.object(services, "provider_status", return_value=(True, "DeepSeek 已配置")),
            patch.object(services, "AI_DAILY_CALL_LIMIT_PER_VISITOR", 1),
            patch.object(services, "generate_interview_question", return_value="请介绍你的研究贡献和验证方法？") as generate,
        ):
            rows = services.generate_session_questions(session_id, self.VISITOR_A)

        self.assertEqual(1, generate.call_count)
        self.assertEqual(["deepseek", "rule"], [row["generation_method"] for row in rows])

    def test_upload_type_name_and_material_limit_are_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "仅支持"):
            services.save_material(
                visitor_id=self.VISITOR_A,
                session_id=None,
                material_type="专业资料",
                file_name="unsafe.exe",
                data=b"content",
            )
        with patch.object(services, "MAX_MATERIALS_PER_VISITOR", 1):
            services.save_material(
                visitor_id=self.VISITOR_A,
                session_id=None,
                material_type="专业资料",
                file_name="../../safe.txt",
                data=b"content",
            )
            self.assertEqual("safe.txt", services.list_materials(self.VISITOR_A)[0]["file_name"])
            with self.assertRaises(services.UsageLimitError):
                services.save_material(
                    visitor_id=self.VISITOR_A,
                    session_id=None,
                    material_type="专业资料",
                    file_name="second.txt",
                    data=b"content",
                )
    def test_large_upload_and_long_answer_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            services.save_material(
                visitor_id=self.VISITOR_A,
                session_id=None,
                material_type="专业资料",
                file_name="large.txt",
                data=b"x" * (services.MAX_UPLOAD_BYTES + 1),
            )
        session_id = services.create_session(self.VISITOR_A, "科研面", "人工智能", {"科研面": 1}, "")
        question = services.generate_session_questions(session_id, self.VISITOR_A)[0]
        with self.assertRaises(ValueError):
            services.submit_answer(
                session_id,
                question["session_question_id"],
                "答" * (services.MAX_ANSWER_CHARS + 1),
                self.VISITOR_A,
            )

    def test_clear_data_only_removes_current_visitor(self) -> None:
        services.create_session(self.VISITOR_A, "行为面", "通用", {"行为面": 1}, "")
        services.create_session(self.VISITOR_B, "行为面", "通用", {"行为面": 1}, "")
        services.clear_visitor_data(self.VISITOR_A)
        self.assertEqual([], services.list_sessions(self.VISITOR_A))
        self.assertEqual(1, len(services.list_sessions(self.VISITOR_B)))

    def test_clear_data_anonymizes_but_retains_global_ai_budget(self) -> None:
        session_id = services.create_session(self.VISITOR_A, "行为面", "通用", {"行为面": 1}, "")
        with db.transaction() as conn:
            self.assertTrue(services._reserve_ai_call(conn, self.VISITOR_A, session_id, "test"))
        services.clear_visitor_data(self.VISITOR_A)

        row = db.fetch_one("SELECT * FROM event_logs WHERE event_name='ai_request_reserved'")
        self.assertEqual("deleted", row["visitor_id"])
        self.assertIsNone(row["session_id"])
        self.assertNotIn(self.VISITOR_A, row["event_params"])
        self.assertEqual(1, services.ai_usage_status(self.VISITOR_B)["global_used"])

    def test_legacy_rows_are_not_exposed(self) -> None:
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO interview_sessions(practice_mode, interview_type, user_major)
                VALUES ('单项面试', '科研面', 'legacy')
                """
            )
        self.assertEqual([], services.list_sessions(self.VISITOR_A))

    def test_partial_supabase_config_is_rejected(self) -> None:
        with patch.dict(os.environ, {"SUPABASE_DB_HOST": "pooler.example.com"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "配置不完整"):
                db.postgres_settings()

    def test_postgres_placeholder_conversion(self) -> None:
        class FakeConnection:
            def execute(self, sql, params):
                return sql, params

        connection = db.DatabaseConnection(FakeConnection(), "postgres")
        sql, params = connection.execute("SELECT * FROM questions WHERE question_id=?", (7,))
        self.assertEqual("SELECT * FROM questions WHERE question_id=%s", sql)
        self.assertEqual((7,), params)

    def test_postgres_executemany_uses_cursor(self) -> None:
        raw_connection = MagicMock()
        cursor = raw_connection.cursor.return_value.__enter__.return_value
        connection = db.DatabaseConnection(raw_connection, "postgres")
        rows = [("科研面", "测试问题")]

        connection.executemany(
            "INSERT INTO questions(interview_type, question_text) VALUES (?, ?)",
            rows,
        )

        cursor.executemany.assert_called_once_with(
            "INSERT INTO questions(interview_type, question_text) VALUES (%s, %s)",
            rows,
        )
        raw_connection.executemany.assert_not_called()

    def test_transaction_pooler_connection_disables_prepared_statements(self) -> None:
        settings = {
            "SUPABASE_DB_HOST": "pooler.example.com",
            "SUPABASE_DB_PORT": "6543",
            "SUPABASE_DB_NAME": "postgres",
            "SUPABASE_DB_USER": "postgres.project-ref",
            "SUPABASE_DB_PASSWORD": "test-password",
        }
        connect_mock = Mock()
        fake_psycopg = types.ModuleType("psycopg")
        fake_psycopg.connect = connect_mock
        fake_rows = types.ModuleType("psycopg.rows")
        fake_rows.dict_row = object()
        with (
            patch.dict(os.environ, settings, clear=False),
            patch.dict(sys.modules, {"psycopg": fake_psycopg, "psycopg.rows": fake_rows}),
        ):
            connection = db.connect()
        self.assertEqual("postgres", connection.backend)
        self.assertEqual(6543, connect_mock.call_args.kwargs["port"])
        self.assertEqual("require", connect_mock.call_args.kwargs["sslmode"])
        self.assertIsNone(connect_mock.call_args.kwargs["prepare_threshold"])

    def test_postgres_schema_enables_rls_for_all_core_tables(self) -> None:
        schema = db.POSTGRES_SCHEMA_PATH.read_text(encoding="utf-8")
        for table in (
            "questions",
            "interview_sessions",
            "materials",
            "session_questions",
            "answers",
            "event_logs",
        ):
            self.assertIn(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", schema)

    def test_database_errors_are_safely_classified(self) -> None:
        auth_error = RuntimeError("password authentication failed for user")
        timeout_error = RuntimeError("connection timed out")
        self.assertEqual("DB-AUTH", db.classify_database_error(auth_error)[0])
        self.assertEqual("DB-TIMEOUT", db.classify_database_error(timeout_error)[0])

        wrapped_error = db.CloudDatabaseError("连接数据库", auth_error)
        self.assertEqual("连接数据库", wrapped_error.phase)
        self.assertEqual("DB-AUTH", db.classify_database_error(wrapped_error)[0])

    def test_database_error_details_hide_connection_secrets(self) -> None:
        settings = {
            "SUPABASE_DB_HOST": "aws-0-region.pooler.supabase.com",
            "SUPABASE_DB_PORT": "6543",
            "SUPABASE_DB_NAME": "postgres",
            "SUPABASE_DB_USER": "postgres.project-ref",
            "SUPABASE_DB_PASSWORD": "secret-password",
        }

        class DiagnosticError(RuntimeError):
            sqlstate = "08006"

        cause = DiagnosticError(
            "connection to 192.0.2.10 failed: "
            "host=aws-0-region.pooler.supabase.com "
            "user=postgres.project-ref password=secret-password "
            "postgresql://postgres.project-ref:secret-password@aws-0-region.pooler.supabase.com/postgres"
        )
        with patch.dict(os.environ, settings, clear=False):
            error_type, sqlstate, message = db.safe_database_error_details(
                db.CloudDatabaseError("连接数据库", cause)
            )

        self.assertEqual("DiagnosticError", error_type)
        self.assertEqual("08006", sqlstate)
        self.assertIn("[IP]", message)
        self.assertNotIn(settings["SUPABASE_DB_HOST"], message)
        self.assertNotIn(settings["SUPABASE_DB_USER"], message)
        self.assertNotIn(settings["SUPABASE_DB_PASSWORD"], message)

    def test_json_parser_accepts_code_fence_and_explanation(self) -> None:
        payload = _parse_json('结果如下：\n```json\n{"ok": true}\n```')
        self.assertIs(payload["ok"], True)


if __name__ == "__main__":
    unittest.main()
