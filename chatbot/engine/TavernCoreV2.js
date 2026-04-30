/**
 * TavernCoreV2 – 融合世界书 (Lorebook) 的工业级对话 Prompt 构造器
 *
 * 在 TavernCoreV1 基础上新增：
 * 1. 世界书前置扫描 (scanLorebook)
 * 2. 系统提示词融合 wiBefore / wiAfter
 * 3. 深度锚点精确注入 (wiDepth)
 * 4. 保持 V1 原有的 Token 预算截断逻辑
 */
const { scanLorebook } = require('./lorebook-engine.js');
const { MacroEngine, getTokenCountAsync } = require('./tavern-engine.js');

class TavernCoreV2 {
  constructor(charCard, userSettings, worldInfoEntries = [], worldInfoConfig = {}) {
    this.charCard = charCard;
    this.settings = userSettings;
    this.worldInfoEntries = worldInfoEntries;
    this.systemTools = []; // Python 端通过 server.js 注入的工具定义
    this.worldInfoConfig = {
      maxDepth: worldInfoConfig.maxDepth ?? 2,
      budget: worldInfoConfig.budget ?? 25,
      maxContext: worldInfoConfig.maxContext ?? (userSettings.openai_max_context || 8192),
      budgetCap: worldInfoConfig.budgetCap ?? 0,
      recursive: worldInfoConfig.recursive ?? true,
      globalOptions: worldInfoConfig.globalOptions ?? {},
      defaultDepth: worldInfoConfig.defaultDepth ?? 4,
    };

    this.userName = userSettings.username || 'User';
    this.charName = charCard.name || 'Assistant';
    this.namesBehavior = userSettings.names_behavior ?? 0;
  }

  _buildEnv(additionalContext = '') {
    const cc = this.charCard;
    const setName = (name) => (name || '').trim();

    return {
      names: {
        user: this.userName,
        char: this.charName,
        charIfNotGroup: this.charName,
        group: this.charName,
        groupNotMuted: this.charName,
        notChar: this.userName,
      },
      character: {
        description: setName(cc.description),
        personality: setName(cc.personality),
        scenario: setName(cc.scenario),
        persona: setName(this.settings.persona_description),
        mesExamples: setName(cc.mes_example),
        mesExamplesRaw: setName(cc.mes_example),
        charPrompt: setName(cc.system_prompt) || '',
        system_prompt: setName(cc.system_prompt) || '',   // ← 新增，供 {{system}} 宏使用
        charInstruction: setName(cc.post_history_instructions),
        charJailbreak: setName(cc.post_history_instructions),
        charVersion: setName(cc.character_version),
        charDepthPrompt: setName(cc.depth_prompt || cc.creator_notes),
        creatorNotes: setName(cc.creator_notes),
        firstMessage: setName(cc.first_mes),
      },
      system: {
        model: '',
        maxPromptTokens: 99999,
        maxContextTokens: 99999,
        maxResponseTokens: 99999,
      },
      originalMessage: additionalContext,
      _originalUsed: false,
      dynamicMacros: {},
    };
  }

  _renderStoryString() {
    const template = this.settings.story_string_template ||
      `{{#if system}}{{system}}\n{{/if}}{{#if description}}{{description}}\n{{/if}}{{#if personality}}{{char}}'s personality: {{personality}}\n{{/if}}{{#if scenario}}Scenario: {{scenario}}\n{{/if}}`;

    let rendered = template
      .replace(/\{\{#if\s+(\w+)\}\}([\s\S]*?)\{\{\/if\}\}/g, (_, varName, block) => {
        const val = this._buildEnv()['character']?.[varName] || this._buildEnv()['names']?.[varName] || '';
        return val.trim() ? block : '';
      });

    // === 新增下面这一行 ===
    rendered = rendered.replace('{{system}}', this.charCard.data?.system_prompt || this.charCard.system_prompt || '');
    const env = this._buildEnv();
    return MacroEngine.evaluate(rendered, env).replace(/^\n+/, '').trimEnd() + '\n';
  }

  _parseExamples() {
    let examplesStr = this.charCard.mes_example || '';
    // 新增这一行：在切割前，提前将宏替换为真实名字
    examplesStr = examplesStr.replace(/\{\{user\}\}/gi, this.userName).replace(/\{\{char\}\}/gi, this.charName);
    
    if (!examplesStr.startsWith('<START>')) return [];

    const blocks = examplesStr.split(/<START>/gi).slice(1).map(b => b.trim());
    const pairs = [];

    for (const block of blocks) {
      const lines = block.split('\n');
      let currentUser = null, currentAssistant = null;
      let buf = [];

      const flush = () => {
        if (currentUser) {
          const content = buf.join('\n').replace(`${this.userName}:`, '').trim();
          if (content) pairs.push({ role: 'user', content });
        } else if (currentAssistant) {
          const content = buf.join('\n').replace(`${this.charName}:`, '').trim();
          if (content) pairs.push({ role: 'assistant', content });
        }
        buf = [];
      };

      for (const line of lines) {
        if (line.startsWith(`${this.userName}:`)) {
          flush();
          currentUser = true;
          currentAssistant = false;
        } else if (line.startsWith(`${this.charName}:`)) {
          flush();
          currentAssistant = true;
          currentUser = false;
        }
        buf.push(line);
      }
      flush();
    }

    const env = this._buildEnv();
    return pairs.map(pair => ({
      role: pair.role,
      content: MacroEngine.evaluate(pair.content, env),
    }));
  }

  _convertHistory(history) {
    const env = this._buildEnv();
    const result = [];

    for (const msg of history) {
      // 修复 Bug 1：准确判断角色
      const isUser = msg.is_user === true || msg.name === this.userName;
      let role = isUser ? 'user' : 'assistant';
      if (msg.is_system || msg.extra?.type === 'narrator') role = 'system';

      let content = msg.mes || msg.content || '';
      const charName = msg.name || (isUser ? this.userName : this.charName);

      // 根据 names_behavior 添加名称前缀（仅当确实需要时）
      if (this.namesBehavior === 2 && role !== 'system') {
        content = `${charName}: ${content}`;
      } else if (this.namesBehavior === 0) {
        if ((msg.force_avatar && charName !== this.userName && role !== 'system') ||
            (role === 'assistant' && charName !== this.charName)) {
          content = `${charName}: ${content}`;
        }
      }

      // 修复 Bug 1：清除可能自动附加的 "User: " 前缀，避免重复
      if (role === 'user' && content.startsWith(this.userName + ':')) {
        content = content.substring(this.userName.length + 1).trim();
      }

      content = MacroEngine.evaluate(content.replace(/\r/g, ''), env);
      result.push({ role, content });
    }

    return result;
  }

  _doChatInject(messages) {

    // 兼容 V2/V3 卡片规范，先找 data 层，再找顶层
    const jailbreak = this.charCard.data?.post_history_instructions || this.charCard.post_history_instructions;
    
    if (!jailbreak) return messages;

    const env = this._buildEnv();
    const jailbreakContent = MacroEngine.evaluate(jailbreak, env);
    if (!jailbreakContent.trim()) return messages;

    let insertIndex = messages.length;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'user') {
        insertIndex = i;
        break;
      }
    }

