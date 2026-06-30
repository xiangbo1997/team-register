/* ============================================================================
 * Team-Register 全局统一反馈系统 (feedback.js)
 *
 * 提供三类全局 API，替换原生 alert / confirm / prompt，并增强 Toast：
 *   - window.showToast(msg, type, opts)  增强版 Toast（向后兼容旧 (msg,type) 调用）
 *   - window.notify(opts)                showToast 的对象式别名
 *   - window.confirmDialog(opts)         返回 Promise<boolean> 的美观确认弹窗
 *   - window.promptDialog(opts)          返回 Promise<string|null> 的美观输入弹窗
 *
 * 设计原则：
 *   1. 纯原生 DOM + CSS transition，不依赖 Alpine（Alpine 为 defer 脚本，
 *      命令式 Promise API 用原生 DOM 时序更可控、零生命周期耦合）。
 *   2. 复用 base.html 的 Tailwind 设计 token（primary/success/danger/warning/info
 *      + surface/body/border/muted 语义色 CSS 变量），自动适配暗色主题。
 *   3. i18n 默认文案惰性取值（弹窗打开时才调 window.i18n），防早于 i18n 初始化取空。
 *   4. 弹窗 Promise 永不 reject：取消/遮罩/ESC/× 一律 resolve(false|null)，
 *      调用方 `if (!(await confirmDialog(...))) return;` 永远安全。
 * ========================================================================== */
