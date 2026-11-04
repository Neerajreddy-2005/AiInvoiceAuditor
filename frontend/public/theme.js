// Runs before first paint so a saved theme never flashes the wrong colours.
try {
  var saved = localStorage.getItem("theme");
  var dark = saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
} catch (e) {
  document.documentElement.dataset.theme = "light";
}
