// Tiny DOM helper for the throwaway setup prototype. No deps.

/**
 * el('div', {class:'x', onClick:fn}, child, child2)
 * - class / html / dataset get special handling
 * - on<Event> registers a listener
 * - everything else becomes a property if it exists, else an attribute
 */
export function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k in node && k !== "list" && k !== "type") {
      try {
        node[k] = v;
      } catch {
        node.setAttribute(k, v);
      }
    } else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/** Copy text and briefly flip a button's label to confirm. */
export function copyBtn(label, getText) {
  const b = el("button", {
    class: "copybtn",
    type: "button",
    onClick: async () => {
      try {
        await navigator.clipboard.writeText(getText());
      } catch {
        /* clipboard blocked in some sandboxes — prototype, ignore */
      }
      const prev = b.textContent;
      b.textContent = "copied ✓";
      b.classList.add("is-copied");
      setTimeout(() => {
        b.textContent = prev;
        b.classList.remove("is-copied");
      }, 1100);
    },
  }, label);
  return b;
}
