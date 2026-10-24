// Fusion-engine splash: fades out once the document is ready and Unicorn
// has had a tick to hydrate. The .gone class triggers the CSS transition;
// we remove the node after the animation completes so it stops repainting.
(function () {
  function dismiss() {
    var el = document.getElementById("splash");
    if (!el || el.classList.contains("gone")) return;
    el.classList.add("gone");
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 700);
  }
  function schedule() {
    if (document.readyState === "complete") dismiss();
    else window.addEventListener("load", dismiss);
    // Failsafe: never let the splash outlive a slow asset load.
    setTimeout(dismiss, 3500);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", schedule);
  else schedule();
})();