# 言言陪练 MVP

一个使用 Python、Streamlit、Supabase Postgres 和 SQLite 实现的保研面试文字陪练闭环：

`面试大厅 → 三步配置 → 抽题/生成题 → 文字作答 → AI 评分 → 报告与训练计划 → 历史记录`

## 项目结构

```text
言言陪练-mvp/
├─ app.py                  # Streamlit 页面与交互流程
├─ ai_client.py            # DeepSeek API、JSON 校验、超时和错误处理
├─ db.py                   # Supabase/SQLite 连接、初始化、示例数据与埋点
├─ services.py             # 出题优先级、资料解析、评分、报告服务
├─ schema.sql              # 七张业务表、约束与索引
├─ schema_postgres.sql     # Supabase Postgres 表结构、索引与 RLS
├─ queries.sql             # 历史、来源、维度、漏斗等分析 SQL
├─ requirements.txt
├─ .streamlit/config.toml # 服务端上传与消息大小限制
├─ tests/
│  └─ test_services.py     # 核心闭环与优先级测试
└─ data/
   └─ yanyan.db            # 首次运行自动生成
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
结构化出题和评分会关闭模型思考模式，避免推理内容占用输出额度或截断 JSON；无效或空响应会触发明确错误并由业务层回退到规则模式。

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

1. 执行 `schema.sql` 创建 `questions`、`interview_sessions`、`session_questions`、`answers`、`materials`、`event_logs`、`session_feedback`。
2. 当系统题库为空时，自动写入四类各 5 道示例题。
3. SQLite 文件默认位于 `data/yanyan.db`。

需要重置本地演示数据时，停止应用后删除 `data/yanyan.db`，重新启动即可。

## 匿名体验与公开安全

- 首次访问会生成随机 `visitor` 参数。会话、资料、报告、首页统计和埋点均按该匿名链接隔离。
- 带 `visitor` 参数的完整网址相当于匿名历史的访问凭证，请勿分享。
- 这不是登录系统：把同一条带 `visitor` 参数的网址粘贴到无痕窗口，仍会打开同一份历史。访问不带参数的主页，或点击“创建全新匿名体验”，才会生成新的匿名历史。
- 每个匿名访客每天最多创建 3 场面试，公开站点每天最多创建 30 场；单个上传文件最大 5MB；单题回答最大 3000 字。
- DeepSeek 调用采用双层预算：每个匿名访客每天最多 20 次、全站每天最多 120 次。达到任一上限后自动使用本地规则，不中断面试流程；调用预留与限额触发会写入 `event_logs`。
- 一次配置最多选择 3 个文件，每个匿名访客最多保留 20 份资料；服务端会再次校验资料类型、文件名和 DOCX 解压大小。
- 服务端不保存上传的原始文件，只在 SQLite 中保存文件名、元数据和最多 30000 字的提取文本。
- 公开版不显示全站数据看板，也不显示会消耗额度的 AI 连接测试。只有设置 `APP_ENV = "development"` 时才显示连接测试。
- 侧边栏“隐私与数据”可以清除当前匿名访客的会话、回答、资料和产品埋点。为防止绕过当日全站成本上限，系统只保留不含回答、资料和访客编号的匿名 AI 调用计数。
- 资料片段和回答会发送给 DeepSeek。请勿上传身份证、电话、住址等无关敏感信息。
- 报告页支持提交“报告有用度、题目贴合度和改进建议”，同一场面试重复提交会更新原反馈。

配置 Supabase Secrets 后，Streamlit Cloud 使用 Supabase Postgres 持久化数据；未配置时自动使用本地 SQLite，便于本地开发和自动测试。首次切换到 Supabase 后，原 Streamlit 临时 SQLite 中的历史不会自动迁移，后续新数据会持久保存。

Supabase 使用 Transaction pooler（通常为端口 6543）和 SSL 连接。代码关闭服务端预编译语句以兼容事务连接池，并为七张表启用 RLS；应用通过仅存放在 Streamlit Secrets 中的数据库账号访问，浏览器不会拿到数据库凭据。

Streamlit Cloud 的 Secrets 需要包含：

```toml
SUPABASE_DB_HOST = "aws-0-区域.pooler.supabase.com"
SUPABASE_DB_PORT = 6543
SUPABASE_DB_NAME = "postgres"
SUPABASE_DB_USER = "postgres.项目编号"
SUPABASE_DB_PASSWORD = "数据库密码"
```

不要将这些真实值写入仓库。部署后侧边栏显示“数据存储：Supabase 云数据库”即表示切换成功。

## MVP 出题规则

- 全流程面试：固定四类各 1 题，共 4 题。
- 单项面试：只生成所选题型，题量由用户设置。
- 专业面：`院校面试真题 > 专业资料 > 系统专业题库`。
- 科研面 / 英语面 / 行为面：`院校面试真题 > 简历 > 系统通用题库`。
- 所有实际题目都会写入 `session_questions`，并记录 `source_scope`、`source_type`、来源题或来源资料 ID。
- 对过短或规则评分偏低的回答，MVP 会插入一条针对薄弱维度的追问；追问保留原题来源字段，且不会递归追问。
- 专业面系统题库当前为会计学方向，非会计用户且未上传可用资料时会收到页面提示，但仍可继续体验。
- 面试题使用浏览器本地语音朗读，中文题选择中文语音、英语题选择英文语音，不消耗 DeepSeek 额度。浏览器阻止自动播放时可点击“播放题目”。
- 回答支持语音转文字和键盘输入。语音转写使用 `streamlit-mic-recorder` 调用 Google Speech Recognition，录音只用于当次转写、不写入数据库；数据库仅保存用户确认提交的回答文字。外部语音服务不可用时仍可继续打字作答。

统一的资料出题入口是 `services.generate_questions_from_materials()`。存在可用资料和 DeepSeek 配置时，每轮最多使用一次 `deepseek-flash` 深度生成问题，其余题继续按资料内容生成；院校真题直接抽取原题。API 未配置、超时、鉴权失败或 JSON 无效时自动使用本地规则。系统题库抽题不调用模型。

## 上传资料说明

- TXT / MD / DOCX：MVP 在内存中提取文本，并用关键词生成题目。
- PDF：MVP 不解析正文，仅保存文件名和元数据，可利用文件名生成模拟问题。
- 原始文件不会写入服务器磁盘。
- “面试题库”页上传的资料只供当前匿名访客后续会话使用；配置流程上传的资料优先服务于当前会话。

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

测试覆盖：全流程四类各一题、资料优先级、规则追问、DeepSeek 出题与评分、AI 双层调用预算、访客读写隔离、每日限额、上传和回答大小限制、清除个人数据、旧数据隐藏，以及回答保存到报告生成的完整往返。测试默认关闭真实 API，不消耗余额。

## SQL 与 BI

`queries.sql` 提供八组分析查询：历史会话、题目来源占比、各题型维度分、高频薄弱项、核心漏斗、资料使用次数、每日 AI 成本事件和报告反馈。公开应用不展示全站数据看板，避免匿名访客看到其他人的聚合数据与埋点。
