/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./src/**/*.{html,ts}"],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        surface: {
          900: '#0a0a0f',
          800: '#12121a',
          700: '#1a1a25',
          600: '#222230',
        },
        accent: '#f59e0b',
        profit: '#22c55e',
        loss: '#ef4444',
      },
    },
  },
  plugins: [],
};
