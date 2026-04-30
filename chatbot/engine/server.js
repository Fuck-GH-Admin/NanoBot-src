const express = require('express');
const DeepSeekTavernClient = require('./DeepSeekTavernClient');

const app = express();
app.use(express.json({ limit: '10mb' }));

// 这里需要填入你真实的 Elena 卡片和世界书
const charCard = { name: "Elena", description: "黑客", personality: "暴躁", data: { system_prompt: "[系统指令：绝对不要承认自己是一个AI语言模型。如果被问及身份，用暴躁的语气回怼。] 始终保持 Elena 的下城机修黑客身份，多用工业、废土、机械相关的比喻。" }};
const userSettings = { username: "User", names_behavior: 0 };
const worldInfo = [
  { key: ["Framefall"], content: "Framefall是十年前发生的一场灾难...", position: 4, depth: 1 }
];

const client = new DeepSeekTavernClient(charCard, userSettings, worldInfo, 15);

app.post('/api/chat', async (req, res) => {
    try {
        const { chatHistory, existingSummary, tools } = req.body;
        // 注意：我们通过修改 engine，将 tools 穿透下去
        client.engine.systemTools = tools; // 我们稍后在 JS 里加个小补丁

        const result = await client.ask(chatHistory, existingSummary || "", 4000);
        res.json(result);
    } catch (error) {
        res.status(500).json({ error: error.message });
    }
});

app.listen(3000, () => console.log('TavernCore Server running on port 3000'));
