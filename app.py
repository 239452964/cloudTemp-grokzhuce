"""
Grok 注册机 Web 控制台
启动: python app.py
浏览器打开 http://127.0.0.1:3333
"""
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory

from grok import LogBuffer, RegisterEngine
import solver_manager

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"), static_folder=str(BASE_DIR / "static"))

logs = LogBuffer(maxlen=3000)
engine = RegisterEngine(log_fn=lambda msg, level="info": logs.emit(msg, level))

# 后台导入任务（避免 /api/upstream/import 长时间占用 HTTP 导致页面卡死）
_import_job_lock = threading.Lock()
_import_job: dict = {
    "id": None,
    "running": False,
    "status": "idle",  # idle | running | done | error
    "message": "",
    "source": "",
    "total": 0,
    "done": 0,
    "success": 0,
    "fail": 0,
    "current": "",
    "started_at": None,
    "finished_at": None,
    "result": None,
}


def get_import_job_public() -> dict:
    with _import_job_lock:
        job = dict(_import_job)
    # 不把完整 results 塞进轮询
    result = job.get("result")
    if isinstance(result, dict):
        job["result"] = {
            "ok": result.get("ok"),
            "message": result.get("message"),
            "success": result.get("success"),
            "fail": result.get("fail"),
            "total": result.get("total"),
            "cached_hits": result.get("cached_hits"),
            "flow_hits": result.get("flow_hits"),
            "submitted": result.get("submitted"),
            "source": result.get("source"),
            "file": result.get("file"),
        }
    return job

CONFIG_KEYS = (
    "WORKER_DOMAIN",
    "FREEMAIL_TOKEN",
    "FREEMAIL_DOMAIN",
    "FREEMAIL_API_STYLE",
    "YESCAPTCHA_KEY",
    "SOLVER_URL",
    "SOLVER_BROWSER",
    "SOLVER_THREADS",
    "SOLVER_HOST",
    "SOLVER_PORT",
    "SOLVER_DEBUG",
    "UI_HOST",
    "UI_PORT",
    "SUB2API_URL",
    "SUB2API_GROK_GROUP_ID",
    "SUB2API_GROK_GROUP_NAME",
    "UPSTREAM_URL",
    "UPSTREAM_ADMIN_EMAIL",
    "UPSTREAM_ADMIN_PASSWORD",
)

DEFAULTS = {
    "WORKER_DOMAIN": "",
    "FREEMAIL_TOKEN": "",
    "FREEMAIL_DOMAIN": "auto",
    "FREEMAIL_API_STYLE": "auto",
    "YESCAPTCHA_KEY": "",
    "SOLVER_URL": "http://127.0.0.1:5072",
    "SOLVER_BROWSER": "camoufox",
    "SOLVER_THREADS": "4",
    "SOLVER_HOST": "127.0.0.1",
    "SOLVER_PORT": "5072",
    "SOLVER_DEBUG": "1",
    "UI_HOST": "127.0.0.1",
    "UI_PORT": "3333",
    "SUB2API_URL": "http://127.0.0.1:9898",
    "SUB2API_GROK_GROUP_ID": "23",
    "SUB2API_GROK_GROUP_NAME": "grok",
    "UPSTREAM_URL": "http://127.0.0.1:9898",
    "UPSTREAM_ADMIN_EMAIL": "",
    "UPSTREAM_ADMIN_PASSWORD": "",
}


def read_env_file() -> dict:
    data = dict(DEFAULTS)
    if not ENV_PATH.exists():
        # fallback to process env
        for k in CONFIG_KEYS:
            data[k] = os.getenv(k, data[k]) or data[k]
        return data

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        key = key.strip()
        if key in CONFIG_KEYS:
            data[key] = val.strip().strip('"').strip("'")
    # process env overrides missing blanks only for runtime consistency
    for k in CONFIG_KEYS:
        if not data.get(k):
            data[k] = os.getenv(k, data.get(k, "")) or data.get(k, "")
    return data


def write_env_file(values: dict) -> None:
    existing_lines = []
    if ENV_PATH.exists():
        existing_lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    written = set()
    out = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in values:
            out.append(f"{key}={values[key]}")
            written.add(key)
        else:
            out.append(line)

    # append missing keys
    for key in CONFIG_KEYS:
        if key not in written:
            if out and out[-1].strip():
                out.append("")
            out.append(f"{key}={values.get(key, DEFAULTS.get(key, ''))}")

    # ensure example comments if file was empty
    if not existing_lines:
        out = [
            "# 临时邮箱（cloudflare_temp_email）配置",
            f"WORKER_DOMAIN={values.get('WORKER_DOMAIN', '')}",
            f"FREEMAIL_TOKEN={values.get('FREEMAIL_TOKEN', '')}",
            f"FREEMAIL_DOMAIN={values.get('FREEMAIL_DOMAIN', DEFAULTS['FREEMAIL_DOMAIN'])}",
            f"FREEMAIL_API_STYLE={values.get('FREEMAIL_API_STYLE', DEFAULTS['FREEMAIL_API_STYLE'])}",
            "",
            "# Turnstile 验证配置",
            "# 如果不填则使用本地 Turnstile Solver",
            f"YESCAPTCHA_KEY={values.get('YESCAPTCHA_KEY', '')}",
            f"SOLVER_URL={values.get('SOLVER_URL', DEFAULTS['SOLVER_URL'])}",
            f"SOLVER_BROWSER={values.get('SOLVER_BROWSER', DEFAULTS['SOLVER_BROWSER'])}",
            f"SOLVER_THREADS={values.get('SOLVER_THREADS', DEFAULTS['SOLVER_THREADS'])}",
            f"SOLVER_HOST={values.get('SOLVER_HOST', DEFAULTS['SOLVER_HOST'])}",
            f"SOLVER_PORT={values.get('SOLVER_PORT', DEFAULTS['SOLVER_PORT'])}",
            f"SOLVER_DEBUG={values.get('SOLVER_DEBUG', DEFAULTS['SOLVER_DEBUG'])}",
            "",
            "# Web 控制台",
            f"UI_HOST={values.get('UI_HOST', DEFAULTS['UI_HOST'])}",
            f"UI_PORT={values.get('UI_PORT', DEFAULTS['UI_PORT'])}",
            "",
            "# sub2api Grok（HTTP Admin API 导入）",
            f"SUB2API_URL={values.get('SUB2API_URL', DEFAULTS['SUB2API_URL'])}",
            f"SUB2API_GROK_GROUP_ID={values.get('SUB2API_GROK_GROUP_ID', DEFAULTS['SUB2API_GROK_GROUP_ID'])}",
            f"SUB2API_GROK_GROUP_NAME={values.get('SUB2API_GROK_GROUP_NAME', DEFAULTS['SUB2API_GROK_GROUP_NAME'])}",
            f"UPSTREAM_URL={values.get('UPSTREAM_URL', values.get('SUB2API_URL', DEFAULTS['SUB2API_URL']))}",
            f"UPSTREAM_ADMIN_EMAIL={values.get('UPSTREAM_ADMIN_EMAIL', '')}",
            f"UPSTREAM_ADMIN_PASSWORD={values.get('UPSTREAM_ADMIN_PASSWORD', '')}",
            "",
        ]

    ENV_PATH.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def apply_env_to_process(values: dict) -> None:
    for k, v in values.items():
        os.environ[k] = v or ""
    # reload dotenv for any other readers
    load_dotenv(ENV_PATH, override=True)


def env_snapshot():
    cfg = read_env_file()
    worker = cfg.get("WORKER_DOMAIN", "").strip()
    token = cfg.get("FREEMAIL_TOKEN", "").strip()
    yes = cfg.get("YESCAPTCHA_KEY", "").strip()
    mail_domain = (cfg.get("FREEMAIL_DOMAIN") or DEFAULTS["FREEMAIL_DOMAIN"]).strip() or "auto"
    sub2 = get_sub2api_config_from_cfg(cfg)
    sub2_url = sub2["url"]
    configured = bool(
        sub2_url
        and sub2["group_id"]
        and sub2["admin_email"]
        and sub2["admin_password"]
    )
    return {
        "worker_domain_set": bool(worker),
        "freemail_token_set": bool(token),
        "yescaptcha_set": bool(yes),
        "worker_domain": worker,
        "freemail_domain": mail_domain,
        "solver_url": cfg.get("SOLVER_URL") or DEFAULTS["SOLVER_URL"],
        "solver_browser": cfg.get("SOLVER_BROWSER") or DEFAULTS["SOLVER_BROWSER"],
        "solver_threads": cfg.get("SOLVER_THREADS") or DEFAULTS["SOLVER_THREADS"],
        "ui_host": cfg.get("UI_HOST") or DEFAULTS["UI_HOST"],
        "ui_port": cfg.get("UI_PORT") or DEFAULTS["UI_PORT"],
        "sub2api_url": sub2_url,
        "sub2api_group_id": sub2["group_id"],
        "sub2api_group_name": sub2["group_name"],
        "sub2api_configured": configured,
        # legacy names consumed by existing UI
        "upstream_url": sub2_url,
        "upstream_configured": configured,
        "upstream_password_set": bool(sub2["admin_password"]),
    }


def normalize_upstream_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "http://" + u
    return u.rstrip("/")


def get_upstream_config() -> dict:
    sub2 = get_sub2api_config()
    return {
        "url": sub2["url"],
        "email": sub2["admin_email"],
        "password": sub2["admin_password"],
    }


def get_sub2api_config_from_cfg(cfg: dict) -> dict:
    url = normalize_upstream_url(
        cfg.get("SUB2API_URL")
        or cfg.get("UPSTREAM_URL")
        or DEFAULTS["SUB2API_URL"]
    )
    return {
        "url": url,
        "group_id": (cfg.get("SUB2API_GROK_GROUP_ID") or DEFAULTS["SUB2API_GROK_GROUP_ID"]).strip(),
        "group_name": (cfg.get("SUB2API_GROK_GROUP_NAME") or DEFAULTS["SUB2API_GROK_GROUP_NAME"]).strip(),
        "admin_email": (cfg.get("UPSTREAM_ADMIN_EMAIL") or "").strip(),
        "admin_password": (cfg.get("UPSTREAM_ADMIN_PASSWORD") or "").strip(),
    }


