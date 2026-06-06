# -*- coding: utf-8 -*-
"""
CSS 选择器常量

从 main.py 提取的页面元素选择器，供 handler 和编排器共用。
"""

# 表单输入
EMAIL_SELECTOR = 'input#email-input, input[name="email"], input[type="email"]'
PASSWORD_SELECTOR = 'input#password, input[name="password"], input[type="password"]'
PHONE_SELECTOR = 'input[name="phoneNumber"]'

# 手机号输入框多重 fallback。
# 真实 DOM（2026-06-01 实测 chatgpt.com 登录弹窗）：
#   <input id="phoneNumberInput" name="phoneNumberInput" type="tel" autocomplete="tel"
#          aria-label="電話番号" placeholder="電話番号">
# 主用 id/name 精确命中，type/autocomplete/aria-label 兜底 DOM 漂移。
PHONE_INPUT_SELECTORS = (
    'input#phoneNumberInput',
    'input[name="phoneNumberInput"]',
    'input[name="phoneNumber"]',       # 历史/其他地区命名兜底
    'input[type="tel"]',
    'input[autocomplete="tel"]',
    'input[inputmode="tel"]',
)

# 国家选择：真实 DOM 是隐藏 <select>（react-phone-number-input）+ 可见 combobox 按钮。
# 隐藏 select 用 ISO 国家码 value（option value="JP"/"US"/"PH"...），
# 用 Playwright select_option(value=ISO) 最可靠通用（比点 combobox 匹配区号稳）。
PHONE_COUNTRY_SELECT_SELECTORS = (
    'div.PhoneInput select',           # react-phone-number-input 隐藏 select
    'select[aria-hidden="true"]',
    'button[aria-label*="国コード"]',   # 兜底：可见 combobox 按钮（需再点 option）
    'button[role="combobox"]',
)

# 手机号 OTP 验证码输入框 fallback（OpenAI 短信验证页，真机可能单框或分离框）。
PHONE_CODE_SELECTORS = (
    'input[name="code"]',
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"][maxlength="1"]',  # 分离式 OTP 第一格
    'input[name="otp"]',
)

# 「電話番号で続行 / Continue with phone」入口按钮（旧版弹窗折叠时需先点开；
# 新版电话框已直接内嵌，无需此按钮）。
# [已弃用文案匹配] 换 IP 换语言枚举不完，改用语言无关的探测式（main._try_open_phone_by_probing）。
# 保留结构强信号作兜底。
PHONE_CONTINUE_SELECTORS = (
    '[data-testid*="phone"]',
    'a[href^="tel:"]',
)

# SMS-Activate 数字国家码 → ISO 3166-1 alpha-2（OpenAI 国家 select 的 option value）。
# 来源：GuJumpgate 15 国表（SMS 数字码）对照真实 DOM 的 <option value="..."> ISO 码。
# 用于 phone 弹窗国家选择：select_option(value=ISO)。缺失则不强制选（沿用默认国）。
COUNTRY_ID_TO_ISO = {
    "4": "PH",    # 菲律宾 Philippines
    "6": "ID",    # 印尼 Indonesia
    "8": "KE",    # 肯尼亚 Kenya
    "10": "VN",   # 越南 Vietnam
    "15": "PL",   # 波兰 Poland
    "16": "GB",   # 英国 United Kingdom
    "32": "RO",   # 罗马尼亚 Romania
    "33": "CO",   # 哥伦比亚 Colombia
    "43": "DE",   # 德国 Germany
    "52": "TH",   # 泰国 Thailand
    "73": "BR",   # 巴西 Brazil
    "78": "FR",   # 法国 France
    "151": "CL",  # 智利 Chile
    "182": "JP",  # 日本 Japan
    "187": "US",  # 美国 USA
}

# SMS-Activate 数字国家码 → 国际电话区号（combobox fallback 路径用，按区号文本匹配 option）。
COUNTRY_ID_TO_DIAL_CODE = {
    "4": "63", "6": "62", "8": "254", "10": "84", "15": "48", "16": "44",
    "32": "40", "33": "57", "43": "49", "52": "66", "73": "55", "78": "33",
    "151": "56", "182": "81", "187": "1",
}

# 邮箱验证页指示器
VERIFICATION_INDICATORS = (
    'text="Consulta tu bandeja"',
    'text="Check your email"',
    'input[name="code"]',
)

# Auth 域名标识
AUTH_HOST_MARKERS = ("auth.openai.com", "auth0.openai.com")

# Cookie 同意按钮
COOKIE_ACCEPT_SELECTORS = (
    '#onetrust-accept-btn-handler',
    'button[data-testid*="cookie"][data-testid*="accept"]',
    'button:has-text("Aceptar todas")',
    'button:has-text("Accept all")',
    'button:has-text("允许所有")',
)

# 注册入口按钮
SIGNUP_SELECTORS = (
    'a[href*="screen_hint=signup"]',
    'a[href*="/signup"]',
    'button[data-testid*="signup"]',
    'a[data-testid*="signup"]',
    'button:has-text("Registrarse gratuitamente")',
    'button:has-text("Sign up")',
    'a:has-text("Sign up")',
)

# 通用提交按钮
PRIMARY_SUBMIT_SELECTORS = (
    'button[type="submit"]',
    'button[data-action-button-primary="true"]',
    'button:has-text("Continue")',
    'button:has-text("Continuar")',
    'button:has-text("Siguiente")',
    'button:has-text("Finalizar")',
)

# 默认账单资料
DEFAULT_BILLING_PROFILE = {
    "country": "US",
    "line1": "350 5th Ave",
    "line2": "",
    "city": "New York",
    "state": "NY",
    "postal_code": "10118",
}

# Checkout 相关
HOSTED_CHECKOUT_PREFIX = "https://pay.openai.com/c/pay/"
CHECKOUT_DECLINE_PATTERNS = (
    r"您的银行卡被拒绝了",
    r"银行卡被拒绝",
    r"Your card was declined",
    r"card was declined",
)

# Stripe 表单选择器
SPLIT_FRAME_CARD_SELECTORS = (
    'input[name="cardnumber"]',
    'input[autocomplete="cc-number"]',
    'input[name="number"]',
    'input[placeholder*="card number" i]',
)
SPLIT_FRAME_EXPIRY_SELECTORS = (
    'input[name="exp-date"]',
    'input[autocomplete="cc-exp"]',
    'input[name="expiry"]',
    'input[placeholder*="MM" i]',
)
SPLIT_FRAME_CVC_SELECTORS = (
    'input[name="cvc"]',
    'input[autocomplete="cc-csc"]',
    'input[name="verification_value"]',
    'input[placeholder*="CVC" i]',
)
SINGLE_FRAME_IFRAME_SELECTORS = (
    'iframe[title="Secure payment input frame"]',
    'iframe[title*="payment" i]',
    'iframe[name*="__privateStripeFrame"]',
)
SINGLE_FRAME_CARD_SELECTORS = (
    'input[name="cardNumber"]',
    'input[name="cardnumber"]',
    'input[name="number"]',
)
SINGLE_FRAME_EXPIRY_SELECTORS = (
    'input[name="cardExpiry"]',
    'input[name="exp-date"]',
    'input[name="expiry"]',
)
SINGLE_FRAME_CVC_SELECTORS = (
    'input[name="cardCvc"]',
    'input[name="cvc"]',
)
