(() => {
    "use strict";

    const REQUEST_TIMEOUT = 12000;
    const POLL_INTERVAL = 5000;
    const UNCERTAIN_SEND = "发送结果不确定，消息可能已送达。请先核对聊天记录，勿直接重复发送；系统不会自动重发。";
    let socket = null;
    try {
        socket = typeof window.io === "function" ? window.io() : null;
    } catch (error) {
        console.error(error);
    }

    function formatTime(value) {
        const raw = String(value || "");
        // The server stores UTC without a timezone suffix. Adding Z prevents
        // browsers from treating it as local time and showing the wrong time.
        const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw);
        const date = new Date(hasTimezone ? raw : `${raw}Z`);
        if (Number.isNaN(date.getTime())) return raw;
        return date.toLocaleString("zh-CN", {
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
        });
    }

    function updateEmptyState(list) {
        const empty = list?.querySelector("[data-message-empty]");
        if (!empty) return;
        empty.hidden = Boolean(list.querySelector(".message[data-message-id]"));
    }

    function buildMessage(message, currentUserId) {
        const mine = message.is_mine ?? Number(message.sender_id) === currentUserId;
        const item = document.createElement("div");
        item.className = `message${mine ? " mine" : ""}`;
        item.dataset.messageId = String(message.id);
        const content = document.createElement("div");
        content.className = "message-content";
        content.textContent = message.content || "";
        const meta = document.createElement("div");
        meta.className = "meta";
        const time = formatTime(message.timestamp);
        meta.textContent = !mine ? time : `${time} · ${message.is_read ? "已读" : "已发送"}`;
        item.append(content, meta);
        return item;
    }

    function appendOnce(list, message, currentUserId) {
        if (!list || !message?.id) return;
        const items = Array.from(list.querySelectorAll(".message[data-message-id]"));
        const exists = items.find((item) => item.dataset.messageId === String(message.id));
        if (exists) {
            // A stale initial response must not undo a later read receipt.
            if (Number(message.sender_id) === currentUserId && message.is_read) {
                exists.querySelector(".meta").textContent = `${formatTime(message.timestamp)} · 已读`;
            }
            return;
        }
        const followBottom = list.scrollHeight - list.scrollTop - list.clientHeight <= 48;
        const listTop = list.getBoundingClientRect().top;
        const anchor = followBottom ? null : items.find((item) => item.getBoundingClientRect().bottom > listTop);
        const anchorTop = anchor?.getBoundingClientRect().top;
        const previousTop = list.scrollTop;
        // Merge snapshots with live messages instead of clearing newer arrivals.
        const next = items.find((item) => Number(item.dataset.messageId) > Number(message.id));
        list.insertBefore(buildMessage(message, currentUserId), next || null);
        updateEmptyState(list);
        list.scrollTop = followBottom ? list.scrollHeight
            : previousTop + (anchor ? anchor.getBoundingClientRect().top - anchorTop : 0);
    }

    async function jsonRequest(url, options = {}) {
        const headers = { Accept: "application/json", ...(options.headers || {}) };
        headers["X-CSRF-Token"] = document.querySelector('meta[name="csrf-token"]')?.content || "";
        if (options.body) headers["Content-Type"] = "application/json";
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
        try {
            const response = await fetch(url, { ...options, headers, signal: controller.signal });
            const data = await response.json().catch(() => null);
            if (!response.ok) {
                const error = new Error(data?.message || data?.error || "请求失败");
                error.definiteFailure = response.status >= 400 && response.status < 500;
                throw error;
            }
            if (data === null) throw new Error("服务器返回无效响应");
            return data;
        } finally {
            window.clearTimeout(timer);
        }
    }

    function bindComposer(form, input) {
        if (!form || !input) return;
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
                event.preventDefault();
                if (input.value.trim()) form.requestSubmit();
            }
        });
        input.addEventListener("input", () => {
            input.style.height = "auto";
            input.style.height = `${Math.min(input.scrollHeight, 132)}px`;
        });
    }

    function setConnectionState(root, connected, label) {
        const state = root?.querySelector("[data-connection-state]");
        if (!state) return;
        state.classList.toggle("connected", connected);
        state.classList.toggle("offline", !connected);
        const text = state.querySelector("span");
        if (text) text.textContent = label || (connected ? "实时连接" : "离线发送");
    }

    const privateRoot = document.querySelector("[data-community-chat]");
    if (privateRoot) {
        const currentUserId = Number(privateRoot.dataset.currentUser);
        const selectedUserId = Number(privateRoot.dataset.selectedUser) || null;
        const list = document.getElementById("private-messages");
        const form = document.getElementById("private-message-form");
        const input = document.getElementById("private-message-input");
        const sendButton = form?.querySelector('button[type="submit"]');
        const sendLabel = sendButton?.querySelector("span") || sendButton;
        const idleLabel = sendLabel?.textContent;
        let sending = false;
        let refreshInFlight = null;
        let pollTimer = null;
        // Also handles the read endpoint: /api/messages/0/read.
        const endpoint = (template, userId) => template.replace(/\/0(?=\/|$)/, `/${userId}`);

        setConnectionState(privateRoot, Boolean(socket?.connected), socket?.connected ? "实时连接" : "HTTP 同步");
        bindComposer(form, input);

        const refreshContactBadges = () => jsonRequest(privateRoot.dataset.unreadUrl)
            .then((counts) => {
                privateRoot.querySelectorAll("[data-user-id]").forEach((contact) => {
                    const count = Number(counts[contact.dataset.userId] || 0);
                    const badge = contact.querySelector(".unread");
                    if (!badge) return;
                    badge.textContent = count > 99 ? "99+" : count;
                    badge.hidden = !count || Number(contact.dataset.userId) === selectedUserId;
                });
            });

        const markRead = () => {
            if (!selectedUserId) return Promise.resolve();
            return jsonRequest(endpoint(privateRoot.dataset.readUrl, selectedUserId), { method: "POST" })
                .then(() => {
                    const contact = privateRoot.querySelector(`[data-user-id="${selectedUserId}"]`);
                    const badge = contact?.querySelector(".unread");
                    if (badge) {
                        badge.textContent = "0";
                        badge.hidden = true;
                    }
                    window.dispatchEvent(new Event("unread-change"));
                });
        };

        const refreshMessages = () => {
            if (!selectedUserId || document.hidden) return Promise.resolve();
            if (refreshInFlight) return refreshInFlight;
            refreshInFlight = jsonRequest(endpoint(privateRoot.dataset.messagesUrl, selectedUserId))
                .then((messages) => {
                    if (!Array.isArray(messages)) throw new Error("消息列表格式无效");
                    messages.forEach((message) => appendOnce(list, message, currentUserId));
                    updateEmptyState(list);
                    // The GET endpoint already marks this conversation as read.
                    window.dispatchEvent(new Event("unread-change"));
                    return refreshContactBadges();
                })
                .finally(() => { refreshInFlight = null; });
            return refreshInFlight;
        };

        const schedulePoll = () => {
            window.clearTimeout(pollTimer);
            pollTimer = null;
            if (!selectedUserId || document.hidden || socket?.connected) return;
            pollTimer = window.setTimeout(syncMessages, POLL_INTERVAL);
        };

        async function syncMessages() {
            window.clearTimeout(pollTimer);
            pollTimer = null;
            try {
                await refreshMessages();
                if (!socket?.connected) setConnectionState(privateRoot, false, "HTTP 同步");
            } catch (error) {
                console.error(error);
                if (!socket?.connected) setConnectionState(privateRoot, false, "网络异常，等待同步");
            } finally {
                schedulePoll();
            }
        }

        updateEmptyState(list);
        if (selectedUserId) syncMessages();
        else refreshContactBadges().catch(console.error);

        document.addEventListener("visibilitychange", () => {
            if (document.hidden) {
                window.clearTimeout(pollTimer);
                pollTimer = null;
            } else {
                syncMessages();
            }
        });

        form?.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (sending) return;
            const content = input.value.trim();
            if (!selectedUserId || !content) return;
            sending = true;
            input.disabled = true;
            form.setAttribute("aria-busy", "true");
            if (sendButton) {
                sendButton.disabled = true;
                sendButton.setAttribute("aria-busy", "true");
            }
            if (sendLabel) sendLabel.textContent = "发送中…";
            try {
                if (socket?.connected) {
                    await new Promise((resolve, reject) => {
                        let settled = false;
                        const timer = window.setTimeout(() => {
                            settled = true;
                            reject(new Error(UNCERTAIN_SEND));
                        }, REQUEST_TIMEOUT);
                        try {
                            socket.emit("send_private_message", { receiver_id: selectedUserId, content }, (result) => {
                                // Late ACKs may fill in history, but must not clear a new draft.
                                if (result?.ok && result.message) appendOnce(list, result.message, currentUserId);
                                if (settled) return;
                                settled = true;
                                window.clearTimeout(timer);
                                if (result?.ok) resolve();
                                else {
                                    const error = new Error(result?.error || UNCERTAIN_SEND);
                                    error.definiteFailure = result?.ok === false;
                                    reject(error);
                                }
                            });
                        } catch (error) {
                            settled = true;
                            window.clearTimeout(timer);
                            reject(error);
                        }
                    });
                } else {
                    const message = await jsonRequest(endpoint(privateRoot.dataset.sendUrl, selectedUserId), {
                        method: "POST", body: JSON.stringify({ content }),
                    });
                    if (!message?.id) throw new Error(UNCERTAIN_SEND);
                    appendOnce(list, message, currentUserId);
                }
                input.value = "";
                input.style.height = "auto";
            } catch (error) {
                syncMessages();
                window.alert(error.definiteFailure ? error.message : UNCERTAIN_SEND);
            } finally {
                sending = false;
                input.disabled = false;
                form.removeAttribute("aria-busy");
                if (sendButton) {
                    sendButton.disabled = false;
                    sendButton.removeAttribute("aria-busy");
                }
                if (sendLabel) sendLabel.textContent = idleLabel;
                input.focus();
            }
        });

        socket?.on("connect", () => {
            setConnectionState(privateRoot, true, "实时连接");
            syncMessages();
        });
        const useHttp = () => {
            setConnectionState(privateRoot, false, "HTTP 同步");
            syncMessages();
        };
        socket?.on("disconnect", useHttp);
        socket?.on("connect_error", useHttp);
        socket?.on("new_message", (message) => {
            const belongsHere = selectedUserId && (
                (Number(message.sender_id) === currentUserId && Number(message.receiver_id) === selectedUserId) ||
                (Number(message.sender_id) === selectedUserId && Number(message.receiver_id) === currentUserId)
            );
            if (belongsHere) {
                appendOnce(list, message, currentUserId);
                if (!document.hidden && Number(message.sender_id) === selectedUserId) markRead().catch(console.error);
            }
            refreshContactBadges().catch(console.error);
            window.dispatchEvent(new Event("unread-change"));
        });
    }

})();
