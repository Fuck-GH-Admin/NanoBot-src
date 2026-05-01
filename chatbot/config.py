import json
import os
import secrets
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Set, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 配置文件路径
CONFIG_FILE = Path("config_bot_base.yaml")
WEB_PORT = 8081


class GroupSettings(BaseModel):
    """群级配置"""
    model_config = ConfigDict(extra="ignore")

    random_reply_prob: float = Field(default=0.0, description="随机插嘴概率 (0.0~1.0)")
    record_all_messages: bool = Field(default=True, description="是否记录所有非@消息")
    allowed_tools: list[str] = Field(
        default_factory=lambda: ["search_acg_image", "recommend_book"],
        description="允许该群使用的工具列表"
    )
    allow_r18: bool = Field(default=False, description="是否允许该群访问 R18 内容")


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

    # Node.js 引擎
    node_chat_url: str = "http://127.0.0.1:3010/api/chat"
    node_deepseek_api_key: str = ""
    node_base_url: str = "https://api.deepseek.com"
    node_model: str = "deepseek-chat"
    node_temperature: float = 0.7

    # Agent 循环
    agent_max_loops: int = 10
    agent_request_timeout: float = 60.0

    # Feature Flags（灰度开关）
    enable_strict_schema: bool = True       # 阶段一：跨端 Schema 校验
    enable_task_queue: bool = False         # 阶段二：高可用任务队列
    enable_dynamic_loop: bool = False       # 阶段二：智能循环终止
    entity_relation_enabled: bool = False   # 阶段三：知识图谱实体/关系提取
    semantic_lorebook_enabled: bool = False  # 阶段三：语义向量检索
    token_arbitration_enabled: bool = False  # 阶段三：Token 预算仲裁

    # 路径
    image_folder: str = r"D:\小项目\pixiv下载图片\pixiv下载图片\最终版\pixiv_downloads"
    books_folder: str = r"D:\文件\学习资料\本"
    excel_path: str = r"D:\小项目\pixiv下载图片\pixiv下载图片\最终版\pixiv_downloads\pixiv_artworks_fix.xlsx"
    jm_download_dir: str = r"data/jm_temp"
    jm_option_path: str = r"data/option.yml"
    font_path: str = r"C:\Windows\Fonts\msyh.ttc"

    # 权限集合
    superusers: Set[str] = set()
    private_whitelist: Set[str] = set()
    ai_admin_qq: Set[str] = set()
    drawing_whitelist: Set[str] = set()
    welcome_groups: Set[str] = set()

    welcome_mode: str = "all"

    group_configs: dict[str, GroupSettings] = {}


