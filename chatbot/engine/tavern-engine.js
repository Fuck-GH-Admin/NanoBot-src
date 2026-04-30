/**
 * Tavern Prompt Builder
 * 
 * 独立 Node.js 模块，将 SillyTavern 酒馆的 prompt 构建逻辑完整平移：
 * - 宏替换引擎 (兼容 {{user}}, {{char}}, {{random::...}}, {{pick::...}} 等)
 * - Token 预算管理与截断算法 (完整平移 ChatCompletion 类)
 * - 消息格式化与角色设定注入
 * 
 * 不依赖浏览器 DOM 和全局变量。
 */

const { TextEncoder } = require('util');

// ============================================================================
// 尝试加载 tiktoken 以获得更精确的 token 计数
// ============================================================================
let tiktokenEncoding = null;
let tiktokenModel = null;
try {
    // 尝试加载 @dqbd/tiktoken (WASM 版本，兼容 Node.js)
    const { get_encoding, encoding_for_model } = require('@dqbd/tiktoken');
    tiktokenEncoding = get_encoding('cl100k_base'); // GPT-4/3.5 使用的编码
    try {
        tiktokenModel = encoding_for_model('gpt-4');
    } catch (_) {
        tiktokenModel = tiktokenEncoding;
    }
} catch (_) {
    // tiktoken 不可用，将使用近似估算
    tiktokenEncoding = null;
    tiktokenModel = null;
}

// ============================================================================
// 常量
// ============================================================================
const BYTES_PER_TOKEN = 3.35;
const TOKENS_PER_MESSAGE = 3; // 每条消息的 role/边界标记约消耗 3 个 token

const CHARACTER_NAMES_BEHAVIOR = {
    NONE: -1,       // 不添加名称前缀
    DEFAULT: 0,     // 群聊中为非当前说话者添加名称前缀
    COMPLETION: 1,  // 不添加前缀（使用 name 字段）
    CONTENT: 2,     // 所有消息都添加名称前缀
};

const CONTINUE_POSTFIX_TYPES = {
    NONE: '',
    SPACE: ' ',
    NEWLINE: '\n',
    DOUBLE_NEWLINE: '\n\n',
};

// ============================================================================
// Token 计数工具
// ============================================================================

/**
 * 使用近似算法估算 token 数（与酒馆原代码的 guesstimate 一致）
 */
function guesstimate(str) {
    if (!str || typeof str !== 'string') return 0;
    const byteLength = Buffer.byteLength(str, 'utf8');
    return Math.ceil(byteLength / BYTES_PER_TOKEN);
}

/**
 * 使用 tiktoken 精确计数（如果可用）
 */
function countWithTiktoken(str, encoding) {
    if (!encoding || !str) return guesstimate(str);
    try {
        const tokens = encoding.encode(str);
        return tokens.length;
    } catch (_) {
        return guesstimate(str);
    }
}

/**
 * 计算单条 OpenAI 消息的 token 数
 * 参考 OpenAI 的计数规则：每条消息的基础 token + 内容 token
 */
function countMessageTokens(message, encoding) {
    if (!message || typeof message !== 'object') return 0;

    let tokens = TOKENS_PER_MESSAGE; // 每条消息的基础开销

    // 计算 role 的 token
    if (message.role) {
        tokens += countWithTiktoken(message.role, encoding);
    }

    // 计算 content 的 token
    if (typeof message.content === 'string') {
        tokens += countWithTiktoken(message.content, encoding);
    } else if (Array.isArray(message.content)) {
        // 多模态内容（文本 + 图片等）
        for (const part of message.content) {
            if (part?.type === 'text' && typeof part.text === 'string') {
                tokens += countWithTiktoken(part.text, encoding);
            } else if (part?.type === 'image_url') {
                tokens += 85; // OpenAI 图片的低质量估算
            }
        }
    }

    // tool_calls 的 token
    if (message.tool_calls) {
        tokens += countWithTiktoken(JSON.stringify(message.tool_calls), encoding);
    }

    // name 字段的 token
    if (message.name) {
        tokens += countWithTiktoken(message.name, encoding) - 1; // -1 因为 name 替换了 role 中的一部分
    }

    // reasoning 的 token
    if (message.reasoning) {
        tokens += countWithTiktoken(message.reasoning, encoding);
    }

    return tokens;
}

/**
 * 批量计算消息数组的总 token 数
 */
function countMessagesTokens(messages, encoding) {
    if (!Array.isArray(messages)) {
        messages = [messages];
    }
    return messages.reduce((sum, msg) => sum + countMessageTokens(msg, encoding), 0);
}

// ============================================================================
// 宏替换引擎 (简化但兼容的版本)
// ============================================================================

/**
 * 解析示例对话字符串
 * 将 <START> 分隔的示例对话块分割为数组
 */
function parseMesExamples(examplesStr) {
    if (!examplesStr || examplesStr.length === 0 || examplesStr === '<START>') {
        return [];
    }
    if (!examplesStr.startsWith('<START>')) {
        examplesStr = '<START>\n' + examplesStr.trim();
    }
    const blockHeading = '<START>\n';
    return examplesStr
        .split(/<START>/gi)
        .slice(1)
        .map(block => `${blockHeading}${block.trim()}\n`);
}

/**
 * 将示例对话块解析为单独的消息对象
 * 返回 { role: 'system', content: ..., name: 'example_user'|'example_assistant' } 数组
 */
function parseExampleIntoIndividual(messageExampleString, userName, charName) {
    const result = [];
    const lines = messageExampleString.split('\n');
    let curMsgLines = [];
    let inUser = false;
    let inBot = false;

    function addMsg(name, systemName) {
        const parsedMsg = curMsgLines.join('\n')
            .replace(name + ':', '')
            .trim();
        if (parsedMsg) {
            result.push({
                role: 'system',
                content: parsedMsg,
                name: systemName,
            });
        }
        curMsgLines = [];
    }

    for (let i = 1; i < lines.length; i++) {
        const curStr = lines[i];
        if (curStr.startsWith(userName + ':')) {
            inUser = true;
            if (inBot) addMsg(charName, 'example_assistant');
            inBot = false;
        } else if (curStr.startsWith(charName + ':')) {
            inBot = true;
            if (inUser) addMsg(userName, 'example_user');
            inUser = false;
        }
        curMsgLines.push(curStr);
    }
    if (inUser) addMsg(userName, 'example_user');
    else if (inBot) addMsg(charName, 'example_assistant');

    return result;
}

