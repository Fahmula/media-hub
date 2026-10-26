// Offline page (served from the service worker cache): apply the theme cookie, since the cached
// copy was rendered without it. Reload on "Try again" or once the connection is back.
(() => {
  "use strict";

  if (/(?:^|;\s*)mh_theme=light(?:;|$)/.test(document.cookie)) {
    document.documentElement.dataset.theme = "mediahub-light";
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", "#f1f4f9");
  }
  const retry = () => window.location.reload();
  window.addEventListener("online", retry);
  document.addEventListener("click", (evt) => {
    if (evt.target instanceof Element && evt.target.closest("[data-retry]")) retry();
  });
})();
