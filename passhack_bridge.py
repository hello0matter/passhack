"""STTool bridge for PassHack's approved login-surface workflow.

This adapter deliberately consumes only candidates that STTool has already
approved.  It is a small headless runner so the GUI is not automated by the
coordinator and no console window is required on Windows.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))



def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def host_allowed(url: str, scope: str, target: str) -> bool:
    """Apply the project scope again at the tool boundary."""
    host = (urlsplit(url).hostname or "").strip(".").lower()
    target_host = (urlsplit(target).hostname or target).strip(".").lower()
    if scope.strip() == "*":
        return not target_host or host == target_host or host.endswith(f".{target_host}")
    for token in scope.replace(",", "\n").replace(";", "\n").splitlines():
        rule = token.strip()
        if not rule or rule == "*":
            continue
        rule_host = (urlsplit(rule).hostname or rule.split("/")[0]).strip(".").lower()
        if host == rule_host or host.endswith(f".{rule_host}"):
            return True
    return False


def inspect_login(url: str, timeout: int) -> tuple[SimpleNamespace, requests.Session, BeautifulSoup]:
    session = requests.Session()
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
    record = SimpleNamespace(
        target=url,
        final_url=response.url,
        login_form=bool(password_fields),
        password_field_count=len(password_fields),
        slider_captcha_present=False,
        field_summary="password:" + ",".join(field_names),
        form_action=action,
        form_method=method,
        result="",
        risk_level="低",
    )
    return record, session, soup


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


def process_candidate(candidate: dict, args: argparse.Namespace, candidate_path: Path) -> dict:
    identity = str(candidate.get("id") or "")
    url = str(candidate.get("url") or "")
    if not host_allowed(url, args.scope, args.target):
        update_candidate(candidate_path, identity, status="saved", action="save_only", result="超出当前工程授权范围，已跳过")
        return {"id": identity, "url": url, "status": "skipped_scope"}
    update_candidate(candidate_path, identity, status="passhack_running", tool="passhack")
    try:
        record, session, soup = inspect_login(url, args.timeout)
        if not record.login_form:
            result = {"id": identity, "url": url, "status": "completed", "login_form": False, "result": "未发现可用密码字段"}
            update_candidate(candidate_path, identity, status="completed", tool="passhack", result=result["result"], completed_at=now_text())
            return result
        action = str(candidate.get("action") or "save_only")
        result_text = (
            "已识别登录表单；PassHack 已接入工程并保存证据，"
            "实际口令验证仍需在界面中单独人工启动"
        )
        result = {
            "id": identity,
            "url": url,
            "status": "completed",
            "login_form": True,
            "action": action,
            "result": result_text,
            "completed_at": now_text(),
        }
        update_candidate(candidate_path, identity, status="completed", tool="passhack", result=result_text, completed_at=now_text())
        return result
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        update_candidate(candidate_path, identity, status="approved_agent", tool="passhack", result=f"执行失败：{detail}")
        return {"id": identity, "url": url, "status": "error", "error": detail}


def run(args: argparse.Namespace) -> int:
    candidate_path = Path(args.candidates)
    state_path = Path(args.state)
    export_path = Path(args.export)
    processed: set[str] = set()
    state = {"status": "running", "tool": "passhack", "updated_at": now_text(), "processed": 0}
    atomic_write(state_path, state)
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
                result = process_candidate(candidate, args, candidate_path)
                processed.add(identity)
                results.append(result)
                atomic_write(export_path, {"schema_version": 1, "tool": "passhack", "updated_at": now_text(), "results": results[-5000:]})
            state.update(status="running", processed=len(processed), updated_at=now_text(), last_result=results[-1] if results else None)
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
