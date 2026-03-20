/**
 * API 请求工具函数
 */

const BASE = '';  // 开发时由 vite proxy 代理到 FastAPI

export async function fetchJSON(path, params = {}) {
    const url = new URL(path, window.location.origin);
    Object.entries(params).forEach(([k, v]) => {
        if (v !== undefined && v !== null) url.searchParams.set(k, v);
    });
    const res = await fetch(url.toString());
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
}

/**
 * 保存文件内容
 * @param {string} path - 文件路径
 * @param {string} content - 文件内容
 * @returns {Promise<object>} - 保存结果
 */
export async function saveFile(path, content) {
    const url = new URL('/api/files/save', window.location.origin);
    const res = await fetch(url.toString(), {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ path, content }),
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '保存失败' }));
        throw new Error(err.detail || `${res.status} ${res.statusText}`);
    }
    return res.json();
}

/**
 * 创建新文件
 * @param {string} path - 文件路径
 * @param {string} content - 文件内容
 * @returns {Promise<object>} - 创建结果
 */
export async function createFile(path, content = '') {
    const url = new URL('/api/files/create', window.location.origin);
    const res = await fetch(url.toString(), {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ path, content }),
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '创建失败' }));
        throw new Error(err.detail || `${res.status} ${res.statusText}`);
    }
    return res.json();
}

/**
 * 获取所有项目列表
 * @returns {Promise<object>} - 项目列表
 */
export async function listProjects() {
    return fetchJSON('/api/projects/list');
}

/**
 * 获取当前项目信息
 * @returns {Promise<object>} - 当前项目
 */
export async function getCurrentProject() {
    return fetchJSON('/api/projects/current');
}

/**
 * 切换当前项目
 * @param {string} path - 项目路径
 * @returns {Promise<object>} - 切换结果
 */
export async function switchProject(path) {
    const url = new URL('/api/projects/switch', window.location.origin);
    const res = await fetch(url.toString(), {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ path }),
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '切换失败' }));
        throw new Error(err.detail || `${res.status} ${res.statusText}`);
    }
    return res.json();
}

/**
 * 创建新项目
 * @param {object} project - 项目信息
 * @returns {Promise<object>} - 创建结果
 */
export async function createProject(project) {
    const url = new URL('/api/projects/create', window.location.origin);
    const res = await fetch(url.toString(), {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(project),
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '创建失败' }));
        throw new Error(err.detail || `${res.status} ${res.statusText}`);
    }
    return res.json();
}

/**
 * 订阅 SSE 实时事件流
 * @param {function} onMessage  收到 data 时回调
 * @param {{onOpen?: function, onError?: function}} handlers 连接状态回调
 * @returns {function} 取消订阅函数
 */
export function subscribeSSE(onMessage, handlers = {}) {
    const { onOpen, onError } = handlers
    const es = new EventSource(`${BASE}/api/events`);
    es.onopen = () => {
        if (onOpen) onOpen()
    };
    es.onmessage = (e) => {
        try {
            onMessage(JSON.parse(e.data));
        } catch { /* ignore parse errors */ }
    };
    es.onerror = (e) => {
        // EventSource 会自动重连，这里只更新连接状态
        if (onError) onError(e)
    };
    return () => es.close();
}

// ===========================================================
// 风格配置相关 API
// ===========================================================

/**
 * 获取可用的写作风格列表
 * @returns {Promise<object>} - 风格列表
 */
export async function getAvailableStyles() {
    return fetchJSON('/api/styles/available');
}

/**
 * 获取当前项目的风格配置
 * @returns {Promise<object>} - 风格配置
 */
export async function getStyleConfig() {
    return fetchJSON('/api/styles/config');
}

/**
 * 更新当前项目的风格配置
 * @param {object} config - 风格配置
 * @param {string} config.primary - 主风格ID
 * @param {boolean} config.intelligence_enabled - 是否开启智能切换
 * @param {object} config.scene_adapters - 场景适配规则
 * @returns {Promise<object>} - 更新结果
 */
export async function updateStyleConfig(config) {
    const url = new URL('/api/styles/config', window.location.origin);
    const res = await fetch(url.toString(), {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(config),
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '更新失败' }));
        throw new Error(err.detail || `${res.status} ${res.statusText}`);
    }
    return res.json();
}

/**
 * 为旧项目初始化风格配置
 * @param {object} options - 初始化选项
 * @param {string} options.primary - 主风格ID
 * @param {boolean} options.intelligence_enabled - 是否开启智能切换
 * @returns {Promise<object>} - 初始化结果
 */
export async function initializeStyle(options = {}) {
    const url = new URL('/api/styles/initialize', window.location.origin);
    const res = await fetch(url.toString(), {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(options),
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '初始化失败' }));
        throw new Error(err.detail || `${res.status} ${res.statusText}`);
    }
    return res.json();
}

/**
 * 快速开关智能风格切换
 * @param {boolean} enabled - 是否开启
 * @returns {Promise<object>} - 切换结果
 */
export async function toggleIntelligence(enabled) {
    const url = new URL('/api/styles/toggle-intelligence', window.location.origin);
    const res = await fetch(url.toString(), {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ enabled }),
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '切换失败' }));
        throw new Error(err.detail || `${res.status} ${res.statusText}`);
    }
    return res.json();
}