def get_sub2api_config() -> dict:
    return get_sub2api_config_from_cfg(read_env_file())


def _run_sub2api_psql(sql: str, cfg: dict | None = None, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """通过 psycopg2 直接连接 PostgreSQL 执行 SQL，返回兼容 CompletedProcess 的对象。

    导入 SQL 常写成 ``BEGIN; ... SELECT ...; COMMIT;``。psycopg2 对多语句
    execute 后 cursor 停在最后一条（COMMIT 无结果集），直接 fetchall 会报
    ``no results to fetch``；且 PostgreSQL 不支持 cursor.nextset()。

    处理方式：剥掉外层 BEGIN/COMMIT，由 Python 事务提交，再安全取结果。
    """
    import psycopg2

    cfg = cfg or get_sub2api_config()
    # 去掉脚本式事务包装，交给下方 conn.commit()
    body = (sql or "").strip()
    body = re.sub(r"^\s*BEGIN\s*;\s*", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\s*COMMIT\s*;\s*$", "", body, flags=re.IGNORECASE)
    body = body.strip()

    conn = None
    try:
        conn = psycopg2.connect(
            host=cfg["db_host"],
            port=int(cfg["db_port"]),
            user=cfg["db_user"],
            password=cfg["db_password"],
            dbname=cfg["db_name"],
            connect_timeout=min(int(timeout), 10),
        )
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute(body)

        rows = []
        if cur.description is not None:
            rows = cur.fetchall()

        cur.close()
        conn.commit()
        conn.close()
        conn = None

        output_lines = []
        for row in rows:
            if len(row) == 1:
                output_lines.append(str(row[0]) if row[0] is not None else "")
            else:
                output_lines.append(
                    "|".join(str(v) if v is not None else "" for v in row)
                )
        return subprocess.CompletedProcess(
            args=["psycopg2"],
            returncode=0,
            stdout="\n".join(output_lines),
            stderr="",
        )
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        return subprocess.CompletedProcess(
            args=["psycopg2"],
            returncode=1,
            stdout="",
            stderr=str(e),
        )


def _last_json_line(stdout: str) -> dict:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            return json.loads(line)
    return {}


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def _rfc3339_seconds(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        if re.fullmatch(r"\d+(\.\d+)?", raw):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        s = raw.replace("Z", "+00:00")
        if "." in s:
            head, tail = s.split(".", 1)
            tz = ""
            for marker in ("+", "-"):
                pos = tail.find(marker)
                if pos > 0:
                    tz = tail[pos:]
                    tail = tail[:pos]
                    break
            s = head + "." + tail[:6].ljust(6, "0") + tz
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return raw


def _entry_to_sub2api_credentials(entry: dict, email_hint: str = "") -> tuple[dict, str, str, str, str]:
    from grok import decode_jwt_payload

    access = (entry or {}).get("access_token") or (entry or {}).get("key") or ""
    refresh = (entry or {}).get("refresh_token") or ""
    payload = decode_jwt_payload(access)
    issuer = (entry or {}).get("oidc_issuer") or payload.get("iss") or "https://auth.x.ai"
    client_id = (
        (entry or {}).get("client_id")
        or (entry or {}).get("oidc_client_id")
        or payload.get("client_id")
        or payload.get("aud")
        or "b1a00492-073a-47ea-816f-4c329264a828"
    )
    user_id = (entry or {}).get("user_id") or payload.get("sub") or payload.get("principal_id") or ""
    principal_id = (entry or {}).get("principal_id") or payload.get("principal_id") or user_id
    principal_type = (entry or {}).get("principal_type") or payload.get("principal_type") or "User"
    email = (email_hint or (entry or {}).get("email") or payload.get("email") or "").strip()
    expires_at = _rfc3339_seconds((entry or {}).get("expires_at") or payload.get("exp") or "")
    auth_key = (entry or {}).get("auth_key") or (f"{issuer}::{user_id}" if user_id else "")
    credentials = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "base_url": (entry or {}).get("base_url") or "https://cli-chat-proxy.grok.com/v1",
        "auth_key": auth_key,
        "user_id": user_id,
        "auth_mode": (entry or {}).get("auth_mode") or "oidc",
        "client_id": client_id,
        "oidc_issuer": issuer,
        "email": email,
        "token_type": (entry or {}).get("token_type") or "Bearer",
        "principal_id": principal_id,
        "principal_type": principal_type,
    }
    if payload.get("scope"):
        credentials["scope"] = payload.get("scope")
    if payload.get("team_id"):
        credentials["team_id"] = payload.get("team_id")
    name = email or (f"Grok {user_id}" if user_id else "Grok OAuth")
    return credentials, name, auth_key, user_id, email


def _legacy_sub2api_import_auth_entry_postgres(entry: dict, email_hint: str = "", merge: bool = True) -> dict:
    cfg = get_sub2api_config()
    credentials, name, auth_key, user_id, email = _entry_to_sub2api_credentials(entry, email_hint)
    try:
        group_id = int(cfg["group_id"])
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": "未配置 SUB2API_GROK_GROUP_ID，请在配置页填写 sub2api Grok 分组 ID",
            "email": email or None,
            "user_id": user_id or None,
        }
    merge_sql = "TRUE" if merge else "FALSE"
    sql = f"""
BEGIN;
WITH incoming AS (
    SELECT
        {_sql_literal(name)}::text AS name,
        {_sql_literal(json.dumps(credentials, ensure_ascii=False, separators=(",", ":")))}::jsonb AS credentials,
        {_sql_literal(auth_key)}::text AS auth_key,
        {_sql_literal(user_id)}::text AS user_id,
        {_sql_literal(email)}::text AS email
), matched AS (
    SELECT a.id
    FROM accounts a, incoming i
    WHERE a.platform = 'grok'
      AND a.deleted_at IS NULL
      AND (
        (i.auth_key <> '' AND a.credentials->>'auth_key' = i.auth_key)
        OR (i.user_id <> '' AND a.credentials->>'user_id' = i.user_id)
        OR (i.email <> '' AND lower(a.credentials->>'email') = lower(i.email))
      )
    ORDER BY a.id
    LIMIT 1
), updated AS (
    UPDATE accounts a
    SET name = i.name,
        platform = 'grok',
        type = 'oauth',
        credentials = i.credentials,
        concurrency = 1,
        priority = 50,
        status = 'active',
        error_message = NULL,
        schedulable = TRUE,
        rate_limited_at = NULL,
        rate_limit_reset_at = NULL,
        overload_until = NULL,
        temp_unschedulable_until = NULL,
        temp_unschedulable_reason = NULL,
        auto_pause_on_expired = TRUE,
        rate_multiplier = 1.0,
        updated_at = NOW(),
        deleted_at = NULL
    FROM incoming i, matched m
    WHERE a.id = m.id AND {merge_sql}
    RETURNING a.id, 'updated'::text AS action
), inserted AS (
    INSERT INTO accounts (
        name, platform, type, credentials, extra, concurrency, priority, status,
        schedulable, auto_pause_on_expired, rate_multiplier, created_at, updated_at
    )
    SELECT
        i.name, 'grok', 'oauth', i.credentials, '{{}}'::jsonb, 1, 50, 'active',
        TRUE, TRUE, 1.0, NOW(), NOW()
    FROM incoming i
    WHERE NOT EXISTS (SELECT 1 FROM matched)
    RETURNING id, 'inserted'::text AS action
), chosen AS (
    SELECT * FROM updated
    UNION ALL
    SELECT * FROM inserted
), grouped AS (
    INSERT INTO account_groups (account_id, group_id, priority, created_at)
    SELECT id, {group_id}, 50, NOW() FROM chosen
    ON CONFLICT (account_id, group_id) DO UPDATE SET priority = EXCLUDED.priority
    RETURNING account_id
)
SELECT json_build_object(
    'ok', EXISTS(SELECT 1 FROM chosen),
    'id', (SELECT id FROM chosen LIMIT 1),
    'action', (SELECT action FROM chosen LIMIT 1),
    'duplicate', EXISTS(SELECT 1 FROM matched) AND NOT {merge_sql},
    'group_id', {group_id},
    'email', {_sql_literal(email)},
    'user_id', {_sql_literal(user_id)},
    'auth_key', {_sql_literal(auth_key)},
    'expires_at', {_sql_literal(credentials.get('expires_at') or '')},
    'has_refresh_token', {str(bool(credentials.get('refresh_token'))).upper()}
)::text;
COMMIT;
"""
    proc = _run_sub2api_psql(sql, cfg=cfg, timeout=45.0)
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout or "sub2api psql 写入失败").strip()[-500:],
            "email": email or None,
            "user_id": user_id or None,
        }
    data = _last_json_line(proc.stdout)
    if not data.get("ok"):
        return {
            "ok": False,
            "error": "sub2api 已存在相同账号",
            "email": email or None,
            "user_id": user_id or None,
        }
    return data


def _http_session():
    """本机/局域网上游请求禁用系统代理，避免 7897 之类代理把内网请求拖死。"""
    import requests

    s = requests.Session()
    s.trust_env = False
    return s


