# 言言陪练 MVP

一个使用 Python、Streamlit 和 SQLite 实现的保研面试文字陪练闭环：

`面试大厅 → 三步配置 → 抽题/生成题 → 文字作答 → AI 评分 → 报告与训练计划 → 历史记录`

## 项目结构

```text
言言陪练-mvp/
├─ app.py                  # Streamlit 页面与交互流程
├─ ai_client.py            # DeepSeek API、JSON 校验、超时和错误处理
├─ db.py                   # SQLite 连接、初始化、示例数据与埋点
├─ services.py             # 出题优先级、资料解析、评分、报告服务
├─ schema.sql              # 六张核心表、约束与索引
├─ queries.sql             # 历史、来源、维度、漏斗等分析 SQL
├─ requirements.txt
├─ tests/
│  └─ test_services.py     # 核心闭环与优先级测试
└─ data/
   ├─ yanyan.db            # 首次运行自动生成
   └─ uploads/             # 上传资料的本地副本
```

## 启动

建议使用 Python 3.11 或 3.12。

### Windows PowerShell

```powershell
cd "C:\Users\温漫玉\Documents\Codex\2026-09-15\n-h\outputs\言言陪练-mvp"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
Copy-Item .streamlit\secrets.toml.example .streamlit\secrets.toml
py -m streamlit run app.py
```

打开 `.streamlit/secrets.toml`，仅将 `DEEPSEEK_API_KEY` 替换为真实 Key。Base URL 必须写成纯文本网址，不能写成 Markdown 链接。`secrets.toml` 已被 `.gitignore` 忽略，禁止上传 GitHub。

启动应用后，侧边栏应显示“DeepSeek 已配置”。点击“测试 AI 连接”可验证 Key、Base URL 和模型 ID；这会产生极少量 API 用量。

浏览器打开终端显示的地址，通常是 `http://localhost:8501`。

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

## 数据库初始化

无需手工执行 SQL。`app.py` 启动时会调用 `init_db()`：

1. 执行 `schema.sql` 创建 `questions`、`interview_sessions`、`session_questions`、`answers`、`materials`、`event_logs`。
2. 当系统题库为空时，自动写入四类各 5 道示例题。
3. SQLite 文件默认位于 `data/yanyan.db`。

需要重置演示数据时，停止应用后删除 `data/yanyan.db`，重新启动即可。上传文件不会自动删除，可按需清理 `data/uploads/`。

## MVP 出题规则

- 全流程面试：固定四类各 1 题，共 4 题。
- 单项面试：只生成所选题型，题量由用户设置。
- 专业面：`院校面试真题 > 专业资料 > 系统专业题库`。
- 科研面 / 英语面 / 行为面：`院校面试真题 > 简历 > 系统通用题库`。
- 所有实际题目都会写入 `session_questions`，并记录 `source_scope`、`source_type`、来源题或来源资料 ID。
- 对过短或规则评分偏低的回答，MVP 会插入一条针对薄弱维度的追问；追问保留原题来源字段，且不会递归追问。
- 专业面系统题库当前为会计学方向，非会计用户且未上传可用资料时会收到页面提示，但仍可继续体验。

统一的资料出题入口是 `services.generate_questions_from_materials()`。存在可用资料和 DeepSeek 配置时，系统使用 `deepseek-flash` 生成问题；API 未配置、超时、鉴权失败或 JSON 无效时自动使用本地规则。系统题库抽题不调用模型。

## 上传资料说明

- TXT / MD / DOCX：MVP 会提取文本，并用关键词生成题目。
- PDF：MVP 保存文件及元数据，但不解析正文；仍可利用文件名生成模拟问题。
- “面试题库”页上传的资料作为全局资料，可供后续会话使用；配置流程上传的资料优先服务于当前会话。
- 演示环境无登录体系，所有历史和全局资料均视为同一个本地用户的数据。

## DeepSeek 配置

本地配置文件 `.streamlit/secrets.toml`：

```toml
DEEPSEEK_API_KEY = "sk-真实密钥"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-flash"
```

部署到 Streamlit Community Cloud 后，在应用 `Settings → Secrets` 中粘贴相同三项配置。不要将真实 Key 写入代码、README 或 GitHub。

## 评分说明

配置 DeepSeek 后，回答分析会生成五个维度分、薄弱项、具体诊断、修改建议和参考回答结构。模型输出经过 JSON 字段、分数范围和文本长度校验；失败时自动退回原规则评分。AI 反馈仅用于练习，不代表真实院校录取评价。

## 测试

```powershell
py -m unittest discover -s tests -v
```

测试覆盖：全流程四类各一题、院校真题最高优先级、专业资料优先级、简历不覆盖专业面规则、规则追问、DeepSeek 出题与评分入库，以及回答保存到报告生成的完整往返。测试默认关闭真实 API，不消耗余额。

## SQL 与 BI

`queries.sql` 提供六组可直接运行的查询：历史会话、题目来源占比、各题型维度分、高频薄弱项、核心漏斗、资料使用次数。应用内“数据看板”可以直接查看基础指标和最近 100 条埋点。