class ConfigManager:
    """负责从 YAML 加载配置、热更新、提供统一的属性访问"""

    def __init__(self):
        self._config = Config()
        self._lock = threading.Lock()
        self._observer: Optional[Observer] = None
        self._admin_token: str = secrets.token_urlsafe(32)
        self.load_config()
        self._start_watcher()

    def get_admin_token(self) -> str:
        return self._admin_token

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

                # group_configs 特殊处理：从 dict 转为 GroupSettings 对象
                if key == "group_configs" and isinstance(value, dict):
                    converted = {}
                    for gid, gcfg in value.items():
                        if isinstance(gcfg, dict):
                            converted[gid] = GroupSettings(**gcfg)
                        elif isinstance(gcfg, GroupSettings):
                            converted[gid] = gcfg
                    setattr(self._config, key, converted)
                    continue

                # 类型转换
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
                except (ValueError, TypeError):
                    continue

                setattr(self._config, key, value)

    def save_config(self):
        """将当前配置序列化并原子写回 YAML 文件"""
        import tempfile

        data = {}
        try:
            with self._lock:
                for name, field_info in Config.model_fields.items():
                    val = getattr(self._config, name)
                    if isinstance(val, set):
                        val = list(val)
                    elif name == 'group_configs':
                        val = {k: v.model_dump() if isinstance(v, GroupSettings) else v
                               for k, v in val.items()}
                    data[name] = val

            tmp_fd, tmp_path = tempfile.mkstemp(dir=CONFIG_FILE.parent,
                                                 prefix=CONFIG_FILE.stem + '_',
                                                 suffix='.tmp')
            try:
                with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                    yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
                os.replace(tmp_path, CONFIG_FILE)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                raise
        except Exception as e:
            print(f"[Config] 保存配置失败: {e}")
            raise

    def _generate_default_yaml(self):
        """生成初始配置文件"""
        default = {
            "deepseek_api_key": "",
            "deepseek_api_url": "https://api.deepseek.com/chat/completions",
            "deepseek_model_name": "deepseek-v4-flash",
            "node_chat_url": "http://127.0.0.1:3010/api/chat",
            "node_deepseek_api_key": "",
            "node_model": "deepseek-chat",
            "node_temperature": 0.7,
            "agent_max_loops": 10,
            "agent_request_timeout": 60.0,
            "enable_strict_schema": True,
            "enable_task_queue": False,
            "enable_dynamic_loop": False,
            "entity_relation_enabled": False,
            "semantic_lorebook_enabled": False,
            "token_arbitration_enabled": False,
            "siliconflow_api_key": "",
            "siliconflow_api_url": "https://api.siliconflow.cn/v1",
            "siliconflow_model_name": "Kwai-Kolors/Kolors",
            "image_folder": r"D:\小项目\pixiv下载图片\pixiv下载图片\最终版\pixiv_downloads",
            "books_folder": r"D:\文件\学习资料\本",
            "excel_path": r"D:\小项目\pixiv下载图片\pixiv下载图片\最终版\pixiv_downloads\pixiv_artworks_fix.xlsx",
            "jm_download_dir": "data/jm_temp",
            "jm_option_path": "data/option.yml",
            "font_path": r"C:\Windows\Fonts\msyh.ttc",
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

    # 代理内部 Config 的属性访问
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
class ConfigAPIHandler(BaseHTTPRequestHandler):
    manager: ConfigManager = None

    def _check_auth(self) -> bool:
        auth_header = self.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            self.send_response(403)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Missing or invalid token"}).encode())
            return False
        token = auth_header[7:]
        if not secrets.compare_digest(token, self.manager.get_admin_token()):
            self.send_response(403)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid token"}).encode())
            return False
        return True

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            # 动态读取 web/index.html，注入鉴权 Token
            html_path = Path(__file__).parent / "web" / "index.html"
            try:
                with open(html_path, 'r', encoding='utf-8') as f:
                    html_content = f.read()
                token = self.manager.get_admin_token()
                html_content = html_content.replace(
                    '</head>',
                    f'<meta name="admin-token" content="{token}"></head>'
                )
                self.send_response(200)
                self.send_header('Content-type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(html_content.encode('utf-8'))
            except FileNotFoundError:
                self.send_error(404, "index.html not found. Please create web/index.html")
        elif self.path == '/api/config':
            if not self._check_auth():
                return
            config_data = {}
            with self.manager._lock:
                for name, field_info in Config.model_fields.items():
                    val = getattr(self.manager._config, name)
                    if isinstance(val, set):
                        val = list(val)
                    elif name == 'group_configs':
                        val = {k: v.model_dump() if isinstance(v, GroupSettings) else v for k, v in val.items()}
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
        if not self._check_auth():
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

        processed = {}
        errors = []
        for key, value in new_data.items():
            if key not in Config.model_fields:
                continue
            if key == 'group_configs':
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        errors.append("group_configs: Invalid JSON string")
                        continue
                if isinstance(value, dict):
                    processed[key] = {}
                    for gid, gcfg in value.items():
                        try:
                            processed[key][gid] = GroupSettings(**gcfg).model_dump()
                        except Exception as e:
                            errors.append(f"group_configs[{gid}]: {e}")
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

        current = {}
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                current = yaml.safe_load(f) or {}
        current.update(processed)

        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                yaml.dump(current, f, allow_unicode=True, default_flow_style=False)
            self.manager.load_config()
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
        pass


def start_config_web_server(manager: ConfigManager, port: int = WEB_PORT):
    ConfigAPIHandler.manager = manager
    server = HTTPServer(('127.0.0.1', port), ConfigAPIHandler)
    print(f"⚙️  配置管理面板已启动: http://127.0.0.1:{port}")
    server.serve_forever()


# 全局配置实例
plugin_config = ConfigManager()