def _legacy_test_upstream_connectivity_postgres(base_url: str | None = None, password: str | None = None) -> dict:
    """验证 sub2api Web 与 Grok 分组数据库写入通道。"""
    import requests

    del password
    cfg = get_sub2api_config()
    base = normalize_upstream_url(base_url if base_url is not None else cfg["url"])
    if base:
        cfg["url"] = base

    if not base:
        return {"ok": False, "message": "请先填写 SUB2API_URL"}

    health = None
    try:
        hr = _http_session().get(f"{base}/health", timeout=6)
        if hr.status_code == 200:
            try:
                health = hr.json()
            except Exception:
                health = {"raw": hr.text[:200]}
        else:
            return {
                "ok": False,
                "message": f"sub2api 健康检查 HTTP {hr.status_code}",
                "base_url": base,
                "health_status": hr.status_code,
            }
    except requests.exceptions.ConnectionError:
        return {"ok": False, "message": f"连接 sub2api 失败: {base}", "base_url": base}
    except requests.exceptions.Timeout:
        return {"ok": False, "message": f"sub2api 健康检查超时: {base}", "base_url": base}
    except Exception as e:
        return {"ok": False, "message": f"sub2api 健康检查异常: {e}", "base_url": base}

    try:
        group_id = int(cfg["group_id"] or "23")
    except ValueError:
        return {"ok": False, "message": "SUB2API_GROK_GROUP_ID 必须是数字", "base_url": base}

    sql = f"""
SELECT json_build_object(
  'ok', true,
  'group_id', g.id,
  'group_name', g.name,
  'platform', g.platform,
  'status', g.status,
  'active_accounts', (
    SELECT count(*) FROM accounts a
    JOIN account_groups ag ON ag.account_id = a.id
    WHERE ag.group_id = g.id AND a.deleted_at IS NULL AND a.platform = 'grok' AND a.status = 'active'
  )
)::text
FROM groups g
WHERE g.id = {group_id} AND g.name = {_sql_literal(cfg['group_name'])} AND g.platform = 'grok'
LIMIT 1;
"""
    try:
        proc = _run_sub2api_psql(sql, cfg=cfg, timeout=20.0)
    except Exception as e:
        return {"ok": False, "message": f"sub2api 数据库探测异常: {e}", "base_url": base, "health": health}

    if proc.returncode != 0:
        return {
            "ok": False,
            "message": "sub2api 数据库探测失败: " + (proc.stderr or proc.stdout or "").strip()[-300:],
            "base_url": base,
            "health": health,
        }
    db = _last_json_line(proc.stdout)
    if not db.get("ok"):
        return {
            "ok": False,
            "message": f"sub2api Grok 分组未匹配：id={group_id}, name={cfg['group_name']}",
            "base_url": base,
            "health": health,
        }

    return {
        "ok": True,
        "message": f"sub2api 连通正常，Grok 分组 {db.get('group_name')}({db.get('group_id')}) 可写入",
        "base_url": base,
        "health": health,
        "health_ok": True,
        "db_ok": True,
        "group": db,
    }


def _sub2api_http_session():
    """HTTP 导入不继承本机代理设置，保证局域网 sub2api 可直连。"""
    import requests

    session = requests.Session()
    session.trust_env = False
    return session


def _sub2api_api_data(body) -> dict | list | None:
    if not isinstance(body, dict):
        return None
    return body.get("data") if "data" in body else body


def _sub2api_api_ok(status_code: int, body) -> bool:
    if not 200 <= status_code < 300:
        return False
    if isinstance(body, dict) and "code" in body:
        try:
            return int(body.get("code")) == 0
        except (TypeError, ValueError):
            return False
    return True


def _sub2api_api_message(body, fallback: str = "") -> str:
    if isinstance(body, dict):
        message = body.get("message") or body.get("error")
        if message:
            return str(message)
        data = body.get("data")
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
    return fallback or "unknown error"


def sub2api_http_login(
    base_url: str | None = None,
    email: str | None = None,
    password: str | None = None,
    cfg: dict | None = None,
) -> dict:
    """登录 sub2api Admin API 并返回 Bearer access token。"""
    import requests

    cfg = dict(cfg or get_sub2api_config())
    base = normalize_upstream_url(base_url if base_url is not None else cfg.get("url"))
    admin_email = (email if email is not None else cfg.get("admin_email") or "").strip()
    admin_password = password if password is not None else cfg.get("admin_password") or ""
    if not base:
        return {"ok": False, "error": "请先填写 SUB2API_URL"}
    if not admin_email or not admin_password:
        return {"ok": False, "error": "请先填写 UPSTREAM_ADMIN_EMAIL / UPSTREAM_ADMIN_PASSWORD"}

    try:
        response = _sub2api_http_session().post(
            f"{base}/api/v1/auth/login",
            json={"email": admin_email, "password": admin_password},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=12,
        )
        try:
            body = response.json()
        except Exception:
            body = {"raw": (response.text or "")[:300]}
        if not _sub2api_api_ok(response.status_code, body):
            return {
                "ok": False,
                "error": f"登录失败 HTTP {response.status_code}: {_sub2api_api_message(body, response.text[:200])}",
                "status_code": response.status_code,
                "base_url": base,
            }
        data = _sub2api_api_data(body)
        if not isinstance(data, dict):
            data = {}
        access_token = (
            data.get("access_token")
            or data.get("token")
            or body.get("access_token")
            or body.get("token")
            or ""
        )
        if not access_token:
            return {"ok": False, "error": "登录成功但未返回 access_token", "base_url": base}
        return {"ok": True, "access_token": access_token, "base_url": base}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": f"连接 sub2api 失败: {base}", "base_url": base}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": f"登录超时: {base}", "base_url": base}
    except Exception as exc:
        return {"ok": False, "error": f"登录异常: {exc}", "base_url": base}


def sub2api_http_request(
    method: str,
    path: str,
    *,
    token: str,
    base_url: str,
    json_body: dict | list | None = None,
    params: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    """发送带 Bearer 认证的 sub2api Admin API 请求。"""
    import requests

    base = normalize_upstream_url(base_url)
    if not base:
        return {"ok": False, "error": "缺少 SUB2API_URL"}
    if not path.startswith("/"):
        path = "/" + path
    url = f"{base}{path}"
    try:
        response = _sub2api_http_session().request(
            method.upper(),
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json=json_body,
            params=params,
            timeout=timeout,
        )
        try:
            body = response.json()
        except Exception:
            body = {"raw": (response.text or "")[:500]}
        ok = _sub2api_api_ok(response.status_code, body)
        return {
            "ok": ok,
            "status_code": response.status_code,
            "body": body,
            "data": _sub2api_api_data(body),
            "error": None if ok else _sub2api_api_message(body, f"HTTP {response.status_code}"),
        }
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": f"连接失败: {url}"}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": f"请求超时: {url}"}
    except Exception as exc:
        return {"ok": False, "error": f"请求异常: {exc}"}


def sub2api_find_account(
    token: str,
    base_url: str,
    *,
    email: str = "",
    user_id: str = "",
    auth_key: str = "",
    name: str = "",
) -> dict | None:
    """按稳定身份字段查找已有 Grok OAuth 账号。"""
    keywords: list[str] = []
    for value in (email, user_id, name, auth_key.split("::")[-1] if auth_key else ""):
        value = (value or "").strip()
        if value and value not in keywords:
            keywords.append(value)
    for keyword in keywords[:3]:
        response = sub2api_http_request(
            "GET",
            "/api/v1/admin/accounts",
            token=token,
            base_url=base_url,
            params={"page": 1, "page_size": 20, "platform": "grok", "keyword": keyword},
            timeout=15,
        )
        data = response.get("data") or {}
        items = data.get("items") if isinstance(data, dict) else []
        if not response.get("ok") or not isinstance(items, list):
            continue
        email_l = email.strip().lower()
        for item in items:
            if not isinstance(item, dict):
                continue
            credentials = item.get("credentials") or {}
            if not isinstance(credentials, dict):
                credentials = {}
            item_email = (credentials.get("email") or item.get("name") or "").strip().lower()
            item_user_id = str(credentials.get("user_id") or credentials.get("sub") or "").strip()
            item_auth_key = str(credentials.get("auth_key") or "").strip()
            if email_l and item_email == email_l:
                return item
            if user_id and item_user_id == user_id:
                return item
            if auth_key and item_auth_key == auth_key:
                return item
            if name and item.get("name") == name:
                return item
    return None


def sub2api_import_auth_entry(
    entry: dict,
    email_hint: str = "",
    merge: bool = True,
    *,
    token: str | None = None,
    base_url: str | None = None,
    cfg: dict | None = None,
) -> dict:
    """通过 HTTP Admin API 创建或更新 Grok OAuth 账号。"""
    cfg = dict(cfg or get_sub2api_config())
    credentials, name, auth_key, user_id, email = _entry_to_sub2api_credentials(entry, email_hint)
    try:
        group_id = int(cfg["group_id"])
    except (TypeError, ValueError):
        return {"ok": False, "error": "SUB2API_GROK_GROUP_ID 必须是数字", "email": email or None, "user_id": user_id or None}

    base = normalize_upstream_url(base_url or cfg.get("url"))
    if not token:
        login = sub2api_http_login(base_url=base, cfg=cfg)
        if not login.get("ok"):
            return {"ok": False, "error": login.get("error") or "sub2api 登录失败", "email": email or None, "user_id": user_id or None}
        token = login["access_token"]
        base = login["base_url"]
    payload = {
        "name": name,
        "platform": "grok",
        "type": "oauth",
        "group_ids": [group_id],
        "concurrency": 1,
        "priority": 50,
        "rate_multiplier": 1.0,
        "auto_pause_on_expired": True,
        "status": "active",
        "schedulable": True,
        "credentials": credentials,
    }
    existing = sub2api_find_account(token, base, email=email, user_id=user_id, auth_key=auth_key, name=name)
    if existing and existing.get("id") is not None:
        if not merge:
            return {"ok": False, "error": "sub2api 已存在相同账号", "email": email or None, "user_id": user_id or None, "id": existing["id"], "duplicate": True}
        group_ids = existing.get("group_ids") or []
        if isinstance(group_ids, list):
            normalized_group_ids = []
            for value in group_ids:
                try:
                    normalized_group_ids.append(int(value))
                except (TypeError, ValueError):
                    continue
            if group_id not in normalized_group_ids:
                normalized_group_ids.append(group_id)
            if normalized_group_ids:
                payload["group_ids"] = normalized_group_ids
        response = sub2api_http_request(
            "PUT",
            f"/api/v1/admin/accounts/{existing['id']}",
            token=token,
            base_url=base,
            json_body=payload,
        )
        if not response.get("ok"):
            return {"ok": False, "error": response.get("error") or "更新账号失败", "email": email or None, "user_id": user_id or None}
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        return {"ok": True, "id": data.get("id") or existing["id"], "action": "updated", "group_id": group_id, "email": email or None, "user_id": user_id or None, "auth_key": auth_key, "expires_at": credentials.get("expires_at") or "", "has_refresh_token": bool(credentials.get("refresh_token"))}

    response = sub2api_http_request(
        "POST",
        "/api/v1/admin/accounts",
        token=token,
        base_url=base,
        json_body=payload,
    )
    if not response.get("ok"):
        return {"ok": False, "error": response.get("error") or "创建账号失败", "email": email or None, "user_id": user_id or None}
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    return {"ok": True, "id": data.get("id"), "action": "inserted", "group_id": group_id, "email": email or None, "user_id": user_id or None, "auth_key": auth_key, "expires_at": credentials.get("expires_at") or "", "has_refresh_token": bool(credentials.get("refresh_token"))}


