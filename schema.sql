PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS questions (
    question_id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_type TEXT NOT NULL CHECK (interview_type IN ('科研面','英语面','专业面','行为面')),
    question_text TEXT NOT NULL,
    difficulty TEXT NOT NULL DEFAULT '中等',
    major_scope TEXT NOT NULL DEFAULT '通用',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS interview_sessions (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    visitor_id TEXT NOT NULL DEFAULT 'legacy',
    practice_mode TEXT NOT NULL CHECK (practice_mode IN ('全流程面试','单项面试')),
    interview_type TEXT CHECK (interview_type IN ('科研面','英语面','专业面','行为面')),
    user_major TEXT NOT NULL,
    research_count INTEGER NOT NULL DEFAULT 0,
    english_count INTEGER NOT NULL DEFAULT 0,
    professional_count INTEGER NOT NULL DEFAULT 0,
    behavior_count INTEGER NOT NULL DEFAULT 0,
    extra_requirement TEXT,
    status TEXT NOT NULL DEFAULT 'configured',
    started_at TEXT,
    finished_at TEXT,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS materials (
    material_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    visitor_id TEXT NOT NULL DEFAULT 'legacy',
    material_type TEXT NOT NULL CHECK (material_type IN ('简历','专业资料','院校面试真题','其他资料')),
    file_name TEXT NOT NULL,
    file_type TEXT,
    file_size INTEGER NOT NULL DEFAULT 0,
    content_text TEXT,
    parse_status TEXT NOT NULL DEFAULT 'success',
    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES interview_sessions(session_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS session_questions (
    session_question_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    sequence_no INTEGER NOT NULL,
    interview_type TEXT NOT NULL CHECK (interview_type IN ('科研面','英语面','专业面','行为面')),
    question_text TEXT NOT NULL,
    source_scope TEXT NOT NULL CHECK (source_scope IN ('system','user_material')),
    source_type TEXT NOT NULL CHECK (source_type IN ('院校面试真题','专业资料','简历','系统题库')),
    source_question_id INTEGER,
    source_material_id INTEGER,
    parent_session_question_id INTEGER,
    generation_method TEXT NOT NULL DEFAULT 'rule',
    is_followup INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (session_id, sequence_no),
    FOREIGN KEY (session_id) REFERENCES interview_sessions(session_id) ON DELETE CASCADE,
    FOREIGN KEY (source_question_id) REFERENCES questions(question_id) ON DELETE SET NULL,
    FOREIGN KEY (source_material_id) REFERENCES materials(material_id) ON DELETE SET NULL,
    FOREIGN KEY (parent_session_question_id) REFERENCES session_questions(session_question_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS answers (
    answer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_question_id INTEGER NOT NULL UNIQUE,
    session_id INTEGER NOT NULL,
    answer_text TEXT NOT NULL,
    answer_duration INTEGER NOT NULL DEFAULT 0,
    overall_score REAL NOT NULL,
    logic_score REAL NOT NULL,
    completeness_score REAL NOT NULL,
    accuracy_score REAL NOT NULL,
    clarity_score REAL NOT NULL,
    response_score REAL NOT NULL,
    weak_dimension TEXT NOT NULL,
    diagnosis TEXT NOT NULL,
    suggestion TEXT NOT NULL,
    reference_structure TEXT NOT NULL,
    evaluation_method TEXT NOT NULL DEFAULT 'rule',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_question_id) REFERENCES session_questions(session_question_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES interview_sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS event_logs (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    visitor_id TEXT NOT NULL DEFAULT 'legacy',
    event_name TEXT NOT NULL,
    page_name TEXT,
    event_params TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES interview_sessions(session_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_questions_type ON questions(interview_type, is_active);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON interview_sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_questions_session ON session_questions(session_id, sequence_no);
CREATE INDEX IF NOT EXISTS idx_answers_session ON answers(session_id);
CREATE INDEX IF NOT EXISTS idx_materials_type ON materials(material_type, session_id);
CREATE INDEX IF NOT EXISTS idx_events_name_time ON event_logs(event_name, created_at);
