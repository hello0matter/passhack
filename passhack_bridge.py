"""STTool bridge for PassHack's approved login-surface workflow.

This adapter deliberately consumes only candidates that STTool has already
approved.  It is a small headless runner so the GUI is not automated by the
coordinator and no console window is required on Windows.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from passhack import AuditRecord, BRUTE_FORCE_SUCCESS_PREFIX, BruteForceHandler


class DefenseDetected(RuntimeError):
    pass


class ControlledBruteForceHandler(BruteForceHandler):
    def __init__(self, *args, requests_per_minute: int, stop_on_defense: bool, **kwargs):
        super().__init__(*args, **kwargs)
        self.minimum_interval = 60.0 / max(requests_per_minute, 1)
        self.stop_on_defense = stop_on_defense
        self.last_request_at = 0.0

    def submit_login(self, action_url, method, payload, referer):
        delay = self.minimum_interval - (time.monotonic() - self.last_request_at)
        if delay > 0:
            time.sleep(delay)
        response = super().submit_login(action_url, method, payload, referer)
        self.last_request_at = time.monotonic()
        if self.stop_on_defense:
            body = (response.text or "").casefold()
            if response.status_code == 429 or any(
                marker in body
                for marker in ("captcha", "验证码", "账号锁定", "账户锁定", "account locked")
            ):
                raise DefenseDetected("检测到验证码、HTTP 429 或账号锁定提示")
        return response



def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


DEFAULT_STTOOL_POLICY = {
    "brute_enabled": True,
    "username_wordlist_path": "",
    "wordlist_path": "",
    "max_attempts": 10,
    "requests_per_minute": 10,
    "concurrency": 1,
    "stop_on_defense": True,
}


def passhack_defaults_path() -> Path:
    override = os.environ.get("PASSHACK_STTOOL_DEFAULTS_PATH", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "output" / "state" / "sttool_defaults.json"


def effective_policy(candidate_path: Path) -> tuple[dict, dict]:
    candidate_value = read_json(candidate_path)
    project_policy = candidate_value.get("policy")
    if not isinstance(project_policy, dict):
        project_policy = {}
    gui_path = passhack_defaults_path()
    gui_exists = gui_path.is_file()
    gui_policy = read_json(gui_path) if gui_exists else {}
    policy = dict(DEFAULT_STTOOL_POLICY)
    if gui_policy:
        policy.update(
            {key: gui_policy[key] for key in DEFAULT_STTOOL_POLICY if key in gui_policy}
        )
    project_override = bool(project_policy.get("project_override", False))
    if project_override:
        policy.update(
            {key: project_policy[key] for key in DEFAULT_STTOOL_POLICY if key in project_policy}
        )
        policy["brute_enabled"] = True
        source = "STTool ????"
    else:
        source = "PassHack GUI ????" if gui_exists else "PassHack ???????"
    summary = {
        "source": source,
        "defaults_path": str(gui_path),
        "project_override": project_override,
        "brute_enabled": bool(policy.get("brute_enabled", True)),
        "username_wordlist_path": str(policy.get("username_wordlist_path") or ""),
        "wordlist_path": str(policy.get("wordlist_path") or ""),
        "max_attempts": max(1, int(policy.get("max_attempts") or 10)),
        "requests_per_minute": max(1, int(policy.get("requests_per_minute") or 10)),
        "concurrency": max(1, int(policy.get("concurrency") or 1)),
        "stop_on_defense": bool(policy.get("stop_on_defense", True)),
    }
    return policy, summary


def atomic_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def append_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_text()}] {message}\n")


def host_allowed(url: str, scope: str, target: str) -> bool:
    """Apply the project scope again at the tool boundary."""
    host = (urlsplit(url).hostname or "").strip(".").lower()
    if not host:
        return False
    rules = [
        token.strip()
        for token in scope.replace(",", "\n").replace(";", "\n").splitlines()
        if token.strip()
    ]
    if not rules:
        target_host = (urlsplit(target).hostname or target).strip(".").lower()
        rules = [target_host] if target_host else []
    if "*" in rules:
        return True
    try:
        host_ip = ipaddress.ip_address(host)
    except ValueError:
        host_ip = None
    for rule in rules:
        try:
            network = ipaddress.ip_network(rule, strict=False)
        except ValueError:
            network = None
        if network is not None:
            if host_ip is not None and host_ip in network:
                return True
            continue
        rule_host = (urlsplit(rule).hostname or rule.split("/")[0]).strip(".").lower()
        rule_host = rule_host.removeprefix("*.")
        if host == rule_host or host.endswith(f".{rule_host}"):
            return True
    return False


def likely_login_url(url: str) -> bool:
    parsed = urlsplit(url.strip())
    path = parsed.path or "/"
    suffix = Path(path).suffix.lower()
    if suffix in {
        ".js", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif",
        ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".pdf",
        ".zip", ".gz", ".7z", ".rar", ".mp3", ".mp4", ".webm",
        ".log", ".txt", ".xml", ".json", ".yaml", ".yml", ".sql", ".bak",
    }:
        return False
    segments = [item.casefold() for item in path.split("/") if item]
    blocked_suffixes = {
        ".js", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif",
        ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".pdf",
        ".zip", ".gz", ".7z", ".rar", ".mp3", ".mp4", ".webm",
        ".log", ".txt", ".xml", ".json", ".yaml", ".yml", ".sql", ".bak",
    }
    if any(Path(segment).suffix.lower() in blocked_suffixes for segment in segments[:-1]):
        return False
    route_names = {Path(segment).stem.strip("-_.") for segment in segments}
    haystack = path.casefold()
    if any(
        marker in haystack
        for marker in ("login", "signin", "sign-in", "logon", "后台", "登录")
    ):
        return True
    if route_names & {"auth", "authenticate", "oauth", "sso"}:
        return True
    last_segment = Path(segments[-1]) if segments else None
    return bool(
        last_segment
        and not last_segment.suffix
        and last_segment.name in {"admin", "manager", "console"}
    )


def requeue_scope_skips(
    candidate_path: Path,
    export_path: Path,
    scope: str,
    target: str,
) -> int:
    exported = read_json(export_path).get("results")
    skipped_ids = {
        str(item.get("id") or "")
        for item in exported or []
        if isinstance(item, dict) and item.get("status") == "skipped_scope"
    }
    if not skipped_ids:
        return 0
    value = read_json(candidate_path)
    rows = value.get("candidates")
    if not isinstance(rows, list):
        return 0
    changed = 0
    requeued_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or str(row.get("id") or "") not in skipped_ids:
            continue
        if row.get("decision_source") == "candidate_filter_tightened":
            continue
        url = str(row.get("url") or "")
        if not likely_login_url(url):
            continue
        if not host_allowed(url, scope, target):
            continue
        action = str(row.get("default_action") or "agent_default_dictionary")
        if action == "save_only":
            action = "agent_default_dictionary"
        row.update(
            status="approved_agent",
            action=action,
            decision_source="scope_rule_repaired",
            result="范围规则已修复，重新进入 PassHack 队列",
            updated_at=now_text(),
        )
        changed += 1
        requeued_ids.add(str(row.get("id") or ""))
    if changed:
        value["updated_at"] = now_text()
        atomic_write(candidate_path, value)
        cleaned_results = [
            item
            for item in exported or []
            if not (
                isinstance(item, dict)
                and str(item.get("id") or "") in requeued_ids
                and item.get("status") == "skipped_scope"
            )
        ]
        atomic_write(
            export_path,
            {
                "schema_version": 1,
                "tool": "passhack",
                "updated_at": now_text(),
                "results": cleaned_results,
            },
        )
    return changed


def socks5_available() -> bool:
    try:
        __import__("socks")
    except ImportError:
        return False
    return True


def configure_session(session: requests.Session) -> None:
    proxy_url = os.environ.get("STTOOL_TOOL_PROXY_URL", "").strip()
    if proxy_url.startswith(("socks5://", "socks5h://")) and not socks5_available():
        proxy_url = os.environ.get(
            "STTOOL_TOOL_HTTP_FALLBACK_PROXY_URL", ""
        ).strip()
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    header_name = os.environ.get("STTOOL_HTTP_HEADER_NAME", "").strip()
    header_value = os.environ.get("STTOOL_HTTP_HEADER_VALUE", "").strip()
    if (
        header_name
        and header_value
        and "\r" not in header_name + header_value
        and "\n" not in header_name + header_value
    ):
        session.headers[header_name] = header_value


def inspect_login(url: str, timeout: int) -> tuple[AuditRecord, requests.Session, BeautifulSoup]:
    session = requests.Session()
    configure_session(session)
    response = session.get(url, timeout=timeout, allow_redirects=True, verify=False)
    soup = BeautifulSoup(response.text, "html.parser")
    forms = soup.find_all("form")
    password_fields = soup.find_all("input", attrs={"type": lambda value: str(value).lower() == "password"})
    form = forms[0] if forms else None
    action = urljoin(response.url, str(form.get("action") or response.url)) if form else response.url
    method = str(form.get("method") or "POST").upper() if form else "POST"
    field_names = []
    if form:
        field_names = [str(item.get("name") or item.get("id") or "") for item in form.find_all("input")]
    body_text = soup.get_text(" ", strip=True).casefold()
    record = AuditRecord(
        record_id=0,
        target=url,
        final_url=response.url,
        login_form=bool(password_fields),
        password_field_count=len(password_fields),
        captcha_present=any(marker in body_text for marker in ("captcha", "验证码", "校验码")),
        slider_captcha_present=any(marker in body_text for marker in ("滑块", "slider", "slide to")),
        field_summary="password:" + ",".join(field_names),
        form_action=action,
        form_method=method,
        result="",
        risk_level="低",
    )
    return record, session, soup


def _read_wordlist(path: str) -> list[str]:
    file_path = Path(path).expanduser()
    if not path or not file_path.is_file():
        return []
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            lines = file_path.read_text(encoding=encoding).splitlines()
            break
        except UnicodeError:
            continue
    else:
        return []
    return list(
        dict.fromkeys(
            line.strip()
            for line in lines
            if line.strip() and not line.lstrip().startswith(("#", ";", "//"))
        )
    )


def verify_approved_login(
    record: AuditRecord,
    session: requests.Session,
    soup: BeautifulSoup,
    candidate: dict,
    policy: dict,
) -> str:
    if record.captcha_present or record.slider_captcha_present:
        return "已停止：登录页存在验证码或滑块"
    max_attempts = max(1, int(policy.get("max_attempts") or 10))
    handler = ControlledBruteForceHandler(
        session,
        None,
        requests_per_minute=max(1, int(policy.get("requests_per_minute") or 10)),
        stop_on_defense=bool(policy.get("stop_on_defense", True)),
    )
    usernames = [
        str(item).strip()
        for item in candidate.get("username_candidates") or []
        if str(item).strip()
    ]
    usernames.extend(_read_wordlist(str(policy.get("username_wordlist_path") or "")))
    usernames = list(dict.fromkeys(usernames))
    if usernames:
        handler.default_user = usernames[:max_attempts]
    else:
        handler.default_user = handler.default_user[:1]
    custom_passwords = _read_wordlist(
        str(candidate.get("wordlist_path") or policy.get("wordlist_path") or "")
    )
    if custom_passwords:
        handler.default_pass = custom_passwords
    elif str(candidate.get("action") or "") == "agent_social_dictionary":
        host_label = (urlsplit(record.target).hostname or "").split(".")[0]
        generated = [
            host_label,
            f"{host_label}123",
            f"{host_label}@123",
            f"{host_label}{datetime.now().year}",
        ]
        handler.default_pass = list(dict.fromkeys(generated + handler.default_pass))
    password_limit = max(1, max_attempts // max(len(handler.default_user), 1))
    handler.default_pass = handler.default_pass[:password_limit]
    try:
        return handler.run(record, soup=soup)
    except DefenseDetected as exc:
        return f"已停止：{exc}"


def update_candidate(candidate_path: Path, identity: str, **updates: object) -> None:
    value = read_json(candidate_path)
    rows = value.get("candidates")
    if not isinstance(rows, list):
        return
    for row in rows:
        if isinstance(row, dict) and str(row.get("id") or "") == identity:
            row.update(updates, updated_at=now_text())
            break
    value["updated_at"] = now_text()
    atomic_write(candidate_path, value)


def process_candidate(
    candidate: dict,
    args: argparse.Namespace,
    candidate_path: Path,
    policy: dict | None = None,
) -> dict:
    if policy is None:
        policy, _summary = effective_policy(candidate_path)
    identity = str(candidate.get("id") or "")
    url = str(candidate.get("url") or "")
    if not host_allowed(url, args.scope, args.target):
        update_candidate(candidate_path, identity, status="saved", result="超出当前工程自动处理范围，已跳过")
        return {"id": identity, "url": url, "status": "skipped_scope"}
    update_candidate(candidate_path, identity, status="passhack_running", tool="passhack")
    try:
        record, session, soup = inspect_login(url, args.timeout)
        if not record.login_form:
            result = {"id": identity, "url": url, "status": "completed", "login_form": False, "result": "未发现可用密码字段"}
            update_candidate(candidate_path, identity, status="completed", tool="passhack", result=result["result"], completed_at=now_text())
            return result
        action = str(candidate.get("action") or "save_only")
        if not bool(policy.get("brute_enabled", True)):
            result_text = "PassHack GUI ????????????????????????"
        else:
            result_text = verify_approved_login(record, session, soup, candidate, policy)
        result_status = (
            "stopped_defense"
            if result_text.startswith("已停止")
            else "weak_password_found"
            if result_text.startswith(BRUTE_FORCE_SUCCESS_PREFIX)
            else "completed"
        )
        result = {
            "id": identity,
            "url": url,
            "status": result_status,
            "login_form": True,
            "action": action,
            "result": result_text,
            "completed_at": now_text(),
        }
        update_candidate(candidate_path, identity, status=result_status, tool="passhack", result=result_text, completed_at=now_text())
        return result
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        update_candidate(candidate_path, identity, status="approved_agent", tool="passhack", result=f"执行失败：{detail}")
        return {"id": identity, "url": url, "status": "error", "error": detail}


def run(args: argparse.Namespace) -> int:
    candidate_path = Path(args.candidates)
    state_path = Path(args.state)
    export_path = Path(args.export)
    log_path = state_path.with_name("passhack.log")
    processed: set[str] = set()
    requeued = requeue_scope_skips(candidate_path, export_path, args.scope, args.target)
    state = {
        "status": "running",
        "stage": "starting",
        "detail": "PassHack 后台审计已启动，正在读取已批准登录入口",
        "tool": "passhack",
        "updated_at": now_text(),
        "processed": 0,
        "requeued_scope_skips": requeued,
        "counts": {},
    }
    atomic_write(state_path, state)
    append_log(log_path, f"后台审计启动；修复范围后重新排队 {requeued} 条。")
    try:
        while True:
            value = read_json(candidate_path)
            rows = value.get("candidates")
            approved = [row for row in rows or [] if isinstance(row, dict) and row.get("status") == "approved_agent"]
            results = read_json(export_path).get("results")
            if not isinstance(results, list):
                results = []
            for candidate in approved:
                identity = str(candidate.get("id") or "")
                if not identity or identity in processed:
                    continue
                state.update(
                    stage="processing",
                    detail=f"正在检查登录入口：{candidate.get('url') or '-'}",
                    current_target=str(candidate.get("url") or ""),
                    approved_waiting=len(approved),
                    updated_at=now_text(),
                )
                policy, config_summary = effective_policy(candidate_path)
                state["effective_config"] = config_summary
                atomic_write(state_path, state)
                result = process_candidate(candidate, args, candidate_path, policy)
                processed.add(identity)
                results.append(result)
                append_log(
                    log_path,
                    f"{result.get('status')}: {result.get('url') or '-'}"
                    + (f"；{result.get('result')}" if result.get("result") else ""),
                )
                atomic_write(export_path, {"schema_version": 1, "tool": "passhack", "updated_at": now_text(), "results": results[-5000:]})
                progress_counts = {
                    status: sum(1 for item in results if item.get("status") == status)
                    for status in {
                        str(item.get("status") or "unknown") for item in results
                    }
                }
                state.update(
                    processed=len(processed),
                    result_total=len(results),
                    approved_waiting=max(len(approved) - len(processed), 0),
                    counts=progress_counts,
                    last_result=result,
                    updated_at=now_text(),
                )
                atomic_write(state_path, state)
            counts = {
                status: sum(1 for item in results if item.get("status") == status)
                for status in {str(item.get("status") or "unknown") for item in results}
            }
            state.update(
                status="running",
                stage="waiting_candidates",
                detail=(
                    f"本次进程已处理 {len(processed)} 条；累计结果 {len(results)} 条；"
                    f"有效检查 {counts.get('completed', 0)} 条；"
                    f"范围跳过 {counts.get('skipped_scope', 0)} 条；"
                    f"失败 {counts.get('error', 0)} 条；持续等待新入口"
                ),
                processed=len(processed),
                result_total=len(results),
                approved_waiting=len(approved),
                current_target="",
                counts=counts,
                updated_at=now_text(),
                last_result=results[-1] if results else None,
            )
            atomic_write(state_path, state)
            if args.once:
                break
            time.sleep(max(args.poll, 1))
    except KeyboardInterrupt:
        state.update(status="stopped", updated_at=now_text())
        atomic_write(state_path, state)
        return 0
    state.update(status="completed" if args.once else "running", updated_at=now_text())
    atomic_write(state_path, state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="STTool PassHack bridge")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--scope", default="*")
    parser.add_argument("--target", required=True)
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--export", required=True)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--poll", type=float, default=2)
    parser.add_argument("--once", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
