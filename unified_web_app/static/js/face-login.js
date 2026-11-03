(() => {
    "use strict";

    const root = document.querySelector("[data-face-login]");
    if (!root) return;

    const preview = root.querySelector("[data-face-preview]");
    const video = root.querySelector("[data-face-video]");
    const photo = root.querySelector("[data-face-photo]");
    const placeholder = root.querySelector("[data-face-placeholder]");
    const canvas = root.querySelector("[data-face-canvas]");
    const openButton = root.querySelector("[data-face-open]");
    const snapshotButton = root.querySelector("[data-face-snapshot]");
    const retryButton = root.querySelector("[data-face-retry]");
    const status = root.querySelector("[data-face-status]");
    let stream = null;

    const setStatus = (message, isError = false) => {
        status.textContent = message;
        status.classList.toggle("is-error", isError);
    };

    const stopCamera = () => {
        if (stream) stream.getTracks().forEach((track) => track.stop());
        stream = null;
        video.srcObject = null;
        video.hidden = true;
        snapshotButton.disabled = true;
    };

    const openCamera = async () => {
        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
            setStatus("浏览器无法使用摄像头。请通过受信任的 HTTPS 地址打开网站。", true);
            return;
        }
        stopCamera();
        preview.hidden = false;
        photo.hidden = true;
        placeholder.hidden = true;
        openButton.disabled = true;
        retryButton.hidden = true;
        snapshotButton.hidden = false;
        setStatus("正在申请当前电脑的摄像头权限……");
        try {
            stream = await navigator.mediaDevices.getUserMedia({
                video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 960 } },
                audio: false,
            });
            video.srcObject = stream;
            video.hidden = false;
            await video.play();
            snapshotButton.disabled = false;
            setStatus("请保持正脸居中，然后点击“拍照并登录”。");
        } catch (error) {
            placeholder.hidden = false;
            const denied = error && (error.name === "NotAllowedError" || error.name === "SecurityError");
            setStatus(denied
                ? "摄像头权限被拒绝，请在浏览器地址栏中允许此网站使用摄像头。"
                : "无法打开摄像头，请检查是否被其他程序占用。", true);
        } finally {
            openButton.disabled = false;
        }
    };

    const captureAndLogin = async () => {
        if (!stream || !video.videoWidth || !video.videoHeight) {
            setStatus("摄像头画面尚未准备好，请稍后再试。", true);
            return;
        }
        const scale = Math.min(1, 1280 / video.videoWidth);
        canvas.width = Math.round(video.videoWidth * scale);
        canvas.height = Math.round(video.videoHeight * scale);
        canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
        const image = canvas.toDataURL("image/jpeg", 0.9);
        photo.src = image;
        photo.hidden = false;
        stopCamera();
        snapshotButton.hidden = true;
        openButton.hidden = true;
        retryButton.hidden = false;
        retryButton.disabled = true;
        setStatus("正在识别人脸并登录……");
        try {
            const response = await fetch(root.dataset.endpoint, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content,
                },
                body: JSON.stringify({ image }),
            });
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.message || "人脸登录失败");
            setStatus(`${result.message}，正在进入工作台……`);
            window.location.assign(result.redirect || "/dashboard");
        } catch (error) {
            setStatus(error.message || "人脸登录失败，请重新拍摄。", true);
            retryButton.disabled = false;
        }
    };

    openButton.addEventListener("click", openCamera);
    snapshotButton.addEventListener("click", captureAndLogin);
    retryButton.addEventListener("click", () => {
        openButton.hidden = false;
        retryButton.hidden = true;
        openCamera();
    });
    window.addEventListener("pagehide", stopCamera);
})();
