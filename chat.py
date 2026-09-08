import websocket
import json
import threading
import time
import requests
import logging
import os
import sys
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor

# ==========================================
# 1. 跨平台通知与键盘监听兼容层
# ==========================================
try:
    from plyer import notification
    PLYER_AVAILABLE = True
except ImportError:
    PLYER_AVAILABLE = False
    print("⚠️ 警告: 未找到 plyer 库。请运行 'pip install plyer'。")

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    print("⚠️ 警告: 未找到 openai 库。请运行 'pip install openai'。")

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
# 2. 配置管理与日志初始化
# ==========================================
CONFIG_FILE = "config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        default_config = {
            "luogu": {"client_id": "在此处粘贴 __client_id", "uid": "在此处粘贴 _uid"},
            "ai": {
                "api_key": "sk-your-api-key",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-3.5-turbo",
                "system_prompt": "你是一个热心的洛谷用户，回答简短友好。"
            },
            "settings": {
                "auto_reply": False,
                "cooldown_seconds": 60,
                "test_target_uid": 123456
            }
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default_config, f, ensure_ascii=False, indent=4)
        print(f"📝 已生成 [{CONFIG_FILE}]，请填写后重新运行。")
        sys.exit(0)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

class LuoguWebSocketClient:
    def __init__(self, config):
        self.config = config
        self.cookies = {'__client_id': config['luogu']['client_id'], '_uid': config['luogu']['uid']}
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Origin': 'https://www.luogu.com.cn',
            'Referer': 'https://www.luogu.com.cn/chat'
        }
        self.ws = None
        self.connected = False
        self.user_id = config['luogu']['uid']
        
        self.auto_reply = config['settings'].get('auto_reply', False)
        self.cooldown_seconds = config['settings'].get('cooldown_seconds', 60)
        self.reply_cooldown = {} 
        self.csrf_token = None
        self.executor = ThreadPoolExecutor(max_workers=3)
        
        # ★ 分离：系统提示词 和 用户消息模板
        self.system_prompt = config['ai'].get('system_prompt', '你是一个热心的洛谷用户。')
        self.user_message_template = config['ai'].get('user_message_template', '{message}')
        
        self.ai_client = None
        if self.auto_reply and OPENAI_AVAILABLE:
            self.ai_client = OpenAI(
                api_key=config['ai']['api_key'],
                base_url=config['ai'].get('base_url', 'https://api.openai.com/v1')
            )
            logging.info("🤖 AI 自动回复已启用 (模型: %s)", config['ai']['model'])
            logging.info("📋 系统提示词: %s", self.system_prompt[:50] + "...")
            self.fetch_csrf_token()
        else:
            logging.warning("⚠️ AI 自动回复未启用 (请在 config.json 中设置 auto_reply: true) 或 openai 库未安装")
        
        self.seen_messages = deque(maxlen=2000) 
        self.last_message_time = time.time()
        self.stop_flag = threading.Event()

    # ==========================================
    # 3. 洛谷 API 交互
    # ==========================================
    def fetch_csrf_token(self):
        try:
            logging.info("🔑 正在获取 CSRF Token...")
            for url in ["https://www.luogu.com.cn/", "https://www.luogu.com.cn/chat"]:
                res = requests.get(url, cookies=self.cookies, headers=self.headers, timeout=10)
                match = re.search(r'<meta name="csrf-token" content="([^"]+)"', res.text)
                if match:
                    self.csrf_token = match.group(1)
                    logging.info("✅ 成功获取 CSRF Token: %s...", self.csrf_token[:10])
                    return True
            logging.error("❌ 未在页面中找到 CSRF Token！")
            return False
        except Exception as e:
            logging.error("❌ 获取 CSRF Token 网络异常: %s", e)
            return False

    def send_message(self, receiver_uid, content):
        if not self.csrf_token and not self.fetch_csrf_token():
            return False
            
        url = "https://www.luogu.com.cn/api/chat/new"
        headers = {
            'Content-Type': 'application/json',
            'x-csrf-token': self.csrf_token,
            'x-requested-with': 'XMLHttpRequest',
            'User-Agent': self.headers['User-Agent'],
            'Origin': 'https://www.luogu.com.cn',
            'Referer': f'https://www.luogu.com.cn/chat/{receiver_uid}'
        }
        data = {
            "user": int(receiver_uid),
            "content": content
        }
        
        try:
            res = requests.post(url, headers=headers, cookies=self.cookies, json=data, timeout=10)
            if res.status_code == 200:
                logging.info("✅ 成功发送回复给 UID:%s", receiver_uid)
                return True
            else:
                logging.error("❌ 发送失败 (HTTP %s): %s", res.status_code, res.text[:200])
                if "csrf" in res.text.lower() or res.status_code == 403:
                    self.fetch_csrf_token()
                    headers['x-csrf-token'] = self.csrf_token
                    res2 = requests.post(url, headers=headers, cookies=self.cookies, json=data, timeout=10)
                    if res2.status_code == 200:
                        logging.info("✅ 刷新 Token 后发送成功")
                        return True
                return False
        except Exception as e:
            logging.error("❌ 发送回复网络异常: %s", e)
            return False

    # ==========================================
    # 4. AI 代理逻辑 (★ 核心重构：提示与回复分离)
    # ==========================================
    def build_prompt(self, user_message):
        """
        ★ 分离函数：构建发送给 AI 的提示词
        将系统提示词和用户消息组合成完整的消息列表
        """
        formatted_user_message = self.user_message_template.format(message=user_message)
        
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": formatted_user_message}
        ]
        return messages

    def call_ai(self, messages):
        """
        ★ 分离函数：调用 AI API 获取回复
        只负责网络请求，不负责提示词构建
        """
        if not self.ai_client:
            return None
        try:
            logging.info("🧠 正在调用 AI 接口...")
            response = self.ai_client.chat.completions.create(
                model=self.config['ai']['model'],
                messages=messages,
                temperature=0.7,
                max_tokens=500
            )
            reply = response.choices[0].message.content.strip()
            return reply
        except Exception as e:
            logging.error("❌ AI 生成失败 (请检查 API Key 和 Base URL): %s", e)
            return None

    def handle_ai_reply(self, receiver_uid, sender_name, user_message, force=False):
        """
        ★ 分离函数：协调整个自动回复流程
        清晰区分：用户提问 -> 构建提示 -> 调用AI -> 发送回复
        """
        # 冷却检查
        if not force:
            current_time = time.time()
            if receiver_uid in self.reply_cooldown and current_time - self.reply_cooldown[receiver_uid] < self.cooldown_seconds:
                logging.info("⏳ 用户 %s 处于冷却时间，跳过", sender_name)
                return
            self.reply_cooldown[receiver_uid] = current_time

        # ★ 第一步：记录用户提问
        logging.info("👤 用户提问 [%s]: %s", sender_name, user_message)
        
        # ★ 第二步：构建提示词
        messages = self.build_prompt(user_message)
        logging.debug("📋 构建的提示词: %s", messages)
        
        # ★ 第三步：调用 AI 获取回复
        ai_reply = self.call_ai(messages)
        
        if ai_reply:
            # ★ 第四步：记录 AI 回复
            logging.info("🤖 AI回复 [%s]: %s", sender_name, ai_reply)
            
            # ★ 第五步：发送回复
            time.sleep(1)  # 模拟人类输入
            self.send_message(receiver_uid, ai_reply)
            
            # ★ 第六步：发送桌面通知（分开显示问题和回答）
            self.show_notification(
                f"已回复 {sender_name}",
                f"问: {user_message[:50]}\n答: {ai_reply[:50]}"
            )
        else:
            logging.warning("⚠️ AI 未生成回复，跳过发送")

    # ==========================================
    # 5. WebSocket 与通知
    # ==========================================
    def show_notification(self, title, message):
        if PLYER_AVAILABLE:
            try:
                notification.notify(title=title, message=message, app_name="Luogu AI", timeout=8)
            except: pass
        print(f"\a\033[94m[🔔 {title}]\033[0m \033[92m{message}\033[0m")

    def on_message(self, ws, message):
        self.last_message_time = time.time()
        try:
            data = json.loads(message)
            if data.get('_ws_type') == 'server_broadcast' and isinstance(data.get('message'), dict):
                msg_data = data.get('message', {})
                sender_uid = str(msg_data.get('sender', {}).get('uid', ''))
                
                if sender_uid != str(self.user_id):
                    msg_id = msg_data.get('id')
                    if msg_id and msg_id in self.seen_messages: return
                    if msg_id: self.seen_messages.append(msg_id)
                        
                    sender_name = msg_data.get('sender', {}).get('name', '未知用户')
                    content = msg_data.get('content', '')
                    
                    # ★ 收到消息时，只显示用户的问题（不显示AI回复）
                    self.show_notification(f"📩 洛谷私信 - {sender_name}", content)
                    
                    if self.auto_reply and self.ai_client:
                        self.executor.submit(self.handle_ai_reply, sender_uid, sender_name, content)
        except Exception as e:
            logging.error("消息处理异常: %s", e)

    def on_error(self, ws, error): logging.error("WS Error: %s", error); self.connected = False
    def on_close(self, ws, *args): logging.info("WS Closed"); self.connected = False
    def on_open(self, ws):
        logging.info("✅ WS Connected")
        self.connected = True
        self.ws.send(json.dumps({"type": "join_channel", "channel": "chat", "channel_param": self.user_id, "exclusive_key": None}))

    def connect(self):
        self.ws = websocket.WebSocketApp(
            "wss://ws.luogu.com.cn/ws",
            header=[f"{k}: {v}" for k, v in self.headers.items()],
            cookie=f"__client_id={self.cookies['__client_id']}; _uid={self.cookies['_uid']}",
            on_message=self.on_message, on_error=self.on_error, on_close=self.on_close, on_open=self.on_open
        )
        self.ws.run_forever(ping_interval=20, ping_timeout=10)

    def run(self):
        logging.info("🚀 洛谷私信监控 (AI 代理版) 启动")
        logging.info("快捷键: 'R' 重连 | 'T' 测试 AI 回复 | 'Ctrl+C' 退出")
        threading.Thread(target=self._keyboard_listener, daemon=True).start()
        
        try:
            while not self.stop_flag.is_set():
                self.connect()
                if not self.stop_flag.is_set(): time.sleep(5)
        except KeyboardInterrupt: self.stop()

    def _keyboard_listener(self):
        while not self.stop_flag.is_set():
            try:
                if kbhit():
                    key = getch()
                    if key == 'R':
                        logging.info("⚡ 强制重连")
                        if self.ws: self.ws.close()
                    elif key == 'T':
                        test_uid = self.config['settings'].get('test_target_uid')
                        if not test_uid:
                            logging.error("❌ 请在 config.json 的 settings 中添加 'test_target_uid'")
                            continue
                        
                        logging.info("🧪 执行手动测试：发送测试消息给 UID %s ...", test_uid)
                        self.executor.submit(
                            self.handle_ai_reply, 
                            str(test_uid), 
                            "测试用户", 
                            "你好，这是一条测试消息，请回复'测试成功'。", 
                            force=True
                        )
            except Exception:
                pass
            time.sleep(0.5)

    def stop(self):
        self.stop_flag.set()
        if self.ws: self.ws.close()
        sys.exit(0)

if __name__ == "__main__":
    config = load_config()
    if "在此处粘贴" in config['luogu']['client_id']:
        print("❌ 请先在 config.json 中填写 Cookie！"); sys.exit(1)
    LuoguWebSocketClient(config).run()