/**
 * 格式化示例对话为消息数组
 */
function formatMessageExamples(mesExamplesArray, userName, charName) {
    const examples = [];
    for (const item of mesExamplesArray) {
        let replaced = item.replace(/<START>/i, '{Example Dialogue:}').replace(/\r/gm, '');
        const parsed = parseExampleIntoIndividual(replaced, userName, charName);
        examples.push(parsed);
    }
    return examples;
}

/**
 * 核心宏替换函数
 * 
 * 支持以下宏：
 *   {{user}} - 用户名
 *   {{char}} - 角色名
 *   {{charIfNotGroup}} - 角色名（非群聊时等于 {{char}}）
 *   {{group}} - 群组名（单角色时等于 {{char}}）
 *   {{description}} - 角色描述
 *   {{personality}} - 角色性格
 *   {{scenario}} - 场景
 *   {{persona}} - 用户 persona 描述
 *   {{mesExamples}} - 示例对话（原始格式）
 *   {{model}} - 模型名
 *   {{time}} - 当前时间 (HH:MM AM/PM)
 *   {{date}} - 当前日期
 *   {{isotime}} - ISO 时间 (HH:mm)
 *   {{isodate}} - ISO 日期 (YYYY-MM-DD)
 *   {{weekday}} - 星期几
 *   {{datetimeformat ...}} - 自定义时间格式
 *   {{newline}} - 换行符
 *   {{trim}} - 去除周围空白行
 *   {{random::a::b::c}} - 随机选择
 *   {{pick::x::y::z}} - 基于位置种子的伪随机选择
 *   {{roll:2d6}} - 骰子
 *   {{reverse:...}} - 字符串反转
 *   {{// ...}} - 注释（被移除）
 *   {{noop}} - 空操作（被移除）
 *   {{input}} - 空字符串（在 prompt 构建中无意义）
 *   {{timeDiff::...::...}} - 时间差
 *   {{time_UTC+8}} - UTC 时间
 *   {{idle_duration}} - 空闲时长（固定返回 'just now'）
 *   {{lastMessage}} - 最后一条消息内容
 *   {{lastUserMessage}} - 最后一条用户消息
 *   {{lastCharMessage}} - 最后一条角色消息
 *   {{maxPrompt}}/{{maxContext}}/{{maxResponse}} - Token 限制
 *   {{original}} - 原始消息字符串（一次性使用）
 *   {{charPrompt}} / {{charInstruction}} / {{charJailbreak}} - 角色设定字段
 *   {{charVersion}} / {{char_version}} - 角色版本
 *   {{charDepthPrompt}} - 角色深度提示
 *   {{creatorNotes}} - 创作者备注
 *   {{firstMessage}} - 角色开场白
 *   {{groupNotMuted}} - 群组未静音成员
 *   {{notChar}} - 非当前角色的其他成员
 *   <USER>, <BOT>, <CHAR>, <CHARIFNOTGROUP>, <GROUP> - 旧式标记 (预处理器)
 */
function substituteParams(content, env) {
    if (!content || typeof content !== 'string') return '';

    const safeEnv = { ...env };
    ensureEnvFunctions(safeEnv);

    let result = content;

    // ============================================================
    // 预处理器：将旧式标记转换为新式宏
    // ============================================================
    result = result
        .replace(/<USER>/gi, '{{user}}')
        .replace(/<BOT>/gi, '{{char}}')
        .replace(/<CHAR>/gi, '{{char}}')
        .replace(/<CHARIFNOTGROUP>/gi, '{{charIfNotGroup}}')
        .replace(/<GROUP>/gi, '{{group}}');

    // ============================================================
    // 多轮替换（处理嵌套宏）
    // ============================================================
    const MAX_PASSES = 5;
    for (let pass = 0; pass < MAX_PASSES; pass++) {
        const before = result;

        // 先处理 {{trim}}
        result = result.replace(/(?:\r?\n)*{{trim}}(?:\r?\n)*/gi, '');

        // 如果没有更多 {{ ，提前退出
        if (!result.includes('{{')) break;

        // 替换各类宏
        result = replaceAllMacros(result, safeEnv);

        if (result === before) break; // 没有变化，退出
    }

    // ============================================================
    // 后处理器
    // ============================================================
    // 去除转义的大括号
    result = result.replace(/\\([{}])/g, '$1');
    // 再次处理 trim
    result = result.replace(/(?:\r?\n)*{{trim}}(?:\r?\n)*/gi, '');

    return result;
}

/**
 * 确保 env 中有必要的函数
 */
function ensureEnvFunctions(env) {
    if (!env.functions) env.functions = {};
    if (typeof env.functions.original !== 'function') {
        let used = false;
        env.functions.original = () => {
            if (used) return '';
            used = true;
            return env.originalMessage || '';
        };
    }
    if (typeof env.functions.postProcess !== 'function') {
        env.functions.postProcess = (x) => x;
    }
}

/**
 * 替换所有已知宏
 */
