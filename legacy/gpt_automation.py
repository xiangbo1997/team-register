import os
import time
import random
import logging
import requests
from typing import Optional, Tuple, Dict, Any
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeoutError

# 1. 基础配置 (Engineering & Security)
load_dotenv()

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("GPT_Automation")

# 配置中心从环境变量加载
CONFIG = {
    "ADS_API": os.getenv("ADS_API", "http://local.adspower.net:50325"),
    "ADS_API_KEY": os.getenv("ADS_API_KEY", ""),
    "EFUNCARD_TOKEN": os.getenv("EFUNCARD_TOKEN", ""),
    "SMS_API_KEY": os.getenv("SMS_API_KEY", ""),
    "SMS_COUNTRY": os.getenv("SMS_COUNTRY", "6"),
    "MAIL_DOMAIN": os.getenv("MAIL_DOMAIN", ""),
    "MAIL_REFRESH_TOKEN": os.getenv("MAIL_REFRESH_TOKEN", ""),
    "MAIL_CLIENT_ID": os.getenv("MAIL_CLIENT_ID", ""),
}

# 辅助函数: 拟人化操作 (Anti-ban)
def human_delay(min_sec: float = 1.0, max_sec: float = 3.0) -> None:
    """模拟人类操作的随机延迟"""
    delay = random.uniform(min_sec, max_sec)
    logger.debug(f"随机延迟 {delay:.2f} 秒...")
    time.sleep(delay)

def human_typing(page: Page, selector: str, text: str) -> None:
    """模拟人类打字速度"""
    page.wait_for_selector(selector, state="visible", timeout=10000)
    page.click(selector) # 先点击聚焦
    human_delay(0.2, 0.8)
    # 使用 press_sequentially 模拟逐字敲击的延迟
    page.locator(selector).press_sequentially(text, delay=random.randint(50, 150))
    logger.debug(f"填入内容到: {selector}")


# ================= 模块 1: Efuncard 支付 =================
class EfunCard:
    """Efuncard 虚拟信用卡支付与 3DS 验证模块"""
    
    def __init__(self) -> None:
        self.base_url = "https://card.efuncard.com/api/external"
        self.headers = {"Authorization": f"Bearer {CONFIG['EFUNCARD_TOKEN']}", "Content-Type": "application/json"}

    def redeem(self, cdk: str) -> Optional[Dict[str, Any]]:
        """激活并获取卡片详情"""
        logger.info(f"正在激活 CDK: {cdk}")
        try:
            res = requests.post(f"{self.base_url}/redeem", json={"code": cdk}, headers=self.headers, timeout=10).json()
            if res.get("success"):
                logger.info("CDK 激活成功获取卡片信息。")
                return res.get("data")
            else:
                logger.error(f"CDK 激活失败: {res.get('message', 'Unknown error')}")
        except requests.RequestException as e:
            logger.error(f"Efuncard API 请求异常: {e}")
        return None

    def wait_for_3ds(self, cdk: str, timeout_sec: int = 300) -> Optional[str]:
        """循环获取 3DS 验证码 (带重试机制)"""
        logger.info("正在监控 3DS 验证码...")
        start_time = time.time()
        attempt = 1
        
        while time.time() - start_time < timeout_sec:
            try:
                res = requests.post(f"{self.base_url}/3ds/verify", json={"code": cdk, "minutes": 5}, headers=self.headers, timeout=10).json()
                if res.get("success") and res.get("data", {}).get("verifications"):
                    otp = res["data"]["verifications"][0]["otp"]
                    logger.info(f"成功获取 3DS 验证码: {otp}")
                    return otp
            except requests.RequestException as e:
                logger.warning(f"获取 3DS 验证码失败 (尝试 {attempt}): {e}")
            
            attempt += 1
            human_delay(8, 12) # 轮询间隔
            
        logger.warning("监控 3DS 验证码超时。")
        return None

