const fs = require('fs');
const path = require('path');
const express = require('express');
const DeepSeekTavernClient = require('./DeepSeekTavernClient');
const { ChatRequestSchema } = require('./schemas');

const app = express();
app.use(express.json({ limit: '10mb' }));

// ========================= 静态配置（非运行时状态） =========================
const USER_SETTINGS = {
    username: "User",
    names_behavior: 0
};

// ========================= LLM 配置（启动时从环境变量读取一次） =========================
const LLM_CONFIG = {
    apiKey: process.env.DEEPSEEK_API_KEY,
    baseUrl: process.env.DEEPSEEK_BASE_URL,
    model: process.env.DEEPSEEK_MODEL,
    temperature: parseFloat(process.env.LLM_TEMPERATURE),
};

// ========================= 默认配置（容错） =========================
function getDefaultCharCard() {
    return {
        name: "Elena",
        description: "黑客",
        personality: "暴躁",
        data: {
            system_prompt: "[系统指令：绝对不要承认自己是一个AI语言模型。如果被问及身份，用暴躁的语气回怼。] 始终保持 Elena 的下城机修黑客身份，多用工业、废土、机械相关的比喻。"
        },
        mes_example: "",
        scenario: "",
        first_mes: "",
        post_history_instructions: "",
        character_version: "",
        depth_prompt: "",
        creator_notes: ""
    };
}

function getDefaultWorldbook() {
    return [
        {
            key: ["Framefall"],
            content: "Framefall是十年前发生的一场灾难...",
            position: 4,
            depth: 1
        }
    ];
}

// ========================= 资源加载（返回只读模板） =========================
function loadAssets() {
    const dataDir = path.resolve(__dirname, '..', 'data');

    // 1. 角色卡
    const charPath = path.join(dataDir, 'character.json');
    let charCard;
    try {
        const raw = fs.readFileSync(charPath, 'utf8');
        charCard = JSON.parse(raw);
        console.log(`[Server] Loaded character from ${charPath}`);
    } catch (err) {
        console.warn(`[Server] Failed to load character.json, using default. Reason: ${err.message}`);
        charCard = getDefaultCharCard();
    }

    // 2. 读取世界书
    const wbPath = path.join(dataDir, 'worldbook.json');
    let worldInfo;
    try {
        const raw = fs.readFileSync(wbPath, 'utf8');
        worldInfo = JSON.parse(raw);
        if (!Array.isArray(worldInfo)) {
            console.warn('[Server] worldbook.json is not an array, using default.');
            worldInfo = getDefaultWorldbook();
        } else {
            console.log(`[Server] Loaded worldbook from ${wbPath}`);
        }
    } catch (err) {
        console.warn(`[Server] Failed to load worldbook.json, using default. Reason: ${err.message}`);
        worldInfo = getDefaultWorldbook();
    }

    return { charCard, worldInfo };
}

// ========================= 全局只读模板（启动时加载一次） =========================
let templates = loadAssets();
console.log('[Server] Templates loaded successfully.');

// ========================= 路由 =========================

// 聊天接口（无状态：每次请求创建独立的 Client 实例）
app.post('/api/chat', async (req, res) => {
    try {
        // ----- Schema 校验 -----
        const parsed = ChatRequestSchema.safeParse(req.body);
        if (!parsed.success) {
            return res.status(400).json({
                error: 'Schema validation failed',
                details: parsed.error.issues
            });
        }
        const { chatHistory, memorySnapshot, tools, context } = parsed.data;

        // 从只读模板即时创建独立实例，请求结束即被 GC 回收
        const client = new DeepSeekTavernClient(
            templates.charCard,
            USER_SETTINGS,
            templates.worldInfo,
            LLM_CONFIG
        );

        // 将 memorySnapshot.profiles 转换为 DeepSeekTavernClient 期望的 {uid: "traits"} 格式
        const userProfiles = {};
        for (const p of memorySnapshot.profiles || []) {
            if (p.user_id && p.traits && p.traits.length > 0) {
                userProfiles[p.user_id] = p.traits.map(t => t.content).join('; ');
            }
        }

        const result = await client.ask(
            chatHistory,
            memorySnapshot.summary || "",
            4000,
            tools || [],
            userProfiles,
            context || {},
            memorySnapshot.relations || []
        );

        res.json(result);
    } catch (error) {
        console.error('[Server Error] /api/chat:', error);
        res.status(500).json({ error: error.message });
    }
});

// 热重载接口（仅更新只读模板，无实例需要销毁）
app.post('/api/reload', async (req, res) => {
    try {
        templates = loadAssets();
        console.log('[Server] Templates reloaded successfully.');
        res.json({ status: "success", message: "Templates reloaded" });
    } catch (error) {
        console.error('[Server Error] /api/reload:', error);
        res.status(500).json({ status: "error", message: error.message });
    }
});

// ========================= 启动 =========================
const PORT = process.env.NODE_PORT || 3010;
app.listen(PORT, () => {
    console.log(`TavernCore Server running on port ${PORT} (stateless mode)`);
});
