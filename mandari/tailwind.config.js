/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./insight_core/templates/**/*.html",
    "./insight_content/templates/**/*.html",
    "./static/js/**/*.js",
    "./frontend/**/*.ts",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#eef2ff',
          100: '#e0e7ff',
          200: '#c7d2fe',
          300: '#a5b4fc',
          400: '#818cf8',
          500: '#6366f1',
          600: '#4f46e5',
          700: '#4338ca',
          800: '#3730a3',
          900: '#312e81',
          950: '#1e1b4b',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'Helvetica Neue', 'Arial', 'sans-serif'],
      },
    },
  },
  safelist: [
    // Dynamic label colors used in task cards and panel
    {
      pattern: /bg-(red|orange|amber|green|teal|blue|indigo|purple|pink|gray)-(100|500|900)/,
      variants: ['dark'],
    },
    {
      pattern: /text-(red|orange|amber|green|teal|blue|indigo|purple|pink|gray)-(300|700)/,
      variants: ['dark'],
    },
    {
      pattern: /ring-(red|orange|amber|green|teal|blue|indigo|purple|pink|gray)-(200|700)/,
      variants: ['dark'],
    },
  ],
  plugins: [],
}
