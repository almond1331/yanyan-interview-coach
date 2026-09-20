-- 1. 历史面试列表与完成率、平均分
SELECT s.session_id, s.practice_mode, s.interview_type, s.user_major, s.created_at,
       COUNT(DISTINCT sq.session_question_id) AS question_count,
       COUNT(DISTINCT a.answer_id) AS answered_count,
       ROUND(AVG(a.overall_score), 1) AS overall_score
FROM interview_sessions s
LEFT JOIN session_questions sq ON sq.session_id = s.session_id
LEFT JOIN answers a ON a.session_id = s.session_id
GROUP BY s.session_id
ORDER BY s.created_at DESC;

-- 2. 题目来源占比，可直接用于 BI 看板
SELECT source_scope, source_type, interview_type, COUNT(*) AS question_count
FROM session_questions
GROUP BY source_scope, source_type, interview_type
ORDER BY question_count DESC;

-- 3. 各面试类型的平均维度分
SELECT sq.interview_type,
       ROUND(AVG(a.logic_score), 1) AS logic_score,
       ROUND(AVG(a.completeness_score), 1) AS completeness_score,
       ROUND(AVG(a.accuracy_score), 1) AS accuracy_score,
       ROUND(AVG(a.clarity_score), 1) AS clarity_score,
       ROUND(AVG(a.response_score), 1) AS response_score
FROM answers a
JOIN session_questions sq ON sq.session_question_id = a.session_question_id
GROUP BY sq.interview_type;

-- 4. 高频薄弱项
SELECT weak_dimension, COUNT(*) AS occurrences, ROUND(AVG(overall_score), 1) AS avg_score
FROM answers
GROUP BY weak_dimension
ORDER BY occurrences DESC, avg_score ASC;

-- 5. 核心漏斗
SELECT event_name, COUNT(*) AS event_count
FROM event_logs
WHERE event_name IN (
  'page_view_home', 'start_interview_click', 'config_complete',
  'interview_started', 'interview_finished', 'report_view'
)
GROUP BY event_name;

-- 6. 上传资料后实际被用于出题的次数
SELECT m.material_type, m.file_name, COUNT(sq.session_question_id) AS used_question_count
FROM materials m
LEFT JOIN session_questions sq ON sq.source_material_id = m.material_id
GROUP BY m.material_id
ORDER BY used_question_count DESC;

-- 7. 每日 AI 调用预算、限额和降级监控
SELECT DATE(created_at) AS usage_date,
       SUM(CASE WHEN event_name = 'ai_request_reserved' THEN 1 ELSE 0 END) AS ai_calls,
       SUM(CASE WHEN event_name = 'ai_limit_reached' THEN 1 ELSE 0 END) AS limited_requests,
       SUM(CASE WHEN event_name = 'ai_fallback_used' THEN 1 ELSE 0 END) AS failed_fallbacks
FROM event_logs
WHERE event_name IN ('ai_request_reserved', 'ai_limit_reached', 'ai_fallback_used')
GROUP BY DATE(created_at)
ORDER BY usage_date DESC;

-- 8. 报告反馈：有用度、题目贴合度和文字建议量
SELECT DATE(f.created_at) AS feedback_date,
       COUNT(*) AS feedback_count,
       ROUND(AVG(f.helpful_score), 2) AS avg_helpful_score,
       ROUND(AVG(f.relevance_score), 2) AS avg_relevance_score,
       SUM(CASE WHEN f.comment <> '' THEN 1 ELSE 0 END) AS comment_count
FROM session_feedback f
GROUP BY DATE(f.created_at)
ORDER BY feedback_date DESC;