# ================= 模块 2: SMS 接码 =================
class SMSManager:
    """SMS-Activate 接码平台交互模块"""
    
    def __init__(self) -> None:
        self.url = "https://api.sms-activate.org/steward.php"
        self.key = CONFIG["SMS_API_KEY"]

    def get_number(self) -> Tuple[Optional[str], str]:
        """获取 OpenAI 专用号码"""
        logger.info(f"请求获取手机号 (国家代码: {CONFIG['SMS_COUNTRY']})...")
        params = {"api_key": self.key, "action": "getNumber", "service": "dr", "country": CONFIG["SMS_COUNTRY"]}
        try:
            res = requests.get(self.url, params=params, timeout=10).text
            if "ACCESS_NUMBER" in res:
                parts = res.split(":")
                order_id, number = parts[1], parts[2]
                logger.info(f"成功获取手机号: {number} (订单ID: {order_id})")
                return order_id, number
            logger.error(f"获取手机号失败，API 返回: {res}")
            return None, res
        except requests.RequestException as e:
            logger.error(f"SMS API 请求异常: {e}")
            return None, str(e)

    def get_code(self, order_id: str, max_retries: int = 30) -> Optional[str]:
        """等待短信验证码 (轮询)"""
        logger.info(f"等待短信验证码 (订单ID: {order_id})...")
        for attempt in range(1, max_retries + 1):
            try:
                res = requests.get(self.url, params={"api_key": self.key, "action": "getStatus", "id": order_id}, timeout=10).text
                if "STATUS_OK" in res:
                    code = res.split(":")[1]
                    logger.info(f"成功获取短信验证码: {code}")
                    return code
            except requests.RequestException as e:
                logger.warning(f"检查验证码状态异常 (尝试 {attempt}/{max_retries}): {e}")
            
            human_delay(4, 6) # 每 5 秒左右重试一次
            
        logger.warning(f"获取短信验证码超时 (尝试了 {max_retries} 次)。")
        return None

# ================= 模块 3: 小苹果邮件服务 =================
class MailManager:
    """小苹果邮件服务 API 封装"""
    
    def __init__(self) -> None:
        self.base_url = CONFIG["MAIL_DOMAIN"].strip("/")
        self.refresh_token = CONFIG["MAIL_REFRESH_TOKEN"]
        self.client_id = CONFIG["MAIL_CLIENT_ID"]
        
        if not all([self.base_url, self.refresh_token, self.client_id]):
            logger.warning("邮件服务配置不完整，部分功能可能受限。")

    def get_latest_mail(self, email: str, mailbox: str = "INBOX") -> Optional[Dict[str, Any]]:
        """获取最新一封邮件"""
        logger.info(f"正在从 {mailbox} 获取 {email} 的最新邮件...")
        params = {
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "email": email,
            "mailbox": mailbox,
            "response_type": "json"
        }
        try:
            res = requests.get(f"{self.base_url}/api/mail-new", params=params, timeout=15)
            res.raise_for_status()
            return res.json()
        except Exception as e:
            logger.error(f"获取最新邮件失败: {e}")
        return None

    def get_verification_code(self, email: str, wait_timeout: int = 60) -> Optional[str]:
        """从邮件中获取 6 位数字验证码 (带轮询逻辑)"""
        logger.info(f"开始轮询验证码 (Email: {email})...")
        start_time = time.time()
        
        while time.time() - start_time < wait_timeout:
            params = {
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "email": email,
                "mailbox": "INBOX"
            }
            try:
                # 使用 /api/mail-all 接口，文档说明它会自动提取验证码
                res = requests.get(f"{self.base_url}/api/mail-all", params=params, timeout=10)
                if res.status_code == 200:
                    mails = res.json()
                    if mails and isinstance(mails, list):
                        import re
                        for mail in mails:
                            content = (mail.get('text') or "") + (mail.get('subject') or "")
                            code_match = re.search(r"\b(\d{6})\b", content)
                            if code_match:
                                logger.info(f"成功捕获邮件验证码: {code_match.group(1)}")
                                return code_match.group(1)
            except Exception as e:
                logger.debug(f"轮询邮件中... {e}")
            
            human_delay(5, 7)
        logger.warning("获取邮件验证码超时。")
        return None

    def clear_mailbox(self, email: str, folder: str = "inbox") -> bool:
        """清空收件箱或垃圾箱 (folder: inbox/junk)"""
        endpoint = f"/api/process-{folder}"
        params = {
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "email": email
        }
        try:
            res = requests.get(f"{self.base_url}{endpoint}", params=params, timeout=10)
            if res.status_code == 200:
                logger.info(f"成功清空 {email} 的 {folder}")
                return True
        except Exception as e:
            logger.error(f"清空邮箱失败: {e}")
        return False

# ================= 模块 4: 浏览器控制 =================
def fetch_1024_proxy() -> Optional[Dict[str, Any]]:
    """从 1024Proxy 获取最新的 IP 和 端口"""
    proxy_url = "https://white.1024proxy.com/white/api?region=Rand&num=1&time=10&format=1&type=txt"
    logger.info("正在从 1024Proxy 提取最新代理 IP...")
    try:
        res = requests.get(proxy_url, timeout=10).text.strip()
        if "not added to whitelist" in res:
            logger.error(f"1024Proxy 提取失败：白名单错误 ({res})。请将该 IP 加入后台。")
            return None
        if ":" in res:
            host, port = res.split(":")
            logger.info(f"成功提取代理: {host}:{port}")
            return {"host": host, "port": port}
        logger.error(f"1024Proxy 返回格式未知: {res}")
    except Exception as e:
        logger.error(f"访问 1024Proxy API 异常: {e}")
    return None