function replaceAllMacros(str, env) {
    // 注释宏 {{// ...}}
    str = str.replace(/\{\{\/\/([\s\S]*?)\}\}/gm, '');

    // {{noop}}
    str = str.replace(/{{noop}}/gi, '');

    // 简单替换宏（无参数）
    const simpleMacros = {
        'user': () => env.names?.user || '',
        'char': () => env.names?.char || '',
        'charIfNotGroup': () => env.names?.charIfNotGroup || env.names?.char || '',
        'group': () => env.names?.group || env.names?.char || '',
        'groupNotMuted': () => env.names?.groupNotMuted || env.names?.group || env.names?.char || '',
        'notChar': () => env.names?.notChar || env.names?.user || '',
        'description': () => env.character?.description || '',
        'personality': () => env.character?.personality || '',
        'scenario': () => env.character?.scenario || '',
        'persona': () => env.character?.persona || '',
        'mesExamples': () => env.character?.mesExamples || '',
        'mesExamplesRaw': () => env.character?.mesExamplesRaw || env.character?.mesExamples || '',
        'model': () => env.system?.model || '',
        'charPrompt': () => env.character?.charPrompt || '',
        'charInstruction': () => env.character?.charInstruction || env.character?.charJailbreak || '',
        'charJailbreak': () => env.character?.charInstruction || env.character?.charJailbreak || '',
        'charVersion': () => env.character?.charVersion || '',
        'char_version': () => env.character?.charVersion || '',
        'charDepthPrompt': () => env.character?.charDepthPrompt || '',
        'creatorNotes': () => env.character?.creatorNotes || '',
        'firstMessage': () => env.character?.firstMessage || '',
        'newline': () => '\n',
        'input': () => '',
        'idle_duration': () => 'just now',
        'lastMessage': () => env.chatInfo?.lastMessage || '',
        'lastUserMessage': () => env.chatInfo?.lastUserMessage || '',
        'lastCharMessage': () => env.chatInfo?.lastCharMessage || '',
        'lastMessageId': () => String(env.chatInfo?.lastMessageId ?? ''),
        'firstIncludedMessageId': () => String(env.chatInfo?.firstIncludedMessageId ?? ''),
        'firstDisplayedMessageId': () => String(env.chatInfo?.firstDisplayedMessageId ?? ''),
        'lastSwipeId': () => String(env.chatInfo?.lastSwipeId ?? ''),
        'currentSwipeId': () => String(env.chatInfo?.currentSwipeId ?? ''),
        'allChatRange': () => {
            const len = env.chatInfo?.chatLength || 0;
            return len === 0 ? '' : `0-${len - 1}`;
        },
        'maxPrompt': () => String(env.system?.maxPromptTokens || ''),
        'maxPromptTokens': () => String(env.system?.maxPromptTokens || ''),
        'maxContext': () => String(env.system?.maxContextTokens || ''),
        'maxContextTokens': () => String(env.system?.maxContextTokens || ''),
        'maxResponse': () => String(env.system?.maxResponseTokens || ''),
        'maxResponseTokens': () => String(env.system?.maxResponseTokens || ''),
    };

    for (const [name, fn] of Object.entries(simpleMacros)) {
        const regex = new RegExp(`{{${name}}}`, 'gi');
        str = str.replace(regex, () => {
            try { return String(fn()); } catch (_) { return ''; }
        });
    }

    // {{original}}
    str = str.replace(/{{original}}/gi, () => {
        try { return String(env.functions.original?.() || ''); } catch (_) { return ''; }
    });

    // {{time}}
    str = str.replace(/{{time}}/gi, () => new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }));

    // {{date}}
    str = str.replace(/{{date}}/gi, () => new Date().toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' }));

    // {{weekday}}
    str = str.replace(/{{weekday}}/gi, () => new Date().toLocaleDateString('en-US', { weekday: 'long' }));

    // {{isotime}}
    str = str.replace(/{{isotime}}/gi, () => {
        const d = new Date();
        return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    });

    // {{isodate}}
    str = str.replace(/{{isodate}}/gi, () => {
        const d = new Date();
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    });

    // {{datetimeformat ...}}
    str = str.replace(/{{datetimeformat +([^}]*)}}/gi, (_, format) => {
        try {
            const d = new Date();
            const f = format.replace(/YYYY/g, String(d.getFullYear()))
                .replace(/MM/g, String(d.getMonth() + 1).padStart(2, '0'))
                .replace(/DD/g, String(d.getDate()).padStart(2, '0'))
                .replace(/HH/g, String(d.getHours()).padStart(2, '0'))
                .replace(/mm/g, String(d.getMinutes()).padStart(2, '0'))
                .replace(/ss/g, String(d.getSeconds()).padStart(2, '0'));
            return f;
        } catch (_) { return ''; }
    });

    // {{time_UTC+/-...}}
    str = str.replace(/{{time_UTC([+-]\d+)}}/gi, (_, offset) => {
        try {
            const utc = new Date();
            const localOffset = parseInt(offset, 10);
            const utcHours = utc.getUTCHours();
            const localHours = (utcHours + localOffset + 24) % 24;
            const mins = utc.getUTCMinutes();
            const ampm = localHours >= 12 ? 'PM' : 'AM';
            const h12 = localHours % 12 || 12;
            return `${h12}:${String(mins).padStart(2, '0')} ${ampm}`;
        } catch (_) { return ''; }
    });

    // {{random::...}} 和 {{pick::...}}
    str = str.replace(/{{(random|pick)\s?::?([^}]+)}}/gi, (match, type, listString) => {
        const isPick = type.toLowerCase() === 'pick';
        const list = listString.includes('::')
            ? listString.split('::')
            : listString.replace(/\\,/g, '##COMMA##').split(',').map(s => s.trim().replace(/##COMMA##/g, ','));
        if (list.length === 0) return '';
        let rng;
        if (isPick) {
            // 基于字符串哈希的伪随机
            const hash = simpleHash(match + str.indexOf(match));
            rng = () => (Math.abs(hash) % 10000) / 10000;
        } else {
            rng = Math.random;
        }
        const idx = Math.floor(rng() * list.length);
        return list[idx] || '';
    });

    // {{roll:...}}
    str = str.replace(/{{roll[ :]([^}]+)}}/gi, (_, formula) => {
        try {
            const trimmed = formula.trim();
            const diceMatch = trimmed.match(/^(\d*)d(\d+)([+-]\d+)?$/i);
            if (diceMatch) {
                const count = parseInt(diceMatch[1]) || 1;
                const sides = parseInt(diceMatch[2]);
                const mod = parseInt(diceMatch[3]) || 0;
                let total = mod;
                for (let i = 0; i < count; i++) {
                    total += Math.floor(Math.random() * sides) + 1;
                }
                return String(total);
            }
            if (/^\d+$/.test(trimmed)) {
                // 纯数字 = 1dN
                return String(Math.floor(Math.random() * parseInt(trimmed)) + 1);
            }
            return '';
        } catch (_) { return ''; }
    });

    // {{reverse:...}}
    str = str.replace(/{{reverse:(.+?)}}/gi, (_, s) => Array.from(s).reverse().join(''));

    // {{timeDiff::...::...}}
    str = str.replace(/{{timeDiff::(.*?)::(.*?)}}/gi, (_, t1, t2) => {
        try {
            const d1 = new Date(t1);
            const d2 = new Date(t2);
            const diff = Math.abs(d1 - d2);
            const seconds = Math.floor(diff / 1000);
            if (seconds < 60) return `${seconds} seconds`;
            const minutes = Math.floor(seconds / 60);
            if (minutes < 60) return `${minutes} minutes`;
            const hours = Math.floor(minutes / 60);
            if (hours < 24) return `${hours} hours`;
            const days = Math.floor(hours / 24);
            return `${days} days`;
        } catch (_) {
            return '';
        }
    });

    // {{outlet::...}}
    str = str.replace(/{{outlet::(.+?)}}/gi, (_, key) => {
        return (env.outlets && env.outlets[key.trim()]) || '';
    });

    // {{banned "..."}}
    str = str.replace(/{{banned "(.*)"}}/gi, () => '');

    // 动态宏（来自 env.dynamicMacros）
    if (env.dynamicMacros && typeof env.dynamicMacros === 'object') {
        for (const [key, value] of Object.entries(env.dynamicMacros)) {
            const regex = new RegExp(`{{${escapeRegex(key)}}}`, 'gi');
            str = str.replace(regex, () => {
                try {
                    return typeof value === 'function' ? String(value()) : String(value ?? '');
                } catch (_) { return ''; }
            });
        }
    }

    return str;
}

