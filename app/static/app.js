const list = document.querySelector("#queue-list");
const orderInput = document.querySelector("#queue-order");
let dragged = null;

function syncOrder() {
  if (!list || !orderInput) return;
  orderInput.value = [...list.querySelectorAll("[data-id]")].map((el) => el.dataset.id).join(",");
}

if (list) {
  list.addEventListener("dragstart", (event) => {
    dragged = event.target.closest("[data-id]");
  });
  list.addEventListener("dragover", (event) => {
    event.preventDefault();
    const target = event.target.closest("[data-id]");
    if (!dragged || !target || dragged === target) return;
    const box = target.getBoundingClientRect();
    const after = event.clientY > box.top + box.height / 2;
    target.parentNode.insertBefore(dragged, after ? target.nextSibling : target);
    syncOrder();
  });
  list.addEventListener("drop", syncOrder);
  syncOrder();
}