def get_browser_ws(user_id: str) -> str:
    """启动 AdsPower 浏览器并获取 CDP WebSocket 端点"""
    logger.info(f"启动 AdsPower 浏览器 (User ID: {user_id})...")
    
    # 获取动态代理
    proxy_info = fetch_1024_proxy()
    
    try:
        # 构建启动参数
        params = {"user_id": user_id}
        if proxy_info:
            # 如果成功获取到 1024Proxy IP，我们通过 API 注入，让 AdsPower 强制使用它
            params["proxy_type"] = "http"
            params["proxy_host"] = proxy_info["host"]
            params["proxy_port"] = proxy_info["port"]
            params["proxy_soft"] = "other"
            logger.info("已将 1024Proxy 注入启动参数。")
        
        # 多重鉴权尝试：Header + Params
        headers = {
            "api-key": CONFIG["ADS_API_KEY"],
            "x-api-key": CONFIG["ADS_API_KEY"] # 部分版本可能使用不同的 header
        }
        
        url = f"{CONFIG['ADS_API']}/api/v1/browser/start"
        logger.debug(f"请求 AdsPower URL: {url} 参数: {params}")
        
        response = requests.get(url, params=params, headers=headers, timeout=20)
        res_data = response.json()
        
        if res_data.get("code") == 0:
            ws_url = res_data["data"]["ws"]["puppeteer"]
            logger.info("AdsPower 浏览器启动成功。")
            return ws_url
        else:
            msg = res_data.get("msg", "Unknown error")
            logger.error(f"AdsPower 启动失败详情: {res_data}")
            if "Require api-key" in msg:
                logger.error("鉴权失败。请尝试在 AdsPower 设置中‘重置 API Key’并更新到脚本中。")
            raise Exception(f"AdsPower 启动失败: {msg}")
            
    except requests.RequestException as e:
        logger.error(f"请求 AdsPower 接口失败: {e}")
        raise

