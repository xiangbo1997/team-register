# -*- coding: utf-8 -*-
"""
CSS 选择器常量

从 main.py 提取的页面元素选择器，供 handler 和编排器共用。
"""

# 表单输入
EMAIL_SELECTOR = 'input#email-input, input[name="email"], input[type="email"]'
PASSWORD_SELECTOR = 'input#password, input[name="password"], input[type="password"]'
PHONE_SELECTOR = 'input[name="phoneNumber"]'

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
