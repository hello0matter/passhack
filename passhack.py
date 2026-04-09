import concurrent.futures
import csv
import hashlib
import html
import json
import os
import queue
import re
import threading
import time
import warnings
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
import tkinter as tk
from urllib.parse import urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from openpyxl import load_workbook
from PIL import Image, ImageTk

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl.styles.stylesheet")
warnings.filterwarnings("ignore", message="Workbook contains no default style, apply openpyxl's default")
os.environ.setdefault("WDM_LOG_LEVEL", "0")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
os.environ.setdefault("NO_COLOR", "1")

LOGIN_KEYWORDS = [
    "login",
    "signin",
    "sign in",
    "auth",
    "passport",
    "admin",
    "登录",
    "登 录",
    "用户登录",
    "统一认证",
    "身份认证",
    "后台",
    "管理平台",
]

CAPTCHA_KEYWORDS = [
    "验证码",
    "captcha",
    "verifycode",
    "checkcode",
    "图形码",
    "校验码",
]

MFA_KEYWORDS = [
    "otp",
    "totp",
    "2fa",
    "mfa",
    "双因素",
    "动态口令",
    "短信验证码",
    "邮箱验证码",
]

LOCKOUT_KEYWORDS = [
    "锁定",
    "冻结",
    "连续失败",
    "多次失败",
    "错误次数",
    "锁住",
    "account locked",
]

DEFAULT_HINT_KEYWORDS = [
    "默认账号",
    "默认密码",
    "初始密码",
    "admin",
    "administrator",
    "root",
    "guest",
    "superadmin",
]

INPUT_HINTS = [
    "用户名",
    "账号",
    "账户",
    "user",
    "account",
    "phone",
    "手机号",
]


@dataclass
class AuditRecord:
    record_id: int
    target: str
    final_url: str = ""
    status: str = "等待中"
    title: str = ""
    risk_level: str = "-"
    result: str = "-"
    login_score: int = 0
    login_form: bool = False
    password_field_count: int = 0
    captcha_present: bool = False
    mfa_present: bool = False
    lockout_hint: bool = False
    default_hint: bool = False
    form_action: str = ""
    form_method: str = ""
    field_summary: str = ""
    screenshot_path: str = ""
    error: str = ""

class BruteForceHandler:
    def __init__(self, session, log_queue):
        self.session = session
        self.log_queue = log_queue
        # 默认中国人常用字典
        self.default_user = ["admin", "root", "administrator", "user", "guest"]
        self.default_pass = ["123456", "admin123", "888888", "666666", "password", "12345678"]

    def run(self, record, dict_path=None):
        if not record.login_form: return "跳过(非登录页)"
        
        # 加载字典
        users, passwords = self.load_dicts(dict_path)
        
        # 识别字段 (从 record.field_summary 解析)
        # 格式示例: "text:username | password:password | checkbox:remember"
        user_key = None
        pass_key = None
        for field in record.field_summary.split(" | "):
            if ":" not in field: continue
            ftype, fname = field.split(":", 1)
            if any(k in fname.lower() for k in ["user", "account", "name", "login"]) and not user_key:
                user_key = fname
            if "password" in ftype.lower() or "pass" in fname.lower():
                pass_key = fname

        if not user_key or not pass_key:
            return "失败(未识别字段)"

        # 确定请求地址
        action_url = self.get_action_url(record)
        
        # 开始尝试
        for u in users:
            for p in passwords:
                try:
                    payload = {user_key: u, pass_key: p}
                    # 模拟登录
                    resp = self.session.post(action_url, data=payload, timeout=5, allow_redirects=False)
                    
                    # 判定成功：302跳转到非登录页面，或者Body包含“成功”
                    if resp.status_code in [302, 301] and "login" not in resp.headers.get("Location", "").lower():
                        return f"🔥 成功: {u}/{p}"
                    if any(k in resp.text for k in ["登录成功", "success", "index.php", "welcome"]):
                        return f"🔥 成功: {u}/{p}"
                        
                except Exception as e:
                    continue
        return "安全(未发现弱口令)"

    def load_dicts(self, path):
        # 如果有路径则读取文件，否则返回默认
        if path and os.path.exists(path):
            # 简单处理：假设字典是 user:pass 格式或逻辑
            return self.default_user, self.default_pass # 实际开发中此处可扩展文件读取
        return self.default_user, self.default_pass

    def get_action_url(self, record):
        if record.form_action and record.form_action.startswith("http"):
            return record.form_action
        parsed = urlparse(record.final_url or record.target)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if record.form_action:
            return f"{base}/{record.form_action.lstrip('/')}"
        return record.final_url or record.target


class SecurityAuditGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("本地安全资产登录面审计工具 v2.0")
        self.root.geometry("1460x860")

        self.log_queue = queue.Queue()
        self.result_lock = threading.Lock()
        self.records_by_item = {}
        self.all_records = []
        self.is_scanning = False
        self.output_dir = Path.cwd() / "output"
        self.output_dir.mkdir(exist_ok=True)
        self.state_dir = self.output_dir / "state"
        self.state_dir.mkdir(exist_ok=True)
        self.projects_dir = self.state_dir / "projects"
        self.projects_dir.mkdir(exist_ok=True)
        self.latest_project_path = self.state_dir / "latest_project.json"
        self.current_project_path = None
        self.last_loaded_path = ""
        self.preview_image = None
        self.brute_var = tk.BooleanVar(value=False)
        self.setup_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.check_log_queue()
        self.load_autosave_if_exists()

    def setup_ui(self):
        top_frame = tk.Frame(self.root, pady=10, padx=12)
        top_frame.pack(fill=tk.X)
        
        tk.Label(top_frame, text="目标文件 (TXT/XLSX):").pack(side=tk.LEFT)
        self.file_entry = tk.Entry(top_frame, width=58)
        self.file_entry.pack(side=tk.LEFT, padx=6)
        tk.Button(top_frame, text="浏览...", command=self.load_file).pack(side=tk.LEFT, padx=4)
        tk.Button(top_frame, text="导出结果", command=self.save_results).pack(side=tk.LEFT, padx=4)

        self.btn_start = tk.Button(top_frame, text="开始审计", bg="#9ed0ff", command=self.toggle_scan)
        self.btn_start.pack(side=tk.RIGHT, padx=4)

        config_frame = tk.Frame(self.root, pady=5, padx=12)
        config_frame.pack(fill=tk.X)

        self.use_proxy_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            config_frame,
            text="启用全局代理 (如 Burp Suite)",
            variable=self.use_proxy_var,
            command=self.toggle_proxy_state,
        ).pack(side=tk.LEFT)

        tk.Label(config_frame, text="地址:").pack(side=tk.LEFT, padx=(10, 2))
        self.proxy_entry = tk.Entry(config_frame, width=18, state=tk.DISABLED)
        self.proxy_entry.insert(0, "127.0.0.1:8080")
        self.proxy_entry.pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(config_frame, text="识别模式:").pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value="规则模式")
        ttk.Combobox(
            config_frame,
            textvariable=self.mode_var,
            values=["规则模式", "NLP模式"],
            width=12,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(4, 12))

        self.capture_var = tk.BooleanVar(value=True)
        tk.Checkbutton(config_frame, text="尝试截图", variable=self.capture_var).pack(side=tk.LEFT)

        tk.Label(config_frame, text="截图策略:").pack(side=tk.LEFT, padx=(8, 4))
        self.capture_policy_var = tk.StringVar(value="命中项")
        ttk.Combobox(
            config_frame,
            textvariable=self.capture_policy_var,
            values=["命中项", "仅高风险"],
            width=8,
            state="readonly",
        ).pack(side=tk.LEFT)

        self.follow_redirect_var = tk.BooleanVar(value=True)
        tk.Checkbutton(config_frame, text="跟随重定向", variable=self.follow_redirect_var).pack(side=tk.LEFT, padx=(8, 0))

        tk.Label(config_frame, text="并发数:").pack(side=tk.LEFT, padx=(10, 4))
        self.worker_var = tk.StringVar(value="4")
        ttk.Combobox(
            config_frame,
            textvariable=self.worker_var,
            values=["1", "2", "4", "6", "8", "12"],
            width=4,
            state="readonly",
        ).pack(side=tk.LEFT)

        tk.Label(config_frame, text="截图节流:").pack(side=tk.LEFT, padx=(8, 4))
        self.capture_delay_var = tk.StringVar(value="0.4")
        ttk.Combobox(
            config_frame,
            textvariable=self.capture_delay_var,
            values=["0", "0.2", "0.4", "0.8", "1.2"],
            width=5,
            state="readonly",
        ).pack(side=tk.LEFT)

        tk.Label(config_frame, text="列表筛选:").pack(side=tk.LEFT, padx=(14, 4))
        self.filter_var = tk.StringVar(value="全部")
        filter_box = ttk.Combobox(
            config_frame,
            textvariable=self.filter_var,
            values=["全部", "仅高风险", "仅疑似登录页", "仅已完成", "仅失败", "仅有截图", "仅有表单字段"],
            width=12,
            state="readonly",
        )
        filter_box.pack(side=tk.LEFT)
        filter_box.bind("<<ComboboxSelected>>", lambda _event: self.apply_filter())

        self.summary_var = tk.StringVar(value="摘要: 总计 0 | 已完成 0 | 待处理 0 | 高 0 | 中 0 | 低 0 | 有截图 0")
        tk.Label(config_frame, textvariable=self.summary_var, anchor="w", fg="#334155").pack(side=tk.RIGHT)

        action_frame = tk.Frame(self.root, pady=4, padx=12)
        action_frame.pack(fill=tk.X)
        tk.Button(action_frame, text="工程列表", command=self.open_project_manager).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(action_frame, text="放大查看", command=self.open_preview_zoom).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(action_frame, text="查看截图", command=self.open_selected_screenshot).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(action_frame, text="打开目标", command=self.open_selected_target).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(action_frame, text="打开工程目录", command=self.open_output_dir).pack(side=tk.LEFT)
        tk.Button(action_frame, text="导出证据页", command=self.export_evidence_page).pack(side=tk.LEFT, padx=(6, 0))

        mid_frame = tk.Frame(self.root, padx=12)
        mid_frame.pack(fill=tk.BOTH, expand=True)

        split_pane = ttk.Panedwindow(mid_frame, orient=tk.HORIZONTAL)
        split_pane.pack(fill=tk.BOTH, expand=True)

        left_panel = tk.Frame(split_pane)
        preview_frame = tk.LabelFrame(split_pane, text="详情预览", padx=10, pady=10)
        split_pane.add(left_panel, weight=5)
        split_pane.add(preview_frame, weight=2)

        tree_frame = tk.Frame(left_panel)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("ID", "目标资产", "状态", "页面标题", "风险级别", "审计结果")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=12)
        v_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        h_scroll = ttk.Scrollbar(left_panel, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)

        for name, width, anchor in [
            ("ID", 56, tk.CENTER),
            ("目标资产", 280, tk.W),
            ("状态", 92, tk.CENTER),
            ("页面标题", 260, tk.W),
            ("风险级别", 80, tk.CENTER),
            ("审计结果", 520, tk.W),
        ]:
            self.tree.heading(name, text=name)
            self.tree.column(name, width=width, anchor=anchor, minwidth=width)

        self.tree.tag_configure("risk_high", background="#ffe1e1")
        self.tree.tag_configure("risk_medium", background="#fff0cf")
        self.tree.tag_configure("risk_low", background="#edf9ed")
        self.tree.tag_configure("status_error", background="#f3d6d6")
        self.tree.bind("<Double-1>", self.on_tree_double_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)

        preview_frame.configure(width=460)
        preview_frame.pack_propagate(False)

        self.preview_title_var = tk.StringVar(value="未选择记录")
        tk.Label(
            preview_frame,
            textvariable=self.preview_title_var,
            anchor="w",
            justify=tk.LEFT,
            wraplength=420,
            font=("Microsoft YaHei", 10, "bold"),
        ).pack(fill=tk.X)

        self.preview_meta_var = tk.StringVar(value="目标、风险、最终URL 会显示在这里")
        tk.Label(
            preview_frame,
            textvariable=self.preview_meta_var,
            anchor="w",
            justify=tk.LEFT,
            wraplength=420,
        ).pack(fill=tk.X, pady=(6, 8))

        nav_frame = tk.Frame(preview_frame)
        nav_frame.pack(fill=tk.X, pady=(0, 8))
        tk.Button(nav_frame, text="上一条", command=lambda: self.select_relative_record(-1)).pack(side=tk.LEFT)
        tk.Button(nav_frame, text="下一条", command=lambda: self.select_relative_record(1)).pack(side=tk.LEFT, padx=(6, 0))

        self.preview_image_label = tk.Label(
            preview_frame,
            text="暂无截图",
            bg="#f3f4f6",
            width=56,
            height=16,
            relief=tk.SOLID,
            bd=1,
            anchor="center",
        )
        self.preview_image_label.pack(fill=tk.BOTH, pady=(0, 8))
        self.preview_image_label.bind("<Double-1>", lambda _event: self.open_preview_zoom())

        tk.Label(preview_frame, text="表单与取证信息:").pack(anchor=tk.W, pady=(10, 4))
        self.preview_text = scrolledtext.ScrolledText(preview_frame, height=14, state=tk.DISABLED, wrap=tk.WORD)
        self.preview_text.pack(fill=tk.BOTH, expand=True)

        bottom_frame = tk.Frame(self.root, pady=10, padx=12)
        bottom_frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(bottom_frame, text="实时引擎日志:").pack(anchor=tk.W)
        self.log_text = scrolledtext.ScrolledText(
            bottom_frame,
            height=13,
            state=tk.DISABLED,
            bg="black",
            fg="#17d917",
            insertbackground="white",
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def toggle_proxy_state(self):
        self.proxy_entry.config(state=tk.NORMAL if self.use_proxy_var.get() else tk.DISABLED)

    def normalize_target(self, target: str) -> str:
        value = target.strip()
        if not value:
            return ""
        if not re.match(r"^https?://", value, flags=re.I):
            value = f"http://{value}"
        return value

    def load_file(self):
        filepath = filedialog.askopenfilename(
            filetypes=[
                ("Supported files", "*.txt *.xlsx"),
                ("Text files", "*.txt"),
                ("Excel files", "*.xlsx"),
            ]
        )
        if not filepath:
            return

        project_path = self.project_path_for_source(filepath)
        if project_path.exists():
            self.load_project_file(project_path)
            self.log_message(f"[*] 已载入历史工程进度: {filepath}")
            return

        self.file_entry.delete(0, tk.END)
        self.file_entry.insert(0, filepath)
        self.last_loaded_path = filepath
        self.current_project_path = project_path
        self.records_by_item.clear()
        self.all_records.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)

        seen = set()
        try:
            path = Path(filepath)
            raw_targets = self.read_targets_from_file(path)
            for raw_line in raw_targets:
                target = self.normalize_target(raw_line)
                if not target or target in seen:
                    continue
                seen.add(target)
                record = AuditRecord(record_id=len(self.all_records) + 1, target=target)
                self.all_records.append(record)
            self.rebuild_tree()
            self.ensure_selection()
            self.save_progress_snapshot(reason="load")
            self.log_message(f"[*] 加载文件: {filepath}")
            self.log_message(f"[*] 成功导入 {len(self.all_records)} 个目标资产，已自动去重与协议补全。")
        except Exception as exc:
            self.log_message(f"[-] 读取文件失败: {exc}")
            messagebox.showerror("错误", f"读取文件失败: {exc}")

    def read_targets_from_file(self, path: Path) -> list[str]:
        if path.suffix.lower() == ".xlsx":
            return self.read_targets_from_xlsx(path)
        with open(path, "r", encoding="utf-8") as handle:
            return [line.strip() for line in handle]

    def read_targets_from_xlsx(self, path: Path) -> list[str]:
        workbook = load_workbook(path, read_only=True, data_only=True)
        sheet = workbook.active
        targets = []
        header_aliases = {
            "host": "host",
            "网址": "host",
            "地址": "host",
            "资产": "host",
            "目标": "host",
            "url": "host",
            "site": "host",
            "ip": "ip",
            "端口": "port",
            "协议": "scheme",
        }
        header_row_found = False
        header_map = {}

        for row in sheet.iter_rows(values_only=True):
            values = ["" if cell is None else str(cell).strip() for cell in row]
            if not any(values):
                continue

            normalized_row = [value.lower() for value in values]
            current_map = {}
            for idx, value in enumerate(normalized_row):
                if value in header_aliases:
                    current_map[header_aliases[value]] = idx

            if not header_row_found:
                if "host" in current_map or {"ip", "port", "scheme"}.issubset(current_map):
                    header_row_found = True
                    header_map = current_map
                continue

            target = self.extract_target_from_row(values, header_map)
            if target:
                targets.append(target)
        workbook.close()
        return targets

    def extract_target_from_row(self, values: list[str], header_map: dict[str, int]) -> str:
        host = self.get_cell(values, header_map.get("host"))
        if host:
            return host

        ip = self.get_cell(values, header_map.get("ip"))
        port = self.get_cell(values, header_map.get("port"))
        scheme = self.get_cell(values, header_map.get("scheme")) or "http"

        if not ip:
            return ""

        port = self.normalize_port(port)
        if port:
            return f"{scheme}://{ip}:{port}"
        return f"{scheme}://{ip}"

    def get_cell(self, values: list[str], index: int | None) -> str:
        if index is None or index >= len(values):
            return ""
        return str(values[index]).strip()

    def normalize_port(self, value: str) -> str:
        if not value:
            return ""
        port = value.strip()
        if re.fullmatch(r"\d+\.0", port):
            return port[:-2]
        return port

    def rebuild_tree(self):
        self.records_by_item.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        for record in self.filtered_records():
            item_id = self.tree.insert(
                "",
                tk.END,
                values=(
                    record.record_id,
                    record.target,
                    record.status,
                    record.title or "-",
                    record.risk_level,
                    record.result,
                ),
                tags=(self.tag_for_record(record),),
            )
            self.records_by_item[item_id] = record
        self.update_summary()

    def filtered_records(self) -> list[AuditRecord]:
        current_filter = self.filter_var.get()
        if current_filter == "仅高风险":
            return [record for record in self.all_records if record.risk_level == "高"]
        if current_filter == "仅疑似登录页":
            return [record for record in self.all_records if record.login_form]
        if current_filter == "仅已完成":
            return [record for record in self.all_records if record.status == "已完成"]
        if current_filter == "仅失败":
            return [record for record in self.all_records if record.risk_level == "失败"]
        if current_filter == "仅有截图":
            return [record for record in self.all_records if record.screenshot_path]
        if current_filter == "仅有表单字段":
            return [record for record in self.all_records if record.field_summary]
        return list(self.all_records)

    def apply_filter(self):
        self.rebuild_tree()
        self.ensure_selection()
        self.log_message(f"[*] 当前列表筛选: {self.filter_var.get()}")

    def save_results(self):
        records = self.get_records_snapshot()
        if not records:
            messagebox.showwarning("提示", "没有可导出的数据。")
            return

        filepath = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[
                ("CSV files", "*.csv"),
                ("JSON files", "*.json"),
                ("HTML files", "*.html"),
            ],
            title="保存审计结果",
        )
        if not filepath:
            return

        output = Path(filepath)
        try:
            if output.suffix.lower() == ".json":
                self.export_json(output, records)
            elif output.suffix.lower() == ".html":
                self.export_html(output, records)
            else:
                self.export_csv(output, records)
            self.log_message(f"[*] 审计结果已导出至: {output}")
            messagebox.showinfo("成功", f"导出成功: {output.name}")
        except Exception as exc:
            self.log_message(f"[-] 导出失败: {exc}")
            messagebox.showerror("错误", f"导出失败: {exc}")

    def export_csv(self, output: Path, records: list[AuditRecord]):
        with open(output, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "ID",
                    "目标资产",
                    "最终URL",
                    "状态",
                    "页面标题",
                    "风险级别",
                    "登录评分",
                    "审计结果",
                    "是否疑似登录页",
                    "密码框数量",
                    "验证码",
                    "多因素",
                    "锁定提示",
                    "默认账号提示",
                    "表单Action",
                    "表单Method",
                    "字段摘要",
                    "截图路径",
                    "错误信息",
                ]
            )
            for record in records:
                writer.writerow(
                    [
                        record.record_id,
                        record.target,
                        record.final_url,
                        record.status,
                        record.title,
                        record.risk_level,
                        record.login_score,
                        record.result,
                        "是" if record.login_form else "否",
                        record.password_field_count,
                        "是" if record.captcha_present else "否",
                        "是" if record.mfa_present else "否",
                        "是" if record.lockout_hint else "否",
                        "是" if record.default_hint else "否",
                        record.form_action,
                        record.form_method,
                        record.field_summary,
                        record.screenshot_path,
                        record.error,
                    ]
                )

    def export_json(self, output: Path, records: list[AuditRecord]):
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "record_count": len(records),
            "records": [asdict(record) for record in records],
        }
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def export_html(self, output: Path, records: list[AuditRecord]):
        rows = []
        for record in records:
            screenshot = self.render_html_screenshot(record)
            rows.append(
                "<tr>"
                f"<td>{record.record_id}</td>"
                f"<td>{html.escape(record.target)}</td>"
                f"<td>{html.escape(record.status)}</td>"
                f"<td>{html.escape(record.title or '-')}</td>"
                f"<td>{html.escape(record.risk_level)}</td>"
                f"<td>{record.login_score}</td>"
                f"<td>{html.escape(record.result)}</td>"
                f"<td>{html.escape(record.form_method or '-')}</td>"
                f"<td>{html.escape(record.field_summary or '-')}</td>"
                f"<td>{screenshot}</td>"
                "</tr>"
            )

        page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>登录面审计报告</title>
