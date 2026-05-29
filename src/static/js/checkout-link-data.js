/* ================================================================
 * Checkout Link 页面：国家 / 货币静态数据
 *
 * 数据来源：基于 src/data/promo_seeds/__init__.py:43-87 的 COUNTRY_SUFFIXES
 * （44 国，过去做 promo 爆破时确认过 ChatGPT 实际支持），扩充 ~16 个常用市场
 * 总计 60 个国家，覆盖 OpenAI 全球绝大多数计费市场。
 *
 * 暴露：window.CheckoutLinkData = { countries, currencies, countryToCurrency, flag(code) }
 * 国旗 emoji 生成器复用 src/templates/components/_country.html:32-36 的方案。
 * ================================================================ */

(function () {
  'use strict';

  // 60 国家 + 货币的权威映射（数据集驱动 UI 三个行为：列表/搜索/联动）
  const COUNTRIES = [
    // 北美 + 大洋洲
    { code: 'US', currency: 'USD' },
    { code: 'CA', currency: 'CAD' },
    { code: 'AU', currency: 'AUD' },
    { code: 'NZ', currency: 'NZD' },
    // 英国 / 爱尔兰
    { code: 'GB', currency: 'GBP' },
    { code: 'IE', currency: 'EUR' },
    // 东亚 + 东南亚
    { code: 'JP', currency: 'JPY' },
    { code: 'KR', currency: 'KRW' },
    { code: 'SG', currency: 'SGD' },
    { code: 'HK', currency: 'HKD' },
    { code: 'TW', currency: 'TWD' },
    { code: 'MY', currency: 'MYR' },
    { code: 'TH', currency: 'THB' },
    { code: 'PH', currency: 'PHP' },
    { code: 'ID', currency: 'IDR' },
    { code: 'VN', currency: 'VND' },
    { code: 'IN', currency: 'INR' },
    // 欧元区
    { code: 'DE', currency: 'EUR' },
    { code: 'FR', currency: 'EUR' },
    { code: 'IT', currency: 'EUR' },
    { code: 'ES', currency: 'EUR' },
    { code: 'NL', currency: 'EUR' },
    { code: 'BE', currency: 'EUR' },
    { code: 'AT', currency: 'EUR' },
    { code: 'PT', currency: 'EUR' },
    { code: 'GR', currency: 'EUR' },
    { code: 'FI', currency: 'EUR' },
    { code: 'LU', currency: 'EUR' },
    { code: 'MT', currency: 'EUR' },
    { code: 'CY', currency: 'EUR' },
    { code: 'EE', currency: 'EUR' },
    { code: 'LV', currency: 'EUR' },
    { code: 'LT', currency: 'EUR' },
    { code: 'HR', currency: 'EUR' },
    { code: 'SK', currency: 'EUR' },
    // 北欧（非欧元区）
    { code: 'SE', currency: 'SEK' },
    { code: 'NO', currency: 'NOK' },
    { code: 'DK', currency: 'DKK' },
    { code: 'IS', currency: 'ISK' },
    // 中欧 + 东欧（非欧元区）
    { code: 'CH', currency: 'CHF' },
    { code: 'PL', currency: 'PLN' },
    { code: 'CZ', currency: 'CZK' },
    { code: 'HU', currency: 'HUF' },
    { code: 'RO', currency: 'RON' },
    { code: 'BG', currency: 'BGN' },
    // 中东
    { code: 'AE', currency: 'AED' },
    { code: 'SA', currency: 'SAR' },
    { code: 'IL', currency: 'ILS' },
    { code: 'TR', currency: 'TRY' },
    // 非洲
    { code: 'ZA', currency: 'ZAR' },
    { code: 'NG', currency: 'NGN' },
    { code: 'KE', currency: 'KES' },
    { code: 'EG', currency: 'EGP' },
    // 拉美
    { code: 'BR', currency: 'BRL' },
    { code: 'MX', currency: 'MXN' },
    { code: 'AR', currency: 'ARS' },
    { code: 'CL', currency: 'CLP' },
    { code: 'CO', currency: 'COP' },
    { code: 'PE', currency: 'PEN' },
  ];

  // 货币去重列表（自动从 COUNTRIES 派生），按字母排序便于"货币 combobox"按字母滚
  const CURRENCIES = [...new Set(COUNTRIES.map((c) => c.currency))].sort();

  // 快查：code → currency（避免每次 .find）
  const COUNTRY_TO_CURRENCY = Object.fromEntries(
    COUNTRIES.map((c) => [c.code, c.currency])
  );

  /**
   * ISO 3166-1 alpha-2 → 国旗 emoji（复用 _country.html:32-36 的方案）
   * 仅对 2 字母大写 ASCII 有效；其它返回 🌐
   */
  function flag(code) {
    const c = String(code || '').toUpperCase();
    if (c.length !== 2 || !/^[A-Z]{2}$/.test(c)) return '🌐';
    return String.fromCodePoint(
      ...[...c].map((ch) => 0x1F1E6 + ch.charCodeAt(0) - 65)
    );
  }

  window.CheckoutLinkData = {
    countries: COUNTRIES,
    currencies: CURRENCIES,
    countryToCurrency: COUNTRY_TO_CURRENCY,
    flag: flag,
  };
})();