    messages.splice(insertIndex, 0, { role: 'system', content: jailbreakContent });
    return messages;
  }

  async buildOpenAIMessages(chatHistory, maxTokens) {
    // 1. 世界书扫描
    // 融入边界标记的扫描文本构建
    const MATCHER = '\x01';
    const scanText = MATCHER + chatHistory.map(m => m.mes || m.content || '').join('\n' + MATCHER);
    const lorebookResult = scanLorebook(this.worldInfoEntries, scanText, this.worldInfoConfig);
    
    // 修复 Bug 3：打印扫描结果便于调试
    console.log('--- WorldInfo Scan Result ---');
    console.log(JSON.stringify(lorebookResult, null, 2));

    const env = this._buildEnv();

    // 2. 系统消息融合 wiBefore / wiAfter
    let systemContent = '';
    if (lorebookResult.wiBefore) {
      systemContent += MacroEngine.evaluate(lorebookResult.wiBefore, env) + '\n';
    }
    systemContent += this._renderStoryString();
    if (lorebookResult.wiAfter) {
      systemContent += '\n' + MacroEngine.evaluate(lorebookResult.wiAfter, env);
    }
    const systemMsg = { role: 'system', content: systemContent };

    // 3. 深度锚点注入 (wiDepth)
    const injectedHistory = chatHistory.slice();
    const originalLength = injectedHistory.length;

    const inserts = [];
    for (const item of lorebookResult.wiDepth || []) {
      const content = MacroEngine.evaluate(item.content || '', env);
      if (!content.trim()) continue;

      const depth = item.depth ?? 0;
      const insertIndex = Math.max(0, Math.min(originalLength, originalLength - depth - 1));
      inserts.push({
        index: insertIndex,
        message: {
          is_system: true,
          is_user: false,
          mes: content,
          name: 'System',
          extra: {},
        },
      });
    }

    inserts.sort((a, b) => b.index - a.index);
    for (const ins of inserts) {
      injectedHistory.splice(ins.index, 0, ins.message);
    }

    // 4. 示例 & 历史转换
    const examples = this._parseExamples();
    let historyMsgs = this._convertHistory(injectedHistory);
    historyMsgs = this._doChatInject(historyMsgs);

    // 5. Token 预算截断
    const baseMessages = [systemMsg, ...examples];
    const finalMessages = [...baseMessages];

    const countTokens = async (msgs) => {
      const text = JSON.stringify(msgs);
      return getTokenCountAsync(text);
    };

    for (let i = historyMsgs.length - 1; i >= 0; i--) {
      const candidate = historyMsgs[i];
      const testMessages = [
        ...finalMessages.slice(0, baseMessages.length),
        candidate,
        ...finalMessages.slice(baseMessages.length),
      ];

      if (await countTokens(testMessages) <= maxTokens) {
        finalMessages.splice(baseMessages.length, 0, candidate);
      } else {
        break;
      }
    }

    const totalTokens = await countTokens(finalMessages);
    return {
      messages: finalMessages,
      tools: this.systemTools,
      tokenInfo: {
        totalTokens,
        maxTokens,
        remainingBudget: maxTokens - totalTokens,
      },
    };
  }
}

module.exports = { TavernCoreV2 };