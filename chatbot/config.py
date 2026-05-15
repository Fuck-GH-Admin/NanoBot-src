import asyncio
import copy
import json
import os
import platform
import secrets
import threading
import time
from pathlib import Path
from typing import Optional, Set, Any

import yaml
from nonebot.log import logger
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings

from .schemas import CommandResult
from .utils.path_utils import get_project_root, CONFIG_DIR
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 配置文件路径
CONFIG_FILE = CONFIG_DIR / "config_bot_base.yaml"
WEB_PORT = 8081


def _default_font_path() -> str:
    """按操作系统返回默认中文字体路径，找不到则返回空字符串。"""
    if platform.system() == "Windows":
        win_font = r"C:\Windows\Fonts\msyh.ttc"
        return win_font if Path(win_font).exists() else ""
    # Linux / macOS
    candidates = [
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return ""


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
    deepseek_memory_model_name: str = "deepseek-v4-flash"
    logic_model_name: str = ""              # 逻辑脑模型，为空则使用 deepseek_model_name
    enable_dual_brain: bool = True          # 双脑模式开关

    # SiliconFlow (画图 + Embedding + Reranker)
    siliconflow_api_key: str = ""
    siliconflow_api_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_model_name: str = "Kwai-Kolors/Kolors"
    embedding_model_name: str = "BAAI/bge-m3"
    reranker_model_name: str = "BAAI/bge-reranker-v2-m3"
    enable_reranker: bool = False

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
    enable_aesam_runtime: bool = False      # 阶段四：事件溯源运行时（ConversationRuntime）

    # 画图
    drawing_enhance_timeout: float = Field(default=30.0, description="画图提示词优化的超时时间")

    # Web 管理面板鉴权
    web_admin_password: str = "ChatB0t-S3cure-P@ssw0rd!2026#XyZ"

    # 路径（均为相对于项目根目录的相对路径，兼容 Windows / Linux）
    image_folder: str = "data/pixiv_downloads"
    books_folder: str = "data/books"
    db_path: str = "data/pixiv.db"
    jm_download_dir: str = "data/jm_temp"
    jm_option_path: str = "config/option.yml"
    font_path: str = Field(default_factory=_default_font_path)

    # 权限集合
    superusers: Set[str] = set()
    private_whitelist: Set[str] = set()
    ai_admin_qq: Set[str] = set()
    drawing_whitelist: Set[str] = set()
    welcome_groups: Set[str] = set()

    welcome_mode: str = "all"

    force_tool_prefixes: dict[str, str] = Field(
        default_factory=lambda: {
            "/jm": "jm_download",
            "#搜图": "search_acg_image",
            "/画图": "generate_image",
        },
        description="硬指令前缀映射：prefix -> tool_name。匹配时跳过逻辑脑直接执行工具。",
    )

    # 对话路由参数
    session_timeout: int = 600                    # 沉浸会话窗口期（秒）
    similarity_threshold: float = 0.35            # 话题向量相似度阈值
    ema_old_weight: float = 0.7                   # EMA 老中心权重
    ema_new_weight: float = 0.3                   # EMA 新消息权重

    group_configs: dict[str, GroupSettings] = {}


class ConfigManager:
    """负责从 YAML 加载配置、热更新、提供统一的属性访问"""

    def __init__(self):
        self._config = Config()
        self._lock = threading.Lock()
        self._version: int = 0
        self._observer: Optional[Observer] = None
        self._admin_token: str = secrets.token_urlsafe(32)
        self.load_config()
        self._start_watcher()

    @property
    def version(self) -> int:
        return self._version

    def load_config(self):
        """读取 config.yaml 并更新内部 Config 对象（深拷贝方案：仅显式出现的键被更新）"""
        if not CONFIG_FILE.exists():
            self._generate_default_yaml()
            return

        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw_data = f.read()
        try:
            data = yaml.safe_load(raw_data) or {}
        except yaml.YAMLError as e:
            logger.critical(f"[Config] YAML 解析失败，保留当前配置: {e}")
            return

        with self._lock:
            temp_config = copy.deepcopy(self._config)

        try:
            for key, value in data.items():
                if key not in Config.model_fields:
                    continue
                field_info = Config.model_fields[key]
                target_type = field_info.annotation

                # group_configs 特殊处理
                if key == "group_configs" and isinstance(value, dict):
                    converted = {}
                    for gid, gcfg in value.items():
                        if isinstance(gcfg, dict):
                            converted[gid] = GroupSettings(**gcfg)
                        elif isinstance(gcfg, GroupSettings):
                            converted[gid] = gcfg
                    setattr(temp_config, key, converted)
                    continue

                # 类型转换
                try:
                    if "Set" in str(target_type) or "set" in str(target_type):
                        if isinstance(value, list):
                            value = set(value)
                        elif isinstance(value, str):
                            value = set(s.strip() for s in value.split(",") if s.strip())
                        else:
                            value = set()
                    elif target_type is bool:
                        if isinstance(value, bool):
                            converted = value
                        elif isinstance(value, str):
                            converted = value.strip().lower() in ("true", "1", "yes", "on")
                        else:
                            converted = bool(value)
                        setattr(temp_config, key, converted)
                        continue
                    elif target_type is float:
                        value = float(value)
                    elif target_type is int:
                        value = int(value)
                except (ValueError, TypeError):
                    continue

                setattr(temp_config, key, value)
        except Exception as e:
            logger.critical(f"[Config] 配置应用失败，保留当前配置: {e}")
            return

        with self._lock:
            self._config = temp_config
        logger.info("[Config] 配置已热更新")

    def save_config(self, data: dict = None):
        """原子写入 YAML。传入 data 则直接序列化该字典，否则从当前 _config 读取。"""
        import tempfile

        if data is None:
            data = {}
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

        self._version += 1

    def _generate_default_yaml(self):
        """生成初始配置文件"""
        default = {
            "deepseek_api_key": "",
            "deepseek_api_url": "https://api.deepseek.com/chat/completions",
            "deepseek_model_name": "deepseek-v4-flash",
            "deepseek_memory_model_name": "deepseek-v4-flash",
            "logic_model_name": "",
            "enable_dual_brain": True,
            "agent_max_loops": 10,
            "agent_request_timeout": 60.0,
            "enable_strict_schema": True,
            "enable_task_queue": False,
            "enable_dynamic_loop": False,
            "entity_relation_enabled": False,
            "semantic_lorebook_enabled": False,
            "token_arbitration_enabled": False,
            "enable_aesam_runtime": False,
            "siliconflow_api_key": "",
            "siliconflow_api_url": "https://api.siliconflow.cn/v1",
            "siliconflow_model_name": "Kwai-Kolors/Kolors",
            "embedding_model_name": "BAAI/bge-m3",
            "reranker_model_name": "BAAI/bge-reranker-v2-m3",
            "enable_reranker": False,
            "drawing_enhance_timeout": 30.0,
            "web_admin_password": "ChatB0t-S3cure-P@ssw0rd!2026#XyZ",
            "image_folder": "data/pixiv_downloads",
            "books_folder": "data/books",
            "db_path": "data/pixiv.db",
            "jm_download_dir": "data/jm_temp",
            "jm_option_path": "config/option.yml",
            "font_path": _default_font_path(),
            "superusers": [],
            "private_whitelist": [],
            "ai_admin_qq": [],
            "drawing_whitelist": [],
            "welcome_groups": [],
            "welcome_mode": "all"
        }
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
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
    def __setattr__(self, name: str, value: Any) -> None:
        # 所有私有属性（_开头）以及核心容器直接存到实例自身
        if name.startswith('_') or name in ("_config", "_lock", "_observer"):
            super().__setattr__(name, value)
        else:
            setattr(self._config, name, value)

    def __getattr__(self, name: str) -> Any:
        # 私有属性不会走到这里，因为实例自身已经有了
        # 这里只是作为兜底，继续对公开属性代理
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._config, name)