/**
 * 简单的字符串哈希函数
 */
function simpleHash(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        const char = str.charCodeAt(i);
        hash = ((hash << 5) - hash) + char;
        hash |= 0;
    }
    return hash;
}

/**
 * 转义正则表达式特殊字符
 */
function escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// ============================================================================
// Message / MessageCollection / ChatCompletion 类
// ============================================================================

/**
 * 消息类 - 管理单条消息及其 token 计数
 */
class Message {
    constructor(role, content, identifier) {
        this.identifier = identifier;
        this.role = role || 'system';
        this.content = content || '';
        this.name = '';
        this.tool_calls = null;
        this.signature = null;
        this.reasoning = null;
        this.tokens = 0;
    }

    /**
     * 异步创建消息并计算 token 数
     */
    static async createAsync(role, content, identifier, tokenCounter) {
        const message = new Message(role, content, identifier);
        if (typeof message.content === 'string' && message.content.length > 0) {
            message.tokens = await tokenCounter.countAsync(
                { role: message.role, content: message.content }
            );
        }
        return message;
    }

    /**
     * 从 Prompt 对象创建消息
     */
    static async fromPromptAsync(prompt, tokenCounter) {
        return Message.createAsync(prompt.role, prompt.content, prompt.identifier, tokenCounter);
    }

    async setName(name, tokenCounter) {
        this.name = name;
        if (tokenCounter) {
            this.tokens = await tokenCounter.countAsync({
                role: this.role,
                content: this.content,
                name: this.name,
            });
        }
    }

    getTokens() { return this.tokens; }
}

/**
 * 消息集合类 - 管理一组相关的消息
 */
class MessageCollection {
    constructor(identifier, ...items) {
        this.identifier = identifier;
        this.collection = [];
        for (const item of items) {
            if (item instanceof Message || item instanceof MessageCollection) {
                this.collection.push(item);
            }
        }
    }

    add(item) {
        this.collection.push(item);
    }

    getTokens() {
        return this.collection.reduce((tokens, item) => tokens + item.getTokens(), 0);
    }

    getChat() {
        const chat = [];
        for (const item of this.collection) {
            if (item instanceof MessageCollection) {
                chat.push(...item.getChat());
            } else if (item instanceof Message && (item.content || item.tool_calls)) {
                const msg = { role: item.role, content: item.content };
                if (item.name) msg.name = item.name;
                if (item.tool_calls) msg.tool_calls = item.tool_calls;
                if (item.signature) msg.signature = item.signature;
                if (item.reasoning) msg.reasoning = item.reasoning;
                chat.push(msg);
            }
        }
        return chat;
    }

    hasItemWithIdentifier(identifier) {
        return this.collection.some(m => m.identifier === identifier);
    }

    flatten() {
        const result = [];
        for (const item of this.collection) {
            if (item instanceof MessageCollection) {
                result.push(...item.flatten());
            } else {
                result.push(item);
            }
        }
        return result;
    }
}

/**
 * TokenBudgetExceededError - token 预算超限
 */
class TokenBudgetExceededError extends Error {
    constructor(identifier = '') {
        super(`Token budget exceeded. Message: ${identifier}`);
        this.name = 'TokenBudgetExceededError';
    }
}

/**
 * IdentifierNotFoundError - 标识符未找到
 */
class IdentifierNotFoundError extends Error {
    constructor(identifier) {
        super(`Identifier ${identifier} not found.`);
        this.name = 'IdentifierNotFoundError';
    }
}

/**
 * ChatCompletion 类 - 管理完整的 Chat Completion 上下文
 * 使用 token 预算来控制哪些内容可以放入上下文窗口
 */
class ChatCompletion {
    constructor() {
        this.tokenBudget = 0;
        this.messages = new MessageCollection('root');
        this.overriddenPrompts = [];
        this._loggingEnabled = false;
    }

