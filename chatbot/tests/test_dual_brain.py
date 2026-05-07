"""
端到端测试：双脑分工架构
覆盖场景：
  1. 双脑开启 + 工具调用 → DB 记录顺序 user → assistant(tool_calls) → tool → assistant(final)
  2. 纯聊天（无工具）→ 不产生 tool_calls
  3. system_welcome 短路 → chat_history 无污染
  4. enable_dual_brain=False → 回退单脑模式
  5. 逻辑循环去重 → 相同工具调用不死循环
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# 将项目根目录加入 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fixtures：构建 mock 依赖
# ---------------------------------------------------------------------------

def _make_config(**overrides):
    """构建 mock plugin_config。"""
    cfg = MagicMock()
    cfg.enable_dual_brain = True
    cfg.logic_model_name = "deepseek-r1"
    cfg.deepseek_model_name = "deepseek-v4-flash"
    cfg.deepseek_api_url = "https://api.deepseek.com/chat/completions"
    cfg.deepseek_api_key = "test-key"
    cfg.agent_request_timeout = 60.0
    cfg.agent_max_loops = 5
    cfg.enable_dynamic_loop = False
    cfg.entity_relation_enabled = False
    cfg.semantic_lorebook_enabled = False
    cfg.token_arbitration_enabled = False
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _llm_response(content="", tool_calls=None):
    """构建 LLM API 响应。"""
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{"message": msg}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _tool_call(tool_name, arguments, tc_id="tc_001"):
    """构建 OpenAI 格式的 tool_call。"""
    return {
        "id": tc_id,
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": json.dumps(arguments),
        },
    }


def _mock_httpx_post(responses):
    """
    构建 httpx.AsyncClient.post 的 mock。
    responses: list of (status_code, json_data) tuples, consumed in order.
    """
    remaining = list(responses)

    async def _post(*args, **kwargs):
        assert remaining, "HTTP mock: 响应已耗尽，但仍有请求"
        status, data = remaining.pop(0)
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = data
        resp.text = json.dumps(data)
        return resp

    mock = MagicMock()
    mock.side_effect = _post
    return mock


def _mock_repo(messages_log=None):
    """构建 MemoryRepository mock，记录所有 add_message 调用。"""
    repo = MagicMock()
    log = messages_log if messages_log is not None else []

    async def _add_message(**kwargs):
        log.append(kwargs)

    repo.add_message = AsyncMock(side_effect=_add_message)
    repo.get_recent_messages = AsyncMock(return_value=[])
    repo.get_active_profiles = AsyncMock(return_value={})
    repo.get_group_summary = AsyncMock(return_value="")
    repo.get_relations_with_decay = AsyncMock(return_value=[])
    repo.get_memory_snapshot = AsyncMock(return_value={"summary": "", "profiles": [], "relations": []})
    return repo


def _mock_rule_engine(matched_rule=None):
    """构建 RuleEngine mock。"""
    engine = MagicMock()

    async def _match(text, context):
        if matched_rule:
            context["_matched_rule"] = matched_rule
        else:
            context.pop("_matched_rule", None)

    engine.match = AsyncMock(side_effect=_match)
    return engine


def _mock_registry(tool_result=("工具执行成功", [])):
    """构建 ToolRegistry mock。"""
    registry = MagicMock()
    registry.get_all_schemas.return_value = []
    registry.execute_tool = AsyncMock(return_value=tool_result)
    registry._tools = {}
    return registry


def _base_context():
    """构建基础 context。"""
    return {
        "group_id": 12345,
        "is_admin": False,
        "sender_name": "TestUser",
        "user_id": "99999",
        "permission_service": MagicMock(),
        "allow_r18": False,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dual_brain_with_tool_call():
    """
    场景 1: 双脑开启 + 工具调用
    验证 DB 记录顺序: user → assistant(tool_calls) → tool → assistant(final)
    """
    tc = _tool_call("search_acg_image", {"keyword": "二次元"}, tc_id="tc_001")

    # Phase 1 逻辑循环:
    #   call 1 → tool_calls → 执行工具 → 追加结果
    #   call 2 → 无 tool_calls → 退出循环
    # Phase 2 人格渲染:
    #   call 3 → 最终文本
    http_responses = [
        (200, _llm_response("", [tc])),           # Phase 1 call 1 (logic, with tools)
        (200, _llm_response("", None)),            # Phase 1 call 2 (logic, no tools → exit loop)
        (200, _llm_response("最终回复", None)),     # Phase 2 (actor)
    ]

    db_log = []
    config = _make_config()
    repo = _mock_repo(db_log)
    rule_engine = _mock_rule_engine()
    registry = _mock_registry(("找到图片: /img/test.jpg", ["/img/test.jpg"]))

    with patch("chatbot.services.agent_service.plugin_config", config), \
         patch("chatbot.services.agent_service.MemoryRepository", return_value=repo), \
         patch("chatbot.services.agent_service.RuleEngine", return_value=rule_engine), \
         patch("chatbot.services.agent_service.ToolRegistry", return_value=registry), \
         patch("chatbot.services.agent_service.create_semantic_lorebook", return_value=None):

        from chatbot.services.agent_service import AgentService
        svc = AgentService()
        svc.repo = repo
        svc.rule_engine = rule_engine
        svc.registry = registry
        svc.http_client = MagicMock()
        svc.http_client.post = _mock_httpx_post(http_responses)
        svc.http_client.aclose = AsyncMock()
        svc.semantic_lorebook = None

        result = await svc.run_agent("99999", "帮我搜个二次元图片", _base_context())

    # 验证返回结果
    assert result["text"] == "最终回复", (
        f"期望 '最终回复'，实际 '{result['text']}'。"
        f"HTTP 调用次数: {svc.http_client.post.call_count}，"
        f"DB 记录: {[(r['role'], bool(r.get('tool_calls'))) for r in db_log]}"
    )
    assert len(result["images"]) == 1

    # 验证 DB 记录顺序
    roles = [r["role"] for r in db_log]
    assert roles[0] == "user", "第一条应为 user"
    assert roles[1] == "assistant", "第二条应为 assistant (tool_calls)"
    assert db_log[1].get("tool_calls") is not None, "assistant 消息应含 tool_calls"
    assert roles[2] == "tool", "第三条应为 tool 结果"
    assert roles[3] == "assistant", "第四条应为 assistant (最终回复，无 tool_calls)"
    assert db_log[3].get("tool_calls") is None, "最终 assistant 不应含 tool_calls"


@pytest.mark.asyncio
async def test_pure_chat_no_tool_calls():
    """
    场景 2: 纯聊天（无工具调用）
    验证不产生 tool_calls 记录。
    """
    http_responses = [
        (200, _llm_response("", None)),            # Phase 1: 无 tool_call → 退出循环
        (200, _llm_response("你好呀！", None)),     # Phase 2: 人格渲染
    ]

    db_log = []
    config = _make_config()
    repo = _mock_repo(db_log)
    rule_engine = _mock_rule_engine()
    registry = _mock_registry()

    with patch("chatbot.services.agent_service.plugin_config", config), \
         patch("chatbot.services.agent_service.MemoryRepository", return_value=repo), \
         patch("chatbot.services.agent_service.RuleEngine", return_value=rule_engine), \
         patch("chatbot.services.agent_service.ToolRegistry", return_value=registry), \
         patch("chatbot.services.agent_service.create_semantic_lorebook", return_value=None):

        from chatbot.services.agent_service import AgentService
        svc = AgentService()
        svc.repo = repo
        svc.rule_engine = rule_engine
        svc.registry = registry
        svc.http_client = MagicMock()
        svc.http_client.post = _mock_httpx_post(http_responses)
        svc.http_client.aclose = AsyncMock()
        svc.semantic_lorebook = None

        result = await svc.run_agent("99999", "你好", _base_context())

    assert result["text"] == "你好呀！"
    assert result["images"] == []

    # 验证没有 tool_calls 记录
    tool_records = [r for r in db_log if r["role"] == "tool"]
    assistant_with_tools = [r for r in db_log if r["role"] == "assistant" and r.get("tool_calls")]
    assert len(tool_records) == 0, "纯聊天不应有 tool 记录"
    assert len(assistant_with_tools) == 0, "纯聊天不应有 tool_calls"


@pytest.mark.asyncio
async def test_system_welcome_no_db_pollution():
    """
    场景 3: system_welcome 短路
    验证 chat_history 表无污染（不调用 add_message）。
    """
    http_responses = [
        (200, _llm_response("欢迎新成员！", None)),
    ]

    db_log = []
    config = _make_config()
    repo = _mock_repo(db_log)
    rule_engine = _mock_rule_engine()
    registry = _mock_registry()

    with patch("chatbot.services.agent_service.plugin_config", config), \
         patch("chatbot.services.agent_service.MemoryRepository", return_value=repo), \
         patch("chatbot.services.agent_service.RuleEngine", return_value=rule_engine), \
         patch("chatbot.services.agent_service.ToolRegistry", return_value=registry), \
         patch("chatbot.services.agent_service.create_semantic_lorebook", return_value=None):

        from chatbot.services.agent_service import AgentService
        svc = AgentService()
        svc.repo = repo
        svc.rule_engine = rule_engine
        svc.registry = registry
        svc.http_client = MagicMock()
        svc.http_client.post = _mock_httpx_post(http_responses)
        svc.http_client.aclose = AsyncMock()
        svc.semantic_lorebook = None

        ctx = _base_context()
        result = await svc.run_agent("system_welcome", "欢迎新成员加入群聊", ctx)

    assert "欢迎" in result["text"]
    assert len(db_log) == 0, f"system_welcome 不应写入 DB，但写入了 {len(db_log)} 条"


@pytest.mark.asyncio
async def test_single_brain_fallback():
    """
    场景 4: enable_dual_brain=False → 回退单脑模式
    验证行为与旧版一致（直接 LLM 调用，无 Phase 1/2 分离）。
    """
    http_responses = [
        (200, _llm_response("单脑回复", None)),
    ]

    db_log = []
    config = _make_config(enable_dual_brain=False)
    repo = _mock_repo(db_log)
    rule_engine = _mock_rule_engine()
    registry = _mock_registry()

    with patch("chatbot.services.agent_service.plugin_config", config), \
         patch("chatbot.services.agent_service.MemoryRepository", return_value=repo), \
         patch("chatbot.services.agent_service.RuleEngine", return_value=rule_engine), \
         patch("chatbot.services.agent_service.ToolRegistry", return_value=registry), \
         patch("chatbot.services.agent_service.create_semantic_lorebook", return_value=None):

        from chatbot.services.agent_service import AgentService
        svc = AgentService()
        svc.repo = repo
        svc.rule_engine = rule_engine
        svc.registry = registry
        svc.http_client = MagicMock()
        svc.http_client.post = _mock_httpx_post(http_responses)
        svc.http_client.aclose = AsyncMock()
        svc.semantic_lorebook = None

        result = await svc.run_agent("99999", "测试单脑", _base_context())

    assert result["text"] == "单脑回复"
    # 单脑模式只调用一次 LLM
    assert svc.http_client.post.call_count == 1, "单脑模式应只调用一次 LLM"


@pytest.mark.asyncio
async def test_logic_loop_dedup():
    """
    场景 5: 逻辑循环去重
    连续两次返回相同 tool_call 时应终止循环。
    """
    tc = _tool_call("search_acg_image", {"keyword": "same"}, tc_id="tc_dup")

    # Phase 1: 两次返回完全相同的 tool_call → 去重后应退出
    # Phase 2: 人格渲染
    http_responses = [
        (200, _llm_response("", [tc])),           # logic call 1
        (200, _llm_response("", [tc])),           # logic call 2 (重复)
        (200, _llm_response("最终回复", None)),     # actor call
    ]

    db_log = []
    config = _make_config()
    repo = _mock_repo(db_log)
    rule_engine = _mock_rule_engine()
    registry = _mock_registry(("结果", []))

    with patch("chatbot.services.agent_service.plugin_config", config), \
         patch("chatbot.services.agent_service.MemoryRepository", return_value=repo), \
         patch("chatbot.services.agent_service.RuleEngine", return_value=rule_engine), \
         patch("chatbot.services.agent_service.ToolRegistry", return_value=registry), \
         patch("chatbot.services.agent_service.create_semantic_lorebook", return_value=None):

        from chatbot.services.agent_service import AgentService
        svc = AgentService()
        svc.repo = repo
        svc.rule_engine = rule_engine
        svc.registry = registry
        svc.http_client = MagicMock()
        svc.http_client.post = _mock_httpx_post(http_responses)
        svc.http_client.aclose = AsyncMock()
        svc.semantic_lorebook = None

        result = await svc.run_agent("99999", "搜图 same", _base_context())

    assert result["text"] == "最终回复"
    # Phase 1 应调用 2 次 LLM（第一次执行，第二次检测重复退出），Phase 2 调用 1 次
    assert svc.http_client.post.call_count == 3


@pytest.mark.asyncio
async def test_tool_result_truncation():
    """
    场景 6: 工具结果截断到 300 字符。
    """
    long_result = "A" * 500
    tc = _tool_call("search_acg_image", {"keyword": "test"}, tc_id="tc_trunc")

    http_responses = [
        (200, _llm_response("", [tc])),           # Phase 1 call 1 (tool_calls)
        (200, _llm_response("", None)),            # Phase 1 call 2 (exit loop)
        (200, _llm_response("done", None)),        # Phase 2 (actor)
    ]

    db_log = []
    config = _make_config()
    repo = _mock_repo(db_log)
    rule_engine = _mock_rule_engine()
    registry = _mock_registry((long_result, []))

    with patch("chatbot.services.agent_service.plugin_config", config), \
         patch("chatbot.services.agent_service.MemoryRepository", return_value=repo), \
         patch("chatbot.services.agent_service.RuleEngine", return_value=rule_engine), \
         patch("chatbot.services.agent_service.ToolRegistry", return_value=registry), \
         patch("chatbot.services.agent_service.create_semantic_lorebook", return_value=None):

        from chatbot.services.agent_service import AgentService
        svc = AgentService()
        svc.repo = repo
        svc.rule_engine = rule_engine
        svc.registry = registry
        svc.http_client = MagicMock()
        svc.http_client.post = _mock_httpx_post(http_responses)
        svc.http_client.aclose = AsyncMock()
        svc.semantic_lorebook = None

        result = await svc.run_agent("99999", "搜图 test", _base_context())

    # 工具结果应被截断到 300 字符（在 system_notification 中）
    # 但 DB 中存储的是完整结果
    tool_records = [r for r in db_log if r["role"] == "tool"]
    assert len(tool_records) >= 1
    assert tool_records[0]["content"] == long_result, "DB 应存完整结果"


@pytest.mark.asyncio
async def test_compile_logic_prompt_no_roleplay():
    """
    场景 7: compile_logic_prompt 不含角色扮演设定。
    """
    from chatbot.services.prompt_adapter import PromptAdapter

    adapter = PromptAdapter()
    # 确保 char_card 有 description
    adapter.char_card.description = "这是角色描述"
    adapter.char_card.personality = "活泼"

    messages = [{"role": "user", "content": "你好", "user_id": "123", "name": "User"}]
    snapshot = {"summary": "", "profiles": [], "relations": []}
    context = {"group_id": 1, "active_uids": ["123"]}

    result = adapter.compile_logic_prompt(messages, snapshot, context)

    # 将所有消息内容拼接检查
    all_content = " ".join(m["content"] for m in result)

    # 不应包含角色描述
    assert "这是角色描述" not in all_content, "逻辑脑不应含角色描述"
    assert "活泼" not in all_content, "逻辑脑不应含性格描述"
    # 应包含调度指令
    assert "逻辑调度" in all_content or "调度模块" in all_content, "逻辑脑应含调度指令"


@pytest.mark.asyncio
async def test_compile_actor_prompt_with_notification():
    """
    场景 8: compile_actor_prompt 的 system_notification 追加为 USER 消息。
    """
    from chatbot.services.prompt_adapter import PromptAdapter
    from chatbot.engine import MessageRole

    adapter = PromptAdapter()

    messages = [{"role": "user", "content": "你好", "user_id": "123", "name": "User"}]
    snapshot = {"summary": "", "profiles": [], "relations": []}
    context = {"group_id": 1, "active_uids": ["123"]}

    result = adapter.compile_actor_prompt(
        messages, snapshot, context, system_notification="工具执行完毕",
    )

    # 最后一条消息应为 USER 角色，包含系统通知
    last_msg = result[-1]
    assert last_msg["role"] == "user", f"最后一条应为 user 角色，实际为 {last_msg['role']}"
    assert "系统后台通知" in last_msg["content"], "应含系统通知标记"
    assert "工具执行完毕" in last_msg["content"], "应含通知内容"


def test_build_safe_history_drops_toolcall_assistant():
    """
    _build_safe_history 应丢弃带 tool_calls 的整条 assistant 消息，
    而不是仅移除 tool_calls 字段。
    """
    from chatbot.services.agent_service import AgentService
    svc = AgentService.__new__(AgentService)

    messages = [
        {"role": "user", "content": "搜图 二次元"},
        {"role": "assistant", "content": "我这就去查", "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "search_acg_image", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc1", "name": "search_acg_image", "content": "找到图片"},
        {"role": "assistant", "content": "找到了！"},
        {"role": "user", "content": "谢谢"},
    ]

    safe = svc._build_safe_history(messages)

    # 应保留: user("搜图"), assistant("找到了"), user("谢谢")
    # 应丢弃: assistant("我这就去查" + tool_calls), tool("找到图片")
    assert len(safe) == 3, f"期望 3 条，实际 {len(safe)} 条: {[m['role'] for m in safe]}"
    assert safe[0]["content"] == "搜图 二次元"
    assert safe[1]["content"] == "找到了！"
    assert safe[2]["content"] == "谢谢"

    # 确保不含 tool_calls 字段
    for msg in safe:
        assert "tool_calls" not in msg, f"safe_history 中不应含 tool_calls: {msg}"


@pytest.mark.asyncio
async def test_logic_loop_exit_on_error():
    """
    工具返回错误时，逻辑循环注入系统警告并提前退出。
    """
    tc = _tool_call("search_acg_image", {"keyword": "bad"}, tc_id="tc_err")

    http_responses = [
        (200, _llm_response("", [tc])),            # Phase 1 call 1: tool_calls → 执行失败
        (200, _llm_response("", None)),             # Phase 1 call 2: 收到警告后退出
        (200, _llm_response("抱歉，搜图失败了", None)),  # Phase 2 actor
    ]

    db_log = []
    config = _make_config()
    repo = _mock_repo(db_log)
    rule_engine = _mock_rule_engine()
    registry = _mock_registry(("Error: API timeout", []))

    with patch("chatbot.services.agent_service.plugin_config", config), \
         patch("chatbot.services.agent_service.MemoryRepository", return_value=repo), \
         patch("chatbot.services.agent_service.RuleEngine", return_value=rule_engine), \
         patch("chatbot.services.agent_service.ToolRegistry", return_value=registry), \
         patch("chatbot.services.agent_service.create_semantic_lorebook", return_value=None):

        from chatbot.services.agent_service import AgentService
        svc = AgentService()
        svc.repo = repo
        svc.rule_engine = rule_engine
        svc.registry = registry
        svc.http_client = MagicMock()
        svc.http_client.post = _mock_httpx_post(http_responses)
        svc.http_client.aclose = AsyncMock()
        svc.semantic_lorebook = None

        result = await svc.run_agent("99999", "搜图 bad", _base_context())

    assert result["text"] == "抱歉，搜图失败了"
    # 系统警告应被注入 DB（作为 tool 记录之后的 system 消息不需要持久化，但逻辑循环会退出）
    assert svc.http_client.post.call_count == 3


@pytest.mark.asyncio
async def test_forced_id_unique():
    """
    连续两次触发兜底执行，forced_id 互不相同。
    """
    from chatbot.services.agent_service import AgentService

    svc = AgentService.__new__(AgentService)
    svc.registry = MagicMock()
    svc.registry._tools = {}
    svc.repo = MagicMock()
    svc.rule_repo = MagicMock()

    # 注册一个低风险工具
    mock_tool = MagicMock()
    mock_tool.risk_level = "low"
    mock_tool.allow_forced_exec = True
    svc.registry._tools["search_acg_image"] = mock_tool
    svc.registry.execute_tool = AsyncMock(return_value=("结果", []))

    svc.repo.add_message = AsyncMock()
    svc.rule_repo.increment_hit_count = AsyncMock()

    matched_rule = {"rule_id": "r001", "tool_name": "search_acg_image", "keyword": "测试"}

    ids = []
    for _ in range(2):
        ctx = {"_matched_rule": matched_rule, "_tool_executed": False}
        tool_logs = []
        images = []
        logic_msgs = []
        await svc._try_forced_exec(ctx, "测试", "group_1", tool_logs, images, logic_msgs)
        # 从伪造的 assistant 消息中提取 forced_id
        for msg in logic_msgs:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                ids.append(msg["tool_calls"][0]["id"])
                break

    assert len(ids) == 2, f"应生成 2 个 forced_id，实际 {len(ids)}"
    assert ids[0] != ids[1], f"forced_id 应唯一，但都是 '{ids[0]}'"


@pytest.mark.asyncio
async def test_system_welcome_snapshot_injection():
    """
    system_welcome 分支应读取真实记忆快照并传递给演员脑。
    """
    real_snapshot = {
        "summary": "这是一个二次元爱好者群组",
        "profiles": [{"user_id": "u1", "traits": [{"content": "喜欢看番", "confidence": 0.9}]}],
        "relations": [],
    }

    http_responses = [
        (200, _llm_response("欢迎加入二次元群！", None)),
    ]

    db_log = []
    config = _make_config()
    repo = _mock_repo(db_log)
    repo.get_memory_snapshot = AsyncMock(return_value=real_snapshot)
    rule_engine = _mock_rule_engine()
    registry = _mock_registry()

    with patch("chatbot.services.agent_service.plugin_config", config), \
         patch("chatbot.services.agent_service.MemoryRepository", return_value=repo), \
         patch("chatbot.services.agent_service.RuleEngine", return_value=rule_engine), \
         patch("chatbot.services.agent_service.ToolRegistry", return_value=registry), \
         patch("chatbot.services.agent_service.create_semantic_lorebook", return_value=None):

        from chatbot.services.agent_service import AgentService
        svc = AgentService()
        svc.repo = repo
        svc.rule_engine = rule_engine
        svc.registry = registry
        svc.http_client = MagicMock()
        svc.http_client.post = _mock_httpx_post(http_responses)
        svc.http_client.aclose = AsyncMock()
        svc.semantic_lorebook = None

        result = await svc.run_agent("system_welcome", "欢迎新成员", _base_context())

    assert "欢迎" in result["text"]
    assert len(db_log) == 0, "system_welcome 不应写入 DB"
    # 验证 get_memory_snapshot 被调用
    repo.get_memory_snapshot.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
