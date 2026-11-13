'use strict';
document.addEventListener('DOMContentLoaded', () => {
  // Preserve the precision of the loaded scene values.
  document.querySelectorAll('#scene-fields input[type="number"]').forEach(input => input.step = 'any');
});