    enableLogging() { this._loggingEnabled = true; }
    disableLogging() { this._loggingEnabled = false; }

    log(output) {
        if (this._loggingEnabled) {
            console.log('[ChatCompletion] ' + output);
        }
    }

    setTokenBudget(context, response) {
        this.log(`Prompt tokens: ${context}`);
        this.log(`Completion tokens: ${response}`);
        this.tokenBudget = context - response;
        this.log(`Token budget: ${this.tokenBudget}`);
    }

    /**
     * 添加消息集合
     */
    add(collection, position = null) {
        this._validateMessageCollection(collection);
        this._checkTokenBudget(collection, collection.identifier);

        if (position !== null && position !== -1) {
            this.messages.collection[position] = collection;
        } else {
            this.messages.collection.push(collection);
        }
        this.tokenBudget -= collection.getTokens();
        this.log(`Added ${collection.identifier}. Remaining tokens: ${this.tokenBudget}`);
        return this;
    }

    /**
     * 在指定集合的开头插入消息
     */
    insertAtStart(message, identifier) {
        this.insert(message, identifier, 'start');
    }

    /**
     * 在指定集合的末尾插入消息
     */
    insertAtEnd(message, identifier) {
        this.insert(message, identifier, 'end');
    }

    /**
     * 在指定集合中插入消息（start 或 end）
     */
    insert(message, identifier, position = 'end') {
        this._validateMessage(message);
        this._checkTokenBudget(message, message.identifier);

        const index = this._findMessageIndex(identifier);
        if (message.content || message.tool_calls) {
            if (position === 'start') {
                this.messages.collection[index].collection.unshift(message);
            } else {
                this.messages.collection[index].collection.push(message);
            }
            this.tokenBudget -= message.getTokens();
            this.log(`Inserted ${message.identifier} into ${identifier}. Remaining tokens: ${this.tokenBudget}`);
        }
    }

    /**
     * 检查是否有足够预算
     */
    canAfford(message) {
        return this.tokenBudget - message.getTokens() >= 0;
    }

    /**
     * 检查是否能负担所有消息
     */
    canAffordAll(messages) {
        const totalTokens = messages.reduce((sum, m) => sum + m.getTokens(), 0);
        return this.tokenBudget - totalTokens >= 0;
    }

    /**
     * 检查集合中是否存在指定标识符
     */
    has(identifier) {
        return this.messages.hasItemWithIdentifier(identifier);
    }

    /**
     * 预留预算
     */
    reserveBudget(messageOrTokens) {
        const tokens = typeof messageOrTokens === 'number'
            ? messageOrTokens
            : messageOrTokens.getTokens();
        this.tokenBudget -= tokens;
    }

    /**
     * 释放预算
     */
    freeBudget(message) {
        this.tokenBudget += message.getTokens();
    }

    /**
     * 获取最终的聊天消息数组
     */
    getChat() {
        return this.messages.getChat();
    }

    /**
     * 合并连续的系统消息
     */
    async squashSystemMessages(excludeList = ['newMainChat', 'newChat', 'groupNudge']) {
        this.messages.collection = this.messages.flatten();
        const squashed = [];
        let lastMessage = null;

        for (const message of this.messages.collection) {
            if (message.role === 'system' && !message.content) continue;

            const shouldSquash = (m) =>
                !excludeList.includes(m.identifier) &&
                m.role === 'system' &&
                !m.name;

            if (shouldSquash(message)) {
                if (lastMessage && shouldSquash(lastMessage)) {
                    lastMessage.content += '\n' + message.content;
                    lastMessage.tokens += message.tokens;
                } else {
                    squashed.push(message);
                    lastMessage = message;
                }
            } else {
                squashed.push(message);
                lastMessage = message;
            }
        }
        this.messages.collection = squashed;
    }

    _validateMessageCollection(collection) {
        if (!(collection instanceof MessageCollection)) {
            throw new Error('Argument must be an instance of MessageCollection');
        }
    }

    _validateMessage(message) {
        if (!(message instanceof Message)) {
            throw new Error('Argument must be an instance of Message');
        }
    }

    _checkTokenBudget(message, identifier) {
        if (!this.canAfford(message)) {
            throw new TokenBudgetExceededError(identifier);
        }
    }

    _findMessageIndex(identifier) {
        const index = this.messages.collection.findIndex(
            item => item?.identifier === identifier
        );
        if (index < 0) {
            throw new IdentifierNotFoundError(identifier);
        }
        return index;
    }

    getOverriddenPrompts() {
        return this.overriddenPrompts || [];
    }

    setOverriddenPrompts(list) {
        this.overriddenPrompts = list;
    }
}

// ============================================================================
// Token 处理器
// ============================================================================

class TokenHandler {
    constructor(countTokenFn) {
        this._countTokenFn = countTokenFn;
        this.counts = {};
    }

    async countAsync(messages, full) {
        if (!Array.isArray(messages)) {
            messages = [messages];
        }
        let total = 0;
        for (const msg of messages) {
            total += await this._countTokenFn(msg, full);
        }
        return total;
    }

    getTotal() {
        return Object.values(this.counts).reduce((a, b) => a + (isNaN(b) ? 0 : b), 0);
    }
}

// ============================================================================
// Prompt 构建主函数
// ============================================================================

/**
 * 将酒馆内部消息格式转换为 OpenAI 消息格式
 */
