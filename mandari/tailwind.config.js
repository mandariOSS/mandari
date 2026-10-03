/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./insight_core/templates/**/*.html",
    "./static/js/**/*.js",
    "./frontend/**/*.ts",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Kennfarbe als CSS-Variablen im Kanalformat (Issue #783): :root hält Indigo für Work und Session,
        // [data-portal="insight"] setzt Grün, Körperschaftsportale ihre eigene Skala (static/css/input.css,
        // insight_core/farbskala.py). <alpha-value> erhält Deckkraft-Klassen wie bg-primary-900/20.
        primary: Object.fromEntries(
          [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950].map((stufe) => [
            stufe,
            `rgb(var(--primary-${stufe}) / <alpha-value>)`,
          ]),
        ),
        // Flächen des Bürgerportals (Issue #783, Stufe 2): Bänder statt Karten, hell und dunkel aus input.css
        band: {
          hell: 'rgb(var(--band-hell) / <alpha-value>)',
          grau: 'rgb(var(--band-grau) / <alpha-value>)',
          tinte: 'rgb(var(--band-tinte) / <alpha-value>)',
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
