// ==UserScript==
// @name         洛谷私信实时监控 (生产版)
// @namespace    http://tampermonkey.net/
// @version      1.0.0
// @description  稳定、轻量、无打扰的洛谷私信浏览器原生通知插件。支持自动重连、防休眠、消息去重。
// @author       Qwen
// @match        *://*.luogu.com.cn/*
// @icon         https://www.luogu.com.cn/favicon.ico
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function() {
    'use strict';

    /**
     * ==========================================
     *               配 置 区
     * ==========================================
     */
    const CONFIG = {
        WS_URL: 'wss://ws.luogu.com.cn/ws',
        RECONNECT_INTERVAL: 5000,      // 断线重连间隔 (毫秒)
        HEARTBEAT_INTERVAL: 25000,     // 心跳发送间隔 (毫秒)
        MAX_CACHE_SIZE: 500,           // 消息去重缓存最大条数 (防止内存泄漏)
        ICON: 'https://www.luogu.com.cn/favicon.ico',
        DEBUG: false                   // 是否在控制台输出详细日志 (排查问题时改为 true)
    };

    /**
     * ==========================================
     *               工 具 函 数
     * ==========================================
     */
    const log = CONFIG.DEBUG ? (...args) => console.log('[LuoguNotify]', ...args) : () => {};
    const warn = (...args) => console.warn('[LuoguNotify]', ...args);
    const error = (...args) => console.error('[LuoguNotify]', ...args);

    /**
     * ==========================================
     *               核 心 类
     * ==========================================
     */
    class LuoguNotifier {
        constructor() {
            this.ws = null;
            this.myUid = this.getMyUid();
            this.seenMessages = new Set();
            this.reconnectTimer = null;
            this.heartbeatTimer = null;
            this.isConnecting = false;
            
            this.init();
        }

        // 1. 初始化
        init() {
            this.injectUI();
            this.checkPermission();
            this.connect();
            this.setupVisibilityListener();
            log('初始化完成, UID:', this.myUid);
        }

        // 2. 多策略获取当前用户 UID
        getMyUid() {
            try {
                // 策略A: 洛谷全局变量
                if (window._feInjection?.currentUser?.uid) {
                    return String(window._feInjection.currentUser.uid);
                }
                // 策略B: 页面链接提取
                const links = document.querySelectorAll('a[href*="/user/"]');
                for (const link of links) {
                    const match = link.getAttribute('href')?.match(/\/user\/(\d+)/);
                    if (match) return match[1];
                }
                // 策略C: 页面源码正则
                const match = document.documentElement.innerHTML.match(/"uid"\s*:\s*"?(\d+)"?/);
                if (match) return match[1];
            } catch (e) {
                warn('获取 UID 失败:', e);
            }
            return null;
        }

        // 3. 极简状态指示器注入
        injectUI() {
            if (document.getElementById('ln-pro-indicator')) return;
            
            const indicator = document.createElement('div');
            indicator.id = 'ln-pro-indicator';
            indicator.innerHTML = '🔔';
            indicator.title = '洛谷通知: 初始化中';
            
            Object.assign(indicator.style, {
                position: 'fixed',
                bottom: '20px',
                right: '20px',
                zIndex: '999999',
                width: '36px',
                height: '36px',
                borderRadius: '50%',
                background: 'rgba(255,255,255,0.9)',
                boxShadow: '0 2px 8px rgba(0,0,0,0.15)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: '18px',
                cursor: 'pointer',
                transition: 'all 0.3s ease',
                opacity: '0.5', // 默认半透明，不打扰用户
                userSelect: 'none'
            });

            // 鼠标悬停时高亮
            indicator.addEventListener('mouseenter', () => { indicator.style.opacity = '1'; });
            indicator.addEventListener('mouseleave', () => { indicator.style.opacity = '0.5'; });
            
            // 点击交互
            indicator.addEventListener('click', () => {
                this.handleIndicatorClick();
            });

            document.body.appendChild(indicator);
            this.indicator = indicator;
            this.updateIndicatorStatus();
        }

        // 4. 权限管理与状态更新
        checkPermission() {
            if (!window.Notification) {
                this.updateIndicatorStatus();
                return;
            }
            if (Notification.permission === 'default') {
                // 如果是默认状态，闪烁提示用户点击授权
                this.blinkIndicator('#ff9800');
            }
        }

        handleIndicatorClick() {
            if (!window.Notification) {
                alert('当前浏览器不支持 Notification API');
                return;
            }
            if (Notification.permission === 'default') {
                Notification.requestPermission().then(perm => {
                    this.updateIndicatorStatus();
                    if (perm === 'granted') {
                        this.sendNotification('✅ 授权成功', '洛谷私信通知已开启！');
                    }
                });
            } else if (Notification.permission === 'denied') {
                alert('通知权限已被拒绝。请在浏览器地址栏左侧的“设置”中，将“通知”权限改为“允许”，然后刷新页面。');
            } else if (Notification.permission === 'granted') {
                // 已授权，点击则手动测试一条通知，或者强制重连
                this.sendNotification('🔔 洛谷监控', '连接正常，正在为您守护私信！');
            }
        }

        updateIndicatorStatus() {
            if (!this.indicator) return;
            const perm = window.Notification ? Notification.permission : 'unsupported';
            const wsState = this.ws ? this.ws.readyState : WebSocket.CLOSED;
            
            let color = '#9e9e9e'; // 灰色
            let tooltip = '洛谷通知: 初始化中';

            if (perm === 'granted' && wsState === WebSocket.OPEN) {
                color = '#4caf50'; // 绿色
                tooltip = '洛谷通知: 运行中 (点击测试)';
            } else if (perm === 'denied') {
                color = '#f44336'; // 红色
                tooltip = '洛谷通知: 权限被拒绝 (点击查看帮助)';
            } else if (perm === 'default') {
                color = '#ff9800'; // 橙色
                tooltip = '洛谷通知: 未授权 (点击授权)';
            } else if (wsState !== WebSocket.OPEN) {
                color = '#ff9800'; // 橙色
                tooltip = '洛谷通知: 连接断开，正在重连...';
            }

            this.indicator.style.border = `2px solid ${color}`;
            this.indicator.title = tooltip;
        }

        blinkIndicator(color) {
            if (!this.indicator) return;
            let count = 0;
            const timer = setInterval(() => {
                this.indicator.style.border = count % 2 === 0 ? `2px solid ${color}` : '2px solid transparent';
                count++;
                if (count > 5) {
                    clearInterval(timer);
                    this.updateIndicatorStatus();
                }
            }, 500);
        }

        // 5. WebSocket 连接管理
        connect() {
            if (this.isConnecting) return;
            if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
                return;
            }

            this.isConnecting = true;
            this.updateIndicatorStatus();
            log('正在建立 WebSocket 连接...');

            try {
                this.ws = new WebSocket(CONFIG.WS_URL);
            } catch (e) {
                error('WebSocket 创建失败:', e);
                this.scheduleReconnect();
                return;
            }

            this.ws.onopen = () => {
                this.isConnecting = false;
                log('✅ WebSocket 已连接');
                this.updateIndicatorStatus();
                
                // 发送订阅消息
                const joinMsg = {
                    type: "join_channel",
                    channel: "chat",
                    channel_param: this.myUid || "",
                    exclusive_key: null
                };
                this.ws.send(JSON.stringify(joinMsg));
                log('已发送频道订阅:', joinMsg);
                
                this.startHeartbeat();
            };

            this.ws.onmessage = (event) => {
                this.handleMessage(event.data);
            };

            this.ws.onclose = (e) => {
                this.isConnecting = false;
                log(`WebSocket 关闭 (code: ${e.code})`);
                this.updateIndicatorStatus();
                this.stopHeartbeat();
                this.scheduleReconnect();
            };

            this.ws.onerror = (e) => {
                error('WebSocket 错误:', e);
                this.updateIndicatorStatus();
            };
        }

        scheduleReconnect() {
            if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
            log(`${CONFIG.RECONNECT_INTERVAL / 1000}秒后尝试重连...`);
            this.reconnectTimer = setTimeout(() => this.connect(), CONFIG.RECONNECT_INTERVAL);
        }

        startHeartbeat() {
            this.stopHeartbeat();
            this.heartbeatTimer = setInterval(() => {
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    try {
                        this.ws.send(JSON.stringify({ type: "heartbeat" }));
                        log('💓 心跳已发送');
                    } catch (e) {
                        warn('心跳发送失败:', e);
                    }
                }
            }, CONFIG.HEARTBEAT_INTERVAL);
        }

        stopHeartbeat() {
            if (this.heartbeatTimer) {
                clearInterval(this.heartbeatTimer);
                this.heartbeatTimer = null;
            }
        }

        // 6. 页面可见性监听 (防休眠唤醒)
        setupVisibilityListener() {
            document.addEventListener('visibilitychange', () => {
                if (!document.hidden) {
                    log('页面恢复可见，检查连接状态...');
                    if (this.ws && this.ws.readyState !== WebSocket.OPEN && this.ws.readyState !== WebSocket.CONNECTING) {
                        log('检测到连接已断开，立即重连');
                        this.connect();
                    }
                }
            });
        }

        // 7. 消息处理核心
        handleMessage(rawData) {
            try {
                const data = JSON.parse(rawData);
                
                // 只处理广播消息
                if (data._ws_type !== 'server_broadcast') return;
                if (!data.message || typeof data.message !== 'object') return;
                
                const msg = data.message;
                const senderUid = String(msg.sender?.uid || '');
                const senderName = msg.sender?.name || '未知用户';
                const content = msg.content || '(空消息)';
                const msgId = msg.id;
                
                log(`收到消息: ${senderName}(${senderUid}): ${content}`);
                
                // 过滤自己
                if (this.myUid && senderUid === this.myUid) {
                    log('跳过自己发的消息');
                    return;
                }
                
                // 去重
                if (msgId) {
                    if (this.seenMessages.has(msgId)) {
                        log('跳过重复消息:', msgId);
                        return;
                    }
                    this.seenMessages.add(msgId);
                    
                    // 内存管理：超过最大缓存时，保留最新的一半
                    if (this.seenMessages.size > CONFIG.MAX_CACHE_SIZE) {
                        const arr = Array.from(this.seenMessages);
                        this.seenMessages.clear();
                        arr.slice(arr.length / 2).forEach(id => this.seenMessages.add(id));
                        log('已清理部分历史消息缓存');
                    }
                }
                
                // 发送通知
                this.sendNotification(`洛谷私信 - ${senderName}`, content);
                
            } catch (e) {
                error('消息解析错误:', e, rawData);
            }
        }

        // 8. 发送浏览器原生通知
        sendNotification(title, body) {
            if (!window.Notification) {
                warn('浏览器不支持 Notification');
                return;
            }
            
            if (Notification.permission !== 'granted') {
                warn(`通知未授权 (当前权限: ${Notification.permission})`);
                return;
            }
            
            try {
                const n = new Notification(title, {
                    body: body,
                    icon: CONFIG.ICON,
                    tag: 'ln-pro-' + Date.now(),
                    requireInteraction: false
                });
                
                n.onclick = () => {
                    window.focus();
                    window.location.href = 'https://www.luogu.com.cn/chat';
                    n.close();
                };
                
                log('✅ 通知已发送');
            } catch (e) {
                error('通知发送失败:', e);
            }
        }
    }

    // ==========================================
    //               启 动 脚 本
    // ==========================================
    window.addEventListener('load', () => {
        // 延迟 1.5 秒启动，确保洛谷前端框架和 DOM 完全渲染
        setTimeout(() => {
            window.luoguNotifierPro = new LuoguNotifier();
        }, 1500);
    });

})();
