import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Set, Optional, Any

import yaml
from pydantic import ConfigDict
from pydantic_settings import BaseSettings
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 配置文件路径（可自定义）
CONFIG_FILE = Path("config_bot_base.yaml")
# 管理面板端口（不要和 NoneBot 的端口冲突，这里用 8081）
WEB_PORT = 8081


class Config(BaseSettings):
    """配置模型定义（全部从 YAML 读取，无 env 依赖）"""
    model_config = ConfigDict(extra="ignore")

    # DeepSeek
    deepseek_api_key: str = ""
    deepseek_api_url: str = "https://api.deepseek.com/chat/completions"
    deepseek_model_name: str = "deepseek-v4-flash"

    # SiliconFlow (画图)
    siliconflow_api_key: str = ""
    siliconflow_api_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_model_name: str = "Kwai-Kolors/Kolors"

    # 开关 / 超时
    enable_ai_command_fixer: bool = True
    ai_command_timeout: float = 9.0

    # 路径
    image_folder: str = r"D:\小项目\pixiv下载图片\pixiv下载图片\最终版\pixiv_downloads"
    books_folder: str = r"D:\文件\学习资料\本"
    excel_path: str = r"D:\小项目\pixiv下载图片\pixiv下载图片\最终版\pixiv_downloads\pixiv_artworks_fix.xlsx"
    jm_download_dir: str = r"data/jm_temp"
    jm_option_path: str = r"data/option.yml"
    font_path: str = r"C:\Windows\Fonts\msyh.ttc"

    # 杂项
    short_message_max_len: int = 2

    # 权限集合
    superusers: Set[str] = set()
    private_whitelist: Set[str] = set()
    ai_admin_qq: Set[str] = set()
    drawing_whitelist: Set[str] = set()
    welcome_groups: Set[str] = set()

    welcome_mode: str = "all"


