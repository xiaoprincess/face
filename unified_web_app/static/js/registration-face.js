(() => {
    "use strict";

    const form = document.querySelector('.auth-card form[action=""]') || document.querySelector(".auth-card form");
    if (!form || !form.querySelector('input[name="register_face"]')) return;

    const checkbox = form.querySelector('input[name="register_face"]');
    const button = form.querySelector("[data-register-submit]");
    const status = form.querySelector("[data-registration-face-status]");
    const capture = form.querySelector("[data-face-capture]");
    const video = form.querySelector("[data-face-video]");
    const photo = form.querySelector("[data-face-photo]");
    const placeholder = form.querySelector("[data-face-placeholder]");
    const canvas = form.querySelector("[data-face-canvas]");
    const imageInput = form.querySelector("[data-face-image]");
    const openButton = form.querySelector("[data-face-open]");
    const snapshotButton = form.querySelector("[data-face-snapshot]");
    const retakeButton = form.querySelector("[data-face-retake]");
    let stream = null;
    let previewUrl = null;

    const stopCamera = () => {
        if (stream) stream.getTracks().forEach((track) => track.stop());
        stream = null;
        video.srcObject = null;
        video.hidden = true;
        snapshotButton.disabled = true;
    };

    const setStatus = (message, isError = false) => {
        status.textContent = message;
        status.classList.toggle("is-error", isError);
    };

    const openCamera = async () => {
        if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            setStatus("当前浏览器无法访问摄像头。请使用支持摄像头权限的现代浏览器，并通过 HTTPS 打开网站。", true);
            return;
        }
        stopCamera();
        imageInput.value = "";
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = null;
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
            setStatus("摄像头已打开，请让面部完整出现在画面中央，然后点击“拍摄照片”。");
        } catch (error) {
            placeholder.hidden = false;
            const denied = error && (error.name === "NotAllowedError" || error.name === "SecurityError");
            setStatus(denied
                ? "未获得摄像头权限，请在浏览器地址栏中允许此网站使用摄像头。"
                : "无法打开当前电脑的摄像头，请检查摄像头是否被其他程序占用。", true);
        } finally {
            openButton.disabled = false;
        }
    };

    const takeSnapshot = async () => {
        if (!stream || !video.videoWidth || !video.videoHeight) {
            setStatus("摄像头画面尚未准备好，请稍后再试。", true);
            return;
        }
        const maxWidth = 1280;
        const scale = Math.min(1, maxWidth / video.videoWidth);
        canvas.width = Math.round(video.videoWidth * scale);
        canvas.height = Math.round(video.videoHeight * scale);
        canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
        snapshotButton.disabled = true;
        const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
        if (!blob) {
            snapshotButton.disabled = false;
            setStatus("照片生成失败，请重新拍摄。", true);
            return;
        }
        const file = new File([blob], "computer-camera-face.jpg", { type: "image/jpeg" });
        const transfer = new DataTransfer();
        transfer.items.add(file);
        imageInput.files = transfer.files;
        previewUrl = URL.createObjectURL(file);
        photo.src = previewUrl;
        photo.hidden = false;
        placeholder.hidden = true;
        stopCamera();
        openButton.hidden = true;
        snapshotButton.hidden = true;
        retakeButton.hidden = false;
        setStatus("照片已准备好。确认清晰且只有本人后，可以创建账号。", false);
    };

    const refresh = () => {
        capture.hidden = !checkbox.checked;
        button.textContent = checkbox.checked ? "创建账号并登记人脸" : "创建账号";
        if (!checkbox.checked) {
            stopCamera();
            setStatus("本次只创建账号，不登记人脸。");
        } else if (imageInput.files.length) {
            setStatus("照片已准备好，可以创建账号。");
        } else {
            setStatus("请先允许浏览器访问当前电脑的摄像头，然后拍摄一张正脸照片。");
        }
    };

    checkbox.addEventListener("change", refresh);
    openButton.addEventListener("click", openCamera);
    snapshotButton.addEventListener("click", takeSnapshot);
    retakeButton.addEventListener("click", () => {
        imageInput.value = "";
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = null;
        photo.hidden = true;
        openButton.hidden = false;
        snapshotButton.hidden = false;
        retakeButton.hidden = true;
        openCamera();
    });
    form.addEventListener("submit", (event) => {
        if (checkbox.checked && !imageInput.files.length) {
            event.preventDefault();
            setStatus("请先打开当前电脑的摄像头并拍摄照片，再创建账号。", true);
            openCamera();
            return;
        }
        stopCamera();
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        button.textContent = checkbox.checked ? "正在登记人脸…" : "正在创建账号…";
        setStatus(checkbox.checked
            ? "正在将已拍摄照片提交给百度人脸服务，完成前不要关闭页面。"
            : "正在保存账号信息。"
        );
    });
    window.addEventListener("pagehide", stopCamera);
    refresh();
})();
