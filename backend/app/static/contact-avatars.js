"use strict";

for (const avatar of document.querySelectorAll("img[data-contact-avatar]")) {
  avatar.addEventListener("load", () => { avatar.style.visibility = "visible"; }, {once: true});
  avatar.addEventListener("error", () => avatar.remove(), {once: true});
  // Set src only after listeners exist; fast failures keep the initial visible.
  avatar.src = avatar.dataset.contactAvatar;
}