# ==================== FastAPI Web 管理面板 ====================

from fastapi import FastAPI, Depends, Response, Cookie, Header
from fastapi.responses import JSONResponse, FileResponse
from starlette.middleware.cors import CORSMiddleware

fastapi_app = FastAPI(title="Chatbot Config Panel")
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_HTML_PATH = Path(__file__).parent / "web" / "index.html"


@fastapi_app.get("/", include_in_schema=False)
@fastapi_app.get("/index.html", include_in_schema=False)
async def serve_frontend():
    if _HTML_PATH.is_file():
        return FileResponse(_HTML_PATH, media_type="text/html")
    return JSONResponse({"error": "index.html not found"}, status_code=404)


# ---------- FastAPI Lifespan ----------
# 使用 background task 模式，不阻塞 NoneBot2 driver 生命周期

_server_task: Optional[asyncio.Task] = None


async def _serve(manager: ConfigManager, port: int):
    import uvicorn
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    await server.serve()


async def start_web_server(manager: ConfigManager, port: int = WEB_PORT):
    global _server_task
    _server_task = asyncio.create_task(_serve(manager, port))
    logger.info(f"[WebPanel] FastAPI 管理面板启动: http://0.0.0.0:{port}")


async def stop_web_server():
    global _server_task
    if _server_task and not _server_task.done():
        _server_task.cancel()
        try:
            await _server_task
        except asyncio.CancelledError:
            pass
        _server_task = None
        logger.info("[WebPanel] FastAPI 管理面板已关闭")


