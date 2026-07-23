# Grok 批量注册工具

批量注册 Grok 账号并自动开启 NSFW 功能。

## 功能

- 自动创建临时邮箱
- 自动获取验证码
- 自动完成注册流程
- 自动开启 NSFW/Unhinged 模式
- 注册完成后自动清理临时邮箱
- 支持多线程并发注册

## 文件说明

| 文件 | 说明 |
|------|------|
| `app.py` | Web 控制台（Flask + Tailwind） |
| `templates/index.html` | 控制台前端页面 |
| `grok.py` | 注册引擎 + CLI 入口 |
| `solver_manager.py` | Turnstile Solver 启动/停止/状态管理 |
| `setup_solver.py` | 一键安装 Solver 依赖与 camoufox |
| `TurnstileSolver.bat` | Turnstile Solver 启动脚本 |
| `api_solver.py` | Turnstile 验证码解决器 |
| `browser_configs.py` | 浏览器指纹配置 |
| `db_results.py` | 验证结果存储 |
| `g/email_service.py` | 临时邮箱服务（cloudflare_temp_email 主路径，兼容旧 freemail） |
| `g/turnstile_service.py` | Turnstile 验证服务 |
| `g/user_agreement_service.py` | 用户协议同意服务 |
| `g/nsfw_service.py` | NSFW 设置服务 |
| `.env.example` | 环境变量模板 |
| `requirements.txt` | Python 依赖列表 |

## 依赖

- [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) - 临时邮箱服务（部署到 Cloudflare Worker）
- Turnstile Solver - 内置验证码解决方案

## 安装

```bash
pip install -r requirements.txt
# 首次使用本地 Turnstile Solver 时，再安装浏览器依赖（约 530MB）
python setup_solver.py
```

## 配置

复制 `.env.example` 为 `.env` 并填写配置：

```bash
cp .env.example .env
```

配置项说明：

| 配置项 | 说明 | 默认 |
|--------|------|------|
| WORKER_DOMAIN | cloudflare_temp_email 的 Worker 域名（不带 `https://`） | — |
| FREEMAIL_TOKEN | cloudflare_temp_email 的站点密码；变量名为兼容旧 freemail 保留 | — |
| FREEMAIL_DOMAIN | 邮箱后缀；`auto` 使用服务端默认值 | auto |
| FREEMAIL_API_STYLE | `cf_temp` 强制 Cloudflare Temp Email；`auto` 优先走该模式 | auto |
| YESCAPTCHA_KEY | YesCaptcha API Key（可选，不填使用本地 Solver） | 空 |
| SOLVER_URL | 本地 Solver 地址 | http://127.0.0.1:5072 |
| SOLVER_BROWSER | 浏览器类型 camoufox / chromium | camoufox |
| SOLVER_THREADS | Solver 浏览器线程数 | 4 |
| UI_HOST | Web 监听地址 | 127.0.0.1 |
| UI_PORT | Web 端口 | 3333 |
| SUB2API_URL | sub2api Web 地址（健康检查） | http://127.0.0.1:9898 |
| SUB2API_GROK_GROUP_ID | **导入目标分组 ID**（必填，见下文） | 空，需自行填写 |
| SUB2API_GROK_GROUP_NAME | 分组名称，须与后台一致 | grok |
| SUB2API_GROK_GROUP_ID / NAME | sub2api 中的 Grok 分组 | 23 / grok |
| UPSTREAM_ADMIN_EMAIL | sub2api 管理员邮箱 | — |
| UPSTREAM_ADMIN_PASSWORD | sub2api 管理员密码（仅本地 `.env`） | — |

> **切勿提交 `.env`**。仓库只带 `.env.example`；本地 `cp .env.example .env` 后填写真实值。

## 使用

### 1. 启动 Turnstile Solver

**推荐**：Web 控制台运行页一键「启动 Solver」。

或命令行：

```bash
python solver_manager.py start
python solver_manager.py status
python solver_manager.py stop
```

也可双击 `TurnstileSolver.bat`。

等待 Solver 启动完成（监听 `http://127.0.0.1:5072`）。日志：`logs/turnstile_solver.log`。

### 2. Web 控制台（推荐）

```bash
python app.py
```

浏览器打开：`http://127.0.0.1:3333`

界面功能：
- **配置页**：在线编辑临时邮箱（Cloudflare Temp Email）/ YesCaptcha / Solver / 端口，保存到 `.env`
- **运行页**：一键启动/停止 Turnstile Solver，查看在线状态与 PID
- 自定义下拉选择并发与数量（非系统原生 select）
- 一键启动 / 停止注册
- 实时进度、成功率、平均耗时
- 实时日志与最近成功账号
- 下载 `keys/` 下的 SSO 文件
- 浅色主题控制台

### 3. 命令行模式

```bash
python grok.py
```

按提示输入：
- 并发数（默认 8）
- 注册数量（默认 100）

注册成功的 SSO Token 保存在 `keys/grok_时间戳_数量.txt`

## 输出示例

```
============================================================
Grok 注册机
============================================================
[*] 正在初始化...
[+] Action ID: 7f67aa61adfb0655899002808e1d443935b057c25b
[*] 启动 8 个线程，目标 10 个
[*] 输出: keys/grok_20260204_190000_10.txt
[*] 开始注册: abc123@example.com
[+] 1/10 abc123@example.com | 5.2s/个
[+] 2/10 def456@example.com | 4.8s/个
...
[*] 开始二次验证 NSFW...
[*] 二次验证完成: 10/10
```

## 注意事项

- 需要部署 cloudflare_temp_email 到 Cloudflare Worker，并配置 `WORKER_DOMAIN` 与站点密码
- 旧 freemail API 仅作为兼容回退；使用 Cloudflare Temp Email 时设置 `FREEMAIL_API_STYLE=cf_temp`
- 运行前必须先启动 Turnstile Solver
- 仅供学习研究使用
