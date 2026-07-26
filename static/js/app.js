/* ============================================================
   幻城 · 签到中枢  ——  前端交互（性能优化版）
   优化项：正则提升 / DOM 缓存 / 并行加载 / 可见性感知轮询
          / DocumentFragment 批量插入 / 单遍计数 / requestIdleCallback
   ============================================================ */

(function () {
    "use strict";

    // ============================================================
    // 模块级常量（js-hoist-regexp / js-cache-property-access）
    // 正则与映射表只创建一次，避免循环内重复构造
    // ============================================================

    const $ = (sel) => document.querySelector(sel);

    // 日志行正则：[时间] [级别] 消息 —— 提升到模块级
    const LOG_LINE_RE = /^(\[[^\]]+\])\s+\[(OK|INFO|WARN|ERR)\s*\]\s+(.*)$/;
    // 余额数字提取正则 —— 提升到模块级
    const NUM_RE = /[\d.]+/;

    // HTML 转义映射 —— 提升到模块级，避免每次调用新建对象
    const ESCAPE_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    const ESCAPE_RE = /[&<>"']/g;
    function escapeHtml(s) {
        return String(s).replace(ESCAPE_RE, (c) => ESCAPE_MAP[c]);
    }

    // ============================================================
    // DOM 引用缓存（js-cache-property-access）
    // 所有元素只查询一次，后续直接引用
    // ============================================================

    const dom = {};

    function cacheDom() {
        const ids = [
            "toast", "stars", "particles",
            "statAccounts", "statSuccess", "statStreak", "statBalance",
            "checkinBtn", "progressWrap", "progressFill", "progressCurrent",
            "progressCount", "actionHint", "resultList",
            "accountList", "accountCountTag",
            "addForm", "usernameInput", "passwordInput",
            "refreshStatusBtn", "statusBody", "statusLoading",
            "logContent", "autoscrollCheck", "clearLogBtn",
        ];
        for (let i = 0; i < ids.length; i++) {
            const id = ids[i];
            dom[id] = document.getElementById(id);
        }
    }

    // ============================================================
    // API 封装
    // ============================================================

    async function api(path, opts) {
        const res = await fetch(path, opts);
        let data = null;
        try { data = await res.json(); } catch (e) { /* noop */ }
        if (!res.ok) {
            throw new Error((data && data.message) || `请求失败 (${res.status})`);
        }
        return data;
    }

    // ============================================================
    // Toast
    // ============================================================

    let toastTimer = null;
    function toast(msg, type) {
        const el = dom.toast;
        el.textContent = msg;
        el.className = "toast show" + (type ? " " + type : "");
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { el.className = "toast"; }, 3200);
    }

    // ============================================================
    // 背景粒子 —— DocumentFragment 批量插入 + requestIdleCallback
    // ============================================================

    function buildBackground() {
        const fragStars = document.createDocumentFragment();
        const fragMotes = document.createDocumentFragment();

        // 星点
        const STAR_COUNT = 70;
        for (let i = 0; i < STAR_COUNT; i++) {
            const s = document.createElement("span");
            s.className = "star";
            const size = Math.random() * 2 + 0.6;
            s.style.cssText =
                `width:${size}px;height:${size}px;left:${Math.random()*100}%;` +
                `top:${Math.random()*70}%;animation-delay:${Math.random()*4}s;` +
                `animation-duration:${3+Math.random()*4}s;`;
            fragStars.appendChild(s);
        }

        // 飘升微粒（沙尘 / 数据尘埃）
        const MOTE_COUNT = 26;
        const colors = ["var(--neon)", "var(--ember)", "var(--gold)", "var(--magenta)"];
        for (let i = 0; i < MOTE_COUNT; i++) {
            const m = document.createElement("span");
            m.className = "mote";
            const size = Math.random() * 5 + 2;
            const dur = 12 + Math.random() * 16;
            m.style.cssText =
                `width:${size}px;height:${size}px;left:${Math.random()*100}%;` +
                `bottom:${-Math.random()*20}px;` +
                `background:radial-gradient(circle,${colors[i%colors.length]},transparent 70%);` +
                `animation-duration:${dur}s;animation-delay:${-Math.random()*dur}s;`;
            fragMotes.appendChild(m);
        }

        // 一次性插入，避免 96 次回流
        dom.stars.appendChild(fragStars);
        dom.particles.appendChild(fragMotes);
    }

    // ============================================================
    // 账号列表
    // ============================================================

    async function loadAccounts() {
        try {
            const list = await api("/api/accounts");
            renderAccounts(list);
            dom.statAccounts.textContent = list.length;
            dom.accountCountTag.textContent = `${list.length} 个账号`;
            updateCheckinBtnState();
        } catch (e) {
            toast("加载账号失败：" + e.message, "err");
        }
    }

    function renderAccounts(list) {
        const ul = dom.accountList;
        if (!list.length) {
            ul.innerHTML = '<li class="empty-state">尚无账号，请在上方添加</li>';
            return;
        }
        let html = "";
        for (let i = 0; i < list.length; i++) {
            const name = list[i].username || "?";
            const initial = escapeHtml(name.charAt(0).toUpperCase());
            const safeName = escapeHtml(name);
            html +=
                `<li class="account-card">` +
                `<div class="card-header">` +
                `<span class="card-avatar">${initial}</span>` +
                `<span class="card-name">${safeName}</span>` +
                `<button class="card-delete" data-user="${safeName}" title="删除账号" aria-label="删除账号">×</button>` +
                `</div>` +
                `<div class="card-body">` +
                `<div class="card-row"><span class="card-label">余额</span><span class="card-val" id="balance-${i}">—</span></div>` +
                `<div class="card-row"><span class="card-label">今日获得</span><span class="card-val" id="today-${i}">—</span></div>` +
                `<div class="card-row"><span class="card-label">连续签到</span><span class="card-val" id="streak-${i}">—</span></div>` +
                `<div class="card-row"><span class="card-label">今日状态</span><span class="card-val card-status" id="status-${i}">—</span></div>` +
                `</div>` +
                `</li>`;
        }
        ul.innerHTML = html;
    }

    async function addAccount(username, password) {
        try {
            await api("/api/accounts", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username, password }),
            });
            toast("账号已添加", "ok");
            await loadAccounts();
        } catch (e) {
            toast("添加失败：" + e.message, "err");
        }
    }

    async function removeAccount(username) {
        if (!confirm(`确定删除账号「${username}」吗？`)) return;
        try {
            await api("/api/accounts/" + encodeURIComponent(username), { method: "DELETE" });
            toast("账号已删除", "ok");
            await loadAccounts();
            if (dom.statusBody.children.length && !dom.statusBody.querySelector(".empty-state")) {
                refreshStatus();
            }
        } catch (e) {
            toast("删除失败：" + e.message, "err");
        }
    }

    // ============================================================
    // 签到
    // ============================================================

    let statusPollTimer = null;
    const POLL_INTERVAL = 1500; // 签到任务进行中的轮询间隔（ms）

    function updateCheckinBtnState(running) {
        const btn = dom.checkinBtn;
        const txt = btn.querySelector(".btn-text");
        if (running) {
            btn.classList.add("running");
            btn.disabled = true;
            txt.textContent = "签到进行中";
        } else {
            btn.classList.remove("running");
            btn.disabled = false;
            txt.textContent = "启动签到";
        }
    }

    async function startCheckin() {
        if (dom.checkinBtn.disabled) return;
        try {
            await api("/api/checkin", { method: "POST" });
            toast("签到任务已启动", "ok");
            dom.resultList.innerHTML = "";
            dom.progressWrap.hidden = false;
            dom.actionHint.textContent = "正在自动登录并签到，请稍候…";
            startPolling();
        } catch (e) {
            toast(e.message, "warn");
        }
    }

    function startPolling() {
        updateCheckinBtnState(true);
        if (statusPollTimer) clearInterval(statusPollTimer);
        statusPollTimer = setInterval(pollCheckinStatus, POLL_INTERVAL);
        pollCheckinStatus();
    }

    function stopPolling() {
        if (statusPollTimer) {
            clearInterval(statusPollTimer);
            statusPollTimer = null;
        }
    }

    async function pollCheckinStatus() {
        // 标签页隐藏时跳过轮询，节省网络与 CPU
        if (document.hidden) return;
        try {
            const st = await api("/api/checkin/status");
            renderProgress(st);
            renderResults(st.results || []);
            renderLogs(st.log_lines || []);

            if (!st.running) {
                stopPolling();
                updateCheckinBtnState(false);
                const results = st.results || [];
                // js-combine-iterations：单遍计数，不产生中间数组
                let ok = 0;
                for (let i = 0; i < results.length; i++) {
                    if (results[i].success) ok++;
                }
                dom.actionHint.textContent = `完成：成功 ${ok} / 共 ${results.length}`;
                if (results.length) {
                    toast(`签到完成：成功 ${ok} / ${results.length}`, ok === results.length ? "ok" : "warn");
                }
                await loadSummary();
            }
        } catch (e) {
            // 网络抖动忽略
        }
    }

    function renderProgress(st) {
        const p = st.progress || { done: 0, total: 0, current: "" };
        const total = p.total || 1;
        const pct = Math.round((p.done / total) * 100);
        dom.progressFill.style.width = pct + "%";
        dom.progressCount.textContent = `${p.done} / ${p.total}`;
        dom.progressCurrent.textContent =
            st.running ? (p.current ? `正在处理：${p.current}` : "准备中…") : "已完成";
    }

    function renderResults(results) {
        const ul = dom.resultList;
        if (!results.length) { ul.innerHTML = ""; return; }
        let html = "";
        for (let i = 0; i < results.length; i++) {
            const r = results[i];
            let cls = "fail", badge = "!", detail = r.message || "失败";
            if (r.already_checked) { cls = "already"; badge = "✓"; detail = `已签到 · 余额 ${r.balance}`; }
            else if (r.success) { cls = "ok"; badge = "✓"; detail = `+${r.quota_awarded} · 余额 ${r.balance}`; }
            html +=
                `<li class="result-item ${cls}">` +
                `<span class="result-badge">${badge}</span>` +
                `<span class="result-name">${escapeHtml(r.username)}</span>` +
                `<span class="result-detail">${escapeHtml(detail)}</span>` +
                `</li>`;
        }
        ul.innerHTML = html;
    }

    // ============================================================
    // 汇总
    // ============================================================

    async function loadSummary() {
        try {
            const s = await api("/api/summary");
            dom.statAccounts.textContent = s.account_count;
            dom.statSuccess.innerHTML =
                `${s.last_success}<span class="stat-unit">/${s.last_total}</span>`;
        } catch (e) { /* noop */ }
    }

    // ============================================================
    // 实时状态表
    // ============================================================

    async function refreshStatus() {
        dom.statusLoading.hidden = false;
        dom.refreshStatusBtn.classList.add("spinning");
        try {
            const data = await api("/api/status");
            renderStatusTable(data);
            // 单遍计算：最大连续天数 + 总余额（js-combine-iterations）
            let maxStreak = 0;
            let total = 0;
            for (let i = 0; i < data.length; i++) {
                const d = data[i];
                const streak = d.continuous_days || 0;
                if (streak > maxStreak) maxStreak = streak;
                const m = NUM_RE.exec(d.balance); // 复用模块级正则
                if (m) total += parseFloat(m[0]);
            }
            dom.statStreak.innerHTML = `${maxStreak}<span class="stat-unit">天</span>`;
            dom.statBalance.textContent = total ? `¥${total.toFixed(2)}` : "—";
        } catch (e) {
            toast("状态查询失败：" + e.message, "err");
        } finally {
            dom.statusLoading.hidden = true;
            dom.refreshStatusBtn.classList.remove("spinning");
        }
    }

    function renderStatusTable(data) {
        const body = dom.statusBody;
        if (!data.length) {
            body.innerHTML = '<tr><td colspan="5" class="empty-state">尚无账号</td></tr>';
            return;
        }
        let html = "";
        for (let i = 0; i < data.length; i++) {
            const d = data[i];
            const todayPill = d.online
                ? (d.checked_today
                    ? '<span class="pill yes">已签到</span>'
                    : '<span class="pill no">未签到</span>')
                : '<span class="pill off">离线</span>';
            html +=
                `<tr>` +
                `<td>${escapeHtml(d.username)}</td>` +
                `<td>${escapeHtml(d.balance)}</td>` +
                `<td>${escapeHtml(d.used)}</td>` +
                `<td>${d.continuous_days || 0}</td>` +
                `<td>${todayPill}</td>` +
                `</tr>`;
        }
        body.innerHTML = html;
        // 同步更新左侧账号卡片的实时数据
        updateAccountCards(data);
    }

    function updateAccountCards(data) {
        for (let i = 0; i < data.length; i++) {
            const d = data[i];
            const bal = document.getElementById(`balance-${i}`);
            const today = document.getElementById(`today-${i}`);
            const streak = document.getElementById(`streak-${i}`);
            const status = document.getElementById(`status-${i}`);
            if (!bal) continue;
            if (d.online) {
                bal.textContent = d.balance || "—";
                today.textContent = d.today_award || "—";
                streak.textContent = (d.continuous_days || 0) + " 天";
                if (status) {
                    status.className = "card-val card-status";
                    if (d.checked_today) {
                        status.textContent = "✓ 已签到";
                        status.style.color = "var(--ai-green)";
                    } else {
                        status.textContent = "○ 未签到";
                        status.style.color = "var(--ai-brown-light)";
                    }
                }
            } else {
                bal.textContent = "—";
                today.textContent = "—";
                streak.textContent = "—";
                if (status) {
                    status.className = "card-val card-status";
                    status.textContent = "× 离线";
                    status.style.color = "var(--ai-red)";
                }
            }
        }
    }

    // ============================================================
    // 日志
    // ============================================================

    let lastLogSig = "";
    function renderLogs(lines) {
        if (!lines || !lines.length) return;
        const sig = lines.length + ":" + (lines[lines.length - 1] || "");
        if (sig === lastLogSig) return;
        lastLogSig = sig;

        const pre = dom.logContent;
        pre.classList.remove("empty");
        // 单遍拼接，避免 map + join 产生中间数组
        let html = "";
        for (let i = 0; i < lines.length; i++) {
            if (i > 0) html += "\n";
            html += formatLogLine(lines[i]);
        }
        pre.innerHTML = html;

        if (dom.autoscrollCheck.checked) {
            pre.scrollTop = pre.scrollHeight;
        }
    }

    function formatLogLine(line) {
        const m = LOG_LINE_RE.exec(line); // 复用模块级正则
        if (!m) return `<span class="log-line">${escapeHtml(line)}</span>`;
        const ts = escapeHtml(m[1]);
        const lvl = m[2].trim();
        const msg = escapeHtml(m[3]);
        return `<span class="log-line"><span class="ts">${ts}</span> [<span class="lvl-${lvl}">${lvl}</span>] ${msg}</span>`;
    }

    async function loadFileLogs() {
        try {
            const data = await api("/api/logs");
            renderLogs(data.lines || []);
        } catch (e) { /* noop */ }
    }

    // ============================================================
    // 可见性感知的空闲轮询（client-event-listeners）
    // 每 3 分钟自动刷新日志（无签到任务时），不自动刷新状态
    // ============================================================

    let idleLogTimer = null;
    const IDLE_LOG_INTERVAL = 3 * 60 * 1000; // 3 分钟

    function scheduleIdleLogPoll() {
        if (idleLogTimer) clearTimeout(idleLogTimer);
        idleLogTimer = setTimeout(() => {
            // 仅在标签可见且无签到任务时拉取日志
            if (!document.hidden && !statusPollTimer) {
                loadFileLogs();
            }
            scheduleIdleLogPoll(); // 自调度，便于动态调整
        }, IDLE_LOG_INTERVAL);
    }

    // ============================================================
    // 初始化
    // ============================================================

    function init() {
        cacheDom();

        // 面板模式（scheme3-panels.html）：仅初始化共享工具，跳过所有渲染逻辑
        if (window.PANELS_MODE) {
            // 粒子背景
            const ric = window.requestIdleCallback || ((fn) => setTimeout(fn, 200));
            ric(buildBackground);

            // 签到控制
            dom.checkinBtn.addEventListener("click", startCheckin);

            // 日志清屏
            dom.clearLogBtn.addEventListener("click", () => {
                dom.logContent.textContent = "（已清屏，等待新日志…）";
                dom.logContent.classList.add("empty");
                lastLogSig = "";
                api("/api/logs", { method: "POST" }).catch(() => {});
            });

            // 启动空闲日志轮询
            scheduleIdleLogPoll();

            // 检查进行中的签到任务
            api("/api/checkin/status").then((st) => {
                if (st.running) {
                    dom.progressWrap.hidden = false;
                    dom.actionHint.textContent = "正在自动登录并签到，请稍候…";
                    startPolling();
                }
            }).catch(() => {});

            // 可见性变化
            document.addEventListener("visibilitychange", () => {
                if (document.hidden) {
                    document.body.classList.add("hidden-animations");
                } else {
                    document.body.classList.remove("hidden-animations");
                    if (!document.hidden && statusPollTimer) pollCheckinStatus();
                }
            });

            return; // 面板模式到此结束，其余逻辑由栏目式内联 JS 处理
        }

        // ===== 卡片式默认逻辑 =====

        // 表单
        dom.addForm.addEventListener("submit", (e) => {
            e.preventDefault();
            const u = dom.usernameInput.value.trim();
            const p = dom.passwordInput.value.trim();
            if (!u || !p) { toast("用户名和密码不能为空", "warn"); return; }
            addAccount(u, p);
            dom.usernameInput.value = "";
            dom.passwordInput.value = "";
            dom.usernameInput.focus();
        });

        // 账号删除（事件委托）
        dom.accountList.addEventListener("click", (e) => {
            const btn = e.target.closest(".card-delete");
            if (btn) removeAccount(btn.dataset.user);
        });

        // 签到按钮
        dom.checkinBtn.addEventListener("click", startCheckin);

        // 状态刷新
        dom.refreshStatusBtn.addEventListener("click", refreshStatus);

        // 日志清屏
        dom.clearLogBtn.addEventListener("click", () => {
            dom.logContent.textContent = "（已清屏，等待新日志…）";
            dom.logContent.classList.add("empty");
            lastLogSig = "";
            // 同时清空后端日志，刷新页面后也不会恢复
            api("/api/logs", { method: "POST" }).catch(() => {});
        });

        // 并行加载初始数据（async-parallel）
        Promise.all([
            loadAccounts(),
            loadSummary(),
        ]).catch(() => {});

        // 检查是否有正在进行的任务
        api("/api/checkin/status").then((st) => {
            if (st.running) {
                dom.progressWrap.hidden = false;
                dom.actionHint.textContent = "正在自动登录并签到，请稍候…";
                startPolling();
            }
        }).catch(() => {});

        // 启动空闲日志轮询
        scheduleIdleLogPoll();

        // 可见性变化时：恢复签到轮询并立即拉取一次
        // 同时暂停/恢复 CSS 动画，节省 CPU
        document.addEventListener("visibilitychange", () => {
            if (document.hidden) {
                document.body.classList.add("hidden-animations");
            } else {
                document.body.classList.remove("hidden-animations");
                if (statusPollTimer) pollCheckinStatus();
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
