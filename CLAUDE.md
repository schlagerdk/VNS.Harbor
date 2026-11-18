# VNS Harbor

## Design

The admin site's design is always premium: every dialog, card, menu and row looks finished and deliberate, never
like plain form controls put on a page.

- Choices are cards, segmented controls or chips, not bare checkboxes, radio buttons or a `<select>` for a few options.
- Related fields stand side by side (a password and its repeat, a name and its folder) where the dialog has the width.
- Icons fit what they stand for, and share size, place and colour with their neighbours: text lines up down a list.
- Spacing, corners, borders and colours follow the existing tokens and components in `admin/static/style.css`.
- It works on a phone as well as on the desktop (`max-width: 800px` and narrower), and nothing disabled reacts to the
  pointer.