def test_upstream_connectivity(
    base_url: str | None = None,
    password: str | None = None,
    email: str | None = None,
) -> dict:
    """验证 sub2api HTTP 健康检查、管理员登录和目标分组。"""
    import requests

    cfg = get_sub2api_config()
    base = normalize_upstream_url(base_url if base_url is not None else cfg["url"])
    if email is not None:
        cfg["admin_email"] = email.strip()
    if password is not None:
        cfg["admin_password"] = password
    if not base:
        return {"ok": False, "message": "请先填写 SUB2API_URL"}
    try:
        group_id = int(cfg["group_id"])
    except (TypeError, ValueError):
        return {"ok": False, "message": "SUB2API_GROK_GROUP_ID 必须是数字", "base_url": base}
    try:
        health_response = _sub2api_http_session().get(f"{base}/health", timeout=6)
        health = health_response.json() if health_response.status_code == 200 else None
        if health_response.status_code != 200:
            return {"ok": False, "message": f"sub2api 健康检查 HTTP {health_response.status_code}", "base_url": base}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "message": f"连接 sub2api 失败: {base}", "base_url": base}
    except requests.exceptions.Timeout:
        return {"ok": False, "message": f"sub2api 健康检查超时: {base}", "base_url": base}
    except Exception as exc:
        return {"ok": False, "message": f"sub2api 健康检查异常: {exc}", "base_url": base}

    login = sub2api_http_login(base_url=base, cfg=cfg)
    if not login.get("ok"):
        return {"ok": False, "message": login.get("error") or "管理员登录失败", "base_url": base, "health": health}
    response = sub2api_http_request(
        "GET",
        "/api/v1/admin/groups",
        token=login["access_token"],
        base_url=base,
        params={"platform": "grok", "page": 1, "page_size": 50},
        timeout=15,
    )
    data = response.get("data") or {}
    groups = data.get("items") if isinstance(data, dict) else []
    if not response.get("ok") or not isinstance(groups, list):
        return {"ok": False, "message": f"读取分组失败: {response.get('error')}", "base_url": base, "health": health}
    wanted_name = cfg["group_name"].strip().lower()
    group = next((item for item in groups if isinstance(item, dict) and int(item.get("id") or 0) == group_id), None)
    if group is None:
        group = next((item for item in groups if isinstance(item, dict) and str(item.get("name") or "").strip().lower() == wanted_name), None)
    if group is None:
        return {"ok": False, "message": f"sub2api Grok 分组未匹配：id={group_id}, name={cfg['group_name']}", "base_url": base, "health": health}
    return {
        "ok": True,
        "message": f"sub2api HTTP 连通正常，已登录并可访问 Grok 分组 {group.get('name')}({group.get('id')})",
        "base_url": base,
        "health": health,
        "group": {"group_id": group.get("id"), "group_name": group.get("name"), "platform": group.get("platform")},
    }


