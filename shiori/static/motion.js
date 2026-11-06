"use strict";

// Presentation-only motion: the sliding sidebar highlight, entrance animation
// only when the page actually changes, and refresh feedback. Nothing here
// touches application state; if this file fails, the CSS fallbacks still work.
(() => {
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)");

  function setupNavPill() {
    const nav = document.getElementById("navigation");
    if (!nav || typeof MutationObserver !== "function") return;
    const pill = document.createElement("span");
    pill.className = "nav-pill"; pill.setAttribute("aria-hidden","true");
    let last = null, frame = 0;

    function place() {
      frame = 0;
      const active = nav.querySelector(".nav-item.active");
      if (pill.parentNode !== nav) nav.prepend(pill);
      if (!active || !active.offsetParent) { pill.style.opacity = "0"; if (nav.classList.contains("has-pill")) nav.classList.remove("has-pill"); last = null; return; }
      const box = nav.getBoundingClientRect(), item = active.getBoundingClientRect();
      const next = {top:item.top - box.top + nav.scrollTop,left:item.left - box.left,width:item.width,height:item.height};
      const from = last || next;
      if (!last || reduced?.matches) pill.style.transition = "none";
      pill.style.left = `${next.left}px`; pill.style.width = `${next.width}px`;
      pill.style.height = `${from.height}px`; pill.style.transform = `translateY(${from.top}px)`;
      pill.style.opacity = "1";
      void pill.offsetWidth;
      pill.style.transition = "";
      pill.style.height = `${next.height}px`; pill.style.transform = `translateY(${next.top}px)`;
      if (!nav.classList.contains("has-pill")) nav.classList.add("has-pill");
      last = next;
    }
    const schedule = () => { if (!frame) frame = requestAnimationFrame(place); };
    new MutationObserver(records => { if (records.some(record => record.target !== pill)) schedule(); })
      .observe(nav,{childList:true,subtree:true,attributes:true,attributeFilter:["class","hidden","open"]});
    nav.addEventListener("transitionend",event => { if (event.target !== pill) schedule(); });
    window.addEventListener("resize",schedule);
    schedule();
  }

  // Re-rendering the same page after a save should not replay the entrance.
  function setupPageMotion() {
    const content = document.getElementById("content"), crumb = document.getElementById("page-breadcrumb");
    if (!content || typeof MutationObserver !== "function") return;
    let shown = "";
    new MutationObserver(() => {
      const view = content.firstElementChild;
      if (!view?.classList.contains("animate-in")) return;
      const key = `${crumb?.textContent || ""}|${document.getElementById("global-scope")?.value || ""}`;
      document.body.classList.toggle("no-page-motion",key === shown);
      shown = key;
    }).observe(content,{childList:true});
  }

  function setupRefreshFeedback() {
    document.addEventListener("click",event => {
      const trigger = event.target.closest?.("#refresh-button");
      if (!trigger) return;
      trigger.classList.add("spinning");
      const content = document.getElementById("content");
      const done = () => { trigger.classList.remove("spinning"); observer.disconnect(); clearTimeout(timer); };
      const observer = new MutationObserver(() => { if (content.querySelector(":scope>.animate-in")) setTimeout(done,250); });
      const timer = setTimeout(done,4000);
      if (content) observer.observe(content,{childList:true});
    });
  }

  function start() { setupNavPill(); setupPageMotion(); setupRefreshFeedback(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded",start); else start();
})();
