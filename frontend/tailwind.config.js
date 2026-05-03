/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      screens: {
        // Full coverage from tiny phones to 4K
        xs: '420px',     // small phones (iPhone SE, Galaxy S8)
        sm: '640px',     // large phones / small tablets
        md: '768px',     // tablets portrait
        lg: '1024px',    // tablets landscape / small laptops
        xl: '1280px',    // laptops / desktops
        '2xl': '1536px', // large desktops
        '3xl': '1920px', // FHD monitors
        '4xl': '2560px', // 4K / wide
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
          950: '#0a2cb8',
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
          700: '#161e34',
          800: '#101626',
          900: '#0a0e1c',
          950: '#05070f',
        },
      },
      borderRadius: {
        '2xl': '1rem',
        '3xl': '1.5rem',
        '4xl': '2rem',
      },
      backdropBlur: {
        xs: '4px',
      },
      boxShadow: {
        glow: '0 0 0 1px rgba(27,111,245,0.4), 0 8px 32px -8px rgba(27,111,245,0.5)',
        'glow-lg': '0 0 0 1px rgba(27,111,245,0.4), 0 24px 60px -16px rgba(27,111,245,0.55)',
        card: '0 4px 24px -8px rgba(0,0,0,0.5)',
        elevated: '0 16px 40px -12px rgba(0,0,0,0.7)',
        'inset-soft': 'inset 0 1px 0 rgba(255,255,255,0.08)',
      },
      backgroundImage: {
        'gradient-brand': 'linear-gradient(135deg, #1b6ff5 0%, #145ae1 100%)',
        'gradient-hero': 'linear-gradient(135deg, #1b6ff5 0%, #1747e8 50%, #0a2cb8 100%)',
        'gradient-mesh':
          'radial-gradient(1200px 600px at 10% -10%, rgba(27,111,245,0.18), transparent 60%), radial-gradient(900px 500px at 110% 10%, rgba(124,58,237,0.14), transparent 60%), radial-gradient(800px 600px at 50% 120%, rgba(14,165,233,0.10), transparent 60%)',
      },
      animation: {
        'fade-in': 'fade-in 0.3s ease-out',
        shimmer: 'shimmer 1.6s infinite',
        'float-glow': 'float-glow 6s ease-in-out infinite',
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
        'float-glow': {
          '0%, 100%': { transform: 'translate(0,0)', opacity: 0.7 },
          '50%': { transform: 'translate(8px,-6px)', opacity: 1 },
        },
      },
      spacing: {
        'safe-top': 'env(safe-area-inset-top)',
        'safe-bottom': 'env(safe-area-inset-bottom)',
      },
    },
  },
  plugins: [],
};