# ================= 主流程自动化 =================
def run_task(ads_id: str, cdk: str, email: str, password: str) -> None:
    """执行自动化注册及绑卡主流程"""
    
    # 基础校验
    if not all([CONFIG["EFUNCARD_TOKEN"], CONFIG["SMS_API_KEY"]]):
        logger.error("缺少必要的环境变量配置 (.env)。请检查 EFUNCARD_TOKEN 或 SMS_API_KEY 是否正确配置。")
        return

    card_api = EfunCard()
    sms_api = SMSManager()
    mail_api = MailManager()

    # 1. 启动浏览器
    try:
        ws_url = get_browser_ws(ads_id)
    except Exception as e:
        logger.error(f"终止任务：无法连接 AdsPower ({e})")
        return

    with sync_playwright() as p:
        try:
            logger.info("连接到 Playwright 浏览器实例...")
            browser = p.chromium.connect_over_cdp(ws_url)
            context: BrowserContext = browser.contexts[0]
            page: Page = context.new_page()
            
            # 设置默认超时时间增强健壮性 (增加到 60 秒应对慢速代理)
            page.set_default_timeout(60000) 

            # 2. 访问注册页
            logger.info("尝试进入 OpenAI 注册流程...")
            page.goto("https://chatgpt.com/auth/login?screen_hint=signup", wait_until="domcontentloaded")
            human_delay(5, 8)

            # --- 处理首页或 Cookie 弹窗 ---
            try:
                # 尝试点击“接受所有 Cookie”按钮 (兼容多语言：Accept all, Aceptar todas, etc)
                cookie_selectors = [
                    'button:has-text("Aceptar todas")', 
                    'button:has-text("Accept all")',
                    'button:has-text("允许所有")'
                ]
                for selector in cookie_selectors:
                    if page.locator(selector).is_visible():
                        logger.info(f"点击 Cookie 同意按钮: {selector}")
                        page.click(selector)
                        human_delay(1, 2)
                        break
                
                # 如果停留在首页，点击“注册”按钮
                signup_selectors = [
                    'button:has-text("Registrarse gratuitamente")',
                    'button:has-text("Sign up")',
                    'a:has-text("Sign up")'
                ]
                for selector in signup_selectors:
                    if page.locator(selector).is_visible():
                        logger.info(f"点击首页注册按钮: {selector}")
                        page.click(selector)
                        human_delay(3, 5)
                        break
            except Exception as e:
                logger.warning(f"处理弹窗或跳转时发生非致命错误: {e}")

            # --- 填写邮箱密码 ---
            logger.info("寻找邮箱输入框并填写...")
            try:
                # 兼容不同的邮箱输入框选择器
                email_selector = 'input#email-input, input[name="email"], input[type="email"]'
                page.wait_for_selector(email_selector, state="visible", timeout=20000)
                human_typing(page, email_selector, email)
                human_delay()
                page.keyboard.press("Enter") # 模拟回车提交
                
                # 等待并填写密码
                logger.info("寻找密码输入框并填写...")
                password_selector = 'input#password, input[name="password"]'
                page.wait_for_selector(password_selector, state="visible", timeout=15000)
                human_delay(1, 2)
                human_typing(page, password_selector, password)
                human_delay()
                page.keyboard.press("Enter")
            except PlaywrightTimeoutError:
                logger.error("未找到输入框。正在保存错误截图到 error_debug.png...")
                page.screenshot(path="error_debug.png")
                raise

            # 3. 手机号验证
            human_delay(5, 8) # 等待页面跳转到手机号验证
            
            # 预先获取手机号
            order_id, phone = sms_api.get_number()
            if not order_id:
                logger.error(f"任务中止: 获取手机号失败 ({phone})")
                return

            logger.info(f"准备填入手机号: {phone}")
            
            # 【占位符】这里需要根据 OpenAI 最新页面填写手机号字段
            try:
                page.wait_for_selector('input[name="phoneNumber"]', state="visible", timeout=20000)
                human_typing(page, 'input[name="phoneNumber"]', phone)
                # 假设有一个发送验证码按钮
                # page.click('button:has-text("Send code")')
            except PlaywrightTimeoutError:
                logger.warning("未找到手机号输入框，可能页面结构已更改或需要解决人机验证。")
                # 可选择在此处抛出异常或截图
                # page.screenshot(path="error_phone_input.png")

            # 等待短信
            code = sms_api.get_code(order_id)
            if not code:
                logger.error("任务中止: 未收到短信验证码。")
                return
                
            logger.info(f"填入短信验证码: {code}")
            # 【占位符】填入短信验证码
            try:
                # human_typing(page, 'input[name="code"]', code)
                pass
            except PlaywrightTimeoutError:
                logger.warning("未找到验证码输入框。")

            # 4. 支付环节 (订阅 Plus)
            human_delay(5, 10)
            logger.info("准备支付订阅...")
            card = card_api.redeem(cdk)
            if not card: 
                logger.error("任务中止: 虚拟卡激活失败。")
                return

            # 跳转到支付页面
            # page.goto("https://chatgpt.com/invite/accepted", wait_until="networkidle") 
            # human_delay(3, 5)

            # 自动填写信用卡信息
            logger.info("填写信用卡信息...")
            try:
                # human_typing(page, 'input[name="cardNumber"]', card["cardNumber"])
                # human_typing(page, 'input[name="expiryDate"]', f"{card['expiryMonth']}/{str(card['expiry_year'])[-2:]}")
                # human_typing(page, 'input[name="cvc"]', card["cvv"])
                # human_delay()
                # page.click('button#checkout-button')
                pass
            except PlaywrightTimeoutError:
                logger.warning("未找到信用卡输入框。")

            # 5. 处理 3DS
            otp = card_api.wait_for_3ds(cdk)
            if otp:
                logger.info(f"捕获到 3DS 验证码: {otp}")
                # 处理 iframe 中的验证码输入
                try:
                    # iframe = page.frame_locator("iframe")
                    # iframe.locator('input#otp').press_sequentially(otp, delay=random.randint(50, 100))
                    # human_delay()
                    # iframe.locator('button[type="submit"]').click()
                    pass
                except Exception as e:
                    logger.error(f"处理 3DS 验证码填写异常: {e}")
            else:
                logger.error("3DS 获取失败，支付可能未完成。")
                
            logger.info("自动化任务执行完毕。保持浏览器开启状态供检查。")
            human_delay(10, 15)

        except Exception as e:
            logger.error(f"执行过程中发生未捕获异常: {e}")
        finally:
            # 根据需要决定是否关闭浏览器
            # browser.close()
            pass

if __name__ == "__main__":
    # 执行任务示例
    run_task(
        ads_id="k1b8us0k",      # 更新为用户提供的真实 AdsPower User ID
        cdk="CDK-ABC12345",      # 你的 Efuncard CDK (如果有)
        email="test@gmail.com",  # 准备注册的邮箱
        password="Password123!"  # 准备注册的密码
    )