function convertToOpenAIMessages(chatMessages, options = {}) {
    const {
        namesBehavior = CHARACTER_NAMES_BEHAVIOR.DEFAULT,
        userName = 'User',
        charName = 'Assistant',
        isGroup = false,
        groupNames = [],
    } = options;

    const messages = [];
    // 反向遍历（原代码逻辑：i 从末尾递减，j 从开头递增）
    let j = 0;
    for (let i = chatMessages.length - 1; i >= 0; i--) {
        if (j >= chatMessages.length) break;

        const chatMsg = chatMessages[j];
        let role = chatMsg.is_user ? 'user' : 'assistant';
        let content = chatMsg.content || chatMsg.mes || '';

        // 系统旁白消息
        if (chatMsg.extra?.type === 'narrator' || chatMsg.is_system) {
            role = 'system';
        }

        // 根据 names_behavior 处理名称前缀
        const msgName = chatMsg.name || (chatMsg.is_user ? userName : charName);
        switch (namesBehavior) {
            case CHARACTER_NAMES_BEHAVIOR.NONE:
                break;
            case CHARACTER_NAMES_BEHAVIOR.DEFAULT:
                if ((isGroup && msgName !== userName) ||
                    (chatMsg.force_avatar && msgName !== userName &&
                        chatMsg.extra?.type !== 'narrator')) {
                    content = `${msgName}: ${content}`;
                }
                break;
            case CHARACTER_NAMES_BEHAVIOR.CONTENT:
                if (chatMsg.extra?.type !== 'narrator') {
                    content = `${msgName}: ${content}`;
                }
                break;
            case CHARACTER_NAMES_BEHAVIOR.COMPLETION:
                break;
        }

        content = content.replace(/\r/gm, '');

        messages[i] = {
            role,
            content,
            name: msgName,
            // 保留额外信息供后续处理
            _extra: chatMsg.extra,
            _reasoning: chatMsg.extra?.reasoning || '',
            _invocations: chatMsg.extra?.tool_invocations,
        };
        j++;
    }

    return messages;
}

/**
 * 格式化世界信息（应用 WI 格式模板）
 */
function formatWorldInfo(value, wiFormat) {
    if (!value) return '';
    if (!wiFormat || !wiFormat.trim()) return value;
    // 简单的 {0} 替换
    return wiFormat.replace(/\{0\}/g, value);
}

/**
 * buildTavernPrompt 的主函数
 * 
 * @param {Object} params - 参数对象
 * @param {Object} params.character - 角色设定
 * @param {string} params.character.name - 角色名
 * @param {string} [params.character.description] - 角色描述
 * @param {string} [params.character.personality] - 角色性格
 * @param {string} [params.character.scenario] - 场景
 * @param {string} [params.character.mes_example] - 示例对话
 * @param {string} [params.character.first_mes] - 开场白
 * @param {string} [params.character.system_prompt] - 系统提示 (main prompt override)
 * @param {string} [params.character.post_history_instructions] - jailbreak
 * @param {string} [params.character.creator_notes] - 创作者备注
 * @param {string} [params.character.character_version] - 角色版本
 * @param {string} [params.character.depth_prompt] - 深度提示
 * @param {string} params.userName - 用户名
 * @param {string} [params.userPersona] - 用户 Persona 描述
 * @param {Object[]} params.messages - 历史消息数组
 * @param {Object} [params.options] - 配置选项
 * @param {number} [params.options.maxContextTokens=4096] - 最大上下文 token 数
 * @param {number} [params.options.maxResponseTokens=300] - 最大响应 token 数
 * @param {string} [params.options.mainPrompt] - 主提示模板
 * @param {string} [params.options.jailbreakPrompt] - jailbreak 提示
 * @param {string} [params.options.nsfwPrompt] - NSFW 提示
 * @param {string} [params.options.impersonationPrompt] - 扮演提示
 * @param {string} [params.options.newChatPrompt] - 新聊天提示
 * @param {string} [params.options.newExampleChatPrompt] - 新示例聊天提示
 * @param {string} [params.options.wiFormat='{0}'] - 世界信息格式
 * @param {string} [params.options.scenarioFormat='{{scenario}}'] - 场景格式
 * @param {string} [params.options.personalityFormat='{{personality}}'] - 性格格式
 * @param {number} [params.options.namesBehavior=0] - 名称前缀行为
 * @param {string} [params.options.continuePostfix=' '] - 续写后缀
 * @param {boolean} [params.options.squashSystemMessages=false] - 合并系统消息
 * @param {string} [params.options.worldInfoBefore] - 前置世界信息
 * @param {string} [params.options.worldInfoAfter] - 后置世界信息
 * @param {string} [params.options.bias] - 提示偏置
 * @param {Object} [params.options.dynamicMacros] - 额外的动态宏
 * @param {boolean} [params.options.pinExamples=false] - 是否固定示例对话
 * @param {string} [params.options.modelName] - 模型名称（用于 {{model}} 宏）
 * @param {Function} [params.options.customTokenCounter] - 自定义 token 计数函数
 * 
 * @returns {Promise<Object>} - 包含 messages 数组和 token 信息
 */
