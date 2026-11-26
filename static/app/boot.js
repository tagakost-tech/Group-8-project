/* Runs before the page paints: the saved readability choices (text size,
   higher contrast, easier reading, less motion), so the page never flashes
   at the wrong size. Storage can be blocked, so every read is guarded.
   For screenshots the same choices can be set in the address, for example
   #today&text=150&contrast=more&easy=on&motion=reduce. */
(() => {
  "use strict";
  const root = document.documentElement;
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem("omega-read") || "{}") || {};
  } catch {
    saved = {};
  }
  const pairs = location.hash.slice(1).split("&").filter((p) => p.includes("="));
  const hash = new URLSearchParams(pairs.join("&"));
  const size = Number(hash.get("text") || saved.size || 100);
  if (size >= 90 && size <= 150 && size !== 100) root.style.fontSize = `${size}%`;
  if ((hash.get("contrast") || saved.contrast) === "more") root.dataset.contrast = "more";
  if ((hash.get("easy") || saved.easy) === "on") root.dataset.easy = "on";
  if ((hash.get("motion") || saved.motion) === "reduce") root.dataset.motion = "reduce";
})();
