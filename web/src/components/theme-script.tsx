const themeScript = `
(() => {
  try {
    const stored = localStorage.getItem("chatgpt2api-theme");
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const theme = stored === "light" || stored === "dark" ? stored : prefersDark ? "dark" : "light";
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.style.colorScheme = theme;
    // 样式表加载完之前先把画布底色写进内联样式，硬刷新时不会闪一下白底。
    document.documentElement.style.backgroundColor = theme === "dark" ? "oklch(0.17 0.012 255)" : "oklch(0.962 0.006 252)";
  } catch {
    document.documentElement.classList.remove("dark");
    document.documentElement.style.colorScheme = "light";
    document.documentElement.style.backgroundColor = "oklch(0.962 0.006 252)";
  }
})();
`;

export function ThemeScript() {
  return <script dangerouslySetInnerHTML={{ __html: themeScript }} />;
}
