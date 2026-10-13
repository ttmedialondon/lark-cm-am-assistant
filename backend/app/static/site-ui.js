"use strict";

(() => {
  const navigating = () => { document.documentElement.dataset.uiNavigating = "true"; };
  for (const control of document.querySelectorAll("[data-ui-auto-submit]")) {
    const form = control.form;
    if (!form || typeof form.requestSubmit !== "function") continue;
    const button = form.querySelector('button[type="submit"]');
    if (button) button.hidden = true;
    control.addEventListener("change", () => form.requestSubmit(button));
  }
  document.addEventListener("click", event => {
    const link = event.target.closest?.("a[href]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey
      || link.hasAttribute("download") || link.hasAttribute("data-ui-download")
      || link.target && link.target !== "_self" || link.getAttribute("aria-disabled") === "true") return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || !["http:", "https:"].includes(url.protocol)
      || url.pathname === location.pathname && url.search === location.search && url.hash) return;
    navigating();
  });
  document.addEventListener("submit", event => {
    const form = event.target;
    if (event.defaultPrevented || form.method === "dialog" || form.target && form.target !== "_self"
      || form.closest("[hx-get], [hx-post], [data-hx-get], [data-hx-post]") || form.hasAttribute("data-ui-download")) return;
    if (form.dataset.uiSubmitting) { event.preventDefault(); return; }
    form.dataset.uiSubmitting = "true";
    form.setAttribute("aria-busy", "true");
    // Keep submitter and input values enabled so native POST/GET serialization stays intact.
    if (event.submitter) {
      event.submitter.dataset.uiSubmitter = "true";
      event.submitter.setAttribute("aria-busy", "true");
      event.submitter.setAttribute("aria-disabled", "true");
    }
    navigating();
  });
  window.addEventListener("pageshow", () => {
    delete document.documentElement.dataset.uiNavigating;
    for (const form of document.querySelectorAll("form[data-ui-submitting]")) {
      delete form.dataset.uiSubmitting; form.removeAttribute("aria-busy");
    }
    for (const button of document.querySelectorAll("[data-ui-submitter]")) {
      delete button.dataset.uiSubmitter; button.removeAttribute("aria-busy"); button.removeAttribute("aria-disabled");
    }
  });
})();