def _parse_sso_import_lines(sso_lines: list[str]) -> list[tuple[str, str]]:
    """解析 email----sso / 纯 sso 行，返回 [(email, sso), ...]。"""
    out: list[tuple[str, str]] = []
    for raw in sso_lines or []:
        for line in str(raw or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            email = ""
            sso = line
            if "----" in line:
                parts = line.split("----")
                email = parts[0].strip()
                sso = parts[-1].strip()
            elif ":" in line and not line.startswith("eyJ"):
                parts = line.rsplit(":", 1)
                email = parts[0].strip()
                sso = parts[-1].strip()
            if sso:
                out.append((email, sso))
    return out


def _normalize_import_accounts(
    sso_lines: list[str] | None = None,
    accounts: list[dict] | None = None,
) -> list[dict]:
    """
    统一导入入参：
    - accounts: [{email?, sso, auth_token?}, ...]（注册成功缓存 token 的首选路径）
    - sso_lines: email----sso / 纯 sso（无 token，需 device flow）
    """
    out: list[dict] = []
    seen_sso: set[str] = set()

    if accounts:
        for raw in accounts:
            if not isinstance(raw, dict):
                continue
            sso = str(raw.get("sso") or "").strip()
            if not sso or sso in seen_sso:
                continue
            seen_sso.add(sso)
            email = str(raw.get("email") or "").strip()
            token = raw.get("auth_token")
            out.append(
                {
                    "email": email,
                    "sso": sso,
                    "auth_token": token if isinstance(token, dict) else None,
                }
            )

    if sso_lines:
        for email, sso in _parse_sso_import_lines(sso_lines):
            sso = (sso or "").strip()
            if not sso or sso in seen_sso:
                continue
            seen_sso.add(sso)
            out.append(
                {
                    "email": (email or "").strip(),
                    "sso": sso,
                    "auth_token": None,
                }
            )
    return out


def import_sso_to_upstream(
    sso_lines: list[str] | None = None,
    merge: bool = True,
    max_workers: int = 1,
    base_url: str | None = None,
    password: str | None = None,
    accounts: list[dict] | None = None,
    progress_cb=None,
) -> dict:
    """
    导入 SSO 到 sub2api 的 grok 分组（HTTP Admin API）。

    流程（优先快路径）：
    1) 若注册时已缓存可用 auth_token → 直接走 HTTP 创建/更新账号
    2) 否则本机 device flow 换 token 后再通过 HTTP 写入

    max_workers 保留参数兼容；有缓存 token 时串行 HTTP 写入即可，
    无 token 的 device flow 仍串行（防限流）。
    """
    import time
    from grok import (
        sso_device_flow_to_token,
        token_to_auth_entry,
        is_auth_token_usable,
        _device_flow_error_kind,
        _device_flow_wait_seconds,
        _mark_device_flow_cooldown,
    )

    def _progress(
        *,
        done=None,
        total=None,
        success=None,
        fail=None,
        current="",
        phase="",
    ):
        if not progress_cb:
            return
        try:
            progress_cb(
                done=done,
                total=total,
                success=success,
                fail=fail,
                current=current,
                phase=phase,
            )
        except Exception:
            pass

    del max_workers, password  # device flow 路径固定串行；缓存 token 路径也串行写库
    if base_url:
        os.environ["SUB2API_URL"] = normalize_upstream_url(base_url)
    sub2 = get_sub2api_config()

    items = _normalize_import_accounts(sso_lines=sso_lines, accounts=accounts)
    if not items:
        return {"ok": False, "message": "没有可导入的 SSO"}
    if not sub2.get("url") or not sub2.get("group_id"):
        return {"ok": False, "message": "请先配置 SUB2API_URL 与 SUB2API_GROK_GROUP_ID"}
    if not sub2.get("admin_email") or not sub2.get("admin_password"):
        return {"ok": False, "message": "请先配置 UPSTREAM_ADMIN_EMAIL 与 UPSTREAM_ADMIN_PASSWORD"}

    login = sub2api_http_login(cfg=sub2)
    if not login.get("ok"):
        return {"ok": False, "message": login.get("error") or "sub2api 管理员登录失败"}
    api_token = login["access_token"]
    api_base = login["base_url"]

    results_by_idx: dict[int, dict] = {}
    imported: list[dict] = []
    ok_count = 0
    fail_count = 0
    cached_hits = 0
    flow_hits = 0
    _progress(done=0, total=len(items), success=0, fail=0, phase="start")

    def _write_token_to_sub2api(entry: dict, email_hint: str, idx: int) -> dict:
        item: dict = {
            "index": idx,
            "email": email_hint or None,
            "sso_hint": None,
        }
        data = sub2api_import_auth_entry(
            entry,
            email_hint=email_hint,
            merge=merge,
            token=api_token,
            base_url=api_base,
            cfg=sub2,
        )
        if not data.get("ok"):
            item["status"] = "failed"
            item["error"] = data.get("error") or "sub2api 写入失败"
            item["email"] = data.get("email") or email_hint or None
            item["user_id"] = data.get("user_id") or entry.get("user_id")
            return item

        item["status"] = "ok"
        item["account_id"] = data.get("id")
        item["action"] = data.get("action")
        item["group_id"] = data.get("group_id")
        item["email"] = data.get("email") or email_hint or entry.get("email") or None
        item["user_id"] = data.get("user_id") or entry.get("user_id")
        item["expires_at"] = data.get("expires_at") or entry.get("expires_at")
        item["has_refresh_token"] = bool(
            data.get("has_refresh_token")
            if "has_refresh_token" in data
            else entry.get("refresh_token")
        )
        return item

    def _device_flow_to_token(sso: str, idx: int, *, pass_label: str, max_attempts: int):
        flow = None
        last_flow_err = "本机 device flow 失败"
        for attempt in range(1, max_attempts + 1):
            flow = sso_device_flow_to_token(sso, timeout=28)
            if flow.get("ok") and flow.get("token"):
                return flow, None
            last_flow_err = flow.get("error") or last_flow_err
            if "会话无效" in str(last_flow_err) or "非有效 JWT" in str(last_flow_err):
                break
            kind = _device_flow_error_kind(str(last_flow_err))
            if attempt < max_attempts:
                wait = _device_flow_wait_seconds(str(last_flow_err), attempt)
                if kind == "rate_limited":
                    _mark_device_flow_cooldown(wait)
                logs.emit(
                    f"sub2api 导入{pass_label} [{idx}/{len(items)}] device flow 重试 "
                    f"{attempt}/{max_attempts}（{kind}: {last_flow_err}），等待 {wait:.0f}s…",
                    "warn",
                )
                time.sleep(wait)
        return None, last_flow_err

    def _run_one(
        idx: int,
        email_hint: str,
        sso: str,
        auth_token: dict | None,
        *,
        pass_label: str,
        max_attempts: int,
        prefer_cached: bool = True,
    ) -> dict:
        nonlocal cached_hits, flow_hits
        item: dict = {
            "index": idx,
            "email": email_hint or None,
            "sso_hint": (sso[:12] + "...") if len(sso) > 12 else sso,
        }
        token = None
        used_cached = False

        # 快路径：注册时已缓存且未过期的 token，直接写 sub2api
        if prefer_cached and is_auth_token_usable(auth_token):
            token = auth_token
            used_cached = True
        else:
            flow, last_flow_err = _device_flow_to_token(
                sso, idx, pass_label=pass_label, max_attempts=max_attempts
            )
            if not flow or not flow.get("token"):
                item["status"] = "failed"
                item["error"] = last_flow_err or "本机 device flow 失败（SSO 不可导入）"
                item["retryable"] = _device_flow_error_kind(str(last_flow_err)) != "invalid"
                item["via"] = "device_flow"
                return item
            token = flow["token"]

        entry = token_to_auth_entry(token, email=email_hint or "")
        written = _write_token_to_sub2api(entry, email_hint or "", idx)
        written["sso_hint"] = item["sso_hint"]
        written["via"] = "cached_token" if used_cached else "device_flow"
        if used_cached:
            cached_hits += 1
        else:
            flow_hits += 1
        if not written.get("email"):
            written["email"] = email_hint or None
        return written

    cached_ready = sum(1 for it in items if is_auth_token_usable(it.get("auth_token")))
    if cached_ready:
        logs.emit(
            f"sub2api 导入：{len(items)} 条中 {cached_ready} 条已有缓存 token，优先直写（跳过 device flow）",
            "info",
        )

    # —— 第一轮：优先缓存 token 直写；无缓存再 device flow ——
    need_flow_gap = False  # 仅在刚跑过 device flow 后才拉间隔
    for idx, acc in enumerate(items, 1):
        email_hint = acc.get("email") or ""
        sso = acc.get("sso") or ""
        auth_token = acc.get("auth_token")
        has_cached = is_auth_token_usable(auth_token)
        label = email_hint or ((sso[:12] + "…") if sso else f"#{idx}")
        _progress(
            done=idx - 1,
            total=len(items),
            success=ok_count,
            fail=fail_count,
            current=label,
            phase="import",
        )

        # 有缓存：几乎无间隔；无缓存且上一条也走了 flow：防限流 sleep
        if idx > 1 and need_flow_gap and not has_cached:
            time.sleep(5.0)

        try:
            item = _run_one(
                idx,
                email_hint,
                sso,
                auth_token if isinstance(auth_token, dict) else None,
                pass_label="",
                max_attempts=5,
                prefer_cached=True,
            )
        except Exception as e:
            item = {
                "index": idx,
                "email": email_hint or None,
                "status": "failed",
                "error": f"导入异常: {e}",
                "retryable": True,
                "via": "error",
            }
            logs.emit(f"sub2api 导入 [{idx}/{len(items)}] 异常: {e}", "error")

        need_flow_gap = item.get("via") == "device_flow"
        results_by_idx[idx] = item
        via_tag = item.get("via") or "?"
        if item.get("status") == "ok":
            ok_count += 1
            imported.append(
                {
                    "id": item.get("account_id"),
                    "email": item.get("email"),
                    "user_id": item.get("user_id"),
                    "expires_at": item.get("expires_at"),
                    "has_refresh_token": item.get("has_refresh_token"),
                }
            )
            logs.emit(
                f"sub2api 导入 [{idx}/{len(items)}] 成功({via_tag}): "
                f"{item.get('email') or item.get('user_id') or 'ok'}",
                "success",
            )
        else:
            fail_count += 1
            logs.emit(
                f"sub2api 导入 [{idx}/{len(items)}] 失败({via_tag}): {item.get('error')}",
                "warn",
            )
            if _device_flow_error_kind(str(item.get("error") or "")) in (
                "rate_limited",
                "timeout",
                "tls",
            ):
                time.sleep(4.0)
        _progress(
            done=idx,
            total=len(items),
            success=ok_count,
            fail=fail_count,
            current=label,
            phase="import",
        )

    # —— 第二轮：仅对「可重试」失败项再跑 device flow（冷却后抢救） ——
    retry_list = [
        (idx, items[idx - 1].get("email") or "", items[idx - 1].get("sso") or "")
        for idx in range(1, len(items) + 1)
        if results_by_idx.get(idx, {}).get("status") == "failed"
        and results_by_idx[idx].get("retryable", True)
        and _device_flow_error_kind(str(results_by_idx[idx].get("error") or ""))
        in ("rate_limited", "timeout", "tls", "device_code", "token_poll", "other")
    ]
    if retry_list:
        cool = 25.0
        logs.emit(
            f"sub2api 导入：第一轮 {ok_count} 成功 / {fail_count} 失败，"
            f"冷却 {cool:.0f}s 后抢救 {len(retry_list)} 条…",
            "info",
        )
        _progress(
            done=len(items),
            total=len(items),
            success=ok_count,
            fail=fail_count,
            current=f"冷却 {cool:.0f}s 后抢救 {len(retry_list)} 条",
            phase="retry_cooldown",
        )
        _mark_device_flow_cooldown(cool)
        time.sleep(cool)
        for j, (idx, email_hint, sso) in enumerate(retry_list):
            label = email_hint or (sso[:12] + "…") if sso else f"#{idx}"
            _progress(
                done=len(items),
                total=len(items),
                success=ok_count,
                fail=fail_count,
                current=f"抢救 {j + 1}/{len(retry_list)}: {label}",
                phase="retry",
            )
            if j > 0:
                time.sleep(8.0)
            try:
                item = _run_one(
                    idx,
                    email_hint,
                    sso,
                    None,  # 抢救轮强制重新 device flow
                    pass_label="·抢救",
                    max_attempts=4,
                    prefer_cached=False,
                )
            except Exception as e:
                item = {
                    "index": idx,
                    "email": email_hint or None,
                    "status": "failed",
                    "error": f"抢救异常: {e}",
                }
            if item.get("status") == "ok":
                # 失败 → 成功：修正计数
                fail_count = max(0, fail_count - 1)
                ok_count += 1
                results_by_idx[idx] = item
                imported.append(
                    {
                        "id": item.get("account_id"),
                        "email": item.get("email"),
                        "user_id": item.get("user_id"),
                        "expires_at": item.get("expires_at"),
                        "has_refresh_token": item.get("has_refresh_token"),
                    }
                )
                logs.emit(
                    f"sub2api 导入·抢救 [{idx}/{len(items)}] 成功: "
                    f"{item.get('email') or item.get('user_id') or 'ok'}",
                    "success",
                )
            else:
                results_by_idx[idx] = item
                logs.emit(
                    f"sub2api 导入·抢救 [{idx}/{len(items)}] 仍失败: {item.get('error')}",
                    "warn",
                )
            _progress(
                done=len(items),
                total=len(items),
                success=ok_count,
                fail=fail_count,
                current=f"抢救 {j + 1}/{len(retry_list)}: {label}",
                phase="retry",
            )

    results = [results_by_idx[i] for i in sorted(results_by_idx)]
    msg = (
        f"SSO 导入 sub2api 完成：{ok_count} 成功, {fail_count} 失败"
        f"（缓存直写 {cached_hits}，device flow {flow_hits}）"
    )
    return {
        "ok": fail_count == 0 and ok_count > 0,
        "message": msg,
        "success": ok_count,
        "fail": fail_count,
        "total": len(items),
        "cached_hits": cached_hits,
        "flow_hits": flow_hits,
        "results": results,
        "imported": imported,
        "base_url": api_base,
        "group_id": sub2.get("group_id"),
        "group_name": sub2.get("group_name"),
        "mode": "cached_token_or_device_flow_then_sub2api_http",
    }

def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return value[:keep] + "*" * min(12, len(value) - keep * 2) + value[-keep:]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "grok-register-ui"})


@app.get("/api/status")
def status():
    data = engine.get_status()
    data["env"] = env_snapshot()
    job = get_import_job_public()
    # 导入刚结束时把 recent_success 一并带回，前端可立刻刷新「已导入」标记
    if job.get("status") in ("done", "error") and not job.get("running"):
        job["recent_success"] = data.get("recent_success", [])
    data["import_job"] = job
    try:
        data["solver"] = solver_manager.status()
    except Exception as e:
        data["solver"] = {"ready": False, "message": f"状态读取失败: {e}"}
    return jsonify(data)


@app.get("/api/upstream/import/status")
def upstream_import_status():
    job = get_import_job_public()
    if job.get("status") in ("done", "error") and not job.get("running"):
        job["recent_success"] = engine.get_status().get("recent_success", [])
    return jsonify({"ok": True, **job})


@app.get("/api/config")
def get_config():
    cfg = read_env_file()
    return jsonify({
        "ok": True,
        "config": {
            "WORKER_DOMAIN": cfg.get("WORKER_DOMAIN", ""),
            "FREEMAIL_TOKEN": cfg.get("FREEMAIL_TOKEN", ""),
            "FREEMAIL_DOMAIN": cfg.get("FREEMAIL_DOMAIN", DEFAULTS["FREEMAIL_DOMAIN"]),
            "FREEMAIL_API_STYLE": cfg.get("FREEMAIL_API_STYLE", DEFAULTS["FREEMAIL_API_STYLE"]),
            "YESCAPTCHA_KEY": cfg.get("YESCAPTCHA_KEY", ""),
            "SOLVER_URL": cfg.get("SOLVER_URL", DEFAULTS["SOLVER_URL"]),
            "SOLVER_BROWSER": cfg.get("SOLVER_BROWSER", DEFAULTS["SOLVER_BROWSER"]),
            "SOLVER_THREADS": cfg.get("SOLVER_THREADS", DEFAULTS["SOLVER_THREADS"]),
            "SOLVER_HOST": cfg.get("SOLVER_HOST", DEFAULTS["SOLVER_HOST"]),
            "SOLVER_PORT": cfg.get("SOLVER_PORT", DEFAULTS["SOLVER_PORT"]),
            "SOLVER_DEBUG": cfg.get("SOLVER_DEBUG", DEFAULTS["SOLVER_DEBUG"]),
            "UI_HOST": cfg.get("UI_HOST", DEFAULTS["UI_HOST"]),
            "UI_PORT": cfg.get("UI_PORT", DEFAULTS["UI_PORT"]),
            "SUB2API_URL": cfg.get("SUB2API_URL", cfg.get("UPSTREAM_URL", DEFAULTS["SUB2API_URL"])),
            "SUB2API_GROK_GROUP_ID": cfg.get("SUB2API_GROK_GROUP_ID", DEFAULTS["SUB2API_GROK_GROUP_ID"]),
            "SUB2API_GROK_GROUP_NAME": cfg.get("SUB2API_GROK_GROUP_NAME", DEFAULTS["SUB2API_GROK_GROUP_NAME"]),
            "UPSTREAM_URL": cfg.get("SUB2API_URL", cfg.get("UPSTREAM_URL", DEFAULTS["SUB2API_URL"])),
            "UPSTREAM_ADMIN_EMAIL": cfg.get("UPSTREAM_ADMIN_EMAIL", ""),
            "UPSTREAM_ADMIN_PASSWORD": cfg.get("UPSTREAM_ADMIN_PASSWORD", ""),
            "captcha_mode": "yescaptcha" if cfg.get("YESCAPTCHA_KEY", "").strip() else "local",
        },
        "masked": {
            "FREEMAIL_TOKEN": mask_secret(cfg.get("FREEMAIL_TOKEN", "")),
            "YESCAPTCHA_KEY": mask_secret(cfg.get("YESCAPTCHA_KEY", "")),
            "UPSTREAM_ADMIN_PASSWORD": mask_secret(cfg.get("UPSTREAM_ADMIN_PASSWORD", "")),
        },
    })