async function buildTavernPrompt(params) {
    const {
        character = {},
        userName = 'User',
        userPersona = '',
        messages: inputMessages = [],
        options = {},
    } = params;

    // 解构角色设定
    const charName = character.name || 'Assistant';
    const charDescription = character.description || '';
    const charPersonality = character.personality || '';
    const scenario = character.scenario || '';
    const mesExample = character.mes_example || '';
    const firstMes = character.first_mes || '';
    const systemPrompt = character.system_prompt || '';
    const jailbreakOverride = character.post_history_instructions || '';
    const creatorNotes = character.creator_notes || '';
    const charVersion = character.character_version || '';
    const depthPrompt = character.depth_prompt || '';

    // 解构选项
    const maxContextTokens = options.maxContextTokens || 4096;
    const maxResponseTokens = options.maxResponseTokens || 300;
    const mainPrompt = options.mainPrompt || `Write ${charName}'s next reply in a fictional chat between ${charName} and ${userName}.`;
    const jailbreakPrompt = options.jailbreakPrompt || '';
    const nsfwPrompt = options.nsfwPrompt || '';
    const impersonationPrompt = options.impersonationPrompt || '';
    const newChatPrompt = options.newChatPrompt || '[Start a new Chat]';
    const newExampleChatPrompt = options.newExampleChatPrompt || '[Example Chat]';
    const wiFormat = options.wiFormat || '{0}';
    const scenarioFormat = options.scenarioFormat || '{{scenario}}';
    const personalityFormat = options.personalityFormat || '{{personality}}';
    const namesBehavior = options.namesBehavior ?? CHARACTER_NAMES_BEHAVIOR.DEFAULT;
    const continuePostfix = options.continuePostfix || CONTINUE_POSTFIX_TYPES.SPACE;
    const squashSystemMsgs = options.squashSystemMessages || false;
    const worldInfoBefore = options.worldInfoBefore || '';
    const worldInfoAfter = options.worldInfoAfter || '';
    const bias = options.bias || '';
    const dynamicMacros = options.dynamicMacros || {};
    const pinExamples = options.pinExamples || false;
    const modelName = options.modelName || '';

    // ============================================================
    // 设置 token 计数器
    // ============================================================
    const encoding = tiktokenModel || tiktokenEncoding;
    const countTokenFn = options.customTokenCounter || (async (msg) => countMessageTokens(msg, encoding));
    const tokenHandler = new TokenHandler(countTokenFn);

    // ============================================================
    // 构建宏替换环境 (env)
    // ============================================================
    const mesExamplesArray = parseMesExamples(mesExample);

    const env = {
        names: {
            user: userName,
            char: charName,
            charIfNotGroup: charName,
            group: charName,
            groupNotMuted: charName,
            notChar: userName,
        },
        character: {
            description: charDescription,
            personality: charPersonality,
            scenario: scenario,
            persona: userPersona,
            mesExamples: mesExamplesArray.join(''),
            mesExamplesRaw: mesExample,
            charPrompt: systemPrompt,
            charInstruction: jailbreakOverride,
            charJailbreak: jailbreakOverride,
            charVersion: charVersion,
            charDepthPrompt: depthPrompt,
            creatorNotes: creatorNotes,
            firstMessage: firstMes,
        },
        system: {
            model: modelName,
            maxPromptTokens: maxContextTokens,
            maxContextTokens: maxContextTokens,
            maxResponseTokens: maxResponseTokens,
        },
        functions: {
            original: () => '',
            postProcess: (x) => x,
        },
        dynamicMacros: dynamicMacros || {},
        chatInfo: {
            lastMessage: inputMessages.length > 0
                ? (inputMessages[inputMessages.length - 1].content || inputMessages[inputMessages.length - 1].mes || '')
                : '',
            lastUserMessage: (() => {
                for (let i = inputMessages.length - 1; i >= 0; i--) {
                    if (inputMessages[i].is_user) return inputMessages[i].content || inputMessages[i].mes || '';
                }
                return '';
            })(),
            lastCharMessage: (() => {
                for (let i = inputMessages.length - 1; i >= 0; i--) {
                    if (!inputMessages[i].is_user && !inputMessages[i].is_system)
                        return inputMessages[i].content || inputMessages[i].mes || '';
                }
                return '';
            })(),
            chatLength: inputMessages.length,
        },
        outlets: {},
    };

    // ============================================================
    // 创建 ChatCompletion 实例
    // ============================================================
    const chatCompletion = new ChatCompletion();
    chatCompletion.setTokenBudget(maxContextTokens, maxResponseTokens);

    try {
        // ============================================================
        // 步骤 1: 准备系统 Prompt 集合
        // ============================================================

        // 应用宏替换并格式化
        const scenarioText = scenario && scenarioFormat
            ? substituteParams(scenarioFormat, env)
            : (scenario || '');
        const personalityText = charPersonality && personalityFormat
            ? substituteParams(personalityFormat, env)
            : (charPersonality || '');

        // 创建系统 prompt 条目
        const systemPromptEntries = [
            { role: 'system', content: formatWorldInfo(worldInfoBefore, wiFormat), identifier: 'worldInfoBefore' },
            { role: 'system', content: formatWorldInfo(worldInfoAfter, wiFormat), identifier: 'worldInfoAfter' },
            { role: 'system', content: charDescription, identifier: 'charDescription' },
            { role: 'system', content: personalityText, identifier: 'charPersonality' },
            { role: 'system', content: scenarioText, identifier: 'scenario' },
        ];

        if (userPersona) {
            systemPromptEntries.push({ role: 'system', content: userPersona, identifier: 'personaDescription' });
        }

        // 主 Prompt（应用宏替换）
        const mainPromptContent = systemPrompt
            ? substituteParams(systemPrompt, env)
            : substituteParams(mainPrompt, env);
        systemPromptEntries.push({ role: 'system', content: mainPromptContent, identifier: 'main' });

        // NSFW / Jailbreak
        if (jailbreakOverride) {
            systemPromptEntries.push({
                role: 'system',
                content: substituteParams(jailbreakOverride, env),
                identifier: 'jailbreak',
            });
        } else if (jailbreakPrompt) {
            systemPromptEntries.push({
                role: 'system',
                content: substituteParams(jailbreakPrompt, env),
                identifier: 'jailbreak',
            });
        }

        if (nsfwPrompt) {
            systemPromptEntries.push({
                role: 'system',
                content: substituteParams(nsfwPrompt, env),
                identifier: 'nsfw',
            });
        }

        if (impersonationPrompt) {
            systemPromptEntries.push({
                role: 'system',
                content: substituteParams(impersonationPrompt, env),
                identifier: 'impersonate',
            });
        }

        // Bias
        if (bias && bias.trim()) {
            systemPromptEntries.push({
                role: 'assistant',
                content: substituteParams(bias.trim(), env),
                identifier: 'bias',
            });
        }

        // ============================================================
        // 步骤 2: 添加主要内容到 ChatCompletion
        // ============================================================
        for (const entry of systemPromptEntries) {
            if (!entry.content || !entry.content.trim()) continue;
            const message = await Message.createAsync(
                entry.role, entry.content, entry.identifier, tokenHandler
            );
            const collection = new MessageCollection(entry.identifier);
            collection.add(message);

            // 检查是否能负担这条消息
            if (!chatCompletion.canAfford(collection)) {
                throw new TokenBudgetExceededError(entry.identifier);
            }
            chatCompletion.reserveBudget(collection);
            chatCompletion.messages.collection.push(collection);
        }

        // ============================================================
        // 步骤 3: 添加示例对话
        // ============================================================
        if (mesExamplesArray.length > 0) {
            const examplesCollection = new MessageCollection('dialogueExamples');
            const newExampleMsg = await Message.createAsync(
                'system',
                substituteParams(newExampleChatPrompt, env),
                'newExampleChat',
                tokenHandler
            );

            for (const dialogue of formatMessageExamples(mesExamplesArray, userName, charName)) {
                const dialogueMessages = [];
                for (let idx = 0; idx < dialogue.length; idx++) {
                    const prompt = dialogue[idx];
                    const msg = await Message.createAsync(
                        prompt.role || 'system',
                        prompt.content || '',
                        `dialogueExamples-${mesExamplesArray.indexOf(dialogue)}-${idx}`,
                        tokenHandler
                    );
                    if (prompt.name) await msg.setName(prompt.name, tokenHandler);
                    dialogueMessages.push(msg);
                }

                if (!chatCompletion.canAffordAll([newExampleMsg, ...dialogueMessages])) {
                    break;
                }

                chatCompletion.reserveBudget(newExampleMsg);
                examplesCollection.add(newExampleMsg);
                for (const dm of dialogueMessages) {
                    chatCompletion.reserveBudget(dm);
                    examplesCollection.add(dm);
                }
            }

            if (examplesCollection.collection.length > 0) {
                chatCompletion.messages.collection.push(examplesCollection);
            }
        }

        // ============================================================
        // 步骤 4: 添加新聊天标记
        // ============================================================
        const newChatMsg = await Message.createAsync(
            'system',
            substituteParams(newChatPrompt, env),
            'newMainChat',
            tokenHandler
        );
        if (chatCompletion.canAfford(newChatMsg)) {
            chatCompletion.reserveBudget(newChatMsg);
            const newChatCollection = new MessageCollection('newChat');
            newChatCollection.add(newChatMsg);
            chatCompletion.messages.collection.push(newChatCollection);
        }

        // ============================================================
        // 步骤 5: 添加聊天历史
        // ============================================================
        const historyCollection = new MessageCollection('chatHistory');

        // 转换消息格式
        const openaiMessages = convertToOpenAIMessages(inputMessages, {
            namesBehavior,
            userName,
            charName,
            isGroup: false,
        });

        // 反向处理（从最新到最旧），通过 insertAtStart 保持正确顺序
        const chatPool = [...openaiMessages].reverse();
        for (let index = 0; index < chatPool.length; index++) {
            const chatPrompt = chatPool[index];

            const msg = await Message.createAsync(
                chatPrompt.role,
                chatPrompt.content,
                `chatHistory-${index}`,
                tokenHandler
            );

            // 处理名称（names_behavior = COMPLETION 时使用 name 字段）
            if (namesBehavior === CHARACTER_NAMES_BEHAVIOR.COMPLETION && chatPrompt.name) {
                const sanitizedName = chatPrompt.name.replace(/[^a-zA-Z0-9_-]/g, '_');
                await msg.setName(sanitizedName, tokenHandler);
            }

            if (chatCompletion.canAfford(msg)) {
                historyCollection.collection.unshift(msg); // insertAtStart
                chatCompletion.reserveBudget(msg);
            } else {
                break;
            }
        }

        if (historyCollection.collection.length > 0) {
            chatCompletion.messages.collection.push(historyCollection);
        }

        // ============================================================
        // 步骤 6: 合并系统消息（可选）
        // ============================================================
        if (squashSystemMsgs) {
            await chatCompletion.squashSystemMessages();
        }

        // ============================================================
        // 步骤 7: 获取最终输出
        // ============================================================
        const finalChat = chatCompletion.getChat();
        const totalTokens = chatCompletion.messages.getTokens();
        const remainingBudget = chatCompletion.tokenBudget;

        return {
            messages: finalChat,
            tokenInfo: {
                totalTokens,
                remainingBudget,
                maxContextTokens,
                maxResponseTokens,
                tokenBudget: maxContextTokens - maxResponseTokens,
            },
        };
    } catch (error) {
        if (error instanceof TokenBudgetExceededError) {
            // 预算超限时，返回已构建的内容（丢弃最旧的历史消息直到满足预算）
            const finalChat = chatCompletion.getChat();
            return {
                messages: finalChat,
                tokenInfo: {
                    totalTokens: chatCompletion.messages.getTokens(),
                    remainingBudget: chatCompletion.tokenBudget,
                    maxContextTokens,
                    maxResponseTokens,
                    tokenBudget: maxContextTokens - maxResponseTokens,
                    warning: 'Token budget exceeded. Some content may have been truncated.',
                    error: error.message,
                },
            };
        }
        throw error;
    }
}

// ============================================================================
// 导出
// ============================================================================
const MacroEngine = {
    evaluate: substituteParams,
};
async function getTokenCountAsync(text) {
    if (tiktokenEncoding) {
        return countWithTiktoken(text, tiktokenEncoding);
    }
    return guesstimate(text);
}

module.exports = {
    buildTavernPrompt,
    // 工具函数
    substituteParams,
    parseMesExamples,
    parseExampleIntoIndividual,
    formatMessageExamples,
    convertToOpenAIMessages,
    countMessageTokens,
    countMessagesTokens,
    guesstimate,
    getTokenCountAsync,
    // 类
    Message,
    MessageCollection,
    ChatCompletion,
    TokenHandler,
    TokenBudgetExceededError,
    IdentifierNotFoundError,
    
    // 常量
    MacroEngine,
    BYTES_PER_TOKEN,
    TOKENS_PER_MESSAGE,
    CHARACTER_NAMES_BEHAVIOR,
    CONTINUE_POSTFIX_TYPES,
};