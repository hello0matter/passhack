import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
import queue

import requests
from bs4 import BeautifulSoup

from passhack import (
    BRUTE_FORCE_SUCCESS_PREFIX,
    CUSTOM_BRUTE_DICT_MODE,
    AuditRecord,
    BruteForceHandler,
    DEFAULT_OCR_ENDPOINT,
    DEFAULT_PRECHECK_COUNT,
    describe_request_exception,
    is_retryable_failure_result,
    PROXY_MODE_POOL,
    PROXY_STRATEGY_DIRECT_FIRST,
    PROXY_STRATEGY_PROXY_ONLY,
    PROXY_STRATEGY_DIRECT_ONLY,
    PROXY_SCHEME_SOCKS5,
    SecurityAuditGUI,
)


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None, url="http://example.com", content=None, encoding="utf-8", apparent_encoding="utf-8"):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url
        self.content = content if content is not None else text.encode(encoding or "utf-8", errors="replace")
        self.encoding = encoding
        self._apparent_encoding = apparent_encoding

    @property
    def apparent_encoding(self):
        return self._apparent_encoding


class FakeSession:
    def __init__(self):
        self.requests = []

    def post(self, url, data=None, **kwargs):
        payload = dict(data or {})
        self.requests.append(("POST", url, payload, kwargs))
        if payload.get("username") == "admin" and payload.get("password") == "123456":
            return FakeResponse(status_code=302, headers={"Location": "/dashboard"})
        return FakeResponse(text="用户名或密码错误")

    def get(self, url, params=None, **kwargs):
        payload = dict(params or {})
        self.requests.append(("GET", url, payload, kwargs))
        return FakeResponse(text="用户名或密码错误")


class FakeDriver:
    def __init__(self, current_url="http://example.com/#/login", page_source="<html></html>"):
        self.current_url = current_url
        self.page_source = page_source
        self.loaded_url = ""
        self.closed = False

    def get(self, url):
        self.loaded_url = url

    def quit(self):
        self.closed = True


class DummyVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class DummyEntry:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class BruteForceHandlerTest(unittest.TestCase):
    def test_load_dicts_uses_custom_files_and_deduplicates(self):
        session = FakeSession()
        handler = BruteForceHandler(session, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            user_file = Path(tmpdir) / "users.txt"
            pass_file = Path(tmpdir) / "passwords.txt"
            user_file.write_text("admin\n#comment\nroot\nadmin\n", encoding="utf-8")
            pass_file.write_text("123456\n;skip\nadmin123\n123456\n", encoding="utf-8")

            users, passwords, label = handler.load_dicts(
                CUSTOM_BRUTE_DICT_MODE,
                str(user_file),
                str(pass_file),
            )

        self.assertEqual(users, ["admin", "root"])
        self.assertEqual(passwords, ["123456", "admin123"])
        self.assertEqual(label, "自定义字典")

    def test_run_hits_default_credential_with_hidden_fields(self):
        session = FakeSession()
        handler = BruteForceHandler(session, None)
        soup = BeautifulSoup(
            """
            <form action="/doLogin" method="post">
              <input type="hidden" name="csrf_token" value="token-1">
              <input type="text" name="username" placeholder="用户名">
              <input type="password" name="password" placeholder="密码">
              <button type="submit" name="submitBtn" value="login">登录</button>
            </form>
            """,
            "html.parser",
        )
        record = AuditRecord(
            record_id=1,
            target="http://example.com",
            final_url="http://example.com/login",
            login_form=True,
            form_action="/doLogin",
            form_method="POST",
            field_summary="text:username | password:password | hidden:csrf_token",
        )

        result = handler.run(record, soup=soup)

        self.assertTrue(result.startswith(BRUTE_FORCE_SUCCESS_PREFIX))
        self.assertGreaterEqual(len(session.requests), 1)
        method, url, payload, kwargs = session.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://example.com/doLogin")
        self.assertEqual(payload["csrf_token"], "token-1")
        self.assertEqual(payload["submitBtn"], "login")
        self.assertIn("Referer", kwargs["headers"])

    def test_run_supports_login_page_without_form_tag(self):
        session = FakeSession()
        handler = BruteForceHandler(session, None)
        soup = BeautifulSoup(
            """
            <div class="login-panel">
              <input type="text" name="username" placeholder="请输入手机号/账号">
              <input type="password" name="password" placeholder="请输入密码">
              <button type="button" name="submitBtn" value="login">登录</button>
            </div>
            """,
            "html.parser",
        )
        record = AuditRecord(
            record_id=3,
            target="http://example.com",
            final_url="http://example.com/#/login",
            login_form=True,
            form_method="POST",
            field_summary="text:username | password:password",
        )

        result = handler.run(record, soup=soup)

        self.assertTrue(result.startswith(BRUTE_FORCE_SUCCESS_PREFIX))

    def test_run_skips_non_actionable_login_record(self):
        session = FakeSession()
        handler = BruteForceHandler(session, None)
        record = AuditRecord(
            record_id=4,
            target="http://example.com",
            final_url="http://example.com/login",
            login_form=True,
            form_method="POST",
        )

        result = handler.run(record, soup=BeautifulSoup("<html><title>登录</title></html>", "html.parser"))

        self.assertEqual(result, "跳过(未识别到可用登录框)")

    def test_run_uses_browser_login_for_cdp_hit_when_driver_available(self):
        session = FakeSession()
        handler = BruteForceHandler(session, None, driver_factory=lambda: object())
        handler.run_browser_login = lambda *_args: "未命中弱口令(已尝试 1 组, 默认中文常用账号/密码, 浏览器模式)"
        record = AuditRecord(
            record_id=5,
            target="http://example.com",
            final_url="http://example.com/#/login",
            login_form=True,
            password_field_count=1,
            result="疑似登录页 | 浏览器渲染补扫命中(CDP)",
            field_summary="text:browser_user | password:browser_password",
        )

        result = handler.run(record, soup=BeautifulSoup("<html></html>", "html.parser"))

        self.assertIn("浏览器模式", result)

    def test_describe_request_exception_handles_connect_timeout(self):
        summary, detail = describe_request_exception(requests.exceptions.ConnectTimeout("timed out"))
        self.assertEqual(summary, "连接超时，目标可能已下线或端口不通")
        self.assertIn("ConnectTimeout", detail)

    def test_describe_request_exception_handles_dns_failure(self):
        exc = requests.exceptions.ConnectionError("getaddrinfo failed for dead.example")
        summary, detail = describe_request_exception(exc)
        self.assertEqual(summary, "DNS解析失败，域名可能已失效")
        self.assertIn("ConnectionError", detail)

    def test_is_retryable_failure_result_accepts_timeout(self):
        self.assertTrue(is_retryable_failure_result("失败", "连接超时，目标可能已下线或端口不通"))

    def test_is_retryable_failure_result_rejects_non_retryable(self):
        self.assertFalse(is_retryable_failure_result("失败", "SSL握手失败，HTTPS 配置异常"))
        self.assertFalse(is_retryable_failure_result("低", "连接超时，目标可能已下线或端口不通"))

    def test_parse_ocr_response_supports_plain_text(self):
        handler = BruteForceHandler(FakeSession(), None)
        self.assertEqual(handler.parse_ocr_response("a7c9\n"), "a7c9")

    def test_parse_ocr_response_supports_json(self):
        handler = BruteForceHandler(FakeSession(), None)
        self.assertEqual(handler.parse_ocr_response('{"code":1,"msg":"ok","return":"7536"}'), "7536")

    def test_resolve_ocr_endpoint_for_record_uses_route_file(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        with tempfile.TemporaryDirectory() as tmpdir:
            route_file = Path(tmpdir) / "ocr_routes.txt"
            route_file.write_text(
                "host:example.com => http://127.0.0.1:8888/reg00\n"
                "title:登录页 => http://127.0.0.1:8888/reg01\n",
                encoding="utf-8",
            )
            gui.ocr_route_entry = DummyEntry(str(route_file))
            gui.ocr_endpoint_entry = DummyEntry(DEFAULT_OCR_ENDPOINT)
            record = AuditRecord(record_id=1, target="http://example.com/login", title="测试登录页")

            endpoint = gui.resolve_ocr_endpoint_for_record(record)

        self.assertEqual(endpoint, "http://127.0.0.1:8888/reg00")

    def test_normalize_proxy_address_supports_socks_scheme_selection(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.proxy_scheme_var = DummyVar(PROXY_SCHEME_SOCKS5)

        self.assertEqual(gui.normalize_proxy_address("127.0.0.1:7891"), "socks5://127.0.0.1:7891")
        self.assertEqual(gui.get_browser_proxy_address("socks5h://127.0.0.1:7891"), "socks5://127.0.0.1:7891")

    def test_get_proxy_candidates_falls_back_to_direct(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.use_proxy_var = DummyVar(True)
        gui.proxy_entry = DummyEntry("127.0.0.1:7890")
        gui.proxy_scheme_var = DummyVar(PROXY_SCHEME_SOCKS5)
        gui.proxy_strategy_var = DummyVar(PROXY_STRATEGY_DIRECT_FIRST)

        record = AuditRecord(record_id=1, target="http://example.com")
        self.assertEqual(gui.get_proxy_candidates_for_record(record), [None, "socks5://127.0.0.1:7890"])

    def test_get_proxy_candidates_supports_direct_only(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.use_proxy_var = DummyVar(True)
        gui.proxy_entry = DummyEntry("127.0.0.1:7890")
        gui.proxy_scheme_var = DummyVar(PROXY_SCHEME_SOCKS5)
        gui.proxy_strategy_var = DummyVar(PROXY_STRATEGY_DIRECT_ONLY)

        record = AuditRecord(record_id=1, target="http://example.com")
        self.assertEqual(gui.get_proxy_candidates_for_record(record), [None])

    def test_get_proxy_candidates_supports_proxy_only(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.use_proxy_var = DummyVar(True)
        gui.proxy_entry = DummyEntry("127.0.0.1:7890")
        gui.proxy_scheme_var = DummyVar(PROXY_SCHEME_SOCKS5)
        gui.proxy_strategy_var = DummyVar(PROXY_STRATEGY_PROXY_ONLY)

        record = AuditRecord(record_id=1, target="http://example.com")
        self.assertEqual(gui.get_proxy_candidates_for_record(record), ["socks5://127.0.0.1:7890"])

    def test_queue_bruteforce_enqueues_task(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.brute_var = DummyVar(True)
        gui.brute_task_queue = queue.deque()
        gui.brute_future_map = {}
        gui.brute_executor = None
        gui.is_actionable_login_record = lambda record: True
        gui.ensure_brute_executor = lambda: setattr(gui, "executor_ready", True)
        gui.schedule_brute_dispatch = lambda: setattr(gui, "dispatch_scheduled", True)

        record = AuditRecord(record_id=1, target="http://example.com", login_form=True)
        task = gui.queue_bruteforce_for_record(record, FakeSession(), BeautifulSoup("<form></form>", "html.parser"))

        self.assertIsNotNone(task)
        self.assertTrue(gui.brute_task_queue)
        self.assertTrue(getattr(gui, "executor_ready", False))
        self.assertTrue(getattr(gui, "dispatch_scheduled", False))


    def test_get_probe_targets_repeats_precheck_count(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.use_proxy_var = DummyVar(True)
        gui.proxy_entry = DummyEntry("127.0.0.1:7890")
        gui.proxy_scheme_var = DummyVar(PROXY_SCHEME_SOCKS5)
        gui.proxy_strategy_var = DummyVar(PROXY_STRATEGY_DIRECT_FIRST)
        gui.proxy_precheck_var = DummyVar(DEFAULT_PRECHECK_COUNT)

        record = AuditRecord(record_id=1, target="http://example.com")
        gui.probe_connection_candidate = lambda target, proxy: (proxy is None, 0.1 if proxy is None else 0.2)
        self.assertEqual(gui.order_connection_candidates(record.target, gui.get_proxy_candidates_for_record(record)), [None, "socks5://127.0.0.1:7890"])

    def test_order_connection_candidates_uses_proxy_when_direct_fails(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.use_proxy_var = DummyVar(True)
        gui.proxy_entry = DummyEntry("127.0.0.1:7890")
        gui.proxy_scheme_var = DummyVar(PROXY_SCHEME_SOCKS5)
        gui.proxy_strategy_var = DummyVar(PROXY_STRATEGY_DIRECT_FIRST)
        gui.proxy_precheck_var = DummyVar(DEFAULT_PRECHECK_COUNT)

        record = AuditRecord(record_id=1, target="http://example.com")
        gui.probe_connection_candidate = lambda target, proxy: ((proxy is not None), 0.2 if proxy is not None else 9.9)
        self.assertEqual(gui.order_connection_candidates(record.target, gui.get_proxy_candidates_for_record(record)), ["socks5://127.0.0.1:7890", None])

    def test_mark_proxy_failure_sets_cooldown(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.proxy_health = {}
        gui.proxy_mode_var = DummyVar(PROXY_MODE_POOL)
        gui.proxy_fail_threshold_var = DummyVar("2")
        gui.proxy_cooldown_var = DummyVar("60")
        gui.log_queue = queue.Queue()
        gui.mark_proxy_failure("http://127.0.0.1:7890", "timeout")
        state = gui.get_proxy_health("http://127.0.0.1:7890")
        self.assertEqual(state["fail_count"], 1)
        self.assertEqual(state["last_error"], "timeout")
        self.assertFalse(gui.is_proxy_in_cooldown("http://127.0.0.1:7890"))
        return

        gui.mark_proxy_failure("http://127.0.0.1:7890", "连接超时，目标可能已下线或端口不通")
        self.assertFalse(gui.is_proxy_in_cooldown("http://127.0.0.1:7890"))
        gui.mark_proxy_failure("http://127.0.0.1:7890", "连接超时，目标可能已下线或端口不通")
        self.assertTrue(gui.is_proxy_in_cooldown("http://127.0.0.1:7890"))

    def test_load_ocr_route_rules_parses_valid_lines(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        with tempfile.TemporaryDirectory() as tmpdir:
            route_file = Path(tmpdir) / "ocr_routes.txt"
            route_file.write_text(
                "# comment\n"
                "host:example.com => http://127.0.0.1:8888/reg00\n"
                "default => http://127.0.0.1:8888/reg\n",
                encoding="utf-8",
            )
            gui.ocr_route_entry = DummyEntry(str(route_file))

            rules = gui.load_ocr_route_rules()

        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]["scope"], "host")
        self.assertEqual(rules[0]["pattern"], "example.com")
        self.assertEqual(rules[1]["scope"], "default")

    def test_load_locator_rules_parses_valid_lines(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        with tempfile.TemporaryDirectory() as tmpdir:
            rule_file = Path(tmpdir) / "locator_rules.txt"
            rule_file.write_text(
                "host:27.150.180.183 => user=css:input[placeholder*='账号']; pass=css:input[type='password']; submit=xpath://button[contains(.,'登录')]\n",
                encoding="utf-8",
            )
            gui.locator_rule_entry = DummyEntry(str(rule_file))

            rules = gui.load_locator_rules()

        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["scope"], "host")
        self.assertIn("user", rules[0]["selectors"])
        self.assertIn("pass", rules[0]["selectors"])

    def test_get_proxy_status_rows_includes_pool_health(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.proxy_mode_var = DummyVar(PROXY_MODE_POOL)
        gui.proxy_entry = DummyEntry("127.0.0.1:7890")
        gui.proxy_scheme_var = DummyVar(PROXY_SCHEME_SOCKS5)
        gui.proxy_health = {}
        gui.proxy_assignment = {"http://a.test": "http://127.0.0.1:7890"}
        gui.log_queue = queue.Queue()
        gui.proxy_fail_threshold_var = DummyVar("1")
        gui.proxy_cooldown_var = DummyVar("60")
        gui.mark_proxy_failure("socks5://127.0.0.1:7890", "timeout")
        rows = gui.get_proxy_status_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proxy"], "socks5://127.0.0.1:7890")
        self.assertEqual(rows[0]["scheme"], PROXY_SCHEME_SOCKS5)
        self.assertEqual(rows[0]["status"], "最近失败")
        return
        with tempfile.TemporaryDirectory() as tmpdir:
            proxy_file = Path(tmpdir) / "pool.txt"
            proxy_file.write_text("127.0.0.1:7890\n127.0.0.1:7891\n", encoding="utf-8")
            gui.proxy_pool_entry = DummyEntry(str(proxy_file))
            gui.mark_proxy_failure("http://127.0.0.1:7890", "连接超时，目标可能已下线或端口不通")
            rows = gui.get_proxy_status_rows()

        self.assertEqual(len(rows), 2)
        row_map = {row["proxy"]: row for row in rows}
        self.assertEqual(row_map["http://127.0.0.1:7890"]["status"], "冷却中")
        self.assertEqual(row_map["http://127.0.0.1:7890"]["assigned"], 1)

    def test_get_profile_dashboard_data_counts_rule_and_proxy_hits(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.all_records = [
            AuditRecord(
                record_id=1,
                target="http://a.test",
                status="已完成",
                login_form=True,
                captcha_present=True,
                risk_level="中",
                result="疑似登录页 | 浏览器渲染补扫命中",
                proxy_used="http://127.0.0.1:7890",
                ocr_endpoint_used="http://127.0.0.1:8888/reg00",
                ocr_route_rule="host:a.test => http://127.0.0.1:8888/reg00",
            ),
            AuditRecord(
                record_id=2,
                target="http://b.test",
                status="已完成",
                risk_level="失败",
                result="连接超时，目标可能已下线或端口不通",
                proxy_used="http://127.0.0.1:7891",
            ),
        ]

        profile = gui.get_profile_dashboard_data()

        summary = {item["label"]: item["value"] for item in profile["summary_items"]}
        self.assertEqual(summary["总目标"], 2)
        self.assertEqual(summary["验证码页"], 1)
        self.assertEqual(summary["补扫命中"], 1)
        self.assertEqual(profile["ocr_rules"][0]["count"], 1)
        self.assertEqual(profile["proxy_usage"][0]["count"], 1)

    def test_should_use_browser_render_fallback_keeps_heuristic_login_page(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.browser_render_var = DummyVar(True)
        record = AuditRecord(
            record_id=1,
            target="http://example.com",
            title="登录",
            login_form=True,
            login_score=3,
        )

        should_probe = gui.should_use_browser_render_fallback(
            record,
            "<html><head><title>登录</title><script src='chunk-vendors.js'></script></head><body><div id='app'></div></body></html>",
        )

        self.assertTrue(should_probe)

    def test_build_common_login_urls_includes_hash_routes(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)

        urls = gui.build_common_login_urls("http://example.com")

        self.assertIn("http://example.com/#/login", urls)
        self.assertIn("http://example.com/#/pages/login/login", urls)
        self.assertIn("http://example.com/login", urls)

    def test_extract_form_details_supports_loose_login_inputs(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        soup = BeautifulSoup(
            """
            <div class="login-card">
              <input type="text" placeholder="请输入账号">
              <input type="text" placeholder="请输入密码">
            </div>
            """,
            "html.parser",
        )

        action, method, summary = gui.extract_form_details(soup)

        self.assertEqual(action, "")
        self.assertEqual(method, "POST")
        self.assertIn("text:请输入账号", summary)
        self.assertIn("text:请输入密码", summary)

    def test_calculate_risk_stays_low_for_non_actionable_login_hint(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        record = AuditRecord(
            record_id=2,
            target="http://example.com",
            title="登录",
            login_form=True,
            login_score=3,
            result="疑似登录页",
        )

        self.assertEqual(gui.calculate_risk(record), "低")

    def test_write_detail_log_writes_text_file(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.detail_log_lock = threading.Lock()
        with tempfile.TemporaryDirectory() as tmpdir:
            gui.detail_log_path = Path(tmpdir) / "scan.detail.log"

            gui.write_detail_log("sample line", level="TRACE")

            self.assertIn("sample line", gui.detail_log_path.read_text(encoding="utf-8"))

    def test_probe_login_with_browser_falls_back_to_webdriver_when_cdp_misses(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui._browser_probe_record = None
        gui.resolve_locator_rule_for_record = lambda record: None
        gui.probe_login_with_devtools = lambda url, proxy=None: {
            "current_url": url,
            "html": "<html></html>",
            "login_form": False,
            "captcha_present": False,
            "field_summary": "",
            "form_method": "",
            "probe_backend": "cdp",
        }
        fake_driver = FakeDriver(page_source="<html><input type='password'></html>")
        gui.init_headless_driver = lambda proxy=None: fake_driver
        gui.wait_for_browser_render = lambda driver: None
        gui.dismiss_browser_obstructions = lambda driver: None

        original_locate = BruteForceHandler.locate_login_dom
        BruteForceHandler.locate_login_dom = lambda self, driver: {
            "form": None,
            "user": object(),
            "pass": object(),
            "captcha": None,
            "submit": None,
        }
        try:
            probe = gui.probe_login_with_browser("http://example.com/login")
        finally:
            BruteForceHandler.locate_login_dom = original_locate

        self.assertEqual(probe["probe_backend"], "webdriver")
        self.assertTrue(probe["login_form"])
        self.assertEqual(probe["field_summary"], "text:browser_user | password:browser_password")
        self.assertTrue(fake_driver.closed)

    def test_scan_timeout_marks_record_completed(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.log_queue = queue.Queue()
        gui.clear_record_stage = SecurityAuditGUI.clear_record_stage.__get__(gui, SecurityAuditGUI)
        gui.format_elapsed_seconds = SecurityAuditGUI.format_elapsed_seconds.__get__(gui, SecurityAuditGUI)
        record = AuditRecord(
            record_id=1,
            target="http://slow.example.com",
            status="扫描中...",
            analysis_stage="HTTP请求",
            analysis_detail="拉取首页",
            analysis_started_ts=1.0,
            analysis_stage_ts=1.0,
        )

        gui.mark_record_scan_timeout(record, "HTTP请求超时，目标长时间无响应")

        self.assertEqual(record.status, "已完成")
        self.assertEqual(record.risk_level, "失败")
        self.assertIn("HTTP请求超时", record.result)
        self.assertIn("ScanTimeout", record.error)
        self.assertEqual(record.analysis_stage, "")

    def test_worker_stage_is_mirrored_to_owner_record(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.log_record_trace = lambda *args, **kwargs: None
        gui.refresh_running_record_ui = lambda *args, **kwargs: None
        owner = AuditRecord(record_id=1, target="http://example.com", status="扫描中...")
        worker = gui.copy_record_for_worker(owner)

        gui.set_record_stage(worker, "HTTP请求", "拉取首页")

        self.assertEqual(owner.analysis_stage, "HTTP请求")
        self.assertEqual(owner.analysis_detail, "拉取首页")
        self.assertGreater(owner.analysis_started_ts, 0)

    def test_browser_probe_hit_runs_bruteforce_inline(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.brute_var = DummyVar(True)
        gui.browser_render_var = DummyVar(True)
        gui.follow_redirect_var = DummyVar(True)
        gui.mode_var = DummyVar("")
        gui.log_queue = queue.Queue()
        gui.log_record_trace = lambda *args, **kwargs: None
        gui.refresh_running_record_ui = lambda *args, **kwargs: None
        gui.uses_llm_primary = lambda: False
        gui.uses_llm_fallback = lambda: False
        gui.calculate_risk = SecurityAuditGUI.calculate_risk.__get__(gui, SecurityAuditGUI)
        gui.is_actionable_login_record = SecurityAuditGUI.is_actionable_login_record.__get__(gui, SecurityAuditGUI)
        gui.should_run_bruteforce_for_record = SecurityAuditGUI.should_run_bruteforce_for_record.__get__(gui, SecurityAuditGUI)
        gui.analyze_record_from_html = SecurityAuditGUI.analyze_record_from_html.__get__(gui, SecurityAuditGUI)
        gui.should_use_browser_render_fallback = lambda record, html: True
        gui.browser_probe_has_login_signal = lambda probe: True
        gui.probe_login_with_browser = lambda url, proxy=None: {
            "current_url": "http://example.com/login",
            "html": "<html><input type='password' name='password'></html>",
            "login_form": True,
            "captcha_present": False,
            "field_summary": "text:browser_user | password:browser_password",
            "form_method": "POST",
            "probe_backend": "cdp",
        }
        calls = []
        gui.run_bruteforce_for_record = lambda record, session, soup: calls.append(record.target)
        session = FakeSession()
        session.get = lambda url, **kwargs: FakeResponse(
            status_code=200,
            text="<html><script src='/app.js'></script><script></script><script></script><script></script></html>",
            url=url,
        )
        record = AuditRecord(record_id=1, target="http://example.com")

        gui.inspect_target(session, record, Path("."))

        self.assertEqual(calls, ["http://example.com"])
        self.assertIn("浏览器渲染补扫命中", record.result)

    def test_choose_response_text_prefers_non_mojibake_encoding(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        html = "<html><title>游戏管理后台</title></html>"
        response = FakeResponse(
            status_code=200,
            text=html.encode("utf-8").decode("gbk", errors="replace"),
            content=html.encode("utf-8"),
            encoding="gbk",
            apparent_encoding="gbk",
        )

        text, encoding = gui.choose_response_text(response)

        self.assertIn("游戏管理后台", text)
        self.assertEqual(encoding, "utf-8")

    def test_waiting_stage_does_not_use_record_hard_timeout(self):
        gui = SecurityAuditGUI.__new__(SecurityAuditGUI)
        gui.browser_render_var = DummyVar(True)
        gui.get_render_wait = lambda: 2.5
        gui.uses_llm_primary = lambda: False
        gui.uses_llm_fallback = lambda: False
        record = AuditRecord(
            record_id=1,
            target="http://example.com",
            status="扫描中...",
            analysis_stage="等待线程",
            analysis_started_ts=1000.0,
            analysis_stage_ts=1000.0,
        )

        with mock.patch("passhack.time.time", return_value=1121.0):
            self.assertFalse(gui.is_record_hard_timeout(record))


if __name__ == "__main__":
    unittest.main()