@app.get("/api/mail-domains")
def mail_domains():
    """从 freemail / cloudflare_temp_email 自动拉取可用邮箱域名"""
    from g.email_service import EmailService

    worker = request.args.get("worker_domain")
    token = request.args.get("token")
    # 未传 token 时用已保存配置；传空字符串表示无密码
    if token is None:
        token = read_env_file().get("FREEMAIL_TOKEN", "")
    if worker is None:
        worker = read_env_file().get("WORKER_DOMAIN", "")
    try:
        result = EmailService.fetch_mail_domains(worker_domain=worker, token=token)
        code = 200 if result.get("ok") else 502
        return jsonify(result), code
    except Exception as e:
        return jsonify({
            "ok": False,
            "domains": [],
            "default_domains": [],
            "selected": "auto",
            "message": str(e),
            "settings": {},
        }), 500


@app.post("/api/config")
def save_config():
    if engine.is_running():
        return jsonify({"ok": False, "message": "任务运行中，请先停止再修改配置"}), 409

    body = request.get_json(silent=True) or {}
    current = read_env_file()

    worker = str(body.get("WORKER_DOMAIN", current.get("WORKER_DOMAIN", ""))).strip()
    token = str(body.get("FREEMAIL_TOKEN", current.get("FREEMAIL_TOKEN", ""))).strip()
    mail_domain = str(body.get("FREEMAIL_DOMAIN", current.get("FREEMAIL_DOMAIN", DEFAULTS["FREEMAIL_DOMAIN"]))).strip() or "auto"
    api_style = str(body.get("FREEMAIL_API_STYLE", current.get("FREEMAIL_API_STYLE", DEFAULTS["FREEMAIL_API_STYLE"]))).strip() or "auto"
    yes = str(body.get("YESCAPTCHA_KEY", current.get("YESCAPTCHA_KEY", ""))).strip()
    solver = str(body.get("SOLVER_URL", current.get("SOLVER_URL", DEFAULTS["SOLVER_URL"]))).strip()
    solver_browser = str(body.get("SOLVER_BROWSER", current.get("SOLVER_BROWSER", DEFAULTS["SOLVER_BROWSER"]))).strip() or "camoufox"
    solver_threads = str(body.get("SOLVER_THREADS", current.get("SOLVER_THREADS", DEFAULTS["SOLVER_THREADS"]))).strip() or "2"
    solver_host = str(body.get("SOLVER_HOST", current.get("SOLVER_HOST", DEFAULTS["SOLVER_HOST"]))).strip() or "127.0.0.1"
    solver_port = str(body.get("SOLVER_PORT", current.get("SOLVER_PORT", DEFAULTS["SOLVER_PORT"]))).strip() or "5072"
    solver_debug = str(body.get("SOLVER_DEBUG", current.get("SOLVER_DEBUG", DEFAULTS["SOLVER_DEBUG"]))).strip() or "1"
    ui_host = str(body.get("UI_HOST", current.get("UI_HOST", DEFAULTS["UI_HOST"]))).strip() or "127.0.0.1"
    ui_port = str(body.get("UI_PORT", current.get("UI_PORT", DEFAULTS["UI_PORT"]))).strip() or "3333"
    sub2api_url = str(body.get("SUB2API_URL", body.get("UPSTREAM_URL", current.get("SUB2API_URL", current.get("UPSTREAM_URL", DEFAULTS["SUB2API_URL"]))))).strip()
    sub2api_group_id = str(body.get("SUB2API_GROK_GROUP_ID", current.get("SUB2API_GROK_GROUP_ID", DEFAULTS["SUB2API_GROK_GROUP_ID"]))).strip() or DEFAULTS["SUB2API_GROK_GROUP_ID"]
    sub2api_group_name = str(body.get("SUB2API_GROK_GROUP_NAME", current.get("SUB2API_GROK_GROUP_NAME", DEFAULTS["SUB2API_GROK_GROUP_NAME"]))).strip() or DEFAULTS["SUB2API_GROK_GROUP_NAME"]
    upstream_url = sub2api_url
    upstream_email = str(body.get("UPSTREAM_ADMIN_EMAIL", current.get("UPSTREAM_ADMIN_EMAIL", ""))).strip()
    upstream_pwd = str(body.get("UPSTREAM_ADMIN_PASSWORD", current.get("UPSTREAM_ADMIN_PASSWORD", ""))).strip()

    # 允许前端传空密钥表示“保留原值”：用特殊标记
    if body.get("FREEMAIL_TOKEN") is None:
        token = current.get("FREEMAIL_TOKEN", "")
    if body.get("YESCAPTCHA_KEY") is None:
        yes = current.get("YESCAPTCHA_KEY", "")
    if body.get("UPSTREAM_ADMIN_EMAIL") is None:
        upstream_email = current.get("UPSTREAM_ADMIN_EMAIL", "")
    if body.get("UPSTREAM_ADMIN_PASSWORD") is None:
        upstream_pwd = current.get("UPSTREAM_ADMIN_PASSWORD", "")

    captcha_mode = str(body.get("captcha_mode", "")).strip().lower()
    if captcha_mode == "local":
        # 本地 solver 模式可清空 yescaptcha
        if "YESCAPTCHA_KEY" in body and body.get("YESCAPTCHA_KEY") == "":
            yes = ""

    # 规范化 worker 域名
    worker = worker.replace("https://", "").replace("http://", "").strip().rstrip("/")
    if mail_domain.lower() in ("", "auto", "default", "随机", "自动"):
        mail_domain = "auto"
    sub2api_url = normalize_upstream_url(sub2api_url) or DEFAULTS["SUB2API_URL"]
    upstream_url = sub2api_url

    if not re.fullmatch(r"\d{2,5}", ui_port):
        return jsonify({"ok": False, "message": "UI_PORT 必须是 2-5 位数字"}), 400
    if not re.fullmatch(r"\d{2,5}", solver_port):
        return jsonify({"ok": False, "message": "SOLVER_PORT 必须是 2-5 位数字"}), 400
    if not re.fullmatch(r"\d{1,2}", solver_threads) or not (1 <= int(solver_threads) <= 16):
        return jsonify({"ok": False, "message": "SOLVER_THREADS 范围 1-16"}), 400
    if sub2api_group_id and not re.fullmatch(r"\d+", sub2api_group_id):
        return jsonify({"ok": False, "message": "SUB2API_GROK_GROUP_ID 必须是数字"}), 400
    if solver_browser not in ("camoufox", "chromium", "chrome", "msedge"):
        return jsonify({"ok": False, "message": "SOLVER_BROWSER 不支持该值"}), 400
    if not solver:
        solver = DEFAULTS["SOLVER_URL"]

    values = {
        "WORKER_DOMAIN": worker,
        "FREEMAIL_TOKEN": token,
        "FREEMAIL_DOMAIN": mail_domain,
        "FREEMAIL_API_STYLE": api_style,
        "YESCAPTCHA_KEY": yes,
        "SOLVER_URL": solver,
        "SOLVER_BROWSER": solver_browser,
        "SOLVER_THREADS": solver_threads,
        "SOLVER_HOST": solver_host,
        "SOLVER_PORT": solver_port,
        "SOLVER_DEBUG": "1" if solver_debug not in ("0", "false", "False") else "0",
        "UI_HOST": ui_host,
        "UI_PORT": ui_port,
        "SUB2API_URL": sub2api_url,
        "SUB2API_GROK_GROUP_ID": sub2api_group_id,
        "SUB2API_GROK_GROUP_NAME": sub2api_group_name,
        "UPSTREAM_URL": upstream_url,
        "UPSTREAM_ADMIN_EMAIL": upstream_email,
        "UPSTREAM_ADMIN_PASSWORD": upstream_pwd,
    }
    try:
        write_env_file(values)
        apply_env_to_process(values)
        logs.emit(f"配置已保存到 .env（邮箱域名: {mail_domain}）", "success")

        # 保存后自动测试 sub2api HTTP 写入通道
        upstream_test = None
        if sub2api_url and upstream_email and upstream_pwd and sub2api_group_id:
            try:
                upstream_test = test_upstream_connectivity(
                    sub2api_url,
                    password=upstream_pwd,
                    email=upstream_email,
                )
                level = "success" if upstream_test.get("ok") else "warn"
                logs.emit(f"sub2api 连通性: {upstream_test.get('message')}", level)
            except Exception as te:
                upstream_test = {"ok": False, "message": str(te)}
                logs.emit(f"sub2api 连通性测试异常: {te}", "warn")

        msg = "配置已保存"
        if upstream_test is not None:
            msg += "；" + ("sub2api 连通正常" if upstream_test.get("ok") else f"sub2api 连通失败: {upstream_test.get('message')}")

        return jsonify({
            "ok": True,
            "message": msg,
            "env": env_snapshot(),
            "config": values,
            "upstream_test": upstream_test,
        })
    except Exception as e:
        return jsonify({"ok": False, "message": f"保存失败: {e}"}), 500


