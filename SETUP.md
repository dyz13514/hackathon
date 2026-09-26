# AI 生产排产助手 — 环境配置与启动说明

## 前置条件

- Python 3.11+（已验证 3.13 可用）
- Node.js 18+
- cmd 或 PowerShell 终端

---

## 第一步：创建虚拟环境并安装依赖

打开 cmd，执行：

```cmd
cd d:\Hackathon\hackathon\backend
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

安装完成后验证：

```cmd
.venv\Scripts\pytest --version
```

能输出版本号即说明环境就绪。

---

## 第二步：配置环境变量文件

项目根目录下已有 `.env` 文件（首次使用需创建）。如果不存在，执行：

```cmd
cd d:\Hackathon\hackathon
copy .env.example .env
```

`.env` 的各键含义如下。**取值一律以你本地 `.env` 的实际内容为准**——本文档不记录任何真实口令或密钥：

```
DATABASE_URL=sqlite:///./var/planning.db
SESSION_SHARED_PASSWORD=<你本地 .env 里的共享口令：登录浏览器时用的就是它>
SESSION_SECRET_KEY=<你本地 .env 里 ≥32 字符的会话签名密钥>
LLM_MODE=STUB
CORS_ALLOW_ORIGINS=http://localhost:5173
UPLOAD_DIR=./var/uploads
```

> **记住你的登录口令**：它就是 `SESSION_SHARED_PASSWORD`。查看方式（不会把口令打印到本文档里）：
> cmd 用 `findstr SESSION_SHARED_PASSWORD .env`，PowerShell 用
> `Select-String -Path .env -Pattern '^SESSION_SHARED_PASSWORD='`。
> **后端以哪个值启动，浏览器登录就必须用哪个值**——两者不一致会一直提示口令错误。

> **注意**：`LLM_MODE=STUB` 表示不调用任何 LLM 服务，无需配置 Bedrock 凭证即可运行。

---

## 第三步：初始化数据库

> 如果 `backend\var\planning.db` 已存在（430KB 左右），此步可跳过。

```cmd
cd d:\Hackathon\hackathon\backend
set DATABASE_URL=sqlite:///./var/planning.db
.venv\Scripts\alembic upgrade head
```

普通启动不会自动写入演示订单或演示资源。建议启动网页后在 Import 页面上传完整演示工作簿（路径和步骤见 [IMPORT_WORKBOOK.md](IMPORT_WORKBOOK.md)），确认后再生成排程。旧 seed 只供测试/显式重置；若确实需要复现旧版固定日期演示，才运行：

```cmd
.venv\Scripts\python -m app.seed --demo
```

---

## 第四步：运行测试（可选，用于验证功能完整性）

### 基础测试（单元 + 属性 + 结构测试）

```cmd
cd d:\Hackathon\hackathon\backend
set LLM_MODE=STUB
set DATABASE_URL=sqlite:///test.db
set SESSION_SHARED_PASSWORD=test-shared-password
set SESSION_SECRET_KEY=test-secret-key-that-is-long-enough-32
.venv\Scripts\pytest tests --ignore=tests/eval -x --tb=short -q
```

预期结果：全部通过，无 FAILED。

### EVAL 评估套件（验证 LLM 路径，REPLAY 模式，零 Bedrock 消耗）

```cmd
set LLM_MODE=REPLAY
set DATABASE_URL=sqlite:///test.db
set SESSION_SHARED_PASSWORD=test-shared-password
set SESSION_SECRET_KEY=test-secret-key-that-is-long-enough-32
.venv\Scripts\pytest tests/eval -v
```

预期结果：45 passed。

### 生成评估报告（可选）

```cmd
set EVAL_REPORT=d:\Hackathon\hackathon\eval_report.md
.venv\Scripts\pytest tests/eval -v
```

报告生成在 `d:\Hackathon\hackathon\eval_report.md`。

---

## 第五步：启动后端

**新开一个 cmd 窗口**，执行：

```cmd
cd d:\Hackathon\hackathon\backend
set DATABASE_URL=sqlite:///./var/planning.db
set SESSION_SHARED_PASSWORD=<你本地 .env 的 SESSION_SHARED_PASSWORD>
set SESSION_SECRET_KEY=<你本地 .env 的 SESSION_SECRET_KEY>
set LLM_MODE=STUB
.venv\Scripts\uvicorn app.main:create_app --factory --reload --workers 1 --port 8000
```

> 这两句 `set` 会**覆盖** `.env` 里的同名值。要么填成与 `.env` 完全一致的值，要么整段删掉
> 这两句、直接沿用 `.env`（推荐：从 `backend/` 启动时 `app/settings.py` 会自动读取仓库根的
> `.env`）。覆盖成别的值以后，第五步的口令与第七步登录用的口令就对不上了。

看到以下输出即启动成功，**保持此窗口不关**：

```
INFO: Application startup complete.
```

后端地址：`http://127.0.0.1:8000`
API 文档：`http://127.0.0.1:8000/docs`

