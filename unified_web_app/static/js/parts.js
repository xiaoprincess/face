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
})();
