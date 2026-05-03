/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      screens: {
        // Extra-small phones (e.g. iPhone SE)
        xs: '420px',
      },
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
        ink: {
          50: '#f5f7fb',
          100: '#e8edf6',
          200: '#c8d2e6',
          300: '#94a3b8',
          400: '#64748b',
          500: '#475569',
          600: '#243153',
          700: '#1a2440',
          800: '#131c30',
          900: '#0d1424',
          950: '#060913',
        },
      },
      borderRadius: {
        '2xl': '1rem',
        '3xl': '1.5rem',
      },
      boxShadow: {
        glow: '0 0 0 1px rgba(51,145,255,0.4), 0 8px 32px -8px rgba(51,145,255,0.35)',
        card: '0 4px 24px -8px rgba(0,0,0,0.4)',
        elevated: '0 12px 32px -8px rgba(0,0,0,0.6)',
      },
      animation: {
        'fade-in': 'fade-in 0.3s ease-out',
        'shimmer': 'shimmer 1.6s infinite',
      },
      keyframes: {
        'fade-in': {
          '0%': { opacity: 0, transform: 'translateY(4px)' },
          '100%': { opacity: 1, transform: 'translateY(0)' },
        },
        shimmer: {
          '0%': { transform: 'translateX(-100%)' },
          '100%': { transform: 'translateX(100%)' },
        },
      },
      // Useful for honouring iOS safe-area insets directly on utility classes
      spacing: {
        'safe-top': 'env(safe-area-inset-top)',
        'safe-bottom': 'env(safe-area-inset-bottom)',
      },
    },
  },
  plugins: [],
};
