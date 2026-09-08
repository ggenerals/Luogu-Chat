import websocket
import json
import threading
import time
import requests
import logging
import os
import sys
from collections import deque
from datetime import datetime

# ==========================================
# 1. 跨平台通知与键盘监听兼容层
# ==========================================
try:
    from plyer import notification
    PLYER_AVAILABLE = True
except ImportError:
    PLYER_AVAILABLE = False
    print("⚠️ 警告: 未找到 plyer 库。请运行 'pip install plyer' 以获得跨平台系统通知。")

# 跨平台键盘监听兼容 (Windows / macOS / Linux)
try:
    import msvcrt
    def getch(): return msvcrt.getch().decode('utf-8', errors='ignore').upper()
    def kbhit(): return msvcrt.kbhit()
except ImportError:
    import tty, termios, select
    def getch():
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch.upper()
    def kbhit():
        return select.select([sys.stdin], [], [], 0)[0] != []

# ==========================================
# 2. 日志与环境初始化
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("luogu_monitor.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

class LuoguWebSocketClient:
    def __init__(self, client_id, uid):
        self.cookies = {'__client_id': client_id, '_uid': uid}
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Origin': 'https://www.luogu.com.cn',
            'Referer': 'https://www.luogu.com.cn/chat'
        }
        self.ws = None
        self.connected = False
        self.user_id = uid
        
        # 网络保活参数
        self.heartbeat_interval = 20  
        self.reconnect_interval = 60  
        self.force_reconnect_interval = 600  
        
        # 核心优化：使用 deque 限制最大长度，防止长期运行内存泄漏
        self.seen_messages = deque(maxlen=2000) 
        
        self.last_message_time = time.time()
        self.last_force_reconnect_time = time.time()
        self.stop_flag = threading.Event()
        
        # 线程池管理
        self.threads = []

    # ==========================================
    # 3. 异步通知系统 (核心重构)
    # ==========================================
    def get_icon_path(self):
        """获取并缓存图标路径"""
        try:
            icon_path = "luogu_icon.ico"
            if not os.path.exists(icon_path):
                response = requests.get("https://www.luogu.com.cn/favicon.ico", timeout=5)
                if response.status_code == 200:
                    with open(icon_path, "wb") as f:
                        f.write(response.content)
            return os.path.abspath(icon_path)
        except Exception as e:
            logging.warning("图标获取失败: %s", e)
            return None

    def show_notification(self, title, message):
        """
        异步触发通知。
        必须放入新线程，防止任何 UI 阻塞影响 WebSocket 接收效率。
        """
        t = threading.Thread(target=self._dispatch_notification, args=(title, message), daemon=True)
        t.start()
        self.threads.append(t)

    def _dispatch_notification(self, title, message):
        try:
            if PLYER_AVAILABLE:
                icon = self.get_icon_path()
                # plyer 会自动适配 Windows Toast / macOS Notification Center / Linux libnotify
                notification.notify(
                    title=title,
                    message=message,
                    app_name="Luogu Monitor",
                    app_icon=icon,
                    timeout=8
                )
                logging.info("✅ 系统通知已发送: %s", message)
            else:
                self.fallback_notification(title, message)
        except Exception as e:
            logging.error("❌ 系统通知调用失败 (%s)，启用降级方案", e)
            self.fallback_notification(title, message)

    def fallback_notification(self, title, message):
        """
        降级方案：终端 ANSI 彩色高亮 + 系统蜂鸣声 (\a)
        确保在无任何 GUI 环境下也能强提醒
        """
        # \a 触发系统提示音，\033[94m 为蓝色，\033[92m 为绿色
        print(f"\a\033[94m[🔔 {title}]\033[0m \033[92m{message}\033[0m")
        logging.info("降级通知(控制台): %s - %s", title, message)

    # ==========================================
    # 4. WebSocket 事件处理
    # ==========================================
    def on_message(self, ws, message):
        self.last_message_time = time.time()
        try:
            data = json.loads(message)
            if data.get('_ws_type') == 'server_broadcast' and isinstance(data.get('message'), dict):
                msg_data = data.get('message', {})
                sender_uid = str(msg_data.get('sender', {}).get('uid', ''))
                
                if sender_uid != str(self.user_id):
                    msg_id = msg_data.get('id')
                    if msg_id and msg_id in self.seen_messages:
                        return
                    
                    if msg_id:
                        self.seen_messages.append(msg_id) # deque 使用 append
                        
                    sender_name = msg_data.get('sender', {}).get('name', '未知用户')
                    content = msg_data.get('content', '')
                    self.show_notification(f"洛谷私信 - {sender_name}", content)
        except Exception as e:
            logging.error("消息处理异常: %s", e)

    def on_error(self, ws, error):
        logging.error("WebSocket 错误: %s", error)
        self.connected = False

    def on_close(self, ws, close_status_code, close_msg):
        logging.info("连接关闭 (Code: %s)", close_status_code)
        self.connected = False

    def on_open(self, ws):
        logging.info("✅ WebSocket 连接已建立")
        self.connected = True
        self.last_message_time = time.time()
        
        join_msg = {"type": "join_channel", "channel": "chat", "channel_param": self.user_id, "exclusive_key": None}
        self.ws.send(json.dumps(join_msg))
        
        self._start_daemon_threads()

    # ==========================================
    # 5. 高可用保活线程管理
    # ==========================================
    def _start_daemon_threads(self):
        """启动所有后台守护线程"""
        def reconnect_checker():
            while not self.stop_flag.is_set():
                if self.connected and (time.time() - self.last_message_time > self.reconnect_interval):
                    logging.warning("⚠️ 超时未收消息，触发假死重连")
                    self.ws.close()
                time.sleep(5)

        def force_reconnect():
            while not self.stop_flag.is_set():
                if time.time() - self.last_force_reconnect_time > self.force_reconnect_interval:
                    logging.info("🔄 达到 10 分钟周期，强制刷新连接")
                    self.last_force_reconnect_time = time.time()
                    if self.ws: self.ws.close()
                time.sleep(30)

        for target in [reconnect_checker, force_reconnect]:
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self.threads.append(t)

    def connect(self):
        self.ws = websocket.WebSocketApp(
            "wss://ws.luogu.com.cn/ws",
            header=[f"{k}: {v}" for k, v in self.headers.items()],
            cookie=f"__client_id={self.cookies['__client_id']}; _uid={self.cookies['_uid']}",
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open
        )
        # ping_interval 和 ping_timeout 由 websocket-client 底层接管，效率更高
        self.ws.run_forever(ping_interval=20, ping_timeout=10)

    def run(self):
        logging.info("🚀 洛谷私信监控启动 (跨平台版)")
        logging.info("快捷键: 'R' 强制重连 | 'Ctrl+C' 退出")
        self.show_notification("洛谷监控", "服务已启动，正在后台静默运行...")
        
        # 键盘监听主循环
        threading.Thread(target=self._keyboard_listener, daemon=True).start()
        
        try:
            while not self.stop_flag.is_set():
                self.connect()
                if not self.stop_flag.is_set():
                    logging.info("⏳ 连接断开，5秒后重连...")
                    time.sleep(5)
        except KeyboardInterrupt:
            self.stop()

    def _keyboard_listener(self):
        while not self.stop_flag.is_set():
            try:
                if kbhit():
                    if getch() == 'R':
                        logging.info("⚡ 收到手动重置指令")
                        self.show_notification("洛谷监控", "正在强制重置连接...")
                        if self.ws: self.ws.close()
            except Exception:
                pass
            time.sleep(0.5)

    def stop(self):
        logging.info("🛑 正在停止监控...")
        self.stop_flag.set()
        if self.ws: self.ws.close()
        self.show_notification("洛谷监控", "服务已安全停止")
        sys.exit(0)

if __name__ == "__main__":
    print("="*40)
    print("洛谷私信监控 (跨平台原生通知版)")
    print("="*40)
    c_id = input("请输入 __client_id: ").strip()
    u_id = input("请输入 _uid: ").strip()
    
    client = LuoguWebSocketClient(c_id, u_id)
    client.run()
