(function () {
    "use strict";
    var allowed = ["blue", "cream", "night"];
    var theme = "blue";
    try {
        var stored = localStorage.getItem("parts-appearance");
        if (allowed.includes(stored)) theme = stored;
    } catch (_) { /* Storage may be unavailable in private/restricted browsers. */ }
    document.documentElement.dataset.theme = theme;
    document.addEventListener("DOMContentLoaded", function () {
        var select = document.getElementById("appearance");
        if (!select) return;
        select.value = theme;
        select.addEventListener("change", function () {
            if (!allowed.includes(select.value)) return;
            document.documentElement.dataset.theme = select.value;
            try { localStorage.setItem("parts-appearance", select.value); } catch (_) {}
        });
    });
})();