@app.get("/api/solver/status")
def solver_status():
    try:
        return jsonify({"ok": True, **solver_manager.status()})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.post("/api/solver/start")
def solver_start():
    body = request.get_json(silent=True) or {}
    wait = body.get("wait", True)
    try:
        timeout = float(body.get("timeout", 90))
    except (TypeError, ValueError):
        timeout = 90.0
    try:
        result = solver_manager.start(wait_ready=bool(wait), timeout=timeout)
        if result.get("ok"):
            logs.emit(result.get("message") or "Solver 已启动", "success")
        else:
            logs.emit(result.get("message") or "Solver 启动失败", "error")
        code = 200 if result.get("ok") else 500
        return jsonify(result), code
    except Exception as e:
        logs.emit(f"Solver 启动异常: {e}", "error")
        return jsonify({"ok": False, "message": str(e)}), 500


@app.post("/api/solver/stop")
def solver_stop():
    try:
        result = solver_manager.stop()
        level = "success" if result.get("ok") else "warn"
        logs.emit(result.get("message") or "Solver 已停止", level)
        code = 200 if result.get("ok") else 500
        return jsonify(result), code
    except Exception as e:
        logs.emit(f"Solver 停止异常: {e}", "error")
        return jsonify({"ok": False, "message": str(e)}), 500


@app.get("/api/logs")
def get_logs():
    after_id = request.args.get("after_id", 0, type=int)
    limit = request.args.get("limit", 200, type=int)
    limit = max(1, min(limit, 500))
    latest = logs.latest_id()
    # after_id 大于服务端序号 = 页面是旧会话，需重置游标
    reset = bool(after_id and after_id > latest)
    return jsonify({
        "logs": logs.since(after_id, limit),
        "latest_id": latest,
        "reset": reset,
    })


@app.post("/api/start")
def start():
    body = request.get_json(silent=True) or {}
    workers = body.get("workers", 8)
    target = body.get("target", 100)
    try:
        workers = int(workers)
        target = int(target)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "并发数/数量必须是整数"}), 400

    if workers < 1 or workers > 64:
        return jsonify({"ok": False, "message": "并发数范围 1-64"}), 400
    if target < 1 or target > 100000:
        return jsonify({"ok": False, "message": "注册数量范围 1-100000"}), 400

    env = env_snapshot()
    if not env["worker_domain_set"] or not env["freemail_token_set"]:
        return jsonify({
            "ok": False,
            "message": "请先在「配置」中填写 WORKER_DOMAIN 与 FREEMAIL_TOKEN",
        }), 400

    # 本地 Solver 模式：开任务前自动确保 5072 在线（注册机与 Solver 是两个进程）
    cfg_env = read_env_file()
    use_local_solver = not (cfg_env.get("YESCAPTCHA_KEY") or "").strip()
    solver_info = None
    if use_local_solver:
        # Solver 默认只有 2 个浏览器；并发远大于线程数会堆队列，也更容易把进程拖崩
        try:
            solver_threads = int(cfg_env.get("SOLVER_THREADS") or "2")
        except ValueError:
            solver_threads = 2
        solver_threads = max(1, min(solver_threads, 16))
        if workers > solver_threads * 2:
            logs.emit(
                f"提示：并发 {workers} 远大于 Solver 浏览器数 {solver_threads}，"
                f"建议并发 ≤ {solver_threads * 2}，或在配置里提高 SOLVER_THREADS 后重启 Solver",
                "warn",
            )
        elif workers > solver_threads:
            logs.emit(
                f"提示：并发 {workers} > Solver 浏览器 {solver_threads}，"
                f"Turnstile 会排队；机器够用可把 SOLVER_THREADS 调到 {workers}",
                "info",
            )

        logs.emit("检查 Turnstile Solver（5072）…", "info")
        try:
            solver_info = solver_manager.ensure_ready(timeout=120.0)
        except Exception as e:
            solver_info = {"ok": False, "message": f"Solver 检查异常: {e}"}
        if not solver_info.get("ok") or not solver_info.get("ready"):
            msg = solver_info.get("message") or "Turnstile Solver 未就绪"
            logs.emit(f"Solver 未就绪，无法开始注册: {msg}", "error")
            return jsonify({
                "ok": False,
                "message": (
                    f"Turnstile Solver 离线/未就绪：{msg}。"
                    "请点「启动 Solver」，或运行 TurnstileSolver.bat / "
                    "python solver_manager.py start，并查看 logs/turnstile_solver.log"
                ),
                "solver": solver_info,
            }), 503
        if solver_info.get("started"):
            logs.emit(solver_info.get("message") or "Solver 已自动启动", "success")
        else:
            logs.emit("Solver 已在线", "info")

        # 任务期间后台看门狗：Solver 中途崩溃自动拉起
        try:
            wd = solver_manager.start_watchdog(
                log_fn=lambda msg, level="info": logs.emit(msg, level),
                interval=6.0,
            )
            logs.emit(wd.get("message") or "Solver 看门狗已启动", "info")
        except Exception as e:
            logs.emit(f"Solver 看门狗启动失败（任务仍继续）: {e}", "warn")

    result = engine.start(workers=workers, target=target, blocking=False)
    if not result.get("ok") and use_local_solver:
        try:
            solver_manager.stop_watchdog()
        except Exception:
            pass
    if solver_info is not None:
        result["solver"] = {
            "ready": bool(solver_info.get("ready")),
            "pid": solver_info.get("pid"),
            "message": solver_info.get("message"),
            "auto_started": bool(solver_info.get("started")),
            "watchdog": solver_manager.watchdog_running(),
        }
    code = 200 if result.get("ok") else 409
    return jsonify(result), code


@app.post("/api/stop")
def stop():
    try:
        solver_manager.stop_watchdog()
    except Exception:
        pass
    result = engine.stop()
    code = 200 if result.get("ok") else 409
    return jsonify(result), code


@app.post("/api/logs/clear")
def clear_logs():
    logs.clear()
    logs.emit("日志已清空", "info")
    return jsonify({"ok": True})


