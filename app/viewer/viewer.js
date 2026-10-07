"use strict";
(function () {
  const body = document.body;
  const token = body.dataset.token;
  const frame = document.getElementById("artifact");
  const more = document.querySelector(".more");
  const menu = document.getElementById("menu");
  const toggle = document.getElementById("menu-toggle");
  const notice = document.getElementById("notice");
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const source = "/" + token + "/content" + (dark ? "?theme=dark" : "");
  let loads = 0;

  function say(message) {
    notice.textContent = message;
    notice.hidden = false;
    clearTimeout(say.timer);
    say.timer = setTimeout(function () { notice.hidden = true; }, 4000);
  }

  frame.addEventListener("load", function () {
    loads += 1;
    if (loads > 1) {
      loads = 0;
      frame.src = source;
      say("The artifact tried to navigate away. It was reloaded.");
    }
  });
  frame.src = source;

  const expires = new Date(Number(body.dataset.expiresAt) * 1000);
  const expiry = document.getElementById("expiry");
  expiry.textContent = expires.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  expiry.dateTime = expires.toISOString();

  function setOpen(open, restoreFocus) {
    if (open === !menu.hidden) return;
    menu.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) menu.querySelector("a").focus();
    else if (restoreFocus) toggle.focus();
  }

  toggle.addEventListener("click", function () { setOpen(menu.hidden, false); });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !menu.hidden) setOpen(false, true);
  });
  document.addEventListener("pointerdown", function (event) {
    if (!more.contains(event.target)) setOpen(false, false);
  });
  // Pointer events inside the artifact frame never reach this document; focus moving into it, or out
  // of the page, does. A null relatedTarget alone is not enough: Safari clears it on button clicks.
  function focusLeft(event) {
    if (event.relatedTarget) {
      if (!more.contains(event.relatedTarget)) setOpen(false, false);
      return;
    }
    setTimeout(function () {
      if (!document.hasFocus() || document.activeElement === frame) setOpen(false, false);
    }, 0);
  }
  more.addEventListener("focusout", focusLeft);
  window.addEventListener("blur", focusLeft);
})();
