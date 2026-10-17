/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "rgb(var(--bg) / <alpha-value>)",
        s1: "rgb(var(--s1) / <alpha-value>)",
        s2: "rgb(var(--s2) / <alpha-value>)",
        s3: "rgb(var(--s3) / <alpha-value>)",
        line: "rgb(var(--line) / <alpha-value>)",
        fg: "rgb(var(--fg) / <alpha-value>)",
        mute: "rgb(var(--mute) / <alpha-value>)",
        dim: "rgb(var(--dim) / <alpha-value>)",
        accent: "rgb(var(--accent) / <alpha-value>)",
        ok: "rgb(var(--ok) / <alpha-value>)",
        warn: "rgb(var(--warn) / <alpha-value>)",
        bad: "rgb(var(--bad) / <alpha-value>)",
        info: "rgb(var(--info) / <alpha-value>)",
      },
      fontFamily: {
        sans: ['"Inter var"', "Inter", "-apple-system", "BlinkMacSystemFont", '"Segoe UI"', "Roboto", "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      boxShadow: { pop: "0 12px 40px -8px rgb(0 0 0 / .6), 0 0 0 1px rgb(var(--line))" },
      keyframes: {
        shimmer: { "100%": { transform: "translateX(100%)" } },
        rise: { from: { opacity: 0, transform: "translateY(6px)" }, to: { opacity: 1, transform: "none" } },
        fade: { from: { opacity: 0 }, to: { opacity: 1 } },
        slide: { from: { transform: "translateX(24px)", opacity: 0 }, to: { transform: "none", opacity: 1 } },
      },
      animation: { rise: "rise .22s ease-out", fade: "fade .15s ease-out", slide: "slide .2s ease-out" },
    },
  },
  plugins: [],
};