(function () {
    'use strict';

    // ── i18n 惰性兜底：feedback.js 可能早于 window.i18n 定义被引用 ──
    function t(key, fallback) {
        if (typeof window.i18n === 'function') {
            return window.i18n(key, null, fallback);
        }
        return fallback;
    }

    /* ========================================================================
     * 一、增强 Toast
     * ====================================================================== */
    const TOAST_META = {
        success: { icon: 'check_circle', accent: 'text-success', border: 'border-success/30', duration: 4000 },
        error:   { icon: 'error',        accent: 'text-danger',  border: 'border-danger/30',  duration: 6000 },
        warning: { icon: 'warning',      accent: 'text-warning', border: 'border-warning/40', duration: 5000 },
        info:    { icon: 'info',         accent: 'text-info-500', border: 'border-info-500/30', duration: 4000 },
        loading: { icon: 'progress_activity', accent: 'text-primary', border: 'border-primary/30', duration: 0 },
    };

    function ensureToastContainer() {
        let c = document.getElementById('toast-container');
        if (!c) {
            c = document.createElement('div');
            c.id = 'toast-container';
            c.className = 'fixed bottom-4 right-4 z-[100] space-y-2';
            document.body.appendChild(c);
        }
        return c;
    }

    // 复用同一 id 的 toast 句柄缓存（loading → success 原地替换用）
    const _toasts = new Map();

    function buildToastEl(msg, type, opts) {
        const meta = TOAST_META[type] || TOAST_META.info;
        const dismissible = opts.dismissible !== false;

        const el = document.createElement('div');
        el.setAttribute('role', 'status');
        el.className =
            'toast-item flex items-start gap-3 w-80 max-w-[calc(100vw-2rem)] ' +
            'rounded-lg border bg-surface text-body shadow-token-md px-4 py-3 ' +
            'transition-all duration-300 translate-x-2 opacity-0 ' + meta.border;

        // 图标
        const iconSpan = document.createElement('span');
        iconSpan.className = 'material-symbols-outlined text-xl shrink-0 ' + meta.accent +
            (type === 'loading' ? ' animate-spin' : '');
        iconSpan.textContent = meta.icon;
        el.appendChild(iconSpan);

        // 文本区
        const textWrap = document.createElement('div');
        textWrap.className = 'flex-1 min-w-0';
        if (opts.title) {
            const titleEl = document.createElement('div');
            titleEl.className = 'text-sm font-semibold leading-tight';
            titleEl.textContent = opts.title;
            textWrap.appendChild(titleEl);
        }
        const msgEl = document.createElement('div');
        msgEl.className = 'text-sm leading-snug break-words' + (opts.title ? ' text-muted mt-0.5' : '');
        msgEl.textContent = msg;
        textWrap.appendChild(msgEl);
        el.appendChild(textWrap);

        // 关闭按钮
        if (dismissible) {
            const closeBtn = document.createElement('button');
            closeBtn.type = 'button';
            closeBtn.className = 'shrink-0 -mr-1 -mt-0.5 p-0.5 rounded text-muted hover:text-body hover:bg-subtle transition-colors';
            closeBtn.innerHTML = '<span class="material-symbols-outlined text-base">close</span>';
            el.appendChild(closeBtn);
        }
        return el;
    }

    function dismissToast(record) {
        if (!record || record._dismissed) return;
        record._dismissed = true;
        if (record.timer) clearTimeout(record.timer);
        const el = record.el;
        el.classList.remove('opacity-100', 'translate-x-0');
        el.classList.add('opacity-0', 'translate-x-2');
        setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 300);
        if (record.id) _toasts.delete(record.id);
    }

    function scheduleAutoDismiss(record, duration) {
        if (record.timer) clearTimeout(record.timer);
        if (duration && duration > 0) {
            record.timer = setTimeout(() => dismissToast(record), duration);
        }
    }

    /**
     * 增强 Toast。向后兼容旧调用 showToast('消息', 'success')。
     * @param {string} msg  消息正文
     * @param {string} [type='info']  success|error|warning|info|loading
     * @param {object} [opts]  { title?, duration?, dismissible?(默认true), id? }
     * @returns {{id, update, dismiss}} 句柄（旧调用不接收，无影响）
     */
    function showToast(msg, type = 'info', opts = {}) {
        const container = ensureToastContainer();
        const meta = TOAST_META[type] || TOAST_META.info;

        // 复用已有 id 的 toast（如 loading → success 原地替换）
        if (opts.id && _toasts.has(opts.id)) {
            return _toasts.get(opts.id).update({ msg, type, ...opts });
        }

        const el = buildToastEl(msg, type, opts);
        container.appendChild(el);
        // 下一帧触发进入动画
        requestAnimationFrame(() => {
            el.classList.remove('translate-x-2', 'opacity-0');
            el.classList.add('translate-x-0', 'opacity-100');
        });

        const record = {
            id: opts.id || null,
            el,
            timer: null,
            _dismissed: false,
            dismiss() { dismissToast(record); },
            update(patch) {
                const nextType = patch.type || type;
                const nextMeta = TOAST_META[nextType] || TOAST_META.info;
                const nextMsg = patch.msg != null ? patch.msg : msg;
                const nextOpts = Object.assign({}, opts, patch);
                // 重建内容（简单稳妥）：替换图标 class/文本 + 文本区 + 边框色
                const fresh = buildToastEl(nextMsg, nextType, nextOpts);
                el.className = fresh.className.replace('translate-x-2 opacity-0', 'translate-x-0 opacity-100');
                el.innerHTML = fresh.innerHTML;
                // 重新绑定关闭按钮
                const cb = el.querySelector('button');
                if (cb) cb.addEventListener('click', () => dismissToast(record));
                const dur = patch.duration != null ? patch.duration : nextMeta.duration;
                scheduleAutoDismiss(record, dur);
                return record;
            },
        };

        const closeBtn = el.querySelector('button');
        if (closeBtn) closeBtn.addEventListener('click', () => dismissToast(record));

        if (opts.id) _toasts.set(opts.id, record);
        const duration = opts.duration != null ? opts.duration : meta.duration;
        scheduleAutoDismiss(record, duration);
        return record;
    }

    /** showToast 的对象式别名：notify({ type, title, message, duration, id, dismissible }) */
    function notify(opts = {}) {
        const { message = '', type = 'info' } = opts;
        return showToast(message, type, opts);
    }

    /* ========================================================================
     * 二、单例弹窗宿主（confirmDialog / promptDialog 复用）
     * ====================================================================== */
    let _dialogBusy = false;       // 当前是否有弹窗打开
    let _dialogQueue = Promise.resolve();  // 串行化队列，防双击双弹窗

    function getModalRoot() {
        let root = document.getElementById('feedback-modal-root');
        if (!root) {
            root = document.createElement('div');
            root.id = 'feedback-modal-root';
            root.className = 'hidden';
            document.body.appendChild(root);
        }
        return root;
    }

    // 转义 HTML，message/label 等用户拼接文本走 textContent，不用 innerHTML
    function setText(el, text) {
        el.textContent = text == null ? '' : String(text);
    }

    /**
     * 打开一个弹窗。kind: 'confirm' | 'prompt'
     * 返回 Promise，resolve 值由具体类型决定。
     */
    function openDialog(kind, opts) {
        // 串行化：上一个弹窗关闭后才开下一个，防 resolve 被覆盖
        const run = () => new Promise((resolve) => {
            _dialogBusy = true;
            const root = getModalRoot();
            root.classList.remove('hidden');

            const isPrompt = kind === 'prompt';
            const danger = !!opts.danger;
            const multiline = !!opts.multiline;
            const optional = !!opts.optional;

            const titleText = opts.title != null ? opts.title
                : (isPrompt ? t('common.input', '请输入') : t('common.confirm', '确认'));
            const confirmText = opts.confirmText != null ? opts.confirmText
                : (danger ? t('common.delete', '删除') : (isPrompt ? t('common.submit', '确定') : t('common.confirm', '确认')));
            const cancelText = opts.cancelText != null ? opts.cancelText : t('common.cancel', '取消');
            const iconName = opts.icon || (danger ? 'warning' : (isPrompt ? 'edit' : 'help'));
            const iconAccent = danger ? 'text-danger' : 'text-primary';
            const iconBg = danger ? 'bg-danger/10' : 'bg-primary/10';
            const confirmBtnClass = danger
                ? 'bg-danger hover:bg-danger/90 text-white'
                : 'bg-primary hover:bg-primary-hover text-white';

            // ── 构建 DOM ──
            const overlay = document.createElement('div');
            overlay.className =
                'fixed inset-0 z-[90] bg-black/40 backdrop-blur-[1px] flex items-center justify-center p-4 ' +
                'opacity-0 transition-opacity duration-150';

            const panel = document.createElement('div');
            panel.className =
                'relative z-[91] w-full max-w-md rounded-xl border border-border bg-surface text-body ' +
                'shadow-token-lg scale-95 opacity-0 transition-all duration-150';
            panel.setAttribute('role', 'dialog');
            panel.setAttribute('aria-modal', 'true');

            // header
            const header = document.createElement('div');
            header.className = 'flex items-start gap-3 px-5 pt-5';
            const iconWrap = document.createElement('div');
            iconWrap.className = 'shrink-0 w-9 h-9 rounded-full flex items-center justify-center ' + iconBg;
            iconWrap.innerHTML = '<span class="material-symbols-outlined text-xl ' + iconAccent + '"></span>';
            iconWrap.querySelector('span').textContent = iconName;
            const headTextWrap = document.createElement('div');
            headTextWrap.className = 'flex-1 min-w-0 pt-1';
            const titleEl = document.createElement('h3');
            titleEl.className = 'text-base font-semibold leading-tight';
            setText(titleEl, titleText);
            headTextWrap.appendChild(titleEl);
            if (opts.message != null && opts.message !== '') {
                const msgEl = document.createElement('p');
                msgEl.className = 'mt-1.5 text-sm text-muted whitespace-pre-line break-words';
                setText(msgEl, opts.message);
                headTextWrap.appendChild(msgEl);
            }
            header.appendChild(iconWrap);
            header.appendChild(headTextWrap);
            panel.appendChild(header);

            // body：prompt 的输入区
            let inputEl = null;
            let errEl = null;
            if (isPrompt) {
                const body = document.createElement('div');
                body.className = 'px-5 pt-4';
                if (opts.label) {
                    const labelEl = document.createElement('label');
                    labelEl.className = 'block text-xs font-medium text-muted mb-1.5';
                    setText(labelEl, opts.label);
                    body.appendChild(labelEl);
                }
                const inputCls =
                    'w-full rounded-lg border border-border bg-surface text-body text-sm px-3 py-2 ' +
                    'placeholder:text-muted focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition';
                if (multiline) {
                    inputEl = document.createElement('textarea');
                    inputEl.rows = 4;
                    inputEl.className = inputCls + ' resize-y font-mono';
                } else {
                    inputEl = document.createElement('input');
                    inputEl.type = 'text';
                    inputEl.className = inputCls;
                }
                if (opts.placeholder) inputEl.placeholder = opts.placeholder;
                if (opts.default != null) inputEl.value = String(opts.default);
                body.appendChild(inputEl);
                errEl = document.createElement('p');
                errEl.className = 'mt-1.5 text-xs text-danger hidden';
                body.appendChild(errEl);
                panel.appendChild(body);
            }

            // footer
            const footer = document.createElement('div');
            footer.className = 'flex items-center justify-end gap-2 px-5 py-4 mt-1';
            const cancelBtn = document.createElement('button');
            cancelBtn.type = 'button';
            cancelBtn.className =
                'inline-flex items-center px-4 py-2 rounded-md text-sm font-medium border border-border ' +
                'text-body bg-surface hover:bg-subtle transition-colors';
            setText(cancelBtn, cancelText);
            const confirmBtn = document.createElement('button');
            confirmBtn.type = 'button';
            confirmBtn.className =
                'inline-flex items-center px-4 py-2 rounded-md text-sm font-medium shadow-token-sm transition-colors ' +
                'disabled:opacity-50 disabled:cursor-not-allowed ' + confirmBtnClass;
            setText(confirmBtn, confirmText);
            footer.appendChild(cancelBtn);
            footer.appendChild(confirmBtn);
            panel.appendChild(footer);

            overlay.appendChild(panel);
            root.appendChild(overlay);

            // ── 进入动画 ──
            requestAnimationFrame(() => {
                overlay.classList.remove('opacity-0');
                panel.classList.remove('scale-95', 'opacity-0');
                panel.classList.add('scale-100', 'opacity-100');
            });

            // ── prompt 输入校验：optional=false 空值禁用确认；validate 钩子 ──
            function currentValue() { return inputEl ? inputEl.value : ''; }
            function refreshValidity() {
                if (!isPrompt) return;
                const v = currentValue();
                let invalid = false;
                if (!optional && v.trim() === '') invalid = true;
                confirmBtn.disabled = invalid;
            }
            function runValidate() {
                if (typeof opts.validate === 'function') {
                    const err = opts.validate(currentValue());
                    if (err) {
                        errEl.textContent = err;
                        errEl.classList.remove('hidden');
                        inputEl.classList.add('border-danger', 'focus:ring-danger/30');
                        return false;
                    }
                }
                errEl && errEl.classList.add('hidden');
                inputEl && inputEl.classList.remove('border-danger', 'focus:ring-danger/30');
                return true;
            }

            // ── 关闭 + resolve ──
            let _settled = false;
            function close(result) {
                if (_settled) return;
                _settled = true;
                document.removeEventListener('keydown', onKeydown, true);
                overlay.classList.add('opacity-0');
                panel.classList.remove('scale-100', 'opacity-100');
                panel.classList.add('scale-95', 'opacity-0');
                setTimeout(() => {
                    if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
                    if (!root.querySelector('[role="dialog"]')) root.classList.add('hidden');
                    _dialogBusy = false;
                    resolve(result);
                }, 150);
            }

            const CANCEL_RESULT = isPrompt ? null : false;
            cancelBtn.addEventListener('click', () => close(CANCEL_RESULT));
            confirmBtn.addEventListener('click', () => {
                if (isPrompt) {
                    if (confirmBtn.disabled) return;
                    if (!runValidate()) return;
                    close(currentValue());
                } else {
                    close(true);
                }
            });
            // 点遮罩（仅遮罩本身，不含面板）= 取消
            overlay.addEventListener('mousedown', (e) => {
                if (e.target === overlay) close(CANCEL_RESULT);
            });

            // 键盘：ESC 取消；Enter(input)/Cmd+Enter(textarea) 确认
            function onKeydown(e) {
                if (e.key === 'Escape') {
                    e.stopImmediatePropagation();  // 防连带关闭 assistant 面板的 @keydown.escape.window
                    close(CANCEL_RESULT);
                    return;
                }
                if (e.key === 'Enter') {
                    if (!isPrompt) {
                        e.preventDefault();
                        close(true);
                    } else if (!multiline || e.metaKey || e.ctrlKey) {
                        e.preventDefault();
                        if (!confirmBtn.disabled && runValidate()) close(currentValue());
                    }
                }
            }
            document.addEventListener('keydown', onKeydown, true);

            // 输入监听 + 初始聚焦
            if (isPrompt && inputEl) {
                inputEl.addEventListener('input', refreshValidity);
                refreshValidity();
                requestAnimationFrame(() => { inputEl.focus(); inputEl.select && inputEl.select(); });
            } else {
                requestAnimationFrame(() => confirmBtn.focus());
            }
        });

        // 串到队列尾，保证串行
        const result = _dialogQueue.then(run);
        _dialogQueue = result.catch(() => {});  // 隔离失败，队列不中断
        return result;
    }

    /**
     * 美观确认弹窗，替换原生 confirm。
     * @param {object} opts { title, message, confirmText, cancelText, danger, icon }
     * @returns {Promise<boolean>} 确认 true；取消/遮罩/ESC/× false（永不 reject）
     */
    function confirmDialog(opts = {}) {
        if (typeof opts === 'string') opts = { message: opts };
        return openDialog('confirm', opts);
    }

    /**
     * 美观输入弹窗，替换原生 prompt。
     * @param {object} opts { title, label, message, default, placeholder, optional, multiline, confirmText, cancelText, validate }
     * @returns {Promise<string|null>} 确认返回输入值（原样不 trim）；取消返回 null（永不 reject）
     */
    function promptDialog(opts = {}) {
        return openDialog('prompt', opts);
    }

    // ── 挂到全局 ──
    window.showToast = showToast;
    window.notify = notify;
    window.confirmDialog = confirmDialog;
    window.promptDialog = promptDialog;
})();
