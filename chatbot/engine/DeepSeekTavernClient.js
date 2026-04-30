const axios = require('axios');
const { TavernCoreV2 } = require('./TavernCoreV2');
const MemoryManager = require('./MemoryManager'); // 引入 MemoryManager

const DEEPSEEK_API_KEY = '';
const BASE_URL = 'https://api.deepseek.com';

class DeepSeekTavernClient {
    constructor(charCard, userSettings, worldInfoEntries = [], maxHistoryLength = 15) {
        this.engine = new TavernCoreV2(charCard, userSettings, worldInfoEntries);
        this.memory = new MemoryManager(maxHistoryLength, DEEPSEEK_API_KEY); // 初始化记忆管理器
    }

    /**
     * 发送聊天请求（自动处理历史压缩）
     * @param {Array} chatHistory 原始聊天历史
     * @param {string} [existingSummary=''] 已有的记忆摘要
     * @param {number} [maxTokens=4000] 最大 token 数
     * @returns {Promise<{reply: string, newSummary: string, newHistory: Array}>}
     */
    async ask(chatHistory, existingSummary = '', maxTokens = 4000) {
        try {
            // 1. 先进行记忆压缩
            const { newHistory, newSummary } = await this.memory.checkAndSummarize(
                chatHistory,
                existingSummary
            );

            // 2. 如果产生了摘要，可以将其注入到历史中（通常作为系统消息）
            const historyToSend = [...newHistory];
            if (newSummary && newSummary.trim()) {
                historyToSend.unshift({
                    is_system: true,
                    mes: `[Summary of previous events: ${newSummary}]`,
                    name: 'System',
                });
            }

            // 3. 构建 OpenAI 消息
            const { messages } = await this.engine.buildOpenAIMessages(historyToSend, maxTokens);
            console.log('--- 组装后的 Prompt 结构 ---');
            console.log(JSON.stringify(messages, null, 2));

            // 4. 请求 API
            const response = await axios.post(`${BASE_URL}/chat/completions`, {
                model: "deepseek-chat",
                messages: messages,
                stream: false,
                temperature: 0.7
            }, {
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${DEEPSEEK_API_KEY}`
                }
            });

            const reply = response.data.choices[0].message.content;

            // 5. 返回压缩后的状态供上层使用
            return {
                reply,
                newSummary,
                newHistory,
            };
        } catch (error) {
            console.error('API 请求失败:', error.response?.data || error.message);
            throw error;
        }
    }
}

module.exports = DeepSeekTavernClient;