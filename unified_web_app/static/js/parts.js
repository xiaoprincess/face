(function () {
    "use strict";

    function bindConfirmation(form) {
        form.addEventListener("submit", function (event) {
            var message = form.dataset.confirm;
            if (message && !window.confirm(message)) {
                event.preventDefault();
            }
        });
    }

    function itemRow() {
        var existing = document.querySelector("[data-item-list] .item-row");
        if (existing && existing.querySelector("[data-borrow-component]")) {
            var copy = existing.cloneNode(true);
            copy.querySelector("select").value = "";
            copy.querySelector('[name="equipment_names[]"]').value = "";
            copy.querySelector('[name="quantities[]"]').value = "1";
            return copy;
        }
        var row = document.createElement("div");
        row.className = "item-row";
        row.innerHTML =
            '<label>器材名称<input name="equipment_names[]" maxlength="200" required></label>' +
            '<label>数量<input name="quantities[]" value="1" maxlength="30" required></label>' +
            '<button class="button small danger" type="button" data-remove-item>移除</button>';
        return row;
    }

    function componentRow() {
        var row = document.createElement("div");
        row.className = "component-row";
        row.innerHTML =
            '<input type="hidden" name="component_ids[]" value="">' +
            '<input type="hidden" name="component_original_quantities[]" value="">' +
            '<label>名称<input name="component_names[]" maxlength="200"></label>' +
            '<label>数量<input name="component_quantities[]" type="number" min="0" value="1"></label>' +
            '<label>单位<input name="component_units[]" maxlength="30" value="件"></label>' +
            '<label>箱号/备注<input name="component_notes[]" maxlength="500"></label>' +
            '<button class="button small danger" type="button" data-remove-component>移除</button>';
        return row;
    }

    document.addEventListener("click", function (event) {
        var addItem = event.target.closest("[data-add-item]");
        if (addItem) {
            var itemList = addItem.closest("form").querySelector("[data-item-list]");
            if (itemList && itemList.children.length < 20) itemList.appendChild(itemRow());
            return;
        }

        var removeItem = event.target.closest("[data-remove-item]");
        if (removeItem) {
            var row = removeItem.closest(".item-row");
            var list = row && row.parentElement;
            if (list && list.children.length > 1) row.remove();
            return;
        }

        var addComponent = event.target.closest("[data-add-component]");
        if (addComponent) {
            var componentList = addComponent.closest("form").querySelector("[data-component-list]");
            if (componentList) componentList.appendChild(componentRow());
            return;
        }

        var removeComponent = event.target.closest("[data-remove-component]");
        if (removeComponent) {
            var component = removeComponent.closest(".component-row");
            if (component) component.remove();
        }
    });

    document.querySelectorAll("form[data-confirm]").forEach(bindConfirmation);

    document.addEventListener("change", function (event) {
        if (!event.target.matches("[data-borrow-component]")) return;
        var option = event.target.options[event.target.selectedIndex];
        if (option.dataset.name) {
            event.target.closest(".item-row").querySelector('[name="equipment_names[]"]').value = option.dataset.name;
        }
    });

    var guidanceLabels = {queued: "排队中", running: "定位中", aligned: "已对准", lost: "目标丢失",
        timeout: "已超时", cancelled: "已取消", hardware_error: "硬件异常", invalid_tag: "硬件异常",
        skipped: "未执行", not_requested: "未执行", interrupted: "已中断"};
    document.querySelectorAll("[data-guidance-status]").forEach(function (element) {
        function show(status, message) {
            element.dataset.guidanceStatus = status;
            element.textContent = "指引：" + (guidanceLabels[status] || "未执行") + (message ? " · " + message : "");
        }
        show(element.dataset.guidanceStatus, element.dataset.guidanceMessage);
        var attempts = 0;
        function refresh() {
            if (!["queued", "running"].includes(element.dataset.guidanceStatus) || attempts++ >= 120) return;
            fetch(element.dataset.statusUrl, {headers: {Accept: "application/json"}})
                .then(function (response) { if (!response.ok) throw new Error("status"); return response.json(); })
                .then(function (data) { show(data.guidance_status, data.guidance_message); window.setTimeout(refresh, 2000); })
                .catch(function () { window.setTimeout(refresh, 5000); });
        }
        refresh();
    });

    var inventorySearch = document.querySelector("[data-inventory-search]");
    if (inventorySearch) {
        var inventoryCards = Array.from(document.querySelectorAll("[data-cabinet-card]"));
        var inventoryEmpty = document.querySelector("[data-inventory-empty]");
        var lowStock = document.querySelector("[data-low-stock]");
        var resultCount = document.querySelector("[data-inventory-result]");
        function filterInventory() {
            var query = inventorySearch.value.trim().toLocaleLowerCase();
            var visible = 0;
            inventoryCards.forEach(function (card) {
                var matches = !query || (card.dataset.search || "").toLocaleLowerCase().includes(query);
                var rows = Array.from(card.querySelectorAll("[data-stock-row]"));
                rows.forEach(function (row) {
                    var rowMatches = matches || row.textContent.toLocaleLowerCase().includes(query);
                    row.hidden = !rowMatches || Boolean(lowStock && lowStock.checked && row.dataset.low !== "true");
                });
                matches = rows.some(function (row) { return !row.hidden; }) ||
                    (rows.length === 0 && matches && (!lowStock || !lowStock.checked));
                card.hidden = !matches;
                if (matches) visible += 1;
            });
            if (inventoryEmpty) inventoryEmpty.hidden = visible !== 0 || inventoryCards.length === 0;
            if (resultCount) resultCount.textContent = "显示 " + visible + " / " + inventoryCards.length + " 个柜子";
        }
        inventorySearch.addEventListener("input", filterInventory);
        if (lowStock) lowStock.addEventListener("change", filterInventory);
        filterInventory();
    }
})();
