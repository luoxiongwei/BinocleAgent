// 内联 SVG 图标（功能性组件，避免引入图标库）
import { h } from 'vue'

const wrap = (children, size = 16) => () =>
  h(
    'svg',
    {
      viewBox: '0 0 24 24',
      width: size,
      height: size,
      fill: 'none',
      stroke: 'currentColor',
      'stroke-width': 1.8,
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
      'aria-hidden': 'true',
      focusable: 'false',
    },
    children,
  )

const d = (paths, size) => wrap(paths.map((p) => h('path', { d: p })), size)

export const IconShield = d(['M12 3l7 3v5c0 4.6-3 8.4-7 10-4-1.6-7-5.4-7-10V6l7-3z', 'M9.5 12l2 2 3.5-4'], 20)
export const IconReview = d(['M9 4h6l1 3h3v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V7h3l1-3z', 'M9.5 13.5l1.8 1.8 3.4-3.6'], 17)
export const IconLayers = d(['M12 3l8 4.5-8 4.5-8-4.5L12 3z', 'M4 12l8 4.5 8-4.5', 'M4 16.5L12 21l8-4.5'], 17)
export const IconPlay = d(['M6 4.5l12 7.5-12 7.5v-15z'], 15)
export const IconRefresh = d(['M20 12a8 8 0 1 1-2.3-5.6', 'M20 4v4h-4'], 15)
export const IconAlert = d(['M12 3.5L2.5 20h19L12 3.5z', 'M12 10v4.5', 'M12 17.2v.3'], 15)
export const IconCheck = d(['M4.5 12.5l5 5L19.5 7'], 15)
export const IconImage = d(['M4 5h16v14H4z', 'M4 15l4.5-4.5 3.5 3.5 3-3L20 16', 'M15.5 8.5v.2'], 15)
export const IconUpload = d(['M12 16V4', 'M7.5 8.5L12 4l4.5 4.5', 'M4.5 15v3.5A2 2 0 0 0 6.5 20.5h11a2 2 0 0 0 2-2V15'], 15)
export const IconSearch = d(['M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13z', 'M15.5 15.5L20 20'], 15)
export const IconActivity = d(['M3.5 12h4l2.5-6 4 12 2.5-6h4'], 15)
export const IconEye = d(['M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z', 'M12 9.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z'], 15)
export const IconGear = d(['M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z', 'M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.65 8.9a1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1.03-1.56V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9v.09a1.7 1.7 0 0 0 1.56 1.03H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51 1.03z'], 15)
export const IconArrowRight = d(['M4.5 12h15', 'M13.5 6l6 6-6 6'], 16)
export const IconMenu = d(['M3.5 7h17', 'M3.5 12h17', 'M3.5 17h17'], 18)
export const IconClose = d(['M6 6l12 12', 'M18 6L6 18'], 18)
export const IconChat = d(['M4 5.5h16v10.5H9.5L4 20V5.5z', 'M8 9.5h8', 'M8 12.5h5'], 15)