---

## 第六步：启动前端

**再开一个 cmd 窗口**，执行：

```cmd
cd d:\Hackathon\hackathon\frontend
npm install
npm run dev
```

看到以下输出即启动成功，**保持此窗口不关**：

```
Local:   http://localhost:5173/
```

---

## 第七步：访问与操作

浏览器打开 `http://localhost:5173`，按以下顺序验证主流程：

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 1 | 打开首页 | 状态看板显示订单、物料、机器、工人、计划五类卡片 |
| 2 | 点击「生成计划」 | 弹出登录框，输入你本地 `.env` 的 `SESSION_SHARED_PASSWORD`（见第二步的说明） |
| 3 | 登录后生成计划 | 返回甘特图 + 基线对比，60 秒内完成 |
| 4 | 查看计划解释 | 显示结构化解释，含 7 个目标分量 |
| 5 | 进入审批页面，点 APPROVE | 计划状态变为 ACTIVE |
| 6 | 导出 xlsx | 下载文件，包含排班、不可排产、元数据三个 Sheet |
| 7 | 修改一条订单后重新生成，再审批 | 提示「提案已陈旧」+ 两个版本号 |

---

## 常见问题

**Q：看板显示 `DATA_UNAVAILABLE`**
启动 uvicorn 前必须先用 `set` 命令设置 `DATABASE_URL`，确保指向 `sqlite:///./var/planning.db`。

**Q：`set` 命令报「语法不正确」**
说明你在 PowerShell 里，应改用：
```powershell
$env:DATABASE_URL="sqlite:///./var/planning.db"
```

**Q：`.venv\Scripts\pytest` 找不到**
虚拟环境依赖未安装，重新执行第一步的 `pip install -e ".[dev]"`。

**Q：前端 `npm install` 很慢**
可以配置 npm 镜像：`npm config set registry https://registry.npmmirror.com`

---

## 附录：接入 AWS Bedrock LLM（可选）

默认情况下系统以 `LLM_MODE=STUB` 运行，核心排产功能完全可用。如需启用以下 LLM 功能，须配置 AWS Bedrock：

- **计划解释**：生成自然语言版的排产决策说明
- **智能重排（ReAct）**：Planning Agent 通过多轮工具调用优化计划
- **What-if 场景翻译**：将自然语言假设转化为结构化场景（P1 功能）

### 前提条件

1. 拥有 AWS 账户，且已在目标区域开通 Bedrock 服务
2. 在 Bedrock 控制台申请 **Anthropic Claude** 模型访问权限（Model Access）
3. 准备一个 Bedrock Gateway 地址（`BEDROCK_GATEWAY_URL`）和对应的 API Key

