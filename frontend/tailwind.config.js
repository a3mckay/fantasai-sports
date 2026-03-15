/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Baseball-themed palette
        field: {
          950: '#040d07',
          900: '#071a0d',
          800: '#0d2b15',
          700: '#143d1e',
          600: '#1a5228',
          500: '#21672f', // main green
          400: '#2d8a40',
          300: '#3da854',
        },
        leather: {
          50:  '#fdf8f0',
          100: '#f9eed8',
          200: '#f0d9ae',
          300: '#e8c484',
          400: '#d4a55a',
          500: '#b8863c',
        },
        stitch: {
          500: '#c0392b',
          400: '#e74c3c',
          300: '#f1948a',
        },
        navy: {
          950: '#020408',
          900: '#0a0f1a',
          800: '#111827',
          700: '#1a2436',
          600: '#1f2d48',
        },
      },
      fontFamily: {
        sans: [
          '-apple-system', 'BlinkMacSystemFont', '"Segoe UI"',
          'Roboto', '"Helvetica Neue"', 'Arial', 'sans-serif',
        ],
        mono: ['"SF Mono"', '"Fira Code"', '"Cascadia Code"', 'monospace'],
      },
      backgroundImage: {
        'field-gradient': 'linear-gradient(135deg, #040d07 0%, #071a0d 50%, #0a0f1a 100%)',
        'card-gradient': 'linear-gradient(135deg, #111827 0%, #111827 100%)',
      },
    },
  },
  plugins: [],
}
