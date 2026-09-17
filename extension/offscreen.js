// Hidden document that hosts the Google AI Mode page in an iframe (no tab, no window, no focus).
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.target !== "offscreen") return;
  if (msg.cmd === "navigate") document.getElementById("page").src = msg.url;
});
