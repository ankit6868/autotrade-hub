/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#eef8ff',
          100: '#d9eeff',
          200: '#bce2ff',
          300: '#8ed0ff',
          400: '#59b4ff',
          500: '#3391ff',
          600: '#1b6ff5',
          700: '#145ae1',
          800: '#1749b6',
          900: '#19408f',
        },
        profit: '#22c55e',
        loss: '#ef4444',
      },
    },
  },
  plugins: [],
};