class ConfigManager:
    """负责从 YAML 加载配置、热更新、提供统一的属性访问"""

    def __init__(self):
        self._config = Config()
        self._lock = threading.Lock()
        self._observer: Optional[Observer] = None
        self.load_config()
        self._start_watcher()

    def load_config(self):
        """读取 config.yaml 并更新内部 Config 对象"""
        if not CONFIG_FILE.exists():
            self._generate_default_yaml()
            return

        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        with self._lock:
            for key, value in data.items():
                if key not in Config.model_fields:
                    continue
                field_info = Config.model_fields[key]
                target_type = field_info.annotation

                # 类型转换：YAML 读取的值可能不是精确类型
                try:
                    if target_type is Set[str]:
                        if isinstance(value, list):
                            value = set(value)
                        elif isinstance(value, str):
                            value = set(s.strip() for s in value.split(",") if s.strip())
                        else:
                            value = set()
                    elif target_type is bool:
                        value = bool(value)
                    elif target_type is float:
                        value = float(value)
                    elif target_type is int:
                        value = int(value)
                    # 其他情况保持原样（字符串）
                except (ValueError, TypeError):
                    continue  # 类型转换失败则跳过该字段

                setattr(self._config, key, value)

    def _generate_default_yaml(self):
        """生成初始配置文件"""
        default = {
            "deepseek_api_key": "",
            "deepseek_api_url": "https://api.deepseek.com/chat/completions",
            "deepseek_model_name": "deepseek-v4-flash",
            "siliconflow_api_key": "",
            "siliconflow_api_url": "https://api.siliconflow.cn/v1",
            "siliconflow_model_name": "Kwai-Kolors/Kolors",
            "enable_ai_command_fixer": True,
            "ai_command_timeout": 9.0,
            "image_folder": r"D:\小项目\pixiv下载图片\pixiv下载图片\最终版\pixiv_downloads",
            "books_folder": r"D:\文件\学习资料\本",
            "excel_path": r"D:\小项目\pixiv下载图片\pixiv下载图片\最终版\pixiv_downloads\pixiv_artworks_fix.xlsx",
            "jm_download_dir": "data/jm_temp",
            "jm_option_path": "data/option.yml",
            "font_path": r"C:\Windows\Fonts\msyh.ttc",
            "short_message_max_len": 2,
            "superusers": [],
            "private_whitelist": [],
            "ai_admin_qq": [],
            "drawing_whitelist": [],
            "welcome_groups": [],
            "welcome_mode": "all"
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml.dump(default, f, allow_unicode=True, default_flow_style=False)
        self.load_config()

    def _start_watcher(self):
        """监控配置文件变化，自动热更新"""

        class Handler(FileSystemEventHandler):
            def __init__(self, manager):
                self.manager = manager

            def on_modified(self, event):
                if event.src_path.endswith(CONFIG_FILE.name):
                    time.sleep(0.5)
                    self.manager.load_config()

        observer = Observer()
        observer.schedule(Handler(self), path=str(CONFIG_FILE.parent), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer

    # 代理内部 Config 的属性访问（让外部像往常一样 plugin_config.xxx 调用）
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._config, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_config", "_lock", "_observer"):
            super().__setattr__(name, value)
        else:
            setattr(self._config, name, value)


# ---------- Web 配置管理面板 ----------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <title>配置管理</title>
    <style>
        body { font-family: sans-serif; max-width: 700px; margin: 2rem auto; }
        label { display: block; margin-top: 1rem; font-weight: bold; }
        input, textarea, select { width: 100%; padding: 0.5rem; margin-top: 0.2rem; }
        textarea { resize: vertical; }
        .help { font-size: 0.8rem; color: #666; }
        button { margin-top: 1.5rem; padding: 0.7rem 1.5rem; font-size: 1rem; }
        .msg { padding: 0.5rem; margin: 1rem 0; border-radius: 4px; }
        .success { background: #d4edda; color: #155724; }
        .error { background: #f8d7da; color: #721c24; }
    </style>
</head>
<body>
    <h1>⚙️ 配置文件管理</h1>
    <p class="help">修改后点击保存，Bot 自动热更新。</p>
    <div id="message"></div>
    <form id="configForm"></form>
    <script>
        const fields = [
            {key:"deepseek_api_key", label:"DeepSeek API Key", type:"text"},
            {key:"deepseek_api_url", label:"DeepSeek API URL", type:"text"},
            {key:"deepseek_model_name", label:"DeepSeek 模型名", type:"text"},
            {key:"siliconflow_api_key", label:"SiliconFlow API Key", type:"text"},
            {key:"siliconflow_api_url", label:"SiliconFlow API URL", type:"text"},
            {key:"siliconflow_model_name", label:"SiliconFlow 模型名", type:"text"},
            {key:"enable_ai_command_fixer", label:"启用 AI 指令修正", type:"checkbox"},
            {key:"ai_command_timeout", label:"AI 指令超时 (秒)", type:"number", step:"0.1"},
            {key:"image_folder", label:"图片存放目录", type:"text"},
            {key:"books_folder", label:"本子最终保存目录", type:"text"},
            {key:"excel_path", label:"Excel 文件路径", type:"text"},
            {key:"jm_download_dir", label:"JM 临时下载目录", type:"text"},
            {key:"jm_option_path", label:"JM 配置路径", type:"text"},
            {key:"font_path", label:"字体路径", type:"text"},
            {key:"short_message_max_len", label:"短消息最大长度", type:"number"},
            {key:"superusers", label:"超级用户 (逗号分隔)", type:"text", isList:true},
            {key:"private_whitelist", label:"私聊白名单", type:"text", isList:true},
            {key:"ai_admin_qq", label:"AI管理员", type:"text", isList:true},
            {key:"drawing_whitelist", label:"绘图白名单", type:"text", isList:true},
            {key:"welcome_groups", label:"欢迎/群管理群号", type:"text", isList:true},
            {key:"welcome_mode", label:"欢迎模式", type:"select", opts:["all","hello","bye"]}
        ];

        async function load() {
            const resp = await fetch('/api/config');
            const data = await resp.json();
            const form = document.getElementById('configForm');
            form.innerHTML = '';
            fields.forEach(f => {
                let val = data[f.key];
                const div = document.createElement('div');
                div.innerHTML = `<label>${f.label}</label>`;
                if (f.type === 'select') {
                    const sel = document.createElement('select');
                    sel.name = f.key;
                    f.opts.forEach(o => {
                        const opt = document.createElement('option');
                        opt.value = o; opt.textContent = o;
                        if (o === val) opt.selected = true;
                        sel.appendChild(opt);
                    });
                    div.appendChild(sel);
                } else if (f.type === 'checkbox') {
                    const cb = document.createElement('input');
                    cb.type = 'checkbox'; cb.name = f.key;
                    cb.checked = !!val;
                    div.appendChild(cb);
                } else {
                    const inp = document.createElement('input');
                    inp.type = f.type; inp.name = f.key;
                    if (f.isList && Array.isArray(val)) val = val.join(',');
                    inp.value = val ?? '';
                    if (f.step) inp.step = f.step;
                    div.appendChild(inp);
                }
                form.appendChild(div);
            });
            const btn = document.createElement('button');
            btn.type = 'submit'; btn.textContent = '💾 保存配置';
            form.appendChild(btn);
        }

        document.addEventListener('DOMContentLoaded', load);

        document.getElementById('configForm').addEventListener('submit', async e => {
            e.preventDefault();
            const fd = new FormData(e.target);
            const payload = {};
            fields.forEach(f => {
                if (f.type === 'checkbox') {
                    payload[f.key] = fd.get(f.key) === 'on';
                } else if (f.isList) {
                    const raw = fd.get(f.key) || '';
                    payload[f.key] = raw.split(',').map(s => s.trim()).filter(Boolean);
                } else if (f.type === 'number' || f.key === 'ai_command_timeout') {
                    payload[f.key] = parseFloat(fd.get(f.key));
                } else if (f.key === 'short_message_max_len') {
                    payload[f.key] = parseInt(fd.get(f.key));
                } else {
                    payload[f.key] = fd.get(f.key) || '';
                }
            });
            try {
                const resp = await fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                const result = await resp.json();
                const msg = document.getElementById('message');
                if (resp.ok) {
                    msg.innerHTML = '<div class="msg success">✅ 配置已保存，Bot 自动热更新。</div>';
                } else {
                    msg.innerHTML = `<div class="msg error">❌ 保存失败: ${result.error || '未知错误'}</div>`;
                }
            } catch {
                document.getElementById('message').innerHTML = '<div class="msg error">❌ 网络错误</div>';
            }
        });
    </script>
</body>
</html>"""


class ConfigAPIHandler(BaseHTTPRequestHandler):
    manager: ConfigManager = None

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode('utf-8'))
        elif self.path == '/api/config':
            config_data = {}
            with self.manager._lock:
                for name, field_info in Config.model_fields.items():
                    val = getattr(self.manager._config, name)
                    if isinstance(val, set):
                        val = list(val)
                    config_data[name] = val
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(config_data, ensure_ascii=False).encode('utf-8'))
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != '/api/config':
            self.send_error(404)
            return
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            new_data = json.loads(body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        # 类型校验与转换
        processed = {}
        errors = []
        for key, value in new_data.items():
            if key not in Config.model_fields:
                continue
            field_info = Config.model_fields[key]
            target_type = field_info.annotation
            try:
                if target_type is Set[str]:
                    if isinstance(value, list):
                        processed[key] = value
                    elif isinstance(value, str):
                        processed[key] = [s.strip() for s in value.split(",") if s.strip()]
                    else:
                        raise ValueError("应为列表或逗号分隔字符串")
                elif target_type is bool:
                    processed[key] = bool(value)
                elif target_type is float:
                    processed[key] = float(value)
                elif target_type is int:
                    processed[key] = int(value)
                else:
                    processed[key] = str(value)
            except (ValueError, TypeError) as e:
                errors.append(f"{key}: {e}")

        if errors:
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "; ".join(errors)}, ensure_ascii=False).encode())
            return

        # 合并到现有 YAML 并写回
        current = {}
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                current = yaml.safe_load(f) or {}
        current.update(processed)

        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                yaml.dump(current, f, allow_unicode=True, default_flow_style=False)
            self.manager.load_config()  # 立即热更（watchdog 也会触发，双保险）
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"无法写入文件: {e}"}, ensure_ascii=False).encode())
            return

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode())

    def log_message(self, format, *args):
        pass  # 关闭日志打印


def start_config_web_server(manager: ConfigManager, port: int = WEB_PORT):
    """启动 Web 配置管理服务器（阻塞式，请在单独线程中运行）"""
    ConfigAPIHandler.manager = manager
    server = HTTPServer(('127.0.0.1', port), ConfigAPIHandler)
    print(f"⚙️  配置管理面板已启动: http://127.0.0.1:{port}")
    server.serve_forever()


# 全局配置实例（替代原来的 plugin_config）
plugin_config = ConfigManager()