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

`.env` 文件内容如下（已配置好，无需修改）：

```
DATABASE_URL=sqlite:///./var/planning.db
SESSION_SHARED_PASSWORD=demo-password-2026
SESSION_SECRET_KEY=demo-secret-key-for-hackathon-use-only-2026
LLM_MODE=STUB
CORS_ALLOW_ORIGINS=http://localhost:5173
UPLOAD_DIR=./var/uploads
```

> **注意**：`LLM_MODE=STUB` 表示不调用任何 LLM 服务，无需配置 Bedrock 凭证即可运行。

---

## 第三步：初始化数据库与演示数据

> 如果 `backend\var\planning.db` 已存在（430KB 左右），此步可跳过。

```cmd
cd d:\Hackathon\hackathon\backend
set DATABASE_URL=sqlite:///./var/planning.db
.venv\Scripts\alembic upgrade head
.venv\Scripts\python -m app.seed --demo
```

成功输出示例：

```json
{"event": "SEED_APPLIED", "row_counts": {"products": 6, "orders": 14, "machines": 5, "workers": 8, ...}}
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
set SESSION_SHARED_PASSWORD=demo-password-2026
set SESSION_SECRET_KEY=demo-secret-key-for-hackathon-use-only-2026
set LLM_MODE=STUB
.venv\Scripts\uvicorn app.main:create_app --factory --reload --workers 1 --port 8000
```

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
| 2 | 点击「生成计划」 | 弹出登录框，输入密码 `demo-password-2026` |
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
