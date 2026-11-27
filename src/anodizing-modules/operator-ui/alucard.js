import { definePreset } from '@primeuix/themes';
import Aura from '@primeuix/themes/aura';

const tones = {
  info: '{operator.action}',
  success: '{operator.good}',
  warn: '{operator.warning}',
  error: '{operator.critical}',
  secondary: '{operator.muted}',
};
const notices = Object.fromEntries(Object.entries(tones).map(([severity, color]) => [severity, {
  color,
  background: '{operator.canvas}',
  borderColor: color,
  simple: { color },
  outlined: { color, borderColor: color },
  closeButton: {
    hoverBackground: '{operator.raised}',
    focusRing: { color: '{operator.focus}' },
  },
}]));

export default definePreset(Aura, {
  extend: {
    operator: {
      canvas: '#FFFBEB',
      panel: '#FFFFFF',
      raised: '#F4F0DD',
      pressed: '#CFCFDE',
      text: '#1F1F1F',
      muted: '#423D2C',
      border: '#6C664B',
      focus: '#036A96',
      action: '#054D6F',
      actionHover: '#003C59',
      actionPressed: '#002B41',
      running: '#0B5007',
      critical: '#8A2118',
      stopHover: '#741A15',
      stopPressed: '#601411',
      serious: '#6C3510',
      warning: '#514305',
      good: '#0B5007',
      headerAccent: '#A3144D',
    },
  },
  semantic: {
    disabledOpacity: '1',
    focusRing: { color: '{operator.focus}', style: 'solid', width: '3px', offset: '3px' },
    formField: {
      transitionDuration: '0s',
      focusRing: {
        color: '{focus.ring.color}', width: '{focus.ring.width}', style: '{focus.ring.style}',
        offset: '{focus.ring.offset}',
      },
    },
    // PrimeVue emits its light branch unconditionally with darkModeSelector disabled.
    colorScheme: {
      light: {
        primary: {
          color: '{operator.action}',
          hoverColor: '{operator.action.hover}',
          activeColor: '{operator.action.pressed}',
          contrastColor: '{operator.canvas}',
        },
        text: {
          color: '{operator.text}', hoverColor: '{operator.text}',
          mutedColor: '{operator.muted}', hoverMutedColor: '{operator.text}',
        },
        content: {
          background: '{operator.panel}', hoverBackground: '{operator.raised}',
          borderColor: '{operator.border}',
        },
        highlight: {
          background: '{operator.action}', color: '{operator.canvas}',
          focusBackground: '{operator.action.hover}', focusColor: '{operator.canvas}',
        },
        list: { option: { focusBackground: '{operator.raised}' } },
        formField: {
          background: '{operator.canvas}',
          disabledBackground: '{operator.raised}',
          borderColor: '{operator.border}',
          hoverBorderColor: '{operator.focus}',
          disabledColor: '{operator.muted}',
          color: '{operator.text}',
          placeholderColor: '{operator.muted}',
          invalidBorderColor: '{operator.critical}',
          iconColor: '{operator.muted}',
          shadow: 'none',
        },
        mask: { background: 'rgba(0, 0, 0, 0.7)' },
        overlay: {
          modal: { background: '{operator.panel}', color: '{operator.text}', borderColor: '{operator.border}' },
          select: { background: '{operator.panel}', borderColor: '{operator.border}' },
          popover: { background: '{operator.panel}', borderColor: '{operator.border}' },
        },
      },
    },
  },
  components: {
    tag: {
      colorScheme: {
        light: Object.fromEntries(Object.entries({
          primary: '{operator.action}', secondary: tones.secondary, success: tones.success,
          info: tones.info, warn: tones.warn, danger: tones.error,
        }).map(([severity, color]) => [severity, { color: '{operator.panel}', background: color }])),
      },
    },
    message: { colorScheme: { light: notices } },
    toast: {
      colorScheme: {
        light: Object.fromEntries(Object.entries(notices).map(([severity, { simple, outlined, ...notice }]) => [
          severity, { ...notice, detailColor: '{operator.text}' },
        ])),
      },
    },
    datatable: {
      colorScheme: { light: { row: { stripedBackground: '{operator.raised}' } } },
    },
    progressspinner: {
      colorScheme: {
        light: {
          root: {
            colorOne: tones.info, colorTwo: tones.info, colorThree: tones.info, colorFour: tones.info,
          },
        },
      },
    },
    toggleswitch: {
      root: {
        width: '3.75rem', height: '44px',
        borderColor: '{operator.border}', hoverBorderColor: '{operator.focus}',
        checkedBorderColor: '{operator.action}', checkedHoverBorderColor: '{operator.focus}',
      },
      handle: { size: '1.375rem' },
      colorScheme: {
        light: {
          root: { background: '{operator.canvas}', hoverBackground: '{operator.raised}' },
          handle: {
            background: '{operator.muted}', hoverBackground: '{operator.text}',
            checkedBackground: '{operator.canvas}', checkedHoverBackground: '{operator.canvas}',
          },
        },
      },
    },
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
