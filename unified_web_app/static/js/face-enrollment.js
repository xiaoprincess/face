(() => {
    "use strict";

    const root = document.querySelector("[data-face-enrollment]");
    if (!root) return;

    const video = root.querySelector("[data-face-video]");
    const photo = root.querySelector("[data-face-photo]");
    const placeholder = root.querySelector("[data-face-placeholder]");
    const canvas = root.querySelector("[data-face-canvas]");
    const openButton = root.querySelector("[data-face-open]");
    const snapshotButton = root.querySelector("[data-face-snapshot]");
    const retakeButton = root.querySelector("[data-face-retake]");
    const form = root.querySelector("[data-face-form]");
    const submitButton = root.querySelector("[data-face-submit]");
    const status = root.querySelector("[data-face-status]");
    const state = root.querySelector("[data-face-state]");
    let stream = null;
    let image = "";

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
        image = "";
        submitButton.disabled = true;
        photo.hidden = true;
        placeholder.hidden = true;
        openButton.disabled = true;
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
            retakeButton.hidden = true;
            setStatus("摄像头已打开，请保持正脸居中，然后拍摄照片。");
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

    const takeSnapshot = () => {
        if (!stream || !video.videoWidth || !video.videoHeight) {
            setStatus("摄像头画面尚未准备好，请稍后再试。", true);
            return;
        }
        const scale = Math.min(1, 1280 / video.videoWidth);
        canvas.width = Math.round(video.videoWidth * scale);
        canvas.height = Math.round(video.videoHeight * scale);
        canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
        image = canvas.toDataURL("image/jpeg", 0.9);
        photo.src = image;
        photo.hidden = false;
        placeholder.hidden = true;
        stopCamera();
        openButton.hidden = true;
        snapshotButton.hidden = true;
        retakeButton.hidden = false;
        submitButton.disabled = false;
        setStatus("照片已准备好。输入当前账号密码后保存。");
    };

    openButton.addEventListener("click", openCamera);
    snapshotButton.addEventListener("click", takeSnapshot);
    retakeButton.addEventListener("click", () => {
        image = "";
        photo.hidden = true;
        openButton.hidden = false;
        snapshotButton.hidden = false;
        retakeButton.hidden = true;
        submitButton.disabled = true;
        openCamera();
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!image) {
            setStatus("请先使用当前电脑的摄像头拍摄照片。", true);
            return;
        }
        submitButton.disabled = true;
        submitButton.setAttribute("aria-busy", "true");
        setStatus("正在保存人脸信息……");
        try {
            const response = await fetch(root.dataset.endpoint, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content,
                },
                body: JSON.stringify({ image, password: form.elements.password.value }),
            });
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.message || "保存失败");
            state.textContent = "已录入 · 可以重新拍摄更新";
            setStatus(result.message || "人脸信息保存成功");
            submitButton.textContent = "保存并更新人脸";
            form.elements.password.value = "";
        } catch (error) {
            setStatus(error.message || "人脸信息保存失败，请稍后重试。", true);
        } finally {
            submitButton.disabled = false;
            submitButton.removeAttribute("aria-busy");
        }
    });

    window.addEventListener("pagehide", stopCamera);
})();
