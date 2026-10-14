// Applies the stored (or system) theme before first paint so nothing flashes.
// Loaded from <head> in the root layout, so it covers the marketing site and
// the signed-in app alike. Kept as a static file rather than an inline script
// so no component has to inject raw markup.
//
// It sets two things, because the app has two token systems: data-theme drives
// the marketing --w-* tokens, and the `dark` class drives the shadcn tokens the
// dashboard is built on (tailwind darkMode: ["class"]).
(function () {
  try {
    var stored = localStorage.getItem("em-theme");
    if (stored !== "light" && stored !== "dark") {
      stored = window.matchMedia("(prefers-color-scheme: light)").matches
        ? "light"
        : "dark";
    }
    var root = document.documentElement;
    root.setAttribute("data-theme", stored);
    root.classList.toggle("dark", stored === "dark");
  } catch (e) {
    document.documentElement.setAttribute("data-theme", "dark");
    document.documentElement.classList.add("dark");
  }
})();
