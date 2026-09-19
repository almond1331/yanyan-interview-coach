from __future__ import annotations

import html
import logging
import os
import re
import uuid
from datetime import datetime

import streamlit as st

from ai_client import AIServiceError, provider_status, test_connection
from db import database_backend, init_db, log_event
from services import (
    DIMENSION_LABELS,
    INTERVIEW_TYPES,
    MAX_ANSWER_CHARS,
    MAX_UPLOAD_BYTES,
    UsageLimitError,
    clear_visitor_data,
    create_session,
    dashboard_metrics,
    finish_session,
    generate_session_questions,
    get_report,
    get_session_questions,
    list_materials,
    list_questions,
    list_sessions,
    maybe_generate_followup,
    save_material,
    submit_answer,
)


LOGGER = logging.getLogger(__name__)


st.set_page_config(page_title="言言陪练", page_icon="言", layout="wide", initial_sidebar_state="expanded")


@st.cache_resource
def initialize_database() -> str:
    init_db()
    return database_backend()


try:
    DATABASE_BACKEND = initialize_database()
except RuntimeError as exc:
    st.error(f"数据库配置错误：{exc}")
    st.info("请检查 Streamlit Cloud 的 Supabase Secrets 是否完整，并确认使用 Transaction pooler 参数。")
    st.stop()
except Exception:
    LOGGER.exception("Supabase database initialization failed")
    st.error("云数据库暂时无法连接，请稍后重试。")
    st.info("应用管理员可在 Streamlit Cloud 日志中查看具体原因。")
    st.stop()


def apply_styles() -> None:
    st.markdown(
        """
        <style>
        :root { --blue:#2563eb; --navy:#17365f; --pale:#f3f7ff; --line:#dce5f2; --muted:#64748b; }
        .stApp { background:#f5f8fd; color:#17365f; }
        [data-testid="stSidebar"] { background:#ffffff; border-right:1px solid #e5ebf4; }
        [data-testid="stSidebar"] .stRadio label { padding:.65rem .75rem; border-radius:8px; }
        [data-testid="stSidebar"] .stRadio label:has(input:checked) { background:#eaf2ff; color:#1d5eea; }
        .block-container { padding-top:2rem; max-width:1440px; }
        h1,h2,h3 { color:#17365f; letter-spacing:0 !important; }
        .eyebrow { color:#2563eb; font-weight:700; font-size:.9rem; margin-bottom:.3rem; }
        .subtle { color:#64748b; }
        .brand { display:flex; align-items:center; gap:.7rem; font-size:1.45rem; font-weight:800; color:#2563eb; margin:.25rem 0 1.7rem; }
        .brandmark { width:42px; height:42px; border-radius:8px; display:grid; place-items:center; color:white; background:#2563eb; }
        .mode-card { background:white; border:1px solid #dce5f2; border-radius:8px; padding:1.1rem; min-height:156px; }
        .mode-top { height:46px; border-radius:6px; display:flex; align-items:center; padding:0 .8rem; color:white; font-weight:800; margin-bottom:1rem; }
        .metric-card { background:white; border:1px solid #dce5f2; border-radius:8px; padding:1rem 1.1rem; min-height:110px; }
        .metric-label { color:#64748b; font-size:.86rem; }
        .metric-value { color:#17365f; font-size:1.9rem; font-weight:800; margin-top:.45rem; }
        .stepbar { display:flex; gap:.5rem; margin-bottom:1.2rem; }
        .step { flex:1; height:6px; border-radius:4px; background:#dce5f2; }
        .step.on { background:#2563eb; }
        .interview-shell { min-height:65vh; display:flex; flex-direction:column; justify-content:center; align-items:center; text-align:center; }
        .wave { display:flex; gap:7px; height:110px; align-items:center; justify-content:center; }
        .wave i { display:block; width:9px; border-radius:9px; background:#3b82f6; animation:pulse 1.1s ease-in-out infinite; }
        .wave i:nth-child(1),.wave i:nth-child(5) { height:36px; opacity:.45; }
        .wave i:nth-child(2),.wave i:nth-child(4) { height:66px; opacity:.75; animation-delay:.15s; }
        .wave i:nth-child(3) { height:96px; animation-delay:.3s; }
        @keyframes pulse { 0%,100% { transform:scaleY(.75); } 50% { transform:scaleY(1.12); } }
        .recording { color:#64748b; text-align:right; font-variant-numeric:tabular-nums; }
        .dot { display:inline-block; width:9px; height:9px; border-radius:50%; background:#ef4444; margin-right:.45rem; }
        .chat-ai,.chat-user { padding:.8rem 1rem; border-radius:8px; margin:.55rem 0; line-height:1.65; }
        .chat-ai { background:#eef3fa; }
        .chat-user { background:#2563eb; color:white; margin-left:12%; }
        .source-badge { display:inline-block; padding:.16rem .5rem; border-radius:999px; background:#eaf2ff; color:#2563eb; font-size:.78rem; }
        .score { font-size:3rem; font-weight:850; color:#2563eb; line-height:1; }
        .training-band { color:white; background:#173f72; border-radius:8px; padding:1.3rem 1.5rem; }
        div[data-testid="stButton"] button { border-radius:7px; min-height:2.65rem; font-weight:650; }
        div[data-testid="stFileUploader"] { background:white; border-radius:8px; }
        div[data-testid="stExpander"] { background:white; border-radius:8px; border-color:#dce5f2; }
        @media (max-width: 760px) { .block-container { padding:1rem; } .mode-card { min-height:auto; } .score { font-size:2.3rem; } }
        </style>
        """,
        unsafe_allow_html=True,
    )


