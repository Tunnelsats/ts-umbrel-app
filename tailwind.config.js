/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./web/index.html",
    "./web/js/app.js"
  ],
  theme: {
    extend: {
      colors: {
        tsgreen: '#22c55e',
        tsyellow: '#facc15',
        tsgray: '#0d1117',
      },
      fontFamily: {
        sans: ['Inter', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
