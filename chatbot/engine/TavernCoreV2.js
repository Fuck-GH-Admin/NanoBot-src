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
const { buildXmlBlock, buildXmlBlockWithChildren, escapeXml } = require('./prompt-template.js');

class TavernCoreV2 {
  constructor(charCard, userSettings, worldInfoEntries = [], worldInfoConfig = {}) {
    this.charCard = charCard;
    this.settings = userSettings;
    this.worldInfoEntries = worldInfoEntries;
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
      // ─── 角色判定：优先信任上游 msg.role 字段 ───
      // Python agent_service 已在 _to_openai_message 中明确设置 role，
      // 无需再依赖脆弱的昵称比对。
      let role;
      const rawRole = (msg.role || '').toLowerCase();
      if (['user', 'assistant', 'system', 'tool'].includes(rawRole)) {
        role = rawRole;
      } else {
        // 兜底：兼容旧版 SillyTavern 格式（无 role 字段）
        const isUser = msg.is_user === true || msg.name === this.userName;
        role = isUser ? 'user' : 'assistant';
        if (msg.is_system || msg.extra?.type === 'narrator') role = 'system';
      }

      let content = msg.mes || msg.content || '';
      const charName = msg.name || (role === 'user' ? this.userName : this.charName);

      // 根据 names_behavior 添加名称前缀（仅当确实需要时）
      if (this.namesBehavior === 2 && role !== 'system') {
        content = `${charName}: ${content}`;
      } else if (this.namesBehavior === 0) {
        if ((msg.force_avatar && charName !== this.userName && role !== 'system') ||
            (role === 'assistant' && charName !== this.charName)) {
          content = `${charName}: ${content}`;
        }
      }

      // 清除可能自动附加的 "User: " 前缀，避免重复
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

  /**
   * 判断消息是否为系统消息（兼容 tavern 和 OpenAI 两种格式）
   */
  _isSystemMsg(msg) {
    return msg.is_system === true || msg.role === 'system' || msg.extra?.type === 'narrator';
  }

  /**
   * 在历史记录中查找指定 user_id 对应的用户名
   * @param {Array} history - 消息历史
   * @param {string} userId - 用户 ID
   * @returns {string} 用户名（未找到则返回 'Unknown'）
   */
  _findUserName(history, userId) {
    for (const msg of history) {
      const uid = String(msg.user_id || msg.extra?.user_id || '');
      if (uid === String(userId) && msg.name) {
        return msg.name;
      }
    }
    return 'Unknown';
  }

  async buildOpenAIMessages(chatHistory, maxTokens, summaryContext = '', userProfiles = {}, context = {}, relations = []) {
    // 1. 世界书扫描
    const MATCHER = '\x01';
    const scanText = MATCHER + chatHistory.map(m => m.mes || m.content || '').join('\n' + MATCHER);
    const lorebookResult = scanLorebook(this.worldInfoEntries, scanText, this.worldInfoConfig, context);

    console.log('--- WorldInfo Scan Result ---');
    console.log(JSON.stringify(lorebookResult, null, 2));

    const env = this._buildEnv();
    const cc = this.charCard;

    // 2. 构建各区块（带优先级元数据）
    const systemBlocks = []; // { name, content, priority, neverCut, items }

    // ─── 区块 1：角色扮演设定 (priority=2) ───
    const identity = MacroEngine.evaluate(cc.description || '', env).trim();
    const personality = MacroEngine.evaluate(cc.personality || '', env).trim();
    const scenario = MacroEngine.evaluate(cc.scenario || '', env).trim();
    const charPrompt = MacroEngine.evaluate(cc.data?.system_prompt || cc.system_prompt || '', env).trim();

    const roleBlock = buildXmlBlockWithChildren('role_play_setting', [
      { tag: 'main_prompt', content: charPrompt },
      { tag: 'identity', content: identity },
      { tag: 'personality', content: personality },
      { tag: 'scenario', content: scenario },
    ]);
    if (roleBlock) {
      systemBlocks.push({ name: 'role_play_setting', content: roleBlock, priority: 2, neverCut: true });
    }

    // ─── 区块 2：世界知识 (priority=6, 可裁剪) ───
    const wiBefore = lorebookResult.wiBefore ? MacroEngine.evaluate(lorebookResult.wiBefore, env).trim() : '';
    const wiAfter  = lorebookResult.wiAfter  ? MacroEngine.evaluate(lorebookResult.wiAfter, env).trim()  : '';
    const wiBeforeItems = wiBefore ? wiBefore.split('\n').filter(l => l.trim()) : [];
    const wiAfterItems = wiAfter ? wiAfter.split('\n').filter(l => l.trim()) : [];
    const wiItems = [...wiBeforeItems, ...wiAfterItems];
    const knowledgeBlock = buildXmlBlockWithChildren('world_knowledge', [
      { tag: 'before_context', content: wiBefore },
      { tag: 'after_context', content: wiAfter },
    ]);
    if (knowledgeBlock) {
      systemBlocks.push({ name: 'world_knowledge', content: knowledgeBlock, priority: 6, items: wiItems });
    }

    // ─── 区块 3：群组记忆 (priority=5) ───
    const summaryText = summaryContext?.trim() || '';
    const profileLines = [];
    if (userProfiles && typeof userProfiles === 'object' && Object.keys(userProfiles).length > 0) {
      const activeUserIds = new Set();
      for (const msg of chatHistory) {
        const uid = msg.user_id || msg.extra?.user_id;
        if (uid && !this._isSystemMsg(msg)) {
          activeUserIds.add(String(uid));
        }
      }
      for (const uid of activeUserIds) {
        if (userProfiles[uid]) {
          const name = this._findUserName(chatHistory, uid);
          profileLines.push(`[群友档案 - ${escapeXml(name)}(ID:${escapeXml(uid)}): ${escapeXml(userProfiles[uid])}]`);
        }
      }
    }

    if (summaryText || profileLines.length > 0) {
      const memoryChildren = [{ tag: 'summary', content: summaryText }];
      if (profileLines.length > 0) {
        memoryChildren.push({ tag: 'active_user_profiles', content: profileLines.join('\n') });
      }
      const memoryBlock = buildXmlBlockWithChildren('group_memory', memoryChildren);
      if (memoryBlock) {
        systemBlocks.push({ name: 'group_memory', content: memoryBlock, priority: 5, items: profileLines });
      }
    }

    // ─── 区块 4：群组动态关系图 (priority=4) ───
    const relLines = [];
    if (Array.isArray(relations) && relations.length > 0) {
      const entityNameMap = new Map();
      for (const msg of chatHistory) {
        const uid = msg.user_id || msg.extra?.user_id;
        if (uid) entityNameMap.set(`user_${uid}`, msg.name || `user_${uid}`);
      }
      if (userProfiles && typeof userProfiles === 'object') {
        for (const uid of Object.keys(userProfiles)) {
          if (!entityNameMap.has(`user_${uid}`)) entityNameMap.set(`user_${uid}`, `user_${uid}`);
        }
      }

      for (const r of relations) {
        if (!r.subject_entity || !r.predicate || !r.object_entity) continue;
        const subj = entityNameMap.get(r.subject_entity) || r.subject_entity;
        const obj = entityNameMap.get(r.object_entity) || r.object_entity;
        const conf = typeof r.confidence === 'number' ? r.confidence.toFixed(2) : '?';
        relLines.push(`[关系] ${escapeXml(subj)} -${escapeXml(r.predicate)}-> ${escapeXml(obj)} (置信度 ${conf})`);
      }

      if (relLines.length > 0) {
        const dynamicsBlock = buildXmlBlock('group_dynamics', relLines.join('\n'));
        systemBlocks.push({ name: 'group_dynamics', content: dynamicsBlock, priority: 4, items: relLines });
      }
    }

    // ─── 区块 5：系统指令 (priority=1, neverCut) ───
    const jailbreak = cc.data?.post_history_instructions || cc.post_history_instructions || '';
    const jailbreakText = MacroEngine.evaluate(jailbreak, env).trim();
    if (jailbreakText) {
      systemBlocks.push({ name: 'system_directives', content: buildXmlBlock('system_directives', jailbreakText), priority: 1, neverCut: true });
    }

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
    const historyMsgs = this._convertHistory(injectedHistory);

    // 5. Token 预算仲裁（受 feature flag 控制）
    if (context.token_arbitration_enabled) {
      return this.applyTokenBudget(systemBlocks, examples, historyMsgs, maxTokens);
    }

    // 降级：简单拼接 + 硬截断历史
    const systemContent = systemBlocks.map(b => b.content).join('\n\n');
    const messages = [{ role: 'system', content: systemContent }, ...examples, ...historyMsgs];
    const totalTokens = await getTokenCountAsync(JSON.stringify(messages));
    return {
      messages,
      tokenInfo: { totalTokens, maxTokens, remainingBudget: maxTokens - totalTokens, arbitrationSkipped: true },
    };
  }

  /**
   * Token 预算仲裁庭
   *
   * 按优先级裁剪区块，确保总 token 不超过 maxTokens。
   *
   * 优先级（数字越小越重要，越晚被裁剪）：
   *   1 = system_directives (neverCut)
   *   2 = role_play_setting (neverCut)
   *   3 = chat_history（从旧到新裁剪，至少保留 2 条）
   *   4 = group_dynamics（按条目裁剪）
   *   5 = group_memory / profiles（按条目裁剪）
   *   6 = world_knowledge（按条目裁剪）
   *
   * @param {Array} systemBlocks - 系统区块 [{name, content, priority, neverCut, items}]
   * @param {Array} examples - 示例消息数组
   * @param {Array} historyMsgs - 历史消息数组
   * @param {number} maxTokens - token 上限
   * @returns {Promise<{messages: Array, tokenInfo: Object}>}
   */
  async applyTokenBudget(systemBlocks, examples, historyMsgs, maxTokens) {
    const countTokens = async (msgs) => {
      return getTokenCountAsync(JSON.stringify(msgs));
    };

    const estimateTokens = (text) => Math.ceil((text || '').length / 3.35);

    // ─── 分离不可裁剪与可裁剪区块 ───
    const protectedBlocks = systemBlocks.filter(b => b.neverCut);
    const cuttableBlocks = systemBlocks.filter(b => !b.neverCut)
      .sort((a, b) => b.priority - a.priority); // 低优先级在前（先被裁剪）

    // 深拷贝可裁剪区块的 items（避免修改原始数据）
    for (const block of cuttableBlocks) {
      if (block.items) {
        block._items = [...block.items];
      }
    }

    // ─── 深拷贝历史消息 ───
    let workingHistory = [...historyMsgs];
    const MIN_HISTORY = 2;

    // ─── 辅助函数 ───
    const buildSystemContent = () => {
      const parts = [...protectedBlocks.map(b => b.content)];
      for (const block of cuttableBlocks) {
        if (block.content) parts.push(block.content);
      }
      return parts.join('\n\n');
    };

    const buildMessages = (systemContent) => {
      return [{ role: 'system', content: systemContent }, ...examples, ...workingHistory];
    };

    const rebuildBlockContent = (block) => {
      if (!block._items || block._items.length === 0) {
        block.content = '';
        return;
      }
      if (block.name === 'world_knowledge') {
        block.content = buildXmlBlock('world_knowledge', block._items.join('\n'));
      } else if (block.name === 'group_dynamics') {
        block.content = buildXmlBlock('group_dynamics', block._items.join('\n'));
      } else if (block.name === 'group_memory') {
        block.content = buildXmlBlockWithChildren('group_memory', [
          { tag: 'summary', content: '' },
          { tag: 'active_user_profiles', content: block._items.join('\n') },
        ]);
      }
    };

    // 快速本地估算总 token
    const estimateTotal = () => {
      const sysLen = buildSystemContent().length;
      const exLen = JSON.stringify(examples).length;
      const histLen = JSON.stringify(workingHistory).length;
      return Math.ceil((sysLen + exLen + histLen) / 3.35);
    };

    // ─── 初始尝试：全量组装（先用本地估算） ───
    if (estimateTotal() <= maxTokens) {
      // 估算通过，精确计数验证
      let systemContent = buildSystemContent();
      let messages = buildMessages(systemContent);
      let totalTokens = await countTokens(messages);
      if (totalTokens <= maxTokens) {
        return {
          messages,
          tokenInfo: { totalTokens, maxTokens, remainingBudget: maxTokens - totalTokens },
        };
      }
    }

    // ─── 超预算：按优先级从低到高裁剪 ───
    console.log(`[TokenBudget] 超预算，开始裁剪 (maxTokens=${maxTokens})`);

    // Phase 1: 裁剪可裁剪区块的 items
    for (const block of cuttableBlocks) {
      if (!block._items || block._items.length === 0) continue;

      while (block._items.length > 0) {
        block._items.pop();
        rebuildBlockContent(block);

        if (estimateTotal() <= maxTokens) {
          // 本地估算通过，精确计数验证
          const systemContent = buildSystemContent();
          const messages = buildMessages(systemContent);
          const totalTokens = await countTokens(messages);
          if (totalTokens <= maxTokens) {
            console.log(`[TokenBudget] 裁剪 ${block.name} 后满足: ${totalTokens}`);
            return {
              messages,
              tokenInfo: { totalTokens, maxTokens, remainingBudget: maxTokens - totalTokens },
            };
          }
        }
      }

      // items 全部移除仍超预算，清空整个区块
      block.content = '';
      block.items = [];
      block._items = [];
    }

    // Phase 2: 裁剪历史消息（从旧到新，保留至少 MIN_HISTORY 条）
    while (workingHistory.length > MIN_HISTORY) {
      workingHistory.shift();

      if (estimateTotal() <= maxTokens) {
        const systemContent = buildSystemContent();
        const messages = buildMessages(systemContent);
        const totalTokens = await countTokens(messages);
        if (totalTokens <= maxTokens) {
          console.log(`[TokenBudget] 裁剪历史至 ${workingHistory.length} 条后满足: ${totalTokens}`);
          return {
            messages,
            tokenInfo: { totalTokens, maxTokens, remainingBudget: maxTokens - totalTokens },
          };
        }
      }
    }

    // Phase 3: 极端情况 — 仍超预算
    const systemContent = buildSystemContent();
    const messages = buildMessages(systemContent);
    const totalTokens = await countTokens(messages);
    console.warn(`[TokenBudget] 极端超预算: ${totalTokens} > ${maxTokens}`);
    return {
      messages,
      tokenInfo: { totalTokens, maxTokens, remainingBudget: maxTokens - totalTokens, overflow: true },
    };
  }
}

module.exports = { TavernCoreV2 };