# ---------- 鉴权 ----------

_SESSION_COOKIE = "chatbot_admin_session"


async def _check_auth(
    chatbot_admin_session: str = Cookie(default=None),
    authorization: str = Header(default=None),
):
    """FastAPI Depends：校验 session cookie 或 Bearer token。"""
    _manager = plugin_config
    token = chatbot_admin_session
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if token and secrets.compare_digest(token, _manager._admin_token):
        return
    raise _Unauthorized


class _Unauthorized(Exception):
    pass


@fastapi_app.exception_handler(_Unauthorized)
async def _unauthorized_handler(request, exc):
    return JSONResponse({"success": False, "error": "未授权，请先登录"}, status_code=401)


# ---------- 路由 ----------

@fastapi_app.get("/api/config")
async def api_get_config(admin=Depends(_check_auth)):
    data = _load_config_dict(plugin_config)
    data["version"] = plugin_config.version
    return data


@fastapi_app.get("/api/worldbook")
async def api_get_worldbook(admin=Depends(_check_auth)):
    wb_path, draft_path = _wb_paths()
    return {"entries": _read_json(wb_path), "drafts": _read_json(draft_path)}


@fastapi_app.post("/api/login")
async def api_login(body: dict, response: Response):
    await asyncio.sleep(1.0)
    password = body.get("password", "")
    if not secrets.compare_digest(password, plugin_config.web_admin_password):
        return JSONResponse({"success": False, "error": "密码错误"}, status_code=401)
    token = plugin_config._admin_token
    response.set_cookie(
        key=_SESSION_COOKIE, value=token,
        httponly=True, samesite="lax", max_age=86400,
    )
    return {"success": True, "token": token}


@fastapi_app.post("/api/config")
async def api_save_config(body: dict, admin=Depends(_check_auth)):
    processed = {}
    errors = []
    for key, value in body.items():
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
            if "Set" in str(target_type) or "set" in str(target_type):
                if isinstance(value, list):
                    processed[key] = value
                elif isinstance(value, str):
                    clean_val = value.replace("[", "").replace("]", "").replace('"', "").replace("'", "")
                    processed[key] = [s.strip() for s in clean_val.split(",") if s.strip()]
                else:
                    raise ValueError("应为列表或逗号分隔字符串")
            elif target_type is bool:
                if isinstance(value, str):
                    processed[key] = value.strip().lower() in ("true", "1", "yes", "on")
                else:
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
        return JSONResponse({"success": False, "error": "; ".join(errors)}, status_code=400)

    # 构建候选配置
    with plugin_config._lock:
        candidate = copy.deepcopy(plugin_config._config)

    for key, value in processed.items():
        if key == "group_configs":
            for gid, gcfg in value.items():
                candidate.group_configs[gid] = GroupSettings(**gcfg)
            continue
        if key in Config.model_fields:
            field_type = Config.model_fields[key].annotation
            if "Set" in str(field_type) or "set" in str(field_type):
                if isinstance(value, list):
                    setattr(candidate, key, set(value))
                else:
                    setattr(candidate, key, set())
                continue
            if field_type is bool:
                if isinstance(value, str):
                    value = value.strip().lower() in ("true", "1", "yes", "on")
                else:
                    value = bool(value)
            setattr(candidate, key, value)

    # 先落盘，后刷新内存
    payload = {}
    for name, field_info in Config.model_fields.items():
        val = getattr(candidate, name)
        if isinstance(val, set):
            val = list(val)
        elif name == 'group_configs':
            val = {k: v.model_dump() if isinstance(v, GroupSettings) else v
                   for k, v in val.items()}
        payload[name] = val

    try:
        plugin_config.save_config(payload)
    except Exception as e:
        logger.error(f"[Config] Web API 保存配置失败: {e}")
        return JSONResponse({"success": False, "error": f"保存配置失败: {e}"}, status_code=500)

    with plugin_config._lock:
        plugin_config._config = candidate

    return {"success": True, "message": "配置已保存"}