> **关于 Bedrock Gateway**：项目通过一个 HTTP 代理网关访问 Bedrock，而不是直接使用 AWS SDK。网关负责签名和转发请求。你可以使用团队内部部署的网关，或自行搭建（参考 AWS 官方的 Bedrock API Gateway 方案）。

> **网关的接口形态**：`Bedrock_Adapter` 按 **Ollama 兼容** 的 `POST /api/chat` 协议收发
> （请求体字段为 `model` / `messages` / `stream` / `options`，响应取 `message.content`
> 与 `prompt_eval_count` / `eval_count`）。因此 `BEDROCK_GATEWAY_URL` 要填**完整端点**
> （含 `/api/chat`）。若网关返回其他常见形态（`content`、`choices[0].message.content`
> 等），解析器同样认得；正文取不出来时会显式报错并降级，而不是发布空解释。

### 配置步骤

**第一步**：编辑 `.env` 文件，修改以下几项（`BEDROCK_MODEL` 可选，默认 `sonnet4.5:latest`）：

```
LLM_MODE=LIVE
BEDROCK_GATEWAY_URL=https://你的网关地址/api/chat
BEDROCK_API_KEY=你的API密钥
BEDROCK_MODEL=sonnet4.5:latest
LLM_API_STYLE=OLLAMA
```

> **OpenAI 兼容服务（例如 DeepSeek）**：把地址换成对方的完整 chat 端点、模型换成对方的
> 模型 id，并把请求体形态切到 `OPENAI`（顶层 `temperature` / `max_tokens`，不发 Ollama 的
> `options`）。其余一切不变——认证头、重试/降级与响应解析两种形态共用同一套实现：
>
> ```
> LLM_MODE=LIVE
> BEDROCK_GATEWAY_URL=https://api.deepseek.com/chat/completions
> BEDROCK_API_KEY=你的DeepSeek密钥
> BEDROCK_MODEL=deepseek-chat
> LLM_API_STYLE=OPENAI
> ```
>
> `BEDROCK_*` 只是历史变量名，含义是「配置的 LLM 端点 / 凭证 / 模型」，不限定供应商。

**第二步**：验证配置是否生效，启动后端后访问健康检查接口：

```
http://127.0.0.1:8000/health
```

返回示例：

```json
{
  "status": "ok",
  "llm_mode": "LIVE",
  "project_usd_spent": 0.0,
  "real_run_count": 0
}
```

`llm_mode` 为 `LIVE` 即表示 LLM 接入成功。

### LLM 模式说明

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| `STUB` | 所有 LLM 调用返回桩数据，不联网 | 本地开发、功能演示 |
| `REPLAY` | 从本地 cassette 文件回放录制好的响应，零成本 | CI 测试、离线验证 |
| `LIVE` | 真实调用 Bedrock，产生费用 | 生产环境、完整功能验证 |
| `DISABLED` | LLM 路径完全关闭，自动走确定性模板回退 | 降级保护 |

### 成本限制

项目内置了以下成本保护机制，无需额外配置：

- **单次计划生成**：上限 4,000 token / $0.02
- **重排（ReAct）**：上限 14,000 token / $0.06
- **每日总额**：默认 $5.00，达 80% 显示告警
- **项目总额**：$35.00，达 90% 自动切换为 `DETERMINISTIC_ONLY` 降级模式
- **真实调用次数上限**：150 次（`PROJECT_REAL_RUN_CAP`），超出后拒绝以 `LIVE` 模式启动

当前用量可在 `/health` 接口的 `project_usd_spent` 和 `real_run_count` 字段查看。

### 降级行为

网关连续失败 3 次后，系统自动切入 `DETERMINISTIC_ONLY` 模式：

- 计划解释回退为**确定性模板**（不依赖 LLM）
- 重排回退为**确定性流水线**（仍能生成计划，只是没有 Agent 优化）
- 降级事件写入审计日志（`DEGRADED_MODE_SWITCH`）

因此即使 Bedrock 不可用，核心排产功能也不受影响。