<style>
body {{ font-family: "Microsoft YaHei", sans-serif; margin: 24px; background: #f5f7fb; color: #1f2937; }}
h1 {{ margin-bottom: 8px; }}
p {{ color: #4b5563; }}
table {{ width: 100%; border-collapse: collapse; background: white; }}
th, td {{ border: 1px solid #d1d5db; padding: 10px; text-align: left; vertical-align: top; }}
th {{ background: #eff6ff; }}
a {{ color: #1d4ed8; text-decoration: none; }}
img {{ max-width: 280px; max-height: 180px; border: 1px solid #d1d5db; border-radius: 6px; display: block; margin-bottom: 6px; }}
.high {{ color: #b91c1c; font-weight: bold; }}
.medium {{ color: #b45309; font-weight: bold; }}
.low {{ color: #166534; font-weight: bold; }}
</style>
</head>
<body>
<h1>登录面审计报告</h1>
<p>生成时间: {html.escape(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}</p>
<p>记录总数: {len(records)}</p>
<table>
<thead>
<tr><th>ID</th><th>目标资产</th><th>状态</th><th>页面标题</th><th>风险级别</th><th>登录评分</th><th>审计结果</th><th>表单方法</th><th>字段摘要</th><th>截图</th></tr>
</thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
</body>
</html>"""
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(page)

    def export_evidence_page(self):
        records = [record for record in self.get_records_snapshot() if record.screenshot_path or record.field_summary]
        if not records:
            messagebox.showinfo("提示", "当前没有可导出的证据记录。")
            return
        output = filedialog.asksaveasfilename(
            defaultextension=".html",
            filetypes=[("HTML files", "*.html")],
            title="导出证据页",
        )
        if not output:
            return
        self.export_evidence_html(Path(output), records)
        self.log_message(f"[*] 证据页已导出至: {output}")
        messagebox.showinfo("成功", "证据页导出成功。")

    def export_evidence_html(self, output: Path, records: list[AuditRecord]):
        cards = []
        for record in records:
            screenshot = self.render_html_screenshot(record)
            cards.append(
                "<section class='card'>"
                f"<h2>{html.escape(record.title or record.target)}</h2>"
                f"<p><strong>目标:</strong> {html.escape(record.target)}</p>"
                f"<p><strong>最终URL:</strong> {html.escape(record.final_url or '-')}</p>"
                f"<p><strong>状态:</strong> {html.escape(record.status)} | <strong>风险:</strong> {html.escape(record.risk_level)}</p>"
                f"<p><strong>表单方法:</strong> {html.escape(record.form_method or '-')}</p>"
                f"<p><strong>表单Action:</strong> {html.escape(record.form_action or '-')}</p>"
                f"<p><strong>字段摘要:</strong> {html.escape(record.field_summary or '-')}</p>"
                f"<p><strong>审计结果:</strong> {html.escape(record.result)}</p>"
                f"<div class='shot'>{screenshot}</div>"
                "</section>"
            )
        page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>登录面证据页</title>
<style>
body {{ font-family: "Microsoft YaHei", sans-serif; margin: 24px; background: #f8fafc; color: #0f172a; }}
h1 {{ margin-bottom: 8px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 16px; }}
.card {{ background: white; border: 1px solid #dbeafe; border-radius: 12px; padding: 16px; box-shadow: 0 8px 24px rgba(15,23,42,.06); }}
.card h2 {{ margin-top: 0; font-size: 18px; }}
.card p {{ margin: 8px 0; line-height: 1.5; word-break: break-all; }}
img {{ max-width: 100%; border-radius: 8px; border: 1px solid #cbd5e1; }}
a {{ color: #2563eb; text-decoration: none; }}
</style>
</head>
<body>
<h1>登录面证据页</h1>
<p>生成时间: {html.escape(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))}</p>
<div class="grid">
{''.join(cards)}
</div>
</body>
</html>"""
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(page)

    def build_requests_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            }
        )
        if self.use_proxy_var.get():
            proxy_addr = self.proxy_entry.get().strip()
            if proxy_addr:
                session.proxies = {
                    "http": f"http://{proxy_addr}",
                    "https": f"http://{proxy_addr}",
                }
        return session

    def get_worker_count(self) -> int:
        try:
            return max(1, min(12, int(self.worker_var.get())))
        except Exception:
            return 4

    def get_capture_delay(self) -> float:
        try:
            return max(0.0, min(3.0, float(self.capture_delay_var.get())))
        except Exception:
            return 0.4

    def toggle_scan(self):
        if not self.all_records:
            messagebox.showwarning("提示", "请先导入目标文件。")
            return

        if not self.is_scanning:
            self.is_scanning = True
            self.btn_start.config(text="停止审计", bg="#ff8d7b")
            self.proxy_entry.config(state=tk.DISABLED)
            self.save_progress_snapshot(reason="scan-start")
            self.log_message("[*] 初始化登录面审计引擎...")
            self.log_message(f"[*] 当前识别模式: {self.mode_var.get()}")
            self.log_message(f"[*] 当前并发数: {self.get_worker_count()}")
            self.log_message(
                f"[*] 截图策略: {self.capture_policy_var.get()} | 截图节流: {self.get_capture_delay():.1f}s"
            )
            self.log_message(f"[*] 本次将从未完成目标继续扫描，已完成记录会自动跳过。")
            if self.use_proxy_var.get():
                self.log_message(f"[*] 已启用代理: {self.proxy_entry.get().strip()}")
            threading.Thread(target=self.scan_engine, daemon=True).start()
        else:
            self.is_scanning = False
            self.save_progress_snapshot(reason="manual-stop")
            self.log_message("[-] 用户终止扫描任务。")

    def scan_engine(self):
        run_dir = self.output_dir / datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        screenshot_dir = run_dir / "screenshots"
        screenshot_dir.mkdir(exist_ok=True)
        pending_records = []
        for record in self.all_records:
            if record.status == "已完成":
                continue
            if record.status == "扫描中...":
                record.status = "等待中"
            pending_records.append(record)

        if not pending_records:
            self.log_queue.put("[*] 当前工程没有待处理目标。")
            self.save_progress_snapshot(reason="scan-finished")
            self.root.after(0, self.reset_scan_button)
            return

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.get_worker_count(),
            thread_name_prefix="audit",
        )
        future_map = {}
        try:
            for record in pending_records:
                if not self.is_scanning:
                    break
                item_id = self.find_item_id_by_record(record)
                if item_id:
                    self.root.after(0, self.update_tree_item, item_id, "扫描中...", record.title or "-", record.risk_level, "正在分析")
                record.status = "扫描中..."
                self.log_queue.put(f"[>] 正在分析: {record.target}")
                future = executor.submit(self.inspect_target_threadsafe, record, screenshot_dir)
                future_map[future] = record

            while future_map:
                done, _pending = concurrent.futures.wait(
                    future_map.keys(),
                    timeout=0.2,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not self.is_scanning:
                    for future in list(future_map):
                        future.cancel()
                    break
                for future in done:
                    record = future_map.pop(future)
                    item_id = self.find_item_id_by_record(record)
                    try:
                        analyzed = future.result()
                        if item_id:
                            self.root.after(
                                0,
                                self.update_tree_item,
                                item_id,
                                analyzed.status,
                                analyzed.title or "-",
                                analyzed.risk_level,
                                analyzed.result,
                            )
                        else:
                            self.root.after(0, self.apply_filter)
                        self.save_progress_snapshot(reason="record-updated")
                    except requests.exceptions.ProxyError:
                        record.status = "已完成"
                        record.risk_level = "失败"
                        record.result = "代理连接失败"
                        record.error = "代理连接失败"
                        if item_id:
                            self.root.after(0, self.update_tree_item, item_id, record.status, "-", record.risk_level, record.result)
                        self.log_queue.put("[-] 代理连接失败，请检查代理服务。")
                        self.save_progress_snapshot(reason="proxy-error")
                        self.is_scanning = False
                        for other_future in list(future_map):
                            other_future.cancel()
                        future_map.clear()
                        break
                    except Exception as exc:
                        record.status = "已完成"
                        record.risk_level = "失败"
                        record.result = "分析异常"
                        record.error = str(exc)
                        if item_id:
                            self.root.after(0, self.update_tree_item, item_id, record.status, "-", record.risk_level, record.result)
                        self.log_queue.put(f"[-] {record.target} 分析异常: {exc}")
                        self.save_progress_snapshot(reason="record-error")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if self.is_scanning and self.capture_var.get():
            self.capture_stage(pending_records, screenshot_dir)

        if self.is_scanning:
            self.log_queue.put("[*] 全部目标分析完成。")
        self.save_progress_snapshot(reason="scan-finished")
        self.root.after(0, self.reset_scan_button)

    def inspect_target_threadsafe(self, record: AuditRecord, screenshot_dir: Path) -> AuditRecord:
        session = self.build_requests_session()
        return self.inspect_target(session, record, screenshot_dir)

    def inspect_target(self, session: requests.Session, record: AuditRecord, screenshot_dir: Path) -> AuditRecord:
        response = session.get(
            record.target,
            timeout=8,
            verify=False,
            allow_redirects=self.follow_redirect_var.get(),
        )
        response.encoding = response.apparent_encoding or response.encoding
        soup = BeautifulSoup(response.text, "html.parser")
        record.final_url = response.url
        record.status = "已完成"
        record.title = self.extract_title(soup)

        text_blob = self.collect_text_blob(soup)
        password_fields = soup.find_all("input", {"type": re.compile("password", re.I)})
        record.password_field_count = len(password_fields)
        record.captcha_present = self.contains_any(text_blob, CAPTCHA_KEYWORDS) or self.has_captcha_widget(soup)
        record.mfa_present = self.contains_any(text_blob, MFA_KEYWORDS)
        record.lockout_hint = self.contains_any(text_blob, LOCKOUT_KEYWORDS)
        record.default_hint = self.contains_any(text_blob, DEFAULT_HINT_KEYWORDS)
        record.login_score = self.compute_login_score(soup, text_blob, record.title)
        record.login_form = record.password_field_count > 0 or record.login_score >= 3
        record.form_action, record.form_method, record.field_summary = self.extract_form_details(soup)

        findings = []
        if record.login_form:
            findings.append("疑似登录页")
        if record.default_hint:
            findings.append("存在默认账号/初始密码提示")
        if not record.captcha_present and record.login_form:
            findings.append("未见验证码")
        if not record.mfa_present and record.login_form:
            findings.append("未见多因素认证提示")
        if not record.lockout_hint and record.login_form:
            findings.append("未见锁定策略提示")
        if response.status_code >= 400:
            findings.append(f"HTTP {response.status_code}")

        record.risk_level = self.calculate_risk(record)
        record.result = " | ".join(findings[:5]) if findings else "未发现明显登录相关风险信号"

        self.log_queue.put(
            f"[+] {record.target} 分析完成: 标题={record.title or '无标题'} 风险={record.risk_level}"
        )
        # --- 新增：弱口令检测环节 ---
        if self.brute_var.get() and record.login_form:
            self.log_queue.put(f"[*] 正在对 {record.target} 进行弱口令尝试...")
            brute = BruteForceHandler(session, self.log_queue)
            dict_file = self.dict_entry.get()
            
            res = brute.run(record, dict_path=dict_file)
            record.result += f" | 登录探测: {res}"
            
            # 如果成功，强制提升风险等级
            if "🔥" in res:
                record.risk_level = "高"
                self.log_queue.put(f"[!] 发现弱口令风险: {record.target} -> {res}")
        
        return record

    def capture_stage(self, records: list[AuditRecord], screenshot_dir: Path):
        capture_targets = [record for record in records if self.should_capture_record(record)]
        if not capture_targets:
            self.log_queue.put("[*] 截图阶段跳过，未命中需要取证的目标。")
            return

        capture_workers = 1 if self.get_worker_count() <= 2 else 2
        self.log_queue.put(f"[*] 进入截图阶段，共 {len(capture_targets)} 条，截图并发 {capture_workers}。")
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=capture_workers,
            thread_name_prefix="capture",
        )
        future_map = {}
        try:
            for record in capture_targets:
                if not self.is_scanning:
                    break
                future = executor.submit(self.capture_record_screenshot, record, screenshot_dir)
                future_map[future] = record

            while future_map:
                done, _pending = concurrent.futures.wait(
                    future_map.keys(),
                    timeout=0.2,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not self.is_scanning:
                    for future in list(future_map):
                        future.cancel()
                    break
                for future in done:
                    record = future_map.pop(future)
                    try:
                        screenshot_path = future.result()
                        if screenshot_path:
                            record.screenshot_path = str(screenshot_path)
                            self.log_queue.put(f"[+] 已截图: {record.target}")
                        else:
                            self.log_queue.put(f"[~] 截图跳过或失败: {record.target}")
                        self.save_progress_snapshot(reason="screenshot-updated")
                        item_id = self.find_item_id_by_record(record)
                        if item_id:
                            self.root.after(
                                0,
                                self.update_tree_item,
                                item_id,
                                record.status,
                                record.title or "-",
                                record.risk_level,
                                record.result,
                            )
                    except Exception as exc:
                        self.log_queue.put(f"[~] 截图异常: {record.target} -> {exc}")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def should_capture_record(self, record: AuditRecord) -> bool:
        if record.screenshot_path:
            return False
        if self.capture_policy_var.get() == "仅高风险":
            return record.risk_level == "高"
        return record.login_form or record.risk_level in {"高", "中"} or bool(record.field_summary)

    def capture_record_screenshot(self, record: AuditRecord, screenshot_dir: Path) -> Path | None:
        delay = self.get_capture_delay()
        if delay > 0:
            time.sleep(delay)
        return self.try_capture_screenshot(record.final_url or record.target, screenshot_dir, record.record_id)

    def extract_form_details(self, soup: BeautifulSoup) -> tuple[str, str, str]:
        form = None
        for candidate in soup.find_all("form"):
            if candidate.find("input", {"type": re.compile("password", re.I)}):
                form = candidate
                break
        if form is None:
            form = soup.find("form")
        if form is None:
            return "", "", ""

        action = (form.get("action") or "").strip()
        method = (form.get("method") or "GET").strip().upper()
        fields = []
        for tag in form.find_all(["input", "select", "textarea"]):
            input_type = (tag.get("type") or tag.name).strip().lower()
            name = (tag.get("name") or tag.get("id") or tag.get("placeholder") or "").strip()
            if not name and input_type == "hidden":
                continue
            label = f"{input_type}:{name}" if name else input_type
            fields.append(label[:48])
        return action[:120], method[:12], " | ".join(fields[:8])

    def extract_title(self, soup: BeautifulSoup) -> str:
        title_tag = soup.find("title")
        if title_tag and title_tag.text:
            return title_tag.text.strip()[:80]
        header = soup.find(["h1", "h2"])
        if header and header.text:
            return header.text.strip()[:80]
        return "无标题"

    def collect_text_blob(self, soup: BeautifulSoup) -> str:
        parts = [soup.get_text(" ", strip=True)]
        for tag in soup.find_all(["input", "button", "label", "a"]):
            for attr in ("placeholder", "value", "name", "id", "aria-label"):
                value = tag.get(attr)
                if value:
                    parts.append(str(value))
        return " ".join(parts).lower()

    def contains_any(self, text_blob: str, keywords: list[str]) -> bool:
        lower_keywords = [kw.lower() for kw in keywords]
        return any(keyword in text_blob for keyword in lower_keywords)

    def has_captcha_widget(self, soup: BeautifulSoup) -> bool:
        for tag in soup.find_all(["img", "input", "div", "span"]):
            raw = " ".join(
                filter(
                    None,
                    [
                        tag.get("src"),
                        tag.get("class") and " ".join(tag.get("class")),
                        tag.get("id"),
                        tag.get("name"),
                    ],
                )
            ).lower()
            if any(keyword.lower() in raw for keyword in CAPTCHA_KEYWORDS):
                return True
        return False

    def compute_login_score(self, soup: BeautifulSoup, text_blob: str, title: str) -> int:
        score = 0
        if self.contains_any(title.lower(), LOGIN_KEYWORDS):
            score += 2
        keyword_hits = sum(1 for keyword in LOGIN_KEYWORDS if keyword.lower() in text_blob)
        score += min(keyword_hits, 3)
        forms = soup.find_all("form")
        if forms:
            score += 1
        if soup.find("input", {"type": re.compile("password", re.I)}):
            score += 3
        hint_hits = 0
        for tag in soup.find_all("input"):
            joined = " ".join(
                filter(None, [tag.get("placeholder"), tag.get("name"), tag.get("id"), tag.get("aria-label")])
            ).lower()
            if any(hint.lower() in joined for hint in INPUT_HINTS):
                hint_hits += 1
        score += min(hint_hits, 2)
        if self.mode_var.get() == "NLP模式":
            text_words = re.findall(r"[\u4e00-\u9fa5a-zA-Z0-9]+", text_blob)
            weight = sum(
                1
                for token in text_words
                if token in {"登录", "登陆", "用户名", "密码", "admin", "login", "account", "password"}
            )
            score += min(weight, 4)
        return score

    def calculate_risk(self, record: AuditRecord) -> str:
        if not record.login_form:
            return "低"
        risk = 0
        if record.default_hint:
            risk += 3
        if not record.captcha_present:
            risk += 1
        if not record.mfa_present:
            risk += 1
        if not record.lockout_hint:
            risk += 1
        if record.password_field_count >= 1:
            risk += 1

        if risk >= 5:
            return "高"
        if risk >= 3:
            return "中"
        return "低"

    def try_capture_screenshot(self, url: str, screenshot_dir: Path, record_id: int) -> Path | None:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options as ChromeOptions
            from selenium.webdriver.chrome.service import Service as ChromeService
            from selenium.webdriver.edge.options import Options as EdgeOptions
            from selenium.webdriver.edge.service import Service as EdgeService
        except Exception:
            return None

        filename = screenshot_dir / f"{record_id:04d}.png"
        driver = None
        for browser in ("edge", "chrome"):
            try:
                if browser == "edge":
                    options = EdgeOptions()
                    options.add_argument("--headless=new")
                    options.add_argument("--ignore-certificate-errors")
                    options.add_argument("--disable-gpu")
                    options.add_argument("--window-size=1440,1080")
                    options.add_argument("--log-level=3")
                    options.add_experimental_option("excludeSwitches", ["enable-logging"])
                    service = EdgeService(log_output=os.devnull)
                    driver = webdriver.Edge(options=options, service=service)
                else:
                    options = ChromeOptions()
                    options.add_argument("--headless=new")
                    options.add_argument("--ignore-certificate-errors")
                    options.add_argument("--disable-gpu")
                    options.add_argument("--window-size=1440,1080")
                    options.add_argument("--log-level=3")
                    options.add_experimental_option("excludeSwitches", ["enable-logging"])
                    service = ChromeService(log_output=os.devnull)
                    driver = webdriver.Chrome(options=options, service=service)
                driver.set_page_load_timeout(20)
                driver.get(url)
                time.sleep(1.4)
                driver.save_screenshot(str(filename))
                return filename
            except Exception:
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = None
            finally:
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = None
        return None

    def render_html_screenshot(self, record: AuditRecord) -> str:
        if not record.screenshot_path:
            return "-"
        path = Path(record.screenshot_path)
        if not path.exists():
            return html.escape(record.screenshot_path)
        uri = path.resolve().as_uri()
        return f'<a href="{uri}" target="_blank"><img src="{uri}" alt="screenshot"><span>查看原图</span></a>'

    def get_selected_record(self) -> AuditRecord | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("提示", "请先选中一条记录。")
            return None
        return self.records_by_item.get(selection[0])

    def get_selected_record_silent(self) -> AuditRecord | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return self.records_by_item.get(selection[0])

    def open_selected_screenshot(self):
        record = self.get_selected_record()
        if not record:
            return
        if not record.screenshot_path:
            messagebox.showinfo("提示", "这条记录还没有截图。")
            return
        path = Path(record.screenshot_path)
        if not path.exists():
            messagebox.showwarning("提示", f"截图文件不存在:\n{path}")
            return
        os.startfile(path)

    def open_preview_zoom(self):
        record = self.get_selected_record_silent()
        if not record or not record.screenshot_path:
            messagebox.showinfo("提示", "当前记录没有可放大的截图。")
            return
        path = Path(record.screenshot_path)
        if not path.exists():
            messagebox.showwarning("提示", f"截图文件不存在:\n{path}")
            return

        window = tk.Toplevel(self.root)
        window.title(f"截图放大查看 - {record.title or record.target}")
        window.geometry("1280x900")
        window.transient(self.root)

        top_bar = tk.Frame(window, pady=8, padx=12)
        top_bar.pack(fill=tk.X)
        tk.Label(
            top_bar,
            text=record.final_url or record.target,
            anchor="w",
            justify=tk.LEFT,
            wraplength=1080,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(top_bar, text="打开原图", command=lambda: os.startfile(path)).pack(side=tk.RIGHT)

        canvas = tk.Canvas(window, bg="#111827", highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)

        image = Image.open(path)
        state = {"scale": 1.0, "photo": None, "image_id": None}

        def redraw():
            display = image.copy()
            scaled_size = (
                max(1, int(display.width * state["scale"])),
                max(1, int(display.height * state["scale"])),
            )
            display = display.resize(scaled_size)
            state["photo"] = ImageTk.PhotoImage(display)
            if state["image_id"] is None:
                state["image_id"] = canvas.create_image(20, 20, image=state["photo"], anchor="nw")
            else:
                canvas.itemconfig(state["image_id"], image=state["photo"])
            canvas.coords(state["image_id"], 20, 20)
            canvas.config(scrollregion=(0, 0, display.width + 40, display.height + 40))

        def zoom(step: float):
            state["scale"] = max(0.2, min(3.0, state["scale"] + step))
            redraw()

        control_bar = tk.Frame(window, pady=8, padx=12)
        control_bar.pack(fill=tk.X)
        tk.Button(control_bar, text="放大", command=lambda: zoom(0.2)).pack(side=tk.LEFT)
        tk.Button(control_bar, text="缩小", command=lambda: zoom(-0.2)).pack(side=tk.LEFT, padx=(6, 0))
        tk.Button(control_bar, text="重置", command=lambda: reset_zoom()).pack(side=tk.LEFT, padx=(6, 0))

        def reset_zoom():
            state["scale"] = 1.0
            redraw()

        def on_mouse_wheel(event):
            if event.delta > 0:
                zoom(0.1)
            elif event.delta < 0:
                zoom(-0.1)

        def start_pan(event):
            canvas.scan_mark(event.x, event.y)

        def drag_pan(event):
            canvas.scan_dragto(event.x, event.y, gain=1)

        canvas.bind("<Configure>", lambda _event: redraw())
        canvas.bind("<MouseWheel>", on_mouse_wheel)
        canvas.bind("<ButtonPress-1>", start_pan)
        canvas.bind("<B1-Motion>", drag_pan)
        window.bind("<Control-plus>", lambda _event: zoom(0.2))
        window.bind("<Control-minus>", lambda _event: zoom(-0.2))
        reset_zoom()

    def open_selected_target(self):
        record = self.get_selected_record()
        if not record:
            return
        target = record.final_url or record.target
        if not target:
            messagebox.showinfo("提示", "这条记录没有可打开的地址。")
            return
        webbrowser.open(target)

    def open_output_dir(self):
        os.startfile(self.output_dir)

    def open_project_manager(self):
        window = tk.Toplevel(self.root)
        window.title("工程列表")
        window.geometry("860x520")
        window.transient(self.root)
        window.grab_set()

        search_frame = tk.Frame(window, pady=10, padx=12)
        search_frame.pack(fill=tk.X)
        tk.Label(search_frame, text="搜索工程:").pack(side=tk.LEFT)
        search_var = tk.StringVar()
        search_entry = tk.Entry(search_frame, textvariable=search_var, width=38)
        search_entry.pack(side=tk.LEFT, padx=(6, 8))

        columns = ("工程名", "源文件", "保存时间", "已完成", "待处理")
        tree = ttk.Treeview(window, columns=columns, show="headings", height=16)
        for name, width in [
            ("工程名", 180),
            ("源文件", 360),
            ("保存时间", 150),
            ("已完成", 70),
            ("待处理", 70),
        ]:
            tree.heading(name, text=name)
            tree.column(name, width=width, anchor=tk.W if name in {"工程名", "源文件"} else tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        status_var = tk.StringVar(value="双击加载工程，删除前请确认当前未在扫描。")
        tk.Label(window, textvariable=status_var, anchor="w").pack(fill=tk.X, padx=12)

        button_bar = tk.Frame(window, pady=10)
        button_bar.pack(fill=tk.X, padx=12)

        projects = self.get_project_summaries()
        item_map = {}

        def populate_projects(query: str = ""):
            item_map.clear()
            for item in tree.get_children():
                tree.delete(item)
            keyword = query.strip().lower()
            filtered = [
                project
                for project in projects
                if not keyword
                or keyword in project["name"].lower()
                or keyword in project["source_file"].lower()
            ]
            for project in filtered:
                item_id = tree.insert(
                    "",
                    tk.END,
                    values=(
                        project["name"],
                        project["source_file"],
                        project["saved_at"],
                        project["completed"],
                        project["pending"],
                    ),
                )
                item_map[item_id] = project
            status_var.set(f"共 {len(filtered)} 个工程，双击可直接加载。")

        populate_projects()
        search_var.trace_add("write", lambda *_args: populate_projects(search_var.get()))
        search_entry.focus_set()

        def get_selected_project():
            selection = tree.selection()
            if not selection:
                messagebox.showinfo("提示", "请先选中一个工程。", parent=window)
                return None
            return item_map.get(selection[0])

        def load_selected():
            project = get_selected_project()
            if not project:
                return
            self.load_project_file(Path(project["path"]))
            self.write_latest_project_pointer(project["saved_at"])
            self.log_message(f"[*] 已切换工程: {project['name']}")
            window.destroy()

        def delete_selected():
            project = get_selected_project()
            if not project:
                return
            if self.is_scanning:
                messagebox.showwarning("提示", "请先停止当前扫描，再删除工程。", parent=window)
                return
            confirmed = messagebox.askyesno(
                "确认删除",
                f"确定删除工程吗？\n\n{project['name']}\n{project['source_file']}",
                parent=window,
            )
            if not confirmed:
                return
            try:
                Path(project["path"]).unlink(missing_ok=True)
                selected = tree.selection()[0]
                projects.remove(project)
                populate_projects(search_var.get())
                status_var.set(f"已删除工程: {project['name']}")
            except Exception as exc:
                messagebox.showerror("错误", f"删除工程失败: {exc}", parent=window)

        def open_projects_dir():
            os.startfile(self.projects_dir)

        tk.Button(button_bar, text="加载工程", command=load_selected).pack(side=tk.LEFT)
        tk.Button(button_bar, text="删除工程", command=delete_selected).pack(side=tk.LEFT, padx=(6, 0))
        tk.Button(button_bar, text="打开工程目录", command=open_projects_dir).pack(side=tk.LEFT, padx=(6, 0))
        tk.Button(button_bar, text="关闭", command=window.destroy).pack(side=tk.RIGHT)
        tree.bind("<Double-1>", lambda _event: load_selected())

    def get_project_summaries(self) -> list[dict]:
        summaries = []
        for path in sorted(self.projects_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                records = payload.get("records") or []
                completed = sum(1 for record in records if record.get("status") == "已完成")
                total = len(records)
                source_file = payload.get("source_file", "")
                summaries.append(
                    {
                        "name": Path(source_file).stem if source_file else path.stem,
                        "source_file": source_file or str(path),
                        "saved_at": payload.get("saved_at", "-"),
                        "completed": completed,
                        "pending": max(0, total - completed),
                        "path": str(path),
                    }
                )
            except Exception:
                continue
        return summaries

    def on_tree_double_click(self, _event):
        record = self.get_selected_record()
        if not record:
            return
        if record.screenshot_path and Path(record.screenshot_path).exists():
            self.open_selected_screenshot()
        else:
            self.open_selected_target()

    def on_tree_select(self, _event):
        record = self.get_selected_record_silent()
        self.update_preview(record)

    def update_preview(self, record: AuditRecord | None):
        if not record:
            self.preview_title_var.set("未选择记录")
            self.preview_meta_var.set("目标、风险、最终URL 会显示在这里")
            self.preview_image = None
            self.preview_image_label.config(image="", text="暂无截图")
            self.set_preview_text("")
            return

        self.preview_title_var.set(record.title or record.target)
        meta_lines = [
            f"目标: {record.target}",
            f"最终URL: {record.final_url or '-'}",
            f"状态: {record.status}    风险: {record.risk_level}",
        ]
        self.preview_meta_var.set("\n".join(meta_lines))
        self.load_preview_image(record.screenshot_path)
        details = [
            f"表单方法: {record.form_method or '-'}",
            f"表单Action: {record.form_action or '-'}",
            f"字段摘要: {record.field_summary or '-'}",
            f"登录评分: {record.login_score}",
            f"密码框数量: {record.password_field_count}",
            f"验证码: {'是' if record.captcha_present else '否'}",
            f"MFA: {'是' if record.mfa_present else '否'}",
            f"锁定提示: {'是' if record.lockout_hint else '否'}",
            f"默认账号提示: {'是' if record.default_hint else '否'}",
            f"审计结果: {record.result}",
            f"错误信息: {record.error or '-'}",
        ]
        self.set_preview_text("\n".join(details))

    def load_preview_image(self, screenshot_path: str):
        if not screenshot_path or not Path(screenshot_path).exists():
            self.preview_image = None
            self.preview_image_label.config(image="", text="暂无截图")
            return
        try:
            image = Image.open(screenshot_path)
            self.preview_image_label.update_idletasks()
            max_width = max(420, self.preview_image_label.winfo_width() - 20)
            max_height = max(300, self.preview_image_label.winfo_height() - 20)
            image.thumbnail((max_width, max_height))
            self.preview_image = ImageTk.PhotoImage(image)
            self.preview_image_label.config(image=self.preview_image, text="")
        except Exception:
            self.preview_image = None
            self.preview_image_label.config(image="", text="截图加载失败")

    def set_preview_text(self, text: str):
        self.preview_text.config(state=tk.NORMAL)
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert(tk.END, text)
        self.preview_text.config(state=tk.DISABLED)

    def update_summary(self):
        total = len(self.all_records)
        completed = sum(1 for record in self.all_records if record.status == "已完成")
        pending = sum(1 for record in self.all_records if record.status != "已完成")
        high = sum(1 for record in self.all_records if record.risk_level == "高")
        medium = sum(1 for record in self.all_records if record.risk_level == "中")
        low = sum(1 for record in self.all_records if record.risk_level == "低")
        screenshots = sum(1 for record in self.all_records if record.screenshot_path)
        self.summary_var.set(
            f"摘要: 总计 {total} | 已完成 {completed} | 待处理 {pending} | 高 {high} | 中 {medium} | 低 {low} | 有截图 {screenshots}"
        )

    def ensure_selection(self):
        children = self.tree.get_children()
        if not children:
            self.update_preview(None)
            return
        selected = self.tree.selection()
        if not selected or selected[0] not in children:
            self.tree.selection_set(children[0])
            self.tree.focus(children[0])
            self.update_preview(self.records_by_item.get(children[0]))

    def select_relative_record(self, offset: int):
        children = list(self.tree.get_children())
        if not children:
            return
        selected = self.tree.selection()
        if selected and selected[0] in children:
            current_index = children.index(selected[0])
        else:
            current_index = 0
        next_index = max(0, min(len(children) - 1, current_index + offset))
        next_item = children[next_index]
        self.tree.selection_set(next_item)
        self.tree.focus(next_item)
        self.tree.see(next_item)
        self.update_preview(self.records_by_item.get(next_item))

    def find_item_id_by_record(self, target_record: AuditRecord):
        for item_id, record in self.records_by_item.items():
            if record is target_record:
                return item_id
        return None

    def tag_for_record(self, record: AuditRecord) -> str:
        if record.risk_level == "高":
            return "risk_high"
        if record.risk_level == "中":
            return "risk_medium"
        if record.risk_level == "失败":
            return "status_error"
        return "risk_low"

    def update_tree_item(self, item_id, status: str, title: str, risk_level: str, result: str):
        current = self.tree.item(item_id)["values"]
        record = self.records_by_item.get(item_id)
        tags = (self.tag_for_record(record),) if record else ()
        self.tree.item(item_id, values=(current[0], current[1], status, title, risk_level, result), tags=tags)

    def reset_scan_button(self):
        self.is_scanning = False
        self.btn_start.config(text="开始审计", bg="#9ed0ff")
        self.toggle_proxy_state()
        self.save_progress_snapshot(reason="ui-reset")

    def get_records_snapshot(self) -> list[AuditRecord]:
        with self.result_lock:
            return list(self.filtered_records())

    def save_progress_snapshot(self, reason: str = "autosave"):
        if not self.current_project_path and self.last_loaded_path:
            self.current_project_path = self.project_path_for_source(self.last_loaded_path)
        if not self.current_project_path:
            return
        try:
            payload = {
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "reason": reason,
                "source_file": self.last_loaded_path,
                "mode": self.mode_var.get(),
                "use_proxy": self.use_proxy_var.get(),
                "proxy": self.proxy_entry.get().strip(),
                "capture": self.capture_var.get(),
                "capture_policy": self.capture_policy_var.get(),
                "capture_delay": self.capture_delay_var.get(),
                "follow_redirect": self.follow_redirect_var.get(),
                "workers": self.worker_var.get(),
                "filter": self.filter_var.get(),
                "is_scanning": self.is_scanning,
                "records": [asdict(record) for record in self.all_records],
            }
            self.current_project_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.current_project_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.current_project_path)
            self.write_latest_project_pointer(payload.get("saved_at", ""))
        except Exception as exc:
            self.log_queue.put(f"[~] 自动保存失败: {exc}")

    def load_autosave_if_exists(self):
        if not self.latest_project_path.exists():
            return
        try:
            with open(self.latest_project_path, "r", encoding="utf-8") as handle:
                latest = json.load(handle)
            project_file = latest.get("project_file", "")
            if not project_file:
                return
            self.load_project_file(Path(project_file))
        except Exception as exc:
            self.log_message(f"[~] 读取自动保存进度失败: {exc}")

    def on_close(self):
        self.is_scanning = False
        self.save_progress_snapshot(reason="window-close")
        self.root.destroy()

    def project_path_for_source(self, source_file: str) -> Path:
        source = str(Path(source_file).resolve())
        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
        safe_name = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fa5-]+", "_", Path(source).stem)[:48] or "project"
        return self.projects_dir / f"{safe_name}_{digest}.json"

    def write_latest_project_pointer(self, saved_at: str):
        payload = {
            "project_file": str(self.current_project_path),
            "source_file": self.last_loaded_path,
            "saved_at": saved_at,
        }
        with open(self.latest_project_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def load_project_file(self, project_path: Path):
        with open(project_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        records = payload.get("records") or []
        if not records:
            return
        self.current_project_path = project_path
        self.all_records = [AuditRecord(**record) for record in records]
        for record in self.all_records:
            if record.status == "扫描中...":
                record.status = "等待中"
        self.last_loaded_path = payload.get("source_file", "")
        if self.last_loaded_path:
            self.file_entry.delete(0, tk.END)
            self.file_entry.insert(0, self.last_loaded_path)
        self.mode_var.set(payload.get("mode", "规则模式"))
        self.use_proxy_var.set(bool(payload.get("use_proxy", False)))
        self.proxy_entry.config(state=tk.NORMAL)
        self.proxy_entry.delete(0, tk.END)
        self.proxy_entry.insert(0, payload.get("proxy", "127.0.0.1:8080"))
        self.capture_var.set(bool(payload.get("capture", True)))
        self.capture_policy_var.set(payload.get("capture_policy", "命中项"))
        self.capture_delay_var.set(str(payload.get("capture_delay", "0.4")))
        self.follow_redirect_var.set(bool(payload.get("follow_redirect", True)))
        self.worker_var.set(str(payload.get("workers", "4")))
        self.filter_var.set(payload.get("filter", "全部"))
        self.toggle_proxy_state()
        self.rebuild_tree()
        self.ensure_selection()
        completed = sum(1 for record in self.all_records if record.status == "已完成")
        pending = len(self.all_records) - completed
        self.log_message(
            f"[*] 已恢复工程: {Path(self.last_loaded_path).name if self.last_loaded_path else project_path.name} "
            f"| 已完成 {completed} 条 | 待继续 {pending} 条 | 保存时间 {payload.get('saved_at', '-')}"
        )

    def log_message(self, message: str):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def check_log_queue(self):
        try:
            while True:
                self.log_message(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.check_log_queue)


if __name__ == "__main__":
    root = tk.Tk()
    app = SecurityAuditGUI(root)
    root.mainloop()