@fastapi_app.post("/api/worldbook/save")
async def api_worldbook_save(body: dict, admin=Depends(_check_auth)):
    entries = body.get("entries")
    if not isinstance(entries, list):
        return JSONResponse({"success": False, "error": "entries 必须是数组"}, status_code=400)
    wb_path, _ = _wb_paths()
    try:
        _write_json(wb_path, entries)
        from .services.topic_router import invalidate_strong_entities_cache
        invalidate_strong_entities_cache()
        return {"success": True, "message": f"已保存 {len(entries)} 条词条"}
    except Exception as e:
        return JSONResponse({"success": False, "error": f"保存失败: {e}"}, status_code=500)


@fastapi_app.post("/api/worldbook/draft/approve")
async def api_draft_approve(body: dict, admin=Depends(_check_auth)):
    uid = body.get("uid")
    if uid is None:
        return JSONResponse({"success": False, "error": "缺少 uid"}, status_code=400)
    wb_path, draft_path = _wb_paths()
    drafts = _read_json(draft_path)
    target = next((e for e in drafts if e.get("uid") == uid), None)
    if target is None:
        return JSONResponse({"success": False, "error": f"草稿 uid={uid} 不存在"}, status_code=404)
    entries = _read_json(wb_path)
    new_uid = max((e.get("uid", 0) for e in entries), default=0) + 1
    target["uid"] = new_uid
    target.pop("custom_scope", None)
    entries.append(target)
    remaining = [e for e in drafts if e.get("uid") != uid]
    try:
        _write_json(wb_path, entries)
        _write_json(draft_path, remaining)
        from .services.topic_router import invalidate_strong_entities_cache
        invalidate_strong_entities_cache()
        return {"success": True, "message": f"已批准 uid={uid} → 正式 uid={new_uid}"}
    except Exception as e:
        return JSONResponse({"success": False, "error": f"操作失败: {e}"}, status_code=500)


@fastapi_app.post("/api/worldbook/draft/reject")
async def api_draft_reject(body: dict, admin=Depends(_check_auth)):
    uid = body.get("uid")
    if uid is None:
        return JSONResponse({"success": False, "error": "缺少 uid"}, status_code=400)
    _, draft_path = _wb_paths()
    drafts = _read_json(draft_path)
    remaining = [e for e in drafts if e.get("uid") != uid]
    if len(remaining) == len(drafts):
        return JSONResponse({"success": False, "error": f"草稿 uid={uid} 不存在"}, status_code=404)
    try:
        _write_json(draft_path, remaining)
        from .services.topic_router import invalidate_strong_entities_cache
        invalidate_strong_entities_cache()
        return {"success": True, "message": f"已丢弃 uid={uid}"}
    except Exception as e:
        return JSONResponse({"success": False, "error": f"操作失败: {e}"}, status_code=500)


# ---------- 辅助函数 ----------

def _load_config_dict(mgr: ConfigManager) -> dict:
    data = {}
    with mgr._lock:
        for name, field_info in Config.model_fields.items():
            val = getattr(mgr._config, name)
            if isinstance(val, set):
                val = list(val)
            elif name == 'group_configs':
                val = {k: v.model_dump() if isinstance(v, GroupSettings) else v
                       for k, v in val.items()}
            data[name] = val
    return data


def _wb_paths():
    return CONFIG_DIR / "worldbook.json", CONFIG_DIR / "draft_worldbook.json"


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("entries", []) if isinstance(data, dict) else data
    except Exception:
        return []


def _write_json(path, entries):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"entries": entries}, f, ensure_ascii=False, indent=2)


# 全局配置实例
plugin_config = ConfigManager()