apply_styles()


def init_state() -> None:
    defaults = {
        "show_config": False,
        "config_step": 1,
        "selected_mode": "全流程面试",
        "active_session_id": None,
        "current_question_index": 0,
        "show_dialog": False,
        "mic_on": True,
        "camera_on": False,
        "report_session_id": None,
        "nav_target": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


init_state()


def init_visitor_identity() -> str:
    raw_token = str(st.query_params.get("visitor", ""))
    existing_token = str(st.session_state.get("visitor_id", ""))
    if re.fullmatch(r"[a-f0-9]{32}", raw_token):
        token = raw_token
    elif re.fullmatch(r"[a-f0-9]{32}", existing_token):
        token = existing_token
    else:
        token = uuid.uuid4().hex
    st.session_state.visitor_id = token
    if raw_token != token:
        st.query_params["visitor"] = token
    return token


VISITOR_ID = init_visitor_identity()


def log_page_once(page: str, event: str) -> None:
    session_marker = st.session_state.get("active_session_id") or st.session_state.get("report_session_id")
    marker = f"{page}:{session_marker}"
    if st.session_state.get("last_page_marker") != marker:
        log_event(event, VISITOR_ID, st.session_state.get("active_session_id"), page)
        st.session_state.last_page_marker = marker


def sidebar() -> str:
    st.sidebar.markdown('<div class="brand"><span class="brandmark">言</span>言言陪练</div>', unsafe_allow_html=True)
    options = ["面试大厅", "面试记录", "面试题库"]
    if st.session_state.get("sidebar_nav") not in {None, *options}:
        st.session_state.sidebar_nav = options[0]
    target = st.session_state.pop("nav_target", None)
    if target in options:
        st.session_state.sidebar_nav = target
    current = st.sidebar.radio("导航", options, key="sidebar_nav", label_visibility="collapsed")
    st.sidebar.caption("保研 AI 面试助手 · MVP")
    if DATABASE_BACKEND == "postgres":
        st.sidebar.caption("数据存储：Supabase 云数据库")
    else:
        st.sidebar.caption("数据存储：本地 SQLite")
    ai_enabled, ai_message = provider_status()
    if ai_enabled:
        st.sidebar.success(ai_message)
        app_env = str(st.secrets.get("APP_ENV", os.getenv("APP_ENV", "production"))).lower()
        if app_env == "development" and st.sidebar.button(
            "测试 AI 连接", icon=":material/wifi_tethering:", use_container_width=True
        ):
            try:
                with st.spinner("正在连接 DeepSeek……"):
                    message = test_connection()
                st.sidebar.success(message)
            except AIServiceError as exc:
                st.sidebar.error(str(exc))
    else:
        st.sidebar.info(ai_message)
    st.sidebar.caption(f"匿名体验编号：{VISITOR_ID[-6:]}")
    st.sidebar.caption("历史属于当前带 visitor 参数的匿名链接。把完整网址发给别人，也会同时分享这份历史。")
    with st.sidebar.expander("隐私与数据"):
        st.caption("资料文本和回答会发送给 DeepSeek 用于出题与反馈。请勿上传身份证、电话等敏感信息。")
        if st.button("创建全新匿名体验", icon=":material/person_add:", use_container_width=True):
            new_visitor_id = uuid.uuid4().hex
            st.session_state.visitor_id = new_visitor_id
            st.query_params["visitor"] = new_visitor_id
            for key in (
                "active_session_id",
                "report_session_id",
                "show_config",
                "last_page_marker",
                "config_profile",
                "config_preferences",
            ):
                st.session_state.pop(key, None)
            st.rerun()
        st.caption("新体验不会删除旧数据；保存旧的完整网址，仍可返回原历史。")
        confirm_clear = st.checkbox("我确认清除当前匿名访客的全部数据", key="confirm_clear_data")
        if st.button("清除我的数据", disabled=not confirm_clear, use_container_width=True):
            clear_visitor_data(VISITOR_ID)
            for key in ("active_session_id", "report_session_id", "show_config"):
                st.session_state[key] = None if key != "show_config" else False
            st.session_state.confirm_clear_data = False
            st.success("当前匿名访客的数据已清除。")
            st.rerun()
    return current


def start_config(mode: str) -> None:
    st.session_state.pop("config_profile", None)
    st.session_state.pop("config_preferences", None)
    for widget_key in (
        "config_major",
        "config_resume",
        "config_material_type",
        "config_materials",
        "config_extra",
        "config_camera",
    ):
        st.session_state.pop(widget_key, None)
    st.session_state.selected_mode = mode
    st.session_state.show_config = True
    st.session_state.config_step = 1
    log_event("interview_type_click", VISITOR_ID, page_name="面试大厅", params={"selection": mode})
    log_event("start_interview_click", VISITOR_ID, page_name="面试大厅", params={"selection": mode})
    log_event("config_modal_open", VISITOR_ID, page_name="配置页", params={"selection": mode})


def defaults_for_mode(mode: str) -> dict[str, int]:
    return {kind: (1 if mode == "全流程面试" else (3 if kind == mode else 0)) for kind in INTERVIEW_TYPES}


@st.dialog("开始一轮模拟面试", width="large")
def config_dialog() -> None:
    step = st.session_state.config_step
    st.markdown(
        '<div class="stepbar">' + "".join(f'<div class="step {"on" if i <= step else ""}"></div>' for i in range(1, 4)) + "</div>",
        unsafe_allow_html=True,
    )
    st.caption(f"{step} / 3　{['用户信息', '面试偏好', '设备检查'][step - 1]}")
    if st.button("取消配置", icon=":material/close:"):
        st.session_state.show_config = False
        st.rerun()

    if step == 1:
        saved_profile = st.session_state.get("config_profile", {})
        st.session_state.setdefault("config_major", saved_profile.get("major", ""))
        st.session_state.setdefault("config_material_type", saved_profile.get("material_type", "专业资料"))
        st.subheader("完善用户信息")
        st.caption("专业为必填；上传资料后，言言会按题型匹配最合适的来源。")
        ai_enabled, _ = provider_status()
        if ai_enabled:
            st.info("AI 模式已开启：上传的资料片段和面试回答会发送给 DeepSeek 用于本轮出题与反馈。请勿上传身份证号等无关敏感信息。")
        major = st.text_input(
            "目标专业 / 申请方向 *",
            key="config_major",
            max_chars=80,
            placeholder="例如：计算机科学与技术",
        )
        resume = st.file_uploader("个人简历（选填，最大 5MB）", type=["pdf", "docx", "txt"], key="config_resume")
        material_type = st.segmented_control(
            "练习资料类型",
            ["院校面试真题", "专业资料", "其他资料"],
            key="config_material_type",
        )
        practice_files = st.file_uploader(
            "练习资料（选填，可多选，单个最大 5MB）",
            type=["pdf", "docx", "txt", "md"],
            accept_multiple_files=True,
            key="config_materials",
        )
        saved_resume = (
            {"name": resume.name, "size": resume.size, "data": resume.getvalue()}
            if resume
            else saved_profile.get("resume")
        )
        saved_materials = (
            [{"name": item.name, "size": item.size, "data": item.getvalue()} for item in practice_files]
            if practice_files
            else saved_profile.get("materials", [])
        )
        st.session_state.config_profile = {
            "major": major.strip(),
            "resume": saved_resume,
            "material_type": material_type,
            "materials": saved_materials,
        }
        if saved_resume or saved_materials:
            st.caption(f"已暂存 {int(bool(saved_resume)) + len(saved_materials)} 个文件，进入下一步后不会丢失。")
        _, right = st.columns([3, 1])
        with right:
            if st.button("继续", type="primary", use_container_width=True, disabled=not major.strip()):
                st.session_state.config_step = 2
                st.rerun()
    elif step == 2:
        mode = st.session_state.selected_mode
        saved_preferences = st.session_state.get("config_preferences", {})
        st.session_state.setdefault("config_extra", saved_preferences.get("extra_requirement", ""))
        st.subheader("设定面试偏好")
        if mode == "全流程面试":
            st.info("全流程面试固定从科研、英语、专业、行为四类各出 1 题，共 4 题。")
            counts = {kind: 1 for kind in INTERVIEW_TYPES}
            st.session_state.config_counts = counts
            st.columns(4)
            for column, kind in zip(st.columns(4), INTERVIEW_TYPES):
                column.metric(kind, "1 题")
        else:
            default_count = saved_preferences.get("counts", defaults_for_mode(mode)).get(mode, defaults_for_mode(mode)[mode])
            count = st.number_input(f"{mode}题目数量", min_value=1, max_value=5, value=default_count, step=1)
            st.session_state.config_counts = {kind: (int(count) if kind == mode else 0) for kind in INTERVIEW_TYPES}
        extra_requirement = st.text_area(
            "其他要求（选填）",
            key="config_extra",
            max_chars=500,
            placeholder="例如：多追问科研经历，回答后给出更严格的结构反馈",
        )
        st.session_state.config_preferences = {
            "counts": st.session_state.config_counts,
            "extra_requirement": extra_requirement.strip(),
        }
        left, _, right = st.columns([1, 2, 1])
        with left:
            if st.button("上一步", use_container_width=True):
                st.session_state.config_step = 1
                st.rerun()
        with right:
            if st.button("继续", type="primary", use_container_width=True):
                st.session_state.config_step = 3
                st.rerun()
    else:
        st.subheader("检查面试设备")
        st.caption("MVP 使用文字作答，以下为模拟设备检查，不会采集音视频。")
        c1, c2 = st.columns(2)
        c1.success("麦克风状态：模拟检测正常")
        c2.info("摄像头状态：可选，不影响文字练习")
        camera = st.toggle("模拟开启摄像头", value=False, key="config_camera")
        mode = st.session_state.selected_mode
        profile = st.session_state.get("config_profile", {})
        preferences = st.session_state.get("config_preferences", {})
        major = profile.get("major", "")
        material_type = profile.get("material_type", "专业资料")
        practice_materials = profile.get("materials", [])
        has_professional = material_type == "专业资料" and bool(practice_materials)
        has_exam = material_type == "院校面试真题" and bool(practice_materials)
        if (mode in {"专业面", "全流程面试"}) and "会计" not in major and not has_professional and not has_exam:
            st.warning("当前系统专业面题库主要覆盖会计学方向，非会计专业建议上传专业资料后练习。你仍可继续体验现有题库。")
        left, _, right = st.columns([1, 2, 1])
        with left:
            if st.button("上一步", use_container_width=True):
                st.session_state.config_step = 2
                st.rerun()
        with right:
            if st.button("开始面试", type="primary", use_container_width=True, icon=":material/play_arrow:"):
                counts = preferences.get("counts", defaults_for_mode(mode))
                extra_requirement = preferences.get("extra_requirement", "")
                resume = profile.get("resume")
                uploads = ([resume] if resume else []) + list(practice_materials)
                if any(uploaded["size"] > MAX_UPLOAD_BYTES for uploaded in uploads):
                    st.error("单个文件不能超过 5MB，请压缩后重试。")
                else:
                    try:
                        session_id = create_session(VISITOR_ID, mode, major, counts, extra_requirement)
                        if resume:
                            save_material(
                                visitor_id=VISITOR_ID,
                                session_id=session_id,
                                material_type="简历",
                                file_name=resume["name"],
                                data=resume["data"],
                            )
                        for uploaded in practice_materials:
                            save_material(
                                visitor_id=VISITOR_ID,
                                session_id=session_id,
                                material_type=material_type,
                                file_name=uploaded["name"],
                                data=uploaded["data"],
                            )
                        with st.spinner("言言正在准备本轮问题……"):
                            generate_session_questions(session_id, VISITOR_ID)
                        log_event(
                            "config_complete", VISITOR_ID, session_id, "配置页", {"major": major, "selection": mode}
                        )
                        log_event(
                            "interview_started",
                            VISITOR_ID,
                            session_id,
                            "模拟面试",
                            {"selection": mode, "camera_on": camera},
                        )
                        st.session_state.active_session_id = session_id
                        st.session_state.current_question_index = 0
                        st.session_state.show_dialog = False
                        st.session_state.camera_on = camera
                        st.session_state.show_config = False
                        st.session_state.pop("config_profile", None)
                        st.session_state.pop("config_preferences", None)
                        st.rerun()
                    except (UsageLimitError, ValueError) as exc:
                        st.error(str(exc))


def render_home() -> None:
    log_page_once("面试大厅", "page_view_home")
    st.markdown('<div class="eyebrow">模拟面试中心</div>', unsafe_allow_html=True)
    st.title("选择你的面试方式")
    st.markdown('<p class="subtle">围绕保研核心能力，完成一轮有针对性的文字模拟训练。</p>', unsafe_allow_html=True)
    modes = [
        ("全流程面试", "一次覆盖四类核心问题", "#2563eb", "约 20 分钟"),
        ("科研面", "讲清研究动机、方法与贡献", "#38a9db", "约 12 分钟"),
        ("英语面", "训练学术表达与即时回应", "#1687df", "约 10 分钟"),
        ("专业面", "夯实基础概念与专业思维", "#f59e0b", "约 12 分钟"),
        ("行为面", "提炼经历亮点与协作故事", "#10a77d", "约 10 分钟"),
    ]
    columns = st.columns(5)
    for column, (name, description, color, duration) in zip(columns, modes):
        with column:
            st.markdown(
                f'<div class="mode-card"><div class="mode-top" style="background:{color}">{name}</div><b>{description}</b><p class="subtle">{duration}</p></div>',
                unsafe_allow_html=True,
            )
            if st.button("开始面试", key=f"start_{name}", type="primary" if name == "全流程面试" else "secondary", use_container_width=True):
                start_config(name)
                st.rerun()
    st.divider()
    st.info("公开体验采用匿名链接隔离数据。上传资料和回答会用于 AI 分析，请勿填写身份证、电话等敏感信息。")
    metrics = dashboard_metrics(VISITOR_ID)
    c1, c2, c3 = st.columns(3)
    c1.metric("累计模拟", f"{metrics['sessions']} 次")
    c2.metric("历史平均分", f"{metrics['avg_score']} 分")
    c3.metric("累计练习时长", f"{round(metrics['total_seconds'] / 60)} 分钟")


def elapsed_label(session_id: int) -> str:
    report = get_report(session_id, VISITOR_ID)
    started = report["session"]["started_at"] if report else None
    if not started:
        return "00:00"
    try:
        seconds = max(0, int((datetime.now() - datetime.fromisoformat(started)).total_seconds()))
    except ValueError:
        seconds = 0
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def render_chat(questions: list, current_index: int) -> None:
    st.subheader("对话记录")
    for index, question in enumerate(questions[: current_index + 1]):
        st.markdown(
            f'<div class="chat-ai"><b>言言 · 面试官</b><br>{html.escape(question["question_text"])}<br><span class="source-badge">{html.escape(question["source_type"])} · {html.escape(question["generation_method"])}</span></div>',
            unsafe_allow_html=True,
        )
        answer = get_report(question["session_id"], VISITOR_ID)["details"][index]["answer_text"]
        if answer:
            st.markdown(f'<div class="chat-user">{html.escape(answer)}</div>', unsafe_allow_html=True)


def render_interview(session_id: int) -> None:
    questions = get_session_questions(session_id, VISITOR_ID)
    index = min(st.session_state.current_question_index, max(0, len(questions) - 1))
    st.markdown(f'<div class="recording"><span class="dot"></span>录制中　{elapsed_label(session_id)}</div>', unsafe_allow_html=True)
    top_left, top_right = st.columns([1, 5])
    with top_left:
        if st.button("返回选择", icon=":material/arrow_back:"):
            st.session_state.active_session_id = None
            st.rerun()
    if not questions:
        st.error("本轮未生成题目，请返回重试。")
        return
    if st.session_state.show_dialog:
        chat_col, stage_col = st.columns([1.15, 2.85])
        with chat_col:
            render_chat(questions, index)
    else:
        stage_col = st.container()
    with stage_col:
        st.markdown(
            '<div class="interview-shell"><div class="wave"><i></i><i></i><i></i><i></i><i></i></div><h3>言言正在聆听</h3><p class="subtle">请在下方完整输入你的回答，页面默认不直接展示题目</p></div>',
            unsafe_allow_html=True,
        )
        answer_key = f"answer_{session_id}_{questions[index]['session_question_id']}"
        st.text_area(
            "输入你的回答",
            key=answer_key,
            height=150,
            max_chars=MAX_ANSWER_CHARS,
            placeholder="完成思考后，在这里输入文字回答……",
        )
        mic_col, camera_col, dialog_col, done_col, end_col = st.columns([.65, .65, .65, 1.2, 1.7])
        with mic_col:
            if st.button("麦克风", icon=":material/mic:" if st.session_state.mic_on else ":material/mic_off:", use_container_width=True):
                st.session_state.mic_on = not st.session_state.mic_on
                log_event("mic_click", VISITOR_ID, session_id, "模拟面试", {"enabled": st.session_state.mic_on})
                st.rerun()
        with camera_col:
            if st.button("摄像头", icon=":material/videocam:" if st.session_state.camera_on else ":material/videocam_off:", use_container_width=True):
                st.session_state.camera_on = not st.session_state.camera_on
                log_event("camera_click", VISITOR_ID, session_id, "模拟面试", {"enabled": st.session_state.camera_on})
                st.rerun()
        with dialog_col:
            if st.button("对话", icon=":material/chat_bubble:", use_container_width=True):
                st.session_state.show_dialog = not st.session_state.show_dialog
                log_event("dialog_open", VISITOR_ID, session_id, "模拟面试")
                st.rerun()
        answer_text = st.session_state.get(answer_key, "").strip()
        with done_col:
            if st.button("答题完毕", type="primary", use_container_width=True, disabled=not answer_text):
                with st.spinner("言言正在分析你的回答……"):
                    evaluation = submit_answer(
                        session_id, questions[index]["session_question_id"], answer_text, VISITOR_ID
                    )
                followup_id = maybe_generate_followup(
                    session_id, questions[index]["session_question_id"], answer_text, evaluation, VISITOR_ID
                )
                log_event(
                    "answer_complete_click",
                    VISITOR_ID,
                    session_id,
                    "模拟面试",
                    {"question_id": questions[index]["session_question_id"]},
                )
                if followup_id:
                    st.session_state.current_question_index = index + 1
                    st.toast("已记录回答，言言生成了一条针对性追问")
                elif index < len(questions) - 1:
                    st.session_state.current_question_index = index + 1
                    st.toast("回答已保存，进入下一题")
                else:
                    st.toast("本轮题目已全部完成")
                st.rerun()
        with end_col:
            report = get_report(session_id, VISITOR_ID)
            if st.button(
                "结束并查看报告",
                use_container_width=True,
                disabled=not answer_text and not report["summary"]["answered_questions"],
            ):
                if answer_text:
                    with st.spinner("正在完成最后一题分析……"):
                        submit_answer(session_id, questions[index]["session_question_id"], answer_text, VISITOR_ID)
                log_event("end_interview_click", VISITOR_ID, session_id, "模拟面试")
                finish_session(session_id, VISITOR_ID)
                st.session_state.report_session_id = session_id
                st.session_state.active_session_id = None
                st.rerun()
        st.caption(f"第 {index + 1} / {len(questions)} 题 · {questions[index]['interview_type']}")


def render_report(session_id: int) -> None:
    report = get_report(session_id, VISITOR_ID)
    if not report:
        st.error("未找到该场面试。")
        return
    log_page_once("面试报告", "report_view")
    session, summary, details = report["session"], report["summary"], report["details"]
    title = session["practice_mode"] if session["practice_mode"] == "全流程面试" else session["interview_type"]
    st.markdown('<div class="eyebrow">训练结果</div>', unsafe_allow_html=True)
    st.title("面试分析报告")
    st.caption(f"{title} · {session['user_major']} · 会话 #{session_id}")
    c1, c2, c3, c4 = st.columns([1.25, 1, 1, 1])
    with c1:
        st.markdown(f'<div class="metric-card"><div class="metric-label">综合评分</div><div class="score">{summary["overall_score"] or 0}</div></div>', unsafe_allow_html=True)
    c2.metric("本轮总题数", summary["total_questions"])
    c3.metric("已回答", summary["answered_questions"])
    c4.metric("面试用时", f"{round(session['duration_seconds'] / 60, 1)} 分钟")
    st.subheader("分维度评分")
    score_cols = st.columns(5)
    for column, (key, label) in zip(score_cols, DIMENSION_LABELS.items()):
        column.metric(label, f"{summary[key] or 0} 分")
        column.progress(float(summary[key] or 0) / 100)
    st.subheader("逐题分析")
    for item in details:
        kind_label = "追问" if item["is_followup"] else item["interview_type"]
        label = f"第 {item['sequence_no']} 题 · {kind_label} · {item['overall_score'] or 0} 分"
        with st.expander(label):
            st.markdown(f"**题目**　{item['question_text']}")
            st.caption(f"来源：{item['source_type']} / {item['source_scope']} · 出题：{item['generation_method']}")
            st.markdown(f"**你的回答**　{item['answer_text'] or '未作答'}")
            st.markdown(f"**问题诊断**　{item['diagnosis'] or '暂无'}")
            st.markdown(f"**修改建议**　{item['suggestion'] or '完成作答后生成'}")
            st.markdown(f"**参考回答结构**　{item['reference_structure'] or '完成作答后生成'}")
            if item["evaluation_method"]:
                st.caption(f"反馈方式：{item['evaluation_method']}")
    weaknesses = [row["weak_dimension"] for row in report["weaknesses"]] or ["回答完整度"]
    st.markdown(
        f'<div class="training-band"><h3 style="color:white">训练计划</h3><b>本轮总体表现</b><p>已完成 {summary["answered_questions"]} / {summary["total_questions"]} 题，综合评分 {summary["overall_score"] or 0}。</p><b>薄弱项</b><p>{"、".join(weaknesses[:3])}</p><b>总结</b><p>保持结论先行，并用具体行动和结果支撑判断。</p><b>下一轮训练建议</b><p>围绕“{weaknesses[0]}”完成一轮专项练习；每题回答后检查是否包含结论、证据和反思。</p></div>',
        unsafe_allow_html=True,
    )
    st.write("")
    left, right = st.columns([1, 4])
    if left.button("返回大厅", icon=":material/home:", use_container_width=True):
        st.session_state.report_session_id = None
        st.session_state.nav_target = "面试大厅"
        st.rerun()
    if right.button("再练一轮", type="primary", icon=":material/replay:"):
        log_event("next_practice_click", VISITOR_ID, session_id, "面试报告", {"weak_dimension": weaknesses[0]})
        start_config(title if title in INTERVIEW_TYPES else "全流程面试")
        st.session_state.report_session_id = None
        st.rerun()


def render_history() -> None:
    log_page_once("面试记录", "page_view_record")
    st.markdown('<div class="eyebrow">训练成长档案</div>', unsafe_allow_html=True)
    st.title("面试记录")
    st.caption("查看每一次模拟的表现，找到下一轮最值得投入的提升点。")
    sessions = list_sessions(VISITOR_ID)
    if not sessions:
        st.info("还没有面试记录，从面试大厅开始第一轮练习。")
        return
    for session in sessions:
        title = session["practice_mode"] if session["practice_mode"] == "全流程面试" else session["interview_type"]
        with st.container(border=True):
            c1, c2, c3, c4, c5 = st.columns([1.1, 1.7, 1.2, .9, .9])
            c1.markdown(f"**{title}**")
            c2.caption(f"{session['created_at']} · {session['user_major']}")
            c3.write(f"{session['answer_count']} / {session['question_count']} 题")
            c4.write(f"**{session['overall_score'] or 0}** / 100")
            if c5.button("查看报告", key=f"history_{session['session_id']}"):
                st.session_state.report_session_id = session["session_id"]
                st.rerun()


def render_library() -> None:
    log_page_once("面试题库", "page_view_question_bank")
    st.markdown('<div class="eyebrow">资料与题目管理</div>', unsafe_allow_html=True)
    st.title("我的面试题库")
    st.caption("上传过往真题或复试资料，后续训练会按业务优先级自动参考。")
    with st.container(border=True):
        st.subheader("上传题目或资料")
        c1, c2 = st.columns([1, 2])
        material_type = c1.selectbox("资料类型", ["院校面试真题", "专业资料", "简历", "其他资料"], key="library_type")
        uploaded = c2.file_uploader("支持 PDF、DOCX、TXT、MD，单个文件不超过 5MB", type=["pdf", "docx", "txt", "md"], key="library_upload")
        if st.button("上传面试资料", type="primary", icon=":material/upload:", disabled=uploaded is None):
            if uploaded.size > MAX_UPLOAD_BYTES:
                st.error("文件超过 5MB。")
            else:
                save_material(
                    visitor_id=VISITOR_ID,
                    session_id=None,
                    material_type=material_type,
                    file_name=uploaded.name,
                    data=uploaded.getvalue(),
                )
                st.success("资料已上传，可用于后续抽题。")
                st.rerun()
    tab_materials, tab_questions = st.tabs(["已上传资料", "系统题库"])
    with tab_materials:
        materials = list_materials(VISITOR_ID)
        if not materials:
            st.info("暂无上传资料。")
        for item in materials:
            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 1, 1])
                c1.markdown(f"**{item['file_name']}**")
                c1.caption(f"{item['material_type']} · {item['uploaded_at']}")
                c2.write(f"{round(item['file_size'] / 1024, 1)} KB")
                c3.write("已解析" if item["parse_status"] == "success" else "已保存元数据")
    with tab_questions:
        question_filter = st.selectbox("题型筛选", ["全部", *INTERVIEW_TYPES], key="question_filter")
        questions = list_questions(question_filter)
        st.caption(f"共 {len(questions)} 道系统题")
        for item in questions:
            st.markdown(f"- **{item['interview_type']}** · {item['question_text']}  `{item['major_scope']}`")


nav = sidebar()
if st.session_state.active_session_id:
    render_interview(st.session_state.active_session_id)
elif st.session_state.report_session_id:
    render_report(st.session_state.report_session_id)
elif nav == "面试大厅":
    render_home()
elif nav == "面试记录":
    render_history()
else:
    render_library()

if st.session_state.show_config:
    config_dialog()