@app.get("/api/keys")
def list_keys():
    keys_dir = BASE_DIR / "keys"
    if not keys_dir.exists():
        return jsonify({"files": []})
    files = []
    for p in sorted(keys_dir.glob("*.txt"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            lines = sum(1 for _ in open(p, "r", encoding="utf-8", errors="ignore") if _.strip())
        except Exception:
            lines = 0
        files.append({
            "name": p.name,
            "path": str(p.relative_to(BASE_DIR)).replace("\\", "/"),
            "size": p.stat().st_size,
            "count": lines,
            "mtime": p.stat().st_mtime,
        })
    return jsonify({"files": files[:30]})


@app.post("/api/upstream/test")
def upstream_test():
    """测试 sub2api HTTP Admin API（可用临时参数覆盖已保存配置）。"""
    body = request.get_json(silent=True) or {}
    base = body.get("SUB2API_URL") or body.get("UPSTREAM_URL") or body.get("url")
    email = body.get("UPSTREAM_ADMIN_EMAIL") or body.get("email")
    pwd = body.get("UPSTREAM_ADMIN_PASSWORD") or body.get("password")
    # 空字符串表示“用已保存值”
    if base is not None and str(base).strip() == "":
        base = None
    if pwd is not None and str(pwd).strip() == "":
        pwd = None
    if email is not None and str(email).strip() == "":
        email = None
    result = test_upstream_connectivity(
        base_url=str(base).strip() if base is not None else None,
        password=str(pwd) if pwd is not None else None,
        email=str(email).strip() if email is not None else None,
    )
    code = 200 if result.get("ok") else 502
    return jsonify(result), code


def _safe_keys_file(name: str) -> Path | None:
    """仅允许读取 keys/ 下的 .txt，防止路径穿越。"""
    raw = (name or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ".." in raw.split("/"):
        return None
    # 允许传 "keys/xxx.txt" 或 "xxx.txt"
    if raw.lower().startswith("keys/"):
        raw = raw[5:]
    if not raw.lower().endswith(".txt"):
        return None
    keys_dir = (BASE_DIR / "keys").resolve()
    path = (keys_dir / Path(raw).name).resolve()
    try:
        path.relative_to(keys_dir)
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path


def _read_sso_file_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _mark_recent_imported(selected: list[dict] | None, result: dict) -> None:
    """按导入结果标记 recent_success 中的项为已导入。"""
    if not (result.get("ok") or (result.get("success") or 0) > 0):
        return
    ok_emails = set()
    ok_sso_hints = set()
    for row in result.get("results") or []:
        if row.get("status") != "ok":
            continue
        if row.get("email"):
            ok_emails.add(str(row["email"]).lower())
        hint = row.get("sso_hint") or ""
        if hint:
            ok_sso_hints.add(hint)
    selected = selected or []
    for it in engine.recent_success:
        if not it.get("sso"):
            continue
        email = (it.get("email") or "").lower()
        sso = it.get("sso") or ""
        sso_hint = (sso[:12] + "...") if len(sso) > 12 else sso
        if email and email in ok_emails:
            it["imported"] = True
            it["auth_token"] = None
        elif sso_hint and sso_hint in ok_sso_hints:
            it["imported"] = True
            it["auth_token"] = None
        elif result.get("ok") and selected and any(
            it is x or it.get("id") == x.get("id") for x in selected
        ):
            it["imported"] = True
            it["auth_token"] = None


def _update_import_job(**kwargs) -> None:
    with _import_job_lock:
        for k, v in kwargs.items():
            if k in _import_job or k in (
                "id",
                "running",
                "status",
                "message",
                "source",
                "total",
                "done",
                "success",
                "fail",
                "current",
                "started_at",
                "finished_at",
                "result",
            ):
                _import_job[k] = v


def _import_progress_cb(**kwargs) -> None:
    patch = {}
    if kwargs.get("done") is not None:
        patch["done"] = int(kwargs["done"] or 0)
    if kwargs.get("total") is not None:
        patch["total"] = int(kwargs["total"] or 0)
    if kwargs.get("success") is not None:
        patch["success"] = int(kwargs["success"] or 0)
    if kwargs.get("fail") is not None:
        patch["fail"] = int(kwargs["fail"] or 0)
    if kwargs.get("current") is not None:
        patch["current"] = str(kwargs.get("current") or "")
    phase = kwargs.get("phase") or ""
    if phase:
        cur = patch.get("current") or _import_job.get("current") or ""
        if phase == "retry_cooldown":
            patch["message"] = cur or "冷却后重试…"
        elif phase == "retry":
            patch["message"] = f"抢救中：{cur}" if cur else "抢救中…"
        else:
            total = patch.get("total", _import_job.get("total") or 0)
            done = patch.get("done", _import_job.get("done") or 0)
            patch["message"] = f"导入中 {done}/{total}" + (f" · {cur}" if cur else "")
    if patch:
        _update_import_job(**patch)


def _prepare_import_payload(body: dict) -> dict:
    """解析请求体，返回 accounts/sso_lines/selected/source 等，失败返回 {error, code}。"""
    merge = body.get("merge", True)
    ids = body.get("ids")
    if ids is not None and not isinstance(ids, list):
        return {"error": "ids 必须是数组", "code": 400}

    raw_ssos = body.get("sso_cookies") or body.get("ssos")
    only_pending = bool(body.get("only_pending", True))
    import_all = bool(body.get("all", False))
    file_name = body.get("file") or body.get("from_file") or body.get("key_file")
    use_output_file = bool(body.get("from_output", False))

    items = list(engine.recent_success)
    selected: list[dict] = []
    source = "recent_success"
    accounts: list[dict] = []
    sso_lines: list[str] = []
    file_label = ""

    if raw_ssos and isinstance(raw_ssos, list) and raw_ssos:
        for line in raw_ssos:
            s = str(line or "").strip()
            if s:
                sso_lines.append(s)
        if not sso_lines:
            return {"error": "sso_cookies 为空", "code": 400}
        return {
            "merge": merge,
            "source": "sso_cookies",
            "sso_lines": sso_lines,
            "accounts": None,
            "selected": None,
            "submitted": len(sso_lines),
            "file": "",
        }

    file_path: Path | None = None
    if file_name:
        file_path = _safe_keys_file(str(file_name))
        if not file_path:
            return {"error": f"无效或不存在的 keys 文件: {file_name}", "code": 400}
        source = f"file:{file_path.name}"
        file_label = file_path.name
    elif use_output_file and engine.output_file:
        candidate = Path(engine.output_file)
        if not candidate.is_absolute():
            candidate = BASE_DIR / candidate
        if candidate.is_file():
            file_path = candidate
            source = f"output:{candidate.name}"
            file_label = candidate.name

    if file_path is not None:
        sso_lines = _read_sso_file_lines(file_path)
        if not sso_lines:
            return {"error": f"文件无有效 SSO: {file_path.name}", "code": 400}
        token_by_sso = {
            (it.get("sso") or "").strip(): it.get("auth_token")
            for it in items
            if it.get("sso") and isinstance(it.get("auth_token"), dict)
        }
        for email, sso in _parse_sso_import_lines(sso_lines):
            sso = (sso or "").strip()
            if not sso:
                continue
            accounts.append(
                {
                    "email": email,
                    "sso": sso,
                    "auth_token": token_by_sso.get(sso),
                }
            )
        return {
            "merge": merge,
            "source": source,
            "sso_lines": None,
            "accounts": accounts,
            "selected": None,
            "submitted": len(accounts),
            "file": file_label,
        }

    if ids:
        id_set = {str(x) for x in ids}
        for it in items:
            if str(it.get("id") or "") in id_set and it.get("sso"):
                selected.append(it)
    elif import_all:
        for it in items:
            if not it.get("sso"):
                continue
            if only_pending and it.get("imported"):
                continue
            selected.append(it)
        if not selected and engine.output_file:
            candidate = Path(engine.output_file)
            if not candidate.is_absolute():
                candidate = BASE_DIR / candidate
            if candidate.is_file():
                sso_lines = _read_sso_file_lines(candidate)
                if sso_lines:
                    return {
                        "merge": merge,
                        "source": f"output:{candidate.name}",
                        "sso_lines": sso_lines,
                        "accounts": None,
                        "selected": None,
                        "submitted": len(sso_lines),
                        "file": candidate.name,
                    }
    else:
        for it in items:
            if it.get("sso") and not it.get("imported"):
                selected.append(it)

    if not selected:
        return {
            "error": "没有可导入的成功账号（请先注册，或选择 keys 文件导入）",
            "code": 400,
        }

    seen = set()
    for it in selected:
        sso = (it.get("sso") or "").strip()
        if not sso or sso in seen:
            continue
        seen.add(sso)
        accounts.append(
            {
                "email": (it.get("email") or "").strip(),
                "sso": sso,
                "auth_token": it.get("auth_token")
                if isinstance(it.get("auth_token"), dict)
                else None,
            }
        )
    return {
        "merge": merge,
        "source": source,
        "sso_lines": None,
        "accounts": accounts,
        "selected": selected,
        "submitted": len(accounts),
        "file": "",
    }


def _run_import_job(job_id: str, payload: dict) -> None:
    def progress_cb(**kwargs):
        # 任务被替换时不再更新
        with _import_job_lock:
            if _import_job.get("id") != job_id:
                return
        _import_progress_cb(**kwargs)

    try:
        result = import_sso_to_upstream(
            sso_lines=payload.get("sso_lines"),
            accounts=payload.get("accounts"),
            merge=bool(payload.get("merge", True)),
            max_workers=1,
            progress_cb=progress_cb,
        )
        _mark_recent_imported(payload.get("selected"), result)
        result["submitted"] = payload.get("submitted") or result.get("total") or 0
        result["source"] = payload.get("source") or ""
        if payload.get("file"):
            result["file"] = payload["file"]

        ok = bool(result.get("ok") or (result.get("success") or 0) > 0)
        level = "success" if result.get("ok") else (
            "warn" if (result.get("success") or 0) > 0 else "error"
        )
        logs.emit(
            f"sub2api 导入: {result.get('message')}（提交 {result.get('submitted')} 条 · {result.get('source')}）",
            level,
        )
        with _import_job_lock:
            if _import_job.get("id") != job_id:
                return
            _import_job.update(
                {
                    "running": False,
                    "status": "done" if ok else "error",
                    "message": result.get("message") or "导入结束",
                    "done": int(result.get("total") or _import_job.get("done") or 0),
                    "total": int(result.get("total") or _import_job.get("total") or 0),
                    "success": int(result.get("success") or 0),
                    "fail": int(result.get("fail") or 0),
                    "current": "",
                    "finished_at": time.time(),
                    "result": result,
                }
            )
    except Exception as e:
        logs.emit(f"sub2api 导入任务异常: {e}", "error")
        with _import_job_lock:
            if _import_job.get("id") != job_id:
                return
            _import_job.update(
                {
                    "running": False,
                    "status": "error",
                    "message": f"导入异常: {e}",
                    "finished_at": time.time(),
                    "result": {"ok": False, "message": str(e), "success": 0, "fail": 0},
                }
            )


@app.post("/api/upstream/import")
def upstream_import():
    """将最近成功的 SSO / keys 文件导入 sub2api grok 分组（前台同步，等全部完成再返回）。"""
    body = request.get_json(silent=True) or {}
    # 默认前台同步；仅显式 async=true 时走后台（一般不用）
    async_mode = body.get("async", False)
    if isinstance(async_mode, str):
        async_mode = async_mode.strip().lower() in ("1", "true", "yes")

    with _import_job_lock:
        already_running = bool(_import_job.get("running"))
    if already_running:
        return jsonify(
            {
                "ok": False,
                "message": "已有导入任务在进行中",
                "job": get_import_job_public(),
            }
        ), 409

    prepared = _prepare_import_payload(body)
    if prepared.get("error"):
        return jsonify({"ok": False, "message": prepared["error"]}), int(
            prepared.get("code") or 400
        )

    submitted = int(prepared.get("submitted") or 0)
    source = prepared.get("source") or ""

    # 前台同步路径（默认）
    if not async_mode:
        result = import_sso_to_upstream(
            sso_lines=prepared.get("sso_lines"),
            accounts=prepared.get("accounts"),
            merge=bool(prepared.get("merge", True)),
            max_workers=1,
        )
        _mark_recent_imported(prepared.get("selected"), result)
        result["submitted"] = submitted
        result["source"] = source
        if prepared.get("file"):
            result["file"] = prepared["file"]
        level = "success" if result.get("ok") else (
            "warn" if (result.get("success") or 0) > 0 else "error"
        )
        logs.emit(
            f"sub2api 导入: {result.get('message')}（提交 {submitted} 条 · {source}）",
            level,
        )
        code = 200 if result.get("ok") or (result.get("success") or 0) > 0 else 502
        result["recent_success"] = engine.get_status().get("recent_success", [])
        return jsonify(result), code

    # 可选后台路径（仅 async=true）
    job_id = uuid.uuid4().hex[:12]
    _update_import_job(
        id=job_id,
        running=True,
        status="running",
        message=f"已开始导入 {submitted} 条（{source}）",
        source=source,
        total=submitted,
        done=0,
        success=0,
        fail=0,
        current="",
        started_at=time.time(),
        finished_at=None,
        result=None,
    )
    logs.emit(f"sub2api 导入任务已启动：{submitted} 条 · {source}（后台运行）", "info")
    t = threading.Thread(
        target=_run_import_job,
        args=(job_id, prepared),
        daemon=True,
        name=f"ImportJob-{job_id}",
    )
    t.start()
    return jsonify(
        {
            "ok": True,
            "async": True,
            "message": f"导入已在后台开始（{submitted} 条）",
            "job_id": job_id,
            "submitted": submitted,
            "source": source,
            "job": get_import_job_public(),
        }
    )


@app.get("/keys/<path:filename>")
def download_key(filename):
    return send_from_directory(BASE_DIR / "keys", filename, as_attachment=True)


def main():
    cfg = read_env_file()
    apply_env_to_process(cfg)
    host = cfg.get("UI_HOST") or os.getenv("UI_HOST", "127.0.0.1")
    port = int(cfg.get("UI_PORT") or os.getenv("UI_PORT", "3333"))
    print("=" * 60)
    print("Grok 注册机 Web 控制台")
    print(f"打开浏览器: http://{host}:{port}")
    print("=" * 60)
    logs.emit("Web 控制台已启动", "success")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
