(() => {
    "use strict";

    const socket = typeof window.io === "function" ? window.io() : null;

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
        const exists = Array.from(list.querySelectorAll(".message[data-message-id]"))
            .some((item) => item.dataset.messageId === String(message.id));
        if (exists) return;
        list.appendChild(buildMessage(message, currentUserId));
        updateEmptyState(list);
        list.scrollTop = list.scrollHeight;
    }

    async function jsonRequest(url, options = {}) {
        const headers = { Accept: "application/json", ...(options.headers || {}) };
        if (options.body) headers["Content-Type"] = "application/json";
        const response = await fetch(url, { ...options, headers });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "请求失败");
        return data;
    }

    function bindComposer(form, input) {
        if (!form || !input) return;
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
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
        // Also handles the read endpoint: /api/messages/0/read.
        const endpoint = (template, userId) => template.replace(/\/0(?=\/|$)/, `/${userId}`);

        setConnectionState(privateRoot, Boolean(socket?.connected), socket ? "连接中" : "离线发送");
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

        if (selectedUserId) {
            jsonRequest(endpoint(privateRoot.dataset.messagesUrl, selectedUserId))
                .then((messages) => {
                    list.querySelectorAll(".message[data-message-id]").forEach((item) => item.remove());
                    messages.forEach((message) => appendOnce(list, message, currentUserId));
                    updateEmptyState(list);
                    list.scrollTop = list.scrollHeight;
                    return markRead();
                })
                .catch(console.error);
        } else {
            updateEmptyState(list);
        }

        refreshContactBadges().catch(console.error);

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const content = input.value.trim();
            if (!selectedUserId || !content) return;
            input.disabled = true;
            try {
                if (socket?.connected) {
                    await new Promise((resolve, reject) => {
                        socket.emit("send_private_message", { receiver_id: selectedUserId, content }, (result) => {
                            if (result?.ok) {
                                if (result.message) appendOnce(list, result.message, currentUserId);
                                resolve();
                            } else reject(new Error(result?.error || "发送失败"));
                        });
                    });
                } else {
                    const message = await jsonRequest(endpoint(privateRoot.dataset.sendUrl, selectedUserId), {
                        method: "POST", body: JSON.stringify({ content }),
                    });
                    appendOnce(list, message, currentUserId);
                }
                input.value = "";
                input.style.height = "auto";
            } catch (error) {
                window.alert(error.message);
            } finally {
                input.disabled = false;
                input.focus();
            }
        });

        socket?.on("connect", () => setConnectionState(privateRoot, true, "实时连接"));
        socket?.on("disconnect", () => setConnectionState(privateRoot, false, "离线发送"));
        socket?.on("connect_error", () => setConnectionState(privateRoot, false, "离线发送"));
        socket?.on("new_message", (message) => {
            const belongsHere = selectedUserId && (
                (Number(message.sender_id) === currentUserId && Number(message.receiver_id) === selectedUserId) ||
                (Number(message.sender_id) === selectedUserId && Number(message.receiver_id) === currentUserId)
            );
            if (belongsHere) {
                appendOnce(list, message, currentUserId);
                if (Number(message.sender_id) === selectedUserId) markRead().catch(console.error);
            }
            refreshContactBadges().catch(console.error);
            window.dispatchEvent(new Event("unread-change"));
        });
    }

})();
