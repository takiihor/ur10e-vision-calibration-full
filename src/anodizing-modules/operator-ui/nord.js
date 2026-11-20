import { definePreset } from '@primeuix/themes';
import Aura from '@primeuix/themes/aura';

export default definePreset(Aura, {
  extend: {
    operator: {
      canvas: '#2E3440',
      panel: '#3B4252',
      raised: '#434C5E',
      pressed: '#4C566A',
      text: '#ECEFF4',
      muted: '#D8DEE9',
      border: '#A3ACBD',
      focus: '#8FBCBB',
      action: '#88C0D0',
      actionPressed: '#81A1C1',
      running: '#ADC2D7',
      critical: '#E2B6BB',
      stopHover: '#E9C9CC',
      stopPressed: '#DAA4A9',
      serious: '#D38E79',
      warning: '#EBCB8B',
      good: '#B1C89D',
    },
  },
  semantic: {
    disabledOpacity: '1',
    focusRing: { color: '{operator.focus}', style: 'solid', width: '3px', offset: '3px' },
    formField: { transitionDuration: '0s' },
    // PrimeVue emits its light branch unconditionally with darkModeSelector disabled.
    colorScheme: {
      light: {
        primary: {
          color: '{operator.action}',
          hoverColor: '{operator.focus}',
          activeColor: '{operator.action.pressed}',
          contrastColor: '{operator.canvas}',
        },
        formField: {
          background: '{operator.canvas}',
          disabledBackground: '{operator.raised}',
          borderColor: '{operator.border}',
          hoverBorderColor: '{operator.focus}',
          disabledColor: '{operator.muted}',
          shadow: 'none',
        },
        mask: { background: 'rgba(0, 0, 0, 0.7)' },
        overlay: {
          modal: { background: '{operator.panel}', color: '{operator.text}', borderColor: '{operator.border}' },
        },
      },
    },
  },
  components: {
    button: {
      colorScheme: {
        light: {
          root: {
            secondary: {
              background: '{operator.canvas}', hoverBackground: '{operator.raised}', activeBackground: '{operator.pressed}',
              borderColor: '{operator.border}', hoverBorderColor: '{operator.border}', activeBorderColor: '{operator.border}',
              color: '{operator.text}', hoverColor: '{operator.text}', activeColor: '{operator.text}',
            },
            danger: {
              background: '{operator.critical}', hoverBackground: '{operator.stop.hover}', activeBackground: '{operator.stop.pressed}',
              borderColor: '{operator.critical}', hoverBorderColor: '{operator.stop.hover}', activeBorderColor: '{operator.stop.pressed}',
              color: '{operator.canvas}', hoverColor: '{operator.canvas}', activeColor: '{operator.canvas}',
            },
          },
        },
      },
    },
  },
